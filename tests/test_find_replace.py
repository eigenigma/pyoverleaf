"""Unit tests for Api.find_and_replace.

Uses the same FakeSocket harness pattern as tests/test_webapi_ot.py so
find_and_replace's full code path (pull doc -> replace -> write_doc ->
verify) exercises real OT plumbing without hitting the network.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

import pytest
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

from pyoverleaf import Api, FindReplaceResult, MultipleMatchesError
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


def _binary(id_: str, name: str) -> ProjectFile:
    return ProjectFile(id=id_, name=name, created=None)


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
    post_text: str | None = None,
) -> Api:
    """Wire an Api whose _pull_doc returns pre_text on first call, post_text on second.

    find_and_replace calls _pull_doc twice: once to get current text, then
    again inside write_doc's silent-no-op verify. post_text defaults to
    "different" so the verify thinks the edit landed.
    """
    api = Api()
    api._session_initialized = True
    if post_text is None:
        post_text = pre_text + "MARKER"
    calls = {"n": 0}

    def _open(self_api, project_id):
        return fake

    def _files(self_api, project_id):
        return tree

    def _pull(self_api, project_id, file_id):
        # First call: pre_text (from find_and_replace's own pull).
        # Second+: post_text (from write_doc's verify).
        calls["n"] += 1
        return pre_text if calls["n"] == 1 else post_text

    monkeypatch.setattr(Api, "_open_socket", _open, raising=True)
    monkeypatch.setattr(Api, "project_get_files", _files, raising=True)
    monkeypatch.setattr(Api, "_pull_doc_project_file_content", _pull, raising=True)
    return api


# ----- happy path: replace-all ------------------------------------------------


def test_replace_all_occurrences_with_opt_in(monkeypatch: pytest.MonkeyPatch):
    """expect_unique=False allows multi-match replace and reports the count."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "foo bar foo baz foo"
    new = "FOO bar FOO baz FOO"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre, post_text=new)

    def _scripted():
        _feed_join_project(fake)
        # joinDoc ack with text matching pre
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, pre.split("\n"), 3, [], {}]))
        # applyOtUpdate ack
        _wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        # sender otUpdateApplied
        fake.feed(
            "5:::"
            + json.dumps(
                {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 3}]}
            )
        )
        # leaveDoc ack
        _wait_for_send(fake, "5:3+::")
        fake.feed("6:::3+[null]")

    runner = threading.Thread(target=_scripted)
    runner.start()
    result = api.find_and_replace("p", "main.tex", "foo", "FOO", expect_unique=False)
    runner.join(timeout=3.0)

    assert isinstance(result, FindReplaceResult)
    assert result.replacements == 3
    assert result.old_version == 3
    assert result.new_version == 4


def test_multiple_matches_default_rejected(monkeypatch: pytest.MonkeyPatch):
    """Default safety: multi-match with no opt-in raises and opens no socket."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "foo bar foo baz foo"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre, post_text=pre)

    with pytest.raises(MultipleMatchesError) as exc_info:
        api.find_and_replace("p", "main.tex", "foo", "FOO")
    assert exc_info.value.occurrences == 3
    assert exc_info.value.find == "foo"
    assert fake.sent == []


def test_single_match_does_not_trigger_safety(monkeypatch: pytest.MonkeyPatch):
    """One match must not be rejected by the multi-match safety check."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "the only foo here"
    new = "the only FOO here"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre, post_text=new)

    def _scripted():
        _feed_join_project(fake)
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, pre.split("\n"), 2, [], {}]))
        _wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        fake.feed(
            "5:::"
            + json.dumps(
                {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 2}]}
            )
        )
        _wait_for_send(fake, "5:3+::")
        fake.feed("6:::3+[null]")

    runner = threading.Thread(target=_scripted)
    runner.start()
    result = api.find_and_replace("p", "main.tex", "foo", "FOO")
    runner.join(timeout=3.0)
    assert result.replacements == 1
    assert result.new_version == 3


# ----- limited count ----------------------------------------------------------


def test_replace_with_count_limit(monkeypatch: pytest.MonkeyPatch):
    """`count=N` caps the number of replacements made even on multi-match."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "foo foo foo"
    new_after_two = "FOO FOO foo"
    api = _make_api(
        monkeypatch, tree=tree, fake=fake, pre_text=pre, post_text=new_after_two
    )

    def _scripted():
        _feed_join_project(fake)
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, pre.split("\n"), 0, [], {}]))
        _wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        fake.feed(
            "5:::"
            + json.dumps(
                {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 0}]}
            )
        )
        _wait_for_send(fake, "5:3+::")
        fake.feed("6:::3+[null]")

    runner = threading.Thread(target=_scripted)
    runner.start()
    result = api.find_and_replace("p", "main.tex", "foo", "FOO", count=2)
    runner.join(timeout=3.0)
    assert result.replacements == 2
    assert result.new_version == 1


# ----- zero occurrences (short-circuit, no socket) ----------------------------


def test_no_occurrences_short_circuits(monkeypatch: pytest.MonkeyPatch):
    """Zero matches must not open the socket; the call returns a zero-count result."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre = "hello world"
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text=pre, post_text=pre)

    result = api.find_and_replace("p", "main.tex", "MISSING", "x")
    assert result.replacements == 0
    assert result.old_version is None
    assert result.new_version is None
    # Crucially: no socket was opened (no joinDoc was sent)
    assert fake.sent == []


# ----- validation -------------------------------------------------------------


def test_rejects_empty_find(monkeypatch: pytest.MonkeyPatch):
    """An empty `find` string must be rejected before any I/O."""
    api = Api()
    api._session_initialized = True
    with pytest.raises(ValueError, match="find must be non-empty"):
        api.find_and_replace("p", "f", "", "x")


def test_rejects_non_str(monkeypatch: pytest.MonkeyPatch):
    """Non-string `replace` argument must raise TypeError."""
    api = Api()
    api._session_initialized = True
    with pytest.raises(TypeError):
        api.find_and_replace("p", "f", "find", 123)  # type: ignore[arg-type]


def test_rejects_negative_count(monkeypatch: pytest.MonkeyPatch):
    """A negative `count` must be rejected before any I/O."""
    api = Api()
    api._session_initialized = True
    with pytest.raises(ValueError, match="count must be a non-negative int"):
        api.find_and_replace("p", "f", "find", "x", count=-1)


def test_non_doc_rejected(monkeypatch: pytest.MonkeyPatch):
    """find_and_replace must refuse non-doc targets via the OT error path."""
    fake = FakeSocket()
    tree = _root_with([_binary("bin-1", "data.bin")])
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text="anything")
    from pyoverleaf._ot import OtError

    with pytest.raises(OtError):
        api.find_and_replace("p", "data.bin", "x", "y")


def test_missing_file_raises(monkeypatch: pytest.MonkeyPatch):
    """A missing file path must raise FileNotFoundError."""
    fake = FakeSocket()
    tree = _root_with([])
    api = _make_api(monkeypatch, tree=tree, fake=fake, pre_text="")
    with pytest.raises(FileNotFoundError):
        api.find_and_replace("p", "main.tex", "x", "y")
