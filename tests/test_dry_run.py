"""Unit tests for dry_run=True path on Api.write_doc / find_and_replace.

Reuses the FakeSocket harness pattern from test_webapi_ot.py. A dry-run
joins the doc to read the baseline, computes ops, and returns without
sending applyOtUpdate — so the FakeSocket.sent list must not contain any
`applyOtUpdate` frame.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

import pytest
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

from pyoverleaf import Api, DryRunResult, MultipleMatchesError
from pyoverleaf._models import ProjectFile, ProjectFolder


class FakeSocket:
    """In-memory bidirectional socket double, same surface as websocket-client."""

    def __init__(self) -> None:
        """Initialise empty inbound/outbound buffers and a 5s default timeout."""
        self.sent: list[str] = []
        self._inbound: deque[Any] = deque()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._timeout: float = 5.0
        self._closed = False

    def settimeout(self, t: float) -> None:
        """Set the blocking recv timeout in seconds."""
        self._timeout = float(t)

    def send(self, frame: Any) -> None:
        """Record an outbound frame, decoding bytes to UTF-8 text first."""
        if self._closed:
            raise WebSocketConnectionClosedException("closed")
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8")
        self.sent.append(frame)

    def recv(self) -> str:
        """Block up to `_timeout` waiting for the next inbound frame."""
        with self._cv:
            deadline = time.time() + self._timeout
            while not self._inbound:
                if self._closed:
                    raise WebSocketConnectionClosedException("closed")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise WebSocketTimeoutException("timeout")
                self._cv.wait(timeout=remaining)
            item = self._inbound.popleft()
            if isinstance(item, BaseException):
                raise item
            return item

    def close(self) -> None:
        """Mark the socket closed and wake any blocked recv calls."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def abort(self) -> None:
        """Alias for `close` matching the websocket-client surface."""
        self.close()

    def feed(self, frame: str) -> None:
        """Push an inbound frame for the next recv call to return."""
        with self._cv:
            self._inbound.append(frame)
            self._cv.notify_all()


def _doc(id_: str, name: str) -> ProjectFile:
    f = ProjectFile(id=id_, name=name, created=None)
    f.type = "doc"
    return f


def _root_with(children) -> ProjectFolder:
    folder = ProjectFolder(id="root", name="rootFolder")
    folder.children = list(children)
    return folder


def _feed_join_project(fake: FakeSocket) -> None:
    fake.feed(
        "5:::"
        + json.dumps(
            {
                "name": "joinProjectResponse",
                "args": [
                    {
                        "publicId": "pub-1",
                        "permissionsLevel": "owner",
                        "protocolVersion": 2,
                        "project": {
                            "rootFolder": [
                                {
                                    "_id": "root",
                                    "name": "root",
                                    "folders": [],
                                    "fileRefs": [],
                                    "docs": [],
                                }
                            ]
                        },
                    }
                ],
            }
        )
    )


def _wait_for_send(fake: FakeSocket, prefix: str, timeout: float = 3.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for f in fake.sent:
            if f.startswith(prefix):
                return f
        time.sleep(0.01)
    raise AssertionError(f"no send starting with {prefix!r} in {fake.sent!r}")


def _make_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tree: ProjectFolder,
    fake: FakeSocket,
    pre_text: str,
) -> Api:
    api = Api()
    api._session_initialized = True

    monkeypatch.setattr(Api, "_open_socket", lambda self, pid: fake, raising=True)
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: tree, raising=True)
    monkeypatch.setattr(
        Api,
        "_pull_doc_project_file_content",
        lambda self, pid, fid: pre_text,
        raising=True,
    )
    return api


# ----- write_doc dry_run ------------------------------------------------------


def _drive_joindoc_only(fake: FakeSocket, pre_text: str, version: int) -> None:
    """Feed only enough frames for joinDoc + leaveDoc (no applyOtUpdate)."""
    _feed_join_project(fake)
    _wait_for_send(fake, "5:1+::")
    fake.feed("6:::1+" + json.dumps([None, pre_text.split("\n"), version, [], {}]))
    # leaveDoc ack (id 2 since no applyOtUpdate happened in dry-run)
    _wait_for_send(fake, "5:2+::")
    fake.feed("6:::2+[null]")


def test_write_doc_dry_run_returns_dryrunresult(monkeypatch: pytest.MonkeyPatch):
    """dry_run on a non-empty diff returns a DryRunResult, no applyOtUpdate sent."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "hello world\nsecond line\n"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre)

    runner = threading.Thread(target=_drive_joindoc_only, args=(fake, pre, 11))
    runner.start()
    result = api.write_doc(
        "p", "main.tex", "hello universe\nsecond line\n", dry_run=True
    )
    runner.join(timeout=3.0)

    assert isinstance(result, DryRunResult)
    assert result.baseline_version == 11
    assert result.ops, "non-empty diff must produce ops"
    # No applyOtUpdate frame sent.
    apply_frames = [f for f in fake.sent if "applyOtUpdate" in f]
    assert apply_frames == []


def test_write_doc_dry_run_empty_diff_returns_empty_ops(
    monkeypatch: pytest.MonkeyPatch,
):
    """Identical content under dry_run yields a result with no ops or lines."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "same text"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre)

    runner = threading.Thread(target=_drive_joindoc_only, args=(fake, pre, 5))
    runner.start()
    result = api.write_doc("p", "main.tex", pre, dry_run=True)
    runner.join(timeout=3.0)

    assert isinstance(result, DryRunResult)
    assert result.baseline_version == 5
    assert result.ops == []
    assert result.affects_lines == []


def test_write_doc_dry_run_does_not_call_verify_read(
    monkeypatch: pytest.MonkeyPatch,
):
    """The post-submit verify _pull_doc must NOT be invoked on dry-run."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "hello"
    api = Api()
    api._session_initialized = True
    pull_calls = []

    monkeypatch.setattr(Api, "_open_socket", lambda self, pid: fake, raising=True)
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: tree, raising=True)

    def _pull(self_api, pid, fid):
        pull_calls.append((pid, fid))
        return pre

    monkeypatch.setattr(Api, "_pull_doc_project_file_content", _pull, raising=True)

    runner = threading.Thread(target=_drive_joindoc_only, args=(fake, pre, 7))
    runner.start()
    api.write_doc("p", "main.tex", "hello world", dry_run=True)
    runner.join(timeout=3.0)
    assert pull_calls == []


# ----- find_and_replace dry_run ----------------------------------------------


def test_find_and_replace_dry_run_single_match(monkeypatch: pytest.MonkeyPatch):
    """find_and_replace dry_run on a single match returns the planned ops only."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "the only foo here"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre)

    runner = threading.Thread(target=_drive_joindoc_only, args=(fake, pre, 4))
    runner.start()
    result = api.find_and_replace("p", "main.tex", "foo", "FOO", dry_run=True)
    runner.join(timeout=3.0)

    assert isinstance(result, DryRunResult)
    assert result.replacements == 1
    assert result.baseline_version == 4
    assert result.ops, "single-match must produce at least one op"
    apply_frames = [f for f in fake.sent if "applyOtUpdate" in f]
    assert apply_frames == []


def test_find_and_replace_dry_run_count_limit(monkeypatch: pytest.MonkeyPatch):
    """find_and_replace dry_run honours `count` and reports the capped count."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "foo foo foo"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre)

    runner = threading.Thread(target=_drive_joindoc_only, args=(fake, pre, 2))
    runner.start()
    result = api.find_and_replace("p", "main.tex", "foo", "FOO", count=2, dry_run=True)
    runner.join(timeout=3.0)
    assert isinstance(result, DryRunResult)
    assert result.replacements == 2


def test_find_and_replace_dry_run_zero_matches_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
):
    """Zero matches must not open any socket, even in dry-run."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "hello world"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre)

    result = api.find_and_replace("p", "main.tex", "MISSING", "x", dry_run=True)
    assert isinstance(result, DryRunResult)
    assert result.replacements == 0
    assert result.ops == []
    assert fake.sent == []


def test_find_and_replace_dry_run_multi_match_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
):
    """Multi-match safety must fire BEFORE dry-run preview; no socket opens."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "foo bar foo baz foo"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre)

    with pytest.raises(MultipleMatchesError) as exc:
        api.find_and_replace("p", "main.tex", "foo", "FOO", dry_run=True)
    assert exc.value.occurrences == 3
    assert fake.sent == []


# ----- CLI surface ------------------------------------------------------------


def test_cli_patch_dry_run_emits_json(monkeypatch: pytest.MonkeyPatch):
    """`patch --dry-run` emits the DryRunResult fields as JSON to stdout."""
    from click.testing import CliRunner

    from pyoverleaf import Api as _Api
    from pyoverleaf.__main__ import main as cli_main
    from pyoverleaf._models import Project

    monkeypatch.setattr(_Api, "login_from_browser", lambda self: None, raising=True)
    proj = Project(
        id="p1",
        name="proj",
        last_updated="",
        access_level="owner",
        source="owner",
        archived=False,
        trashed=False,
    )
    monkeypatch.setattr(_Api, "get_projects", lambda self, **kw: [proj], raising=True)
    captured: list[dict] = []

    def _stub(
        self,
        project_id,
        file_path,
        new_content,
        *,
        track_changes=False,
        raise_on_silent_noop=True,
        dry_run=False,
        timeout=15.0,
    ):
        captured.append({"dry_run": dry_run, "new_content": new_content})
        return DryRunResult(
            baseline_version=9,
            ops=[{"p": 0, "i": "hi "}],
            affects_lines=[1],
        )

    monkeypatch.setattr(_Api, "write_doc", _stub, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["patch", "--dry-run", "proj/main.tex"], input="hi")
    assert r.exit_code == 0, r.output
    obj = json.loads(r.output.strip())
    assert obj == {
        "baseline_version": 9,
        "ops": [{"p": 0, "i": "hi "}],
        "affects_lines": [1],
    }
    assert captured == [{"dry_run": True, "new_content": "hi"}]


def test_cli_replace_dry_run_includes_replacements(monkeypatch: pytest.MonkeyPatch):
    """`replace --dry-run` emits JSON including the planned replacement count."""
    from click.testing import CliRunner

    from pyoverleaf import Api as _Api
    from pyoverleaf.__main__ import main as cli_main
    from pyoverleaf._models import Project

    monkeypatch.setattr(_Api, "login_from_browser", lambda self: None, raising=True)
    proj = Project(
        id="p1",
        name="proj",
        last_updated="",
        access_level="owner",
        source="owner",
        archived=False,
        trashed=False,
    )
    monkeypatch.setattr(_Api, "get_projects", lambda self, **kw: [proj], raising=True)

    def _stub(self, *a: Any, **kw: Any) -> DryRunResult:
        return DryRunResult(
            baseline_version=4,
            ops=[{"p": 12, "d": "old"}, {"p": 12, "i": "new"}],
            affects_lines=[3],
            replacements=1,
        )

    monkeypatch.setattr(_Api, "find_and_replace", _stub, raising=False)

    runner = CliRunner()
    r = runner.invoke(
        cli_main,
        ["replace", "--dry-run", "proj/main.tex", "-f", "old", "-r", "new"],
    )
    assert r.exit_code == 0, r.output
    obj = json.loads(r.output.strip())
    assert obj["replacements"] == 1
    assert obj["baseline_version"] == 4
    assert obj["ops"] == [{"p": 12, "d": "old"}, {"p": 12, "i": "new"}]


def test_cli_replace_dry_run_zero_matches_exits_one(monkeypatch: pytest.MonkeyPatch):
    """`replace --dry-run` with zero matches must exit nonzero."""
    from click.testing import CliRunner

    from pyoverleaf import Api as _Api
    from pyoverleaf.__main__ import main as cli_main
    from pyoverleaf._models import Project

    monkeypatch.setattr(_Api, "login_from_browser", lambda self: None, raising=True)
    proj = Project(
        id="p1",
        name="proj",
        last_updated="",
        access_level="owner",
        source="owner",
        archived=False,
        trashed=False,
    )
    monkeypatch.setattr(_Api, "get_projects", lambda self, **kw: [proj], raising=True)

    def _stub(self, *a: Any, **kw: Any) -> DryRunResult:
        return DryRunResult(
            baseline_version=0, ops=[], affects_lines=[], replacements=0
        )

    monkeypatch.setattr(_Api, "find_and_replace", _stub, raising=False)

    runner = CliRunner()
    r = runner.invoke(
        cli_main,
        ["replace", "--dry-run", "proj/main.tex", "-f", "x", "-r", "y"],
    )
    assert r.exit_code == 1, r.output


# ----- affected_lines unit ----------------------------------------------------


def test_affected_lines_basic():
    """_affected_lines maps op positions back to 1-based line numbers."""
    from pyoverleaf._otapi import _affected_lines

    text = "line1\nline2\nline3\nline4\n"
    # `line1\n` ends at offset 6, `line2\n` ends at 12, etc.
    ops = [{"p": 6, "i": "X"}]  # at start of line2
    assert _affected_lines(text, ops) == [2]
    ops2 = [{"p": 0, "d": "line1\nlin"}]  # span line1 -> line2
    assert _affected_lines(text, ops2) == [1, 2]
    assert _affected_lines(text, []) == []
