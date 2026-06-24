"""Integration tests for Api.write_doc / Api.apply_ot_update.

Uses a FakeSocket-driven session by monkey-patching `Api._open_socket` and
`Api._pull_doc_project_file_content`. No network.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

import pytest
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

from pyoverleaf import Api, SilentNoOpError, WriteResult
from pyoverleaf._models import ProjectFile, ProjectFolder
from pyoverleaf._ot import OtUpdateError


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


def _make_api_with_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tree: ProjectFolder,
    fake: FakeSocket,
    server_text_after: str | None = None,
) -> Api:
    """Wire an Api up with a FakeSocket and a stubbed read-back."""
    api = Api()
    api._session_initialized = True  # bypass login_*

    def _open(self_api, project_id):
        return fake

    def _files(self_api, project_id):
        return tree

    def _pull(self_api, project_id, file_id):
        if server_text_after is None:
            return ""
        return server_text_after

    monkeypatch.setattr(Api, "_open_socket", _open, raising=True)
    monkeypatch.setattr(Api, "project_get_files", _files, raising=True)
    monkeypatch.setattr(Api, "_pull_doc_project_file_content", _pull, raising=True)
    return api


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


# ---------------------------------------------------------------- write_doc


def test_write_doc_empty_diff_short_circuits(monkeypatch: pytest.MonkeyPatch):
    """Identical content -> no applyOtUpdate emit, silent_no_op=False."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    api = _make_api_with_fake(monkeypatch, tree=tree, fake=fake)

    pre_text = "hello world"

    def _scripted():
        _feed_join_project(fake)
        # Respond to joinDoc with the same content the caller will submit.
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, pre_text.split("\n"), 4, [], {}]))

    runner = threading.Thread(target=_scripted)
    runner.start()
    result = api.write_doc("p", "main.tex", pre_text)
    runner.join(timeout=3.0)

    assert isinstance(result, WriteResult)
    assert result.old_version == 4
    assert result.new_version == 4
    assert result.silent_no_op is False
    # Only joinDoc + leaveDoc were sent; no applyOtUpdate.
    apply_frames = [
        f
        for f in fake.sent
        if '"name": "applyOtUpdate"' in f or '"name":"applyOtUpdate"' in f
    ]
    assert apply_frames == []


def test_write_doc_non_doc_rejected(monkeypatch: pytest.MonkeyPatch):
    """write_doc must refuse non-doc targets via the OT error path."""
    fake = FakeSocket()
    tree = _root_with([_binary("bin-1", "data.bin")])
    api = _make_api_with_fake(monkeypatch, tree=tree, fake=fake)

    from pyoverleaf._ot import OtError

    with pytest.raises(OtError):
        api.write_doc("p", "data.bin", "noop")


def test_write_doc_missing_file_raises(monkeypatch: pytest.MonkeyPatch):
    """A missing file path must raise FileNotFoundError before any I/O."""
    fake = FakeSocket()
    tree = _root_with([])
    api = _make_api_with_fake(monkeypatch, tree=tree, fake=fake)
    with pytest.raises(FileNotFoundError):
        api.write_doc("p", "main.tex", "x")


def test_write_doc_silent_noop_raises_by_default(monkeypatch: pytest.MonkeyPatch):
    """Server reports apply, but post-read shows pre-edit text -> SilentNoOpError."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre_text = "hello"
    new_text = "hello world"
    # Server post-edit text matches the pre-edit text (the op was nullified).
    api = _make_api_with_fake(
        monkeypatch, tree=tree, fake=fake, server_text_after=pre_text
    )

    def _scripted():
        _feed_join_project(fake)
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, pre_text.split("\n"), 4, [], {}]))
        _wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        # sender otUpdateApplied: doc + v but no 'op'
        fake.feed(
            "5:::"
            + json.dumps(
                {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 4}]}
            )
        )
        # leaveDoc ack
        _wait_for_send(fake, "5:3+::")
        fake.feed("6:::3+[null]")

    runner = threading.Thread(target=_scripted)
    runner.start()
    with pytest.raises(SilentNoOpError):
        api.write_doc("p", "main.tex", new_text)
    runner.join(timeout=3.0)


def test_write_doc_silent_noop_flag_when_not_raising(monkeypatch: pytest.MonkeyPatch):
    """raise_on_silent_noop=False surfaces the no-op via the result flag."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre_text = "hello"
    new_text = "hello world"
    api = _make_api_with_fake(
        monkeypatch, tree=tree, fake=fake, server_text_after=pre_text
    )

    def _scripted():
        _feed_join_project(fake)
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, pre_text.split("\n"), 4, [], {}]))
        _wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        fake.feed(
            "5:::"
            + json.dumps(
                {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 4}]}
            )
        )
        _wait_for_send(fake, "5:3+::")
        fake.feed("6:::3+[null]")

    runner = threading.Thread(target=_scripted)
    runner.start()
    result = api.write_doc("p", "main.tex", new_text, raise_on_silent_noop=False)
    runner.join(timeout=3.0)
    assert result.silent_no_op is True
    assert result.old_version == 4
    assert result.new_version == 5


def test_write_doc_returns_server_confirmed_version(monkeypatch: pytest.MonkeyPatch):
    """The returned version comes from the sender echo (event.v + 1).

    Even if the server transformed our op against a concurrent change, the
    new version comes from the echo, not an optimistic baseline + 1.
    """
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    pre_text = "hello"
    new_text = "hello world"
    server_text_after = "hello universe world"  # something changed
    api = _make_api_with_fake(
        monkeypatch, tree=tree, fake=fake, server_text_after=server_text_after
    )

    def _scripted():
        _feed_join_project(fake)
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, pre_text.split("\n"), 10, [], {}]))
        _wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        # The server transformed our op against a concurrent change; the
        # sender echo's `v` is 12 (not our baseline of 10), so new = 13.
        fake.feed(
            "5:::"
            + json.dumps(
                {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 12}]}
            )
        )
        _wait_for_send(fake, "5:3+::")
        fake.feed("6:::3+[null]")

    runner = threading.Thread(target=_scripted)
    runner.start()
    result = api.write_doc("p", "main.tex", new_text)
    runner.join(timeout=3.0)
    assert result.old_version == 10
    assert result.new_version == 13
    assert result.silent_no_op is False


def test_write_doc_ot_update_error_propagates(monkeypatch: pytest.MonkeyPatch):
    """A server `otUpdateError` frame must propagate as `OtUpdateError`."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    api = _make_api_with_fake(monkeypatch, tree=tree, fake=fake)

    def _scripted():
        _feed_join_project(fake)
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, ["hello"], 4, [], {}]))
        _wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        # Server emits otUpdateError instead of applied
        fake.feed(
            "5:::" + json.dumps({"name": "otUpdateError", "args": [{"code": "TooBig"}]})
        )

    runner = threading.Thread(target=_scripted)
    runner.start()
    with pytest.raises(OtUpdateError):
        api.write_doc("p", "main.tex", "different content")
    runner.join(timeout=3.0)


# -------------------------------------------------------- apply_ot_update


def test_apply_ot_update_returns_sender_event_v_plus_one(
    monkeypatch: pytest.MonkeyPatch,
):
    """apply_ot_update returns the sender-echo event.v plus one."""
    fake = FakeSocket()
    tree = _root_with([_doc("doc-1", "main.tex")])
    api = _make_api_with_fake(monkeypatch, tree=tree, fake=fake)

    def _scripted():
        _feed_join_project(fake)
        # ack for joinDoc (ack id 1)
        _wait_for_send(fake, "5:1+::")
        fake.feed("6:::1+" + json.dumps([None, ["hello"], 4, [], {}]))
        # ack for applyOtUpdate (ack id 2)
        _wait_for_send(fake, "5:2+::")
        fake.feed("6:::2+[null]")
        # sender echo with v=7 -> caller should return 8
        fake.feed(
            "5:::"
            + json.dumps(
                {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 7}]}
            )
        )
        _wait_for_send(fake, "5:3+::")
        fake.feed("6:::3+[null]")

    runner = threading.Thread(target=_scripted)
    runner.start()
    new_v = api.apply_ot_update("p", "doc-1", [{"p": 0, "i": "x"}], 4)
    runner.join(timeout=3.0)
    assert new_v == 8


def test_apply_ot_update_rejects_invalid_ops(monkeypatch: pytest.MonkeyPatch):
    """apply_ot_update validates op shape and version before any I/O."""
    api = Api()
    api._session_initialized = True
    # No need to wire socket: validation happens before any I/O.
    with pytest.raises(ValueError, match="ops must be non-empty"):
        api.apply_ot_update("p", "doc-1", [], 0)
    with pytest.raises(ValueError, match=r"ops\[0\].p must be a non-negative int"):
        api.apply_ot_update("p", "doc-1", [{"p": -1, "i": "x"}], 0)
    with pytest.raises(ValueError, match="exactly one of 'i'"):
        api.apply_ot_update("p", "doc-1", [{"p": 0}], 0)
    with pytest.raises(ValueError, match="exactly one of 'i'"):
        api.apply_ot_update("p", "doc-1", [{"p": 0, "i": "x", "d": "y"}], 0)
    with pytest.raises(ValueError, match="version must be a non-negative int"):
        api.apply_ot_update("p", "doc-1", [{"p": 0, "i": "x"}], -1)
