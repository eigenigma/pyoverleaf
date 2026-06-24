"""Unit tests for pyoverleaf._otapi_reviews comments operations + CLI."""

from __future__ import annotations

import json as _json
from typing import Any

import pytest
from click.testing import CliRunner

from pyoverleaf import Api, CommentThread
from pyoverleaf.__main__ import main as cli_main
from pyoverleaf._comments import (
    list_comments,
    reopen_comment,
    reply_to_comment,
    resolve_comment,
)
from pyoverleaf._models import Project, ProjectFile, ProjectFolder


def _folder(id_: str, name: str, children=()) -> ProjectFolder:
    f = ProjectFolder(id=id_, name=name)
    f.children = list(children)
    return f


def _doc(id_: str, name: str) -> ProjectFile:
    f = ProjectFile(id=id_, name=name, created=None)
    f.type = "doc"
    return f


class FakeSession:
    """Mocks requests.Session for the GET /threads + /ranges + POST endpoints."""

    def __init__(
        self,
        threads: dict[str, dict] | None = None,
        ranges: list[dict] | None = None,
    ) -> None:
        """Store thread/ranges payloads and an empty POST capture list."""
        self.threads = threads or {}
        self.ranges = ranges or []
        self.posts: list[tuple[str, dict | None]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        """Return canned payloads for /threads and /ranges URLs."""
        if url.endswith("/threads"):
            return FakeResponse(self.threads)
        if url.endswith("/ranges"):
            return FakeResponse(self.ranges)
        raise AssertionError(f"unexpected GET to {url}")

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """Capture POSTs to `self.posts` and return an empty response."""
        body = kwargs.get("json")
        self.posts.append((url, body))
        return FakeResponse({})


class FakeResponse:
    """Mocks requests.Response with `.content` and a no-op `raise_for_status`."""

    def __init__(self, payload: Any) -> None:
        """Encode `payload` as JSON bytes available on `self.content`."""
        self.content = _json.dumps(payload).encode("utf-8")

    def raise_for_status(self) -> None:
        """No-op: FakeResponse never simulates HTTP errors."""


def _make_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tree: ProjectFolder,
    session: FakeSession,
) -> Api:
    api = Api()
    api._session_initialized = True
    monkeypatch.setattr(Api, "_get_session", lambda self: session, raising=True)
    monkeypatch.setattr(
        Api, "_get_csrf_token", lambda self, pid: "csrf-token", raising=True
    )
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: tree, raising=True)
    return api


# ---- list_comments ----------------------------------------------------------


def test_list_comments_enriches_with_anchor(monkeypatch: pytest.MonkeyPatch):
    """`list_comments` joins /threads with /ranges to attach doc, quote, position."""
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex")])
    session = FakeSession(
        threads={
            "thread-A": {
                "messages": [
                    {
                        "id": "m1",
                        "content": "fix this",
                        "timestamp": 100,
                        "user_id": "u1",
                        "user": {"first_name": "Alice", "last_name": "Q"},
                    }
                ],
                "resolved": False,
            }
        },
        ranges=[
            {
                "id": "doc-1",
                "ranges": {
                    "comments": [
                        {
                            "id": "c1",
                            "op": {"p": 42, "c": "quoted bit", "t": "thread-A"},
                        }
                    ]
                },
            }
        ],
    )
    api = _make_api(monkeypatch, tree=tree, session=session)
    threads = list_comments(api, "p1")
    assert len(threads) == 1
    t = threads[0]
    assert t.thread_id == "thread-A"
    assert t.doc_id == "doc-1"
    assert t.doc_path == "main.tex"
    assert t.quoted_text == "quoted bit"
    assert t.position == 42
    assert t.resolved is False
    assert t.messages[0].user_name == "Alice Q"


def test_list_comments_filters_resolved_by_default(monkeypatch: pytest.MonkeyPatch):
    """`list_comments` hides resolved threads unless `include_resolved=True`."""
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex")])
    session = FakeSession(
        threads={
            "open-thread": {
                "messages": [{"id": "m1", "content": "x", "timestamp": 1}],
                "resolved": False,
            },
            "done-thread": {
                "messages": [{"id": "m2", "content": "y", "timestamp": 2}],
                "resolved": True,
            },
        },
    )
    api = _make_api(monkeypatch, tree=tree, session=session)
    out = list_comments(api, "p1")
    assert [t.thread_id for t in out] == ["open-thread"]


def test_list_comments_include_resolved(monkeypatch: pytest.MonkeyPatch):
    """`include_resolved=True` keeps resolved threads in the output."""
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex")])
    session = FakeSession(
        threads={
            "open-thread": {"messages": [{"id": "m1", "content": "x", "timestamp": 1}]},
            "done-thread": {
                "messages": [{"id": "m2", "content": "y", "timestamp": 2}],
                "resolved": True,
            },
        }
    )
    api = _make_api(monkeypatch, tree=tree, session=session)
    out = list_comments(api, "p1", include_resolved=True)
    ids = sorted(t.thread_id for t in out)
    assert ids == ["done-thread", "open-thread"]


def test_list_comments_filters_by_doc_path(monkeypatch: pytest.MonkeyPatch):
    """`doc_path_filter` keeps only threads anchored under the given subtree."""
    chap = _folder("chap-id", "chapters", [_doc("doc-2", "intro.tex")])
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex"), chap])
    session = FakeSession(
        threads={
            "t-main": {"messages": [{"id": "m", "content": "a", "timestamp": 1}]},
            "t-intro": {"messages": [{"id": "m", "content": "b", "timestamp": 2}]},
        },
        ranges=[
            {
                "id": "doc-1",
                "ranges": {
                    "comments": [{"id": "c", "op": {"p": 0, "c": "", "t": "t-main"}}]
                },
            },
            {
                "id": "doc-2",
                "ranges": {
                    "comments": [{"id": "c", "op": {"p": 0, "c": "", "t": "t-intro"}}]
                },
            },
        ],
    )
    api = _make_api(monkeypatch, tree=tree, session=session)
    out = list_comments(api, "p1", doc_path_filter="chapters")
    assert [t.thread_id for t in out] == ["t-intro"]


def test_list_comments_sorted_by_recent(monkeypatch: pytest.MonkeyPatch):
    """`list_comments` returns threads sorted by most-recent message first."""
    tree = _folder("root", "rootFolder", [_doc("doc-1", "main.tex")])
    session = FakeSession(
        threads={
            "old": {"messages": [{"id": "m1", "content": "x", "timestamp": 100}]},
            "new": {"messages": [{"id": "m2", "content": "y", "timestamp": 999}]},
        }
    )
    api = _make_api(monkeypatch, tree=tree, session=session)
    out = list_comments(api, "p1")
    assert [t.thread_id for t in out] == ["new", "old"]


# ---- mutating endpoints ------------------------------------------------------


def test_reply_to_comment_posts(monkeypatch: pytest.MonkeyPatch):
    """`reply_to_comment` POSTs the message body to the thread messages endpoint."""
    session = FakeSession()
    tree = _folder("root", "rootFolder", [])
    api = _make_api(monkeypatch, tree=tree, session=session)
    result = reply_to_comment(api, "p1", "thread-A", "Looks good!")
    assert result == {"thread_id": "thread-A", "content_length": 11}
    assert session.posts == [
        (
            "https://www.overleaf.com/project/p1/thread/thread-A/messages",
            {"content": "Looks good!"},
        )
    ]


def test_reply_to_comment_rejects_empty(monkeypatch: pytest.MonkeyPatch):
    """Whitespace-only content is rejected before any HTTP POST is sent."""
    session = FakeSession()
    tree = _folder("root", "rootFolder", [])
    api = _make_api(monkeypatch, tree=tree, session=session)
    with pytest.raises(ValueError, match="non-empty string"):
        reply_to_comment(api, "p1", "thread-A", "   ")
    assert session.posts == []


_ANCHOR_RANGES_FOR_THREAD_A = [
    {
        "id": "doc-7",
        "ranges": {
            "comments": [{"id": "c1", "op": {"p": 0, "c": "x", "t": "thread-A"}}]
        },
    }
]


def test_resolve_comment_posts(monkeypatch: pytest.MonkeyPatch):
    """`resolve_comment` POSTs to the resolve endpoint for the thread's anchor doc."""
    session = FakeSession(ranges=_ANCHOR_RANGES_FOR_THREAD_A)
    tree = _folder("root", "rootFolder", [])
    api = _make_api(monkeypatch, tree=tree, session=session)
    assert resolve_comment(api, "p1", "thread-A") == {
        "thread_id": "thread-A",
        "resolved": True,
    }
    assert session.posts == [
        (
            "https://www.overleaf.com/project/p1/doc/doc-7/thread/thread-A/resolve",
            {},
        )
    ]


def test_reopen_comment_posts(monkeypatch: pytest.MonkeyPatch):
    """`reopen_comment` POSTs to the reopen endpoint for the thread's anchor doc."""
    session = FakeSession(ranges=_ANCHOR_RANGES_FOR_THREAD_A)
    tree = _folder("root", "rootFolder", [])
    api = _make_api(monkeypatch, tree=tree, session=session)
    assert reopen_comment(api, "p1", "thread-A") == {
        "thread_id": "thread-A",
        "resolved": False,
    }
    assert session.posts == [
        (
            "https://www.overleaf.com/project/p1/doc/doc-7/thread/thread-A/reopen",
            {},
        )
    ]


def test_resolve_comment_raises_when_anchor_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """Resolve raises `ValueError` and skips POST when no anchoring doc is found."""
    session = FakeSession(ranges=[])
    tree = _folder("root", "rootFolder", [])
    api = _make_api(monkeypatch, tree=tree, session=session)
    with pytest.raises(ValueError, match="no live anchor"):
        resolve_comment(api, "p1", "thread-orphan")
    assert session.posts == []


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


def test_cli_comments_list_outputs_json(monkeypatch: pytest.MonkeyPatch):
    """`comments list` prints a JSON array of CommentThread dicts on stdout."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub_list(self, project_id, *, doc_path_filter=None, include_resolved=False):
        captured.append(
            {
                "project_id": project_id,
                "doc_path_filter": doc_path_filter,
                "include_resolved": include_resolved,
            }
        )
        return [
            CommentThread(
                thread_id="t-1",
                doc_id="d1",
                doc_path="main.tex",
                quoted_text="hello",
                position=12,
                resolved=False,
                messages=[],
            )
        ]

    monkeypatch.setattr(Api, "list_comments", _stub_list, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["comments", "list", "proj/main.tex"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output.strip())
    assert isinstance(payload, list)
    assert payload[0]["thread_id"] == "t-1"
    assert captured == [
        {"project_id": "p1", "doc_path_filter": "main.tex", "include_resolved": False}
    ]


def test_cli_comments_reply(monkeypatch: pytest.MonkeyPatch):
    """`comments reply` forwards `-m` content and thread_id to the Api method."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub(self, project_id, thread_id, content):
        captured.append(
            {"project_id": project_id, "thread_id": thread_id, "content": content}
        )
        return {"thread_id": thread_id, "content_length": len(content)}

    monkeypatch.setattr(Api, "reply_to_comment", _stub, raising=False)

    runner = CliRunner()
    r = runner.invoke(
        cli_main, ["comments", "reply", "proj", "thread-A", "-m", "Done in v3"]
    )
    assert r.exit_code == 0, r.output
    assert captured == [
        {"project_id": "p1", "thread_id": "thread-A", "content": "Done in v3"}
    ]


def test_cli_comments_resolve_and_reopen(monkeypatch: pytest.MonkeyPatch):
    """`comments resolve`/`reopen` dispatch to the matching Api methods."""
    _stub_login_and_projects(monkeypatch)
    calls: list[tuple[str, str]] = []

    def _stub_resolve(self, pid, tid):
        calls.append(("resolve", tid))
        return {"thread_id": tid, "resolved": True}

    def _stub_reopen(self, pid, tid):
        calls.append(("reopen", tid))
        return {"thread_id": tid, "resolved": False}

    monkeypatch.setattr(Api, "resolve_comment", _stub_resolve, raising=False)
    monkeypatch.setattr(Api, "reopen_comment", _stub_reopen, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["comments", "resolve", "proj", "t-A"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli_main, ["comments", "reopen", "proj", "t-A"])
    assert r.exit_code == 0, r.output
    assert calls == [("resolve", "t-A"), ("reopen", "t-A")]


def test_cli_comments_help_lists_subcommands():
    """`comments --help` advertises the list/reply/resolve/reopen subcommands."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["comments", "--help"])
    assert r.exit_code == 0
    for sub in ("list", "reply", "resolve", "reopen"):
        assert sub in r.output
