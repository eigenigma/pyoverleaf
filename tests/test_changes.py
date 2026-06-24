"""Unit tests for pyoverleaf tracked-changes operations + CLI."""

from __future__ import annotations

import json as _json
import threading
from typing import TYPE_CHECKING, Any

from click.testing import CliRunner

from pyoverleaf import Api, TrackedChange
from pyoverleaf.__main__ import main as cli_main
from pyoverleaf._models import Project, ProjectFile, ProjectFolder
from pyoverleaf._otapi_reviews import (
    _inverse_op_for,
    _tracked_change_from,
    accept_tracked_changes,
    list_tracked_changes,
    reject_tracked_changes,
)
from tests._fakesocket import FakeSocket, feed_join_project, wait_for_send

if TYPE_CHECKING:
    import pytest


def _folder(id_: str, name: str, children=()) -> ProjectFolder:
    f = ProjectFolder(id=id_, name=name)
    f.children = list(children)
    return f


def _doc(id_: str, name: str) -> ProjectFile:
    f = ProjectFile(id=id_, name=name, created=None)
    f.type = "doc"
    return f


class FakeSession:
    """Mocks requests.Session for the GET /ranges + /threads + POST endpoints."""

    def __init__(self, ranges: list[dict]) -> None:
        """Store the ranges payload returned for GET /ranges."""
        self.ranges = ranges
        self.posts: list[tuple[str, dict | None]] = []

    def get(self, url: str, **_: Any) -> FakeResponse:
        """Return canned payloads for /ranges and /threads URLs."""
        if url.endswith("/ranges"):
            return FakeResponse(self.ranges)
        if url.endswith("/threads"):
            return FakeResponse({})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """Capture POSTs to `self.posts` and return an empty response."""
        self.posts.append((url, kwargs.get("json")))
        return FakeResponse({})


class FakeResponse:
    """Mocks requests.Response with `.content` and a no-op `raise_for_status`."""

    def __init__(self, payload: Any) -> None:
        """Encode `payload` as JSON bytes available on `self.content`."""
        self.content = _json.dumps(payload).encode("utf-8")

    def raise_for_status(self) -> None:
        """No-op: FakeResponse never simulates HTTP errors."""


def _make_http_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tree: ProjectFolder,
    session: FakeSession,
) -> Api:
    api = Api()
    api._session_initialized = True
    monkeypatch.setattr(Api, "_get_session", lambda self: session, raising=True)
    monkeypatch.setattr(Api, "_get_csrf_token", lambda self, pid: "csrf", raising=True)
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: tree, raising=True)
    return api


# ---- _tracked_change_from + _inverse_op_for ---------------------------------


def test_tracked_change_from_insert():
    """`_tracked_change_from` decodes an `i:` op into an insert TrackedChange."""
    raw = {
        "id": "c-1",
        "op": {"p": 12, "i": "hello"},
        "metadata": {"user_id": "u1", "ts": "2026-06-22T10:00:00Z"},
    }
    tc = _tracked_change_from("doc-1", "main.tex", raw)
    assert tc is not None
    assert tc.kind == "insert"
    assert tc.text == "hello"
    assert tc.position == 12
    assert tc.user_id == "u1"


def test_tracked_change_from_delete():
    """`_tracked_change_from` decodes a `d:` op into a delete TrackedChange."""
    tc = _tracked_change_from("doc-1", None, {"id": "c-2", "op": {"p": 5, "d": "old"}})
    assert tc is not None
    assert tc.kind == "delete"
    assert tc.text == "old"


def test_tracked_change_from_invalid_returns_none():
    """Ops with neither `i` nor `d` decode to None."""
    assert _tracked_change_from("d", None, {"id": "x", "op": {"p": 0}}) is None


def test_inverse_op_insert_to_delete():
    """`_inverse_op_for` flips an insert into a tracked-undo delete (`u: true`)."""
    assert _inverse_op_for({"op": {"p": 7, "i": "hello"}}) == {
        "p": 7,
        "d": "hello",
        "u": True,
    }


def test_inverse_op_delete_to_insert():
    """`_inverse_op_for` flips a delete into a tracked-undo insert (`u: true`)."""
    assert _inverse_op_for({"op": {"p": 3, "d": "bye"}}) == {
        "p": 3,
        "i": "bye",
        "u": True,
    }


# ---- list_tracked_changes ----------------------------------------------------


def test_list_tracked_changes_flattens(monkeypatch: pytest.MonkeyPatch):
    """Changes from every doc are flattened and tagged with the full doc path."""
    chap = _folder("chap-id", "chapters", [_doc("doc-2", "intro.tex")])
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex"), chap])
    session = FakeSession(
        ranges=[
            {
                "id": "doc-1",
                "ranges": {
                    "changes": [
                        {"id": "c-A", "op": {"p": 5, "i": "hi"}},
                        {"id": "c-B", "op": {"p": 100, "d": "rm"}},
                    ]
                },
            },
            {
                "id": "doc-2",
                "ranges": {"changes": [{"id": "c-C", "op": {"p": 0, "i": "X"}}]},
            },
        ]
    )
    api = _make_http_api(monkeypatch, tree=tree, session=session)
    out = list_tracked_changes(api, "p1")
    assert sorted(c.change_id for c in out) == ["c-A", "c-B", "c-C"]
    by_id = {c.change_id: c for c in out}
    assert by_id["c-A"].doc_path == "main.tex"
    assert by_id["c-C"].doc_path == "chapters/intro.tex"


def test_list_tracked_changes_path_filter(monkeypatch: pytest.MonkeyPatch):
    """`doc_path_filter` keeps only changes under the given subtree."""
    chap = _folder("chap-id", "chapters", [_doc("doc-2", "intro.tex")])
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex"), chap])
    session = FakeSession(
        ranges=[
            {
                "id": "doc-1",
                "ranges": {"changes": [{"id": "c-A", "op": {"p": 0, "i": "x"}}]},
            },
            {
                "id": "doc-2",
                "ranges": {"changes": [{"id": "c-B", "op": {"p": 0, "i": "y"}}]},
            },
        ]
    )
    api = _make_http_api(monkeypatch, tree=tree, session=session)
    out = list_tracked_changes(api, "p1", doc_path_filter="chapters")
    assert [c.change_id for c in out] == ["c-B"]


# ---- accept_tracked_changes -------------------------------------------------


def test_accept_groups_by_doc(monkeypatch: pytest.MonkeyPatch):
    """Accept batches POSTs per doc and reports per-doc counts in the summary."""
    tree = _folder(
        "root", "rootFolder", [_doc("doc-1", "main.tex"), _doc("doc-2", "a.tex")]
    )
    session = FakeSession(
        ranges=[
            {
                "id": "doc-1",
                "ranges": {
                    "changes": [
                        {"id": "c-A", "op": {"p": 0, "i": "x"}},
                        {"id": "c-B", "op": {"p": 1, "d": "y"}},
                    ]
                },
            },
            {
                "id": "doc-2",
                "ranges": {"changes": [{"id": "c-C", "op": {"p": 0, "i": "z"}}]},
            },
        ]
    )
    api = _make_http_api(monkeypatch, tree=tree, session=session)
    summary = accept_tracked_changes(api, "p1", ["c-A", "c-B", "c-C"])
    assert summary["accepted"] == 3
    assert summary["unknown"] == []
    docs = sorted(summary["docs"], key=lambda d: d["doc_id"])
    assert docs == [{"doc_id": "doc-1", "count": 2}, {"doc_id": "doc-2", "count": 1}]
    by_url = sorted(session.posts, key=lambda p: p[0])
    assert (
        by_url[0][0] == "https://www.overleaf.com/project/p1/doc/doc-1/changes/accept"
    )
    assert sorted(by_url[0][1]["change_ids"]) == ["c-A", "c-B"]
    assert (
        by_url[1][0] == "https://www.overleaf.com/project/p1/doc/doc-2/changes/accept"
    )


def test_accept_unknown_listed(monkeypatch: pytest.MonkeyPatch):
    """Unknown change_ids surface in `summary['unknown']` and trigger no POST."""
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex")])
    session = FakeSession(ranges=[{"id": "doc-1", "ranges": {"changes": []}}])
    api = _make_http_api(monkeypatch, tree=tree, session=session)
    summary = accept_tracked_changes(api, "p1", ["nope"])
    assert summary["unknown"] == ["nope"]
    assert session.posts == []


def test_accept_empty_short_circuits(monkeypatch: pytest.MonkeyPatch):
    """Accepting an empty change list returns a zero-summary without I/O."""
    api = Api()
    api._session_initialized = True
    assert accept_tracked_changes(api, "p1", []) == {
        "accepted": 0,
        "docs": [],
        "unknown": [],
    }


# ---- reject_tracked_changes (socket-driven) ---------------------------------


def test_reject_inverse_ops_descending_with_u_flag(monkeypatch: pytest.MonkeyPatch):
    """Reject sends descending-position inverse ops with `u: true` and no `meta.tc`."""
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex")])
    session = FakeSession(
        ranges=[
            {
                "id": "doc-1",
                "ranges": {
                    "changes": [
                        {"id": "c-LOW", "op": {"p": 5, "i": "low"}},
                        {"id": "c-MID", "op": {"p": 10, "d": "mid"}},
                        {"id": "c-HIGH", "op": {"p": 20, "i": "high"}},
                    ]
                },
            }
        ]
    )
    fake = FakeSocket()
    api = _make_http_api(monkeypatch, tree=tree, session=session)
    monkeypatch.setattr(Api, "_open_socket", lambda self, pid: fake, raising=True)

    def _scripted():
        feed_join_project(fake)
        wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + _json.dumps([None, ["body"], 42, [], {}]))
        wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        fake.feed(
            "5:::"
            + _json.dumps(
                {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 42}]}
            )
        )
        wait_for_send(fake, "5:3+::")
        fake.feed("6:::3+[null]")

    t = threading.Thread(target=_scripted)
    t.start()
    summary = reject_tracked_changes(api, "p1", ["c-LOW", "c-MID", "c-HIGH"])
    t.join(timeout=3.0)

    apply_frame = next(f for f in fake.sent if "applyOtUpdate" in f)
    body = _json.loads(apply_frame[apply_frame.index("{") :])
    update = body["args"][1]
    ops = update["op"]
    positions = [op["p"] for op in ops]
    assert positions == sorted(positions, reverse=True), (
        "ops must be position-descending"
    )
    assert all(op.get("u") is True for op in ops)
    by_p = {op["p"]: op for op in ops}
    assert by_p[20] == {"p": 20, "d": "high", "u": True}
    assert by_p[10] == {"p": 10, "i": "mid", "u": True}
    assert by_p[5] == {"p": 5, "d": "low", "u": True}
    assert "tc" not in (update.get("meta") or {})
    assert summary["rejected"] == 3


def test_reject_missing_listed(monkeypatch: pytest.MonkeyPatch):
    """Reject surfaces unknown change_ids in `summary['missing']` without sending."""
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex")])
    session = FakeSession(ranges=[{"id": "doc-1", "ranges": {"changes": []}}])
    api = _make_http_api(monkeypatch, tree=tree, session=session)
    summary = reject_tracked_changes(api, "p1", ["nope"])
    assert summary == {"rejected": 0, "docs": [], "missing": ["nope"]}


def test_reject_empty_short_circuits(monkeypatch: pytest.MonkeyPatch):
    """Rejecting an empty change list returns a zero-summary without I/O."""
    api = Api()
    api._session_initialized = True
    assert reject_tracked_changes(api, "p1", []) == {
        "rejected": 0,
        "docs": [],
        "missing": [],
    }


# ---- CLI ---------------------------------------------------------------------


def _stub_login_and_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Api, "login_from_browser", lambda self: None, raising=True)
    proj = Project(
        id="p1",
        name="proj",
        last_updated="",
        access_level="owner",
        source="owner",
        archived=False,
        trashed=False,
    )
    monkeypatch.setattr(Api, "get_projects", lambda self, **kw: [proj], raising=True)


def test_cli_changes_help():
    """`changes --help` advertises the list/accept/reject subcommands."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["changes", "--help"])
    assert r.exit_code == 0
    for sub in ("list", "accept", "reject"):
        assert sub in r.output


def test_cli_changes_list_outputs_json(monkeypatch: pytest.MonkeyPatch):
    """`changes list` emits a JSON array of TrackedChange dicts on stdout."""
    _stub_login_and_projects(monkeypatch)

    def _stub(self, project_id, *, doc_path_filter=None):
        return [
            TrackedChange(
                change_id="c-1",
                doc_id="doc-1",
                doc_path="main.tex",
                kind="insert",
                position=5,
                text="hello",
                user_id="u1",
                timestamp=None,
            )
        ]

    monkeypatch.setattr(Api, "list_tracked_changes", _stub, raising=False)
    runner = CliRunner()
    r = runner.invoke(cli_main, ["changes", "list", "proj/main.tex"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output.strip())
    assert payload[0]["change_id"] == "c-1"


def test_cli_changes_accept(monkeypatch: pytest.MonkeyPatch):
    """`changes accept` forwards positional change_ids to the Api method."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub(self, project_id, change_ids):
        captured.append({"project_id": project_id, "change_ids": change_ids})
        return {"accepted": 2, "docs": [{"doc_id": "d", "count": 2}], "unknown": []}

    monkeypatch.setattr(Api, "accept_tracked_changes", _stub, raising=False)
    runner = CliRunner()
    r = runner.invoke(cli_main, ["changes", "accept", "proj", "c-1", "c-2"])
    assert r.exit_code == 0
    assert captured == [{"project_id": "p1", "change_ids": ["c-1", "c-2"]}]


def test_cli_changes_accept_unknown_exits_one(monkeypatch: pytest.MonkeyPatch):
    """`changes accept` exits 1 when the summary reports unknown change_ids."""
    _stub_login_and_projects(monkeypatch)

    def _stub(self, project_id, change_ids):
        return {"accepted": 0, "docs": [], "unknown": ["bad"]}

    monkeypatch.setattr(Api, "accept_tracked_changes", _stub, raising=False)
    runner = CliRunner()
    r = runner.invoke(cli_main, ["changes", "accept", "proj", "bad"])
    assert r.exit_code == 1


def test_cli_changes_reject(monkeypatch: pytest.MonkeyPatch):
    """`changes reject` forwards positional change_ids to the Api method."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub(self, project_id, change_ids):
        captured.append({"change_ids": change_ids})
        return {"rejected": 1, "docs": [{"doc_id": "d", "count": 1}], "missing": []}

    monkeypatch.setattr(Api, "reject_tracked_changes", _stub, raising=False)
    runner = CliRunner()
    r = runner.invoke(cli_main, ["changes", "reject", "proj", "c-1"])
    assert r.exit_code == 0
    assert captured == [{"change_ids": ["c-1"]}]


def test_cli_changes_reject_missing_exits_one(monkeypatch: pytest.MonkeyPatch):
    """`changes reject` exits 1 when the summary reports missing change_ids."""
    _stub_login_and_projects(monkeypatch)

    def _stub(self, project_id, change_ids):
        return {"rejected": 0, "docs": [], "missing": ["bad"]}

    monkeypatch.setattr(Api, "reject_tracked_changes", _stub, raising=False)
    runner = CliRunner()
    r = runner.invoke(cli_main, ["changes", "reject", "proj", "bad"])
    assert r.exit_code == 1
