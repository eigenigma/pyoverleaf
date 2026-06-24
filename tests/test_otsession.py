"""Unit tests for pyoverleaf._otsession.OtSession against a FakeSocket."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

import pytest
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

from pyoverleaf._ot import OtUpdateError
from pyoverleaf._otframes import SocketClosed, parse_frame
from pyoverleaf._otsession import OtSession


class FakeSocket:
    """In-memory bidirectional socket double for OtSession tests.

    The reader pulls from `_inbound`; the test pushes frames via `feed()`.
    Sends are captured in `sent`. `recv()` blocks on a Condition with a
    timeout that mimics `websocket-client`'s `WebSocketTimeoutException`.
    """

    def __init__(self) -> None:
        """Initialize empty inbound/sent buffers and a default 5s timeout."""
        self.sent: list[str] = []
        self._inbound: deque[Any] = deque()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._timeout: float = 5.0
        self._closed = False
        self._aborted = False

    # OtSession surface ------------------------------------------------
    def settimeout(self, t: float) -> None:
        """Set the recv timeout (seconds) used by `recv()`."""
        self._timeout = float(t)

    def send(self, frame: Any) -> None:
        """Capture an outbound frame into `self.sent` (utf-8 decoded if bytes)."""
        if self._closed:
            raise WebSocketConnectionClosedException("closed")
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8")
        self.sent.append(frame)

    def recv(self) -> str:
        """Block until a frame is fed; raise on close/abort/timeout."""
        with self._cv:
            deadline = time.time() + self._timeout
            while not self._inbound:
                if self._closed or self._aborted:
                    raise WebSocketConnectionClosedException("closed")
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise WebSocketTimeoutException("recv timeout")
                self._cv.wait(timeout=remaining)
            item = self._inbound.popleft()
            if isinstance(item, BaseException):
                raise item
            return item

    def close(self) -> None:
        """Mark the socket closed and wake any blocked readers."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def abort(self) -> None:
        """Mark the socket aborted (also closed) and wake any blocked readers."""
        with self._cv:
            self._aborted = True
            self._closed = True
            self._cv.notify_all()

    # Test surface -----------------------------------------------------
    def feed(self, frame: str) -> None:
        """Push a server-origin frame onto the inbound queue."""
        with self._cv:
            self._inbound.append(frame)
            self._cv.notify_all()

    def feed_exception(self, exc: BaseException) -> None:
        """Push an exception to be raised by the next `recv()` call."""
        with self._cv:
            self._inbound.append(exc)
            self._cv.notify_all()


# -------- frame parser ---------------------------------------------------


def test_parse_frame_event_with_ack_id():
    """`parse_frame` extracts type, ack id, and payload from an event frame."""
    assert parse_frame('5:7+::{"name":"joinDoc"}') == (
        "5",
        "7+",
        "",
        '{"name":"joinDoc"}',
    )


def test_parse_frame_event_no_ack_id():
    """`parse_frame` accepts event frames with an empty ack id (`5:::...`)."""
    # joinProjectResponse arrives as `5:::{...}` (empty id field)
    parsed = parse_frame('5:::{"name":"joinProjectResponse"}')
    assert parsed is not None
    assert parsed[0] == "5"
    assert parsed[2] == ""
    assert parsed[3] == '{"name":"joinProjectResponse"}'


def test_parse_frame_ack():
    """`parse_frame` returns the ack id and JSON payload for type-6 ack frames."""
    parsed = parse_frame("6:::1+[null,3]")
    assert parsed == ("6", "", "", "1+[null,3]")


def test_parse_frame_heartbeat():
    """`parse_frame` recognizes the bare `2::` server heartbeat frame."""
    assert parse_frame("2::") == ("2", "", "", "")


def test_parse_frame_malformed():
    """`parse_frame` returns None on empty/garbled input rather than raising."""
    assert parse_frame("") is None
    assert parse_frame("not-a-frame") is None
    assert parse_frame("abc:def") is None


# -------- await_join_project / capture ------------------------------------


def _make_session(ws: FakeSocket, *, hb: float = 60.0) -> OtSession:
    s = OtSession(ws, heartbeat_interval=hb)
    s.start()
    return s


def _feed_join_project(
    ws: FakeSocket,
    *,
    public_id: str = "pub-1",
    perm: str = "owner",
    proto: int = 2,
) -> None:
    payload = {
        "name": "joinProjectResponse",
        "args": [
            {
                "publicId": public_id,
                "permissionsLevel": perm,
                "protocolVersion": proto,
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
    ws.feed("5:::" + json.dumps(payload))


def test_await_join_project_captures_fields():
    """`await_join_project` captures publicId/permissions/protocol/root fields."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws, public_id="abc")
        payload = s.await_join_project(timeout=3.0)
        assert payload["publicId"] == "abc"
        assert s.public_id == "abc"
        assert s.permissions_level == "owner"
        assert s.protocol_version == 2
        assert s.root_folder == {
            "_id": "root",
            "name": "root",
            "folders": [],
            "fileRefs": [],
            "docs": [],
        }
    finally:
        s.close()


# -------- join_doc returns (text, version) --------------------------------


def test_join_doc_decodes_lines_and_version():
    """`join_doc` returns the joined text and the document version from the ack."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws)
        s.await_join_project(timeout=3.0)

        # Ack arrives for ack_id=1, with [null, docLines, version, [], {}]
        # Use ASCII so latin-1 -> utf-8 round-trip is a no-op.
        # The session sends joinDoc with ack id 1 (first emit).
        # Test thread: wait until a send shows up, then feed the ack.
        def _emit():
            return s.join_doc("doc-1", timeout=3.0)

        result_box = {}
        t = threading.Thread(target=lambda: result_box.setdefault("r", _emit()))
        t.start()

        # wait until the emit lands
        deadline = time.time() + 3.0
        while not ws.sent and time.time() < deadline:
            time.sleep(0.01)
        assert ws.sent, "joinDoc emit not observed"
        ws.feed('6:::1+[null,["hello","world"],7,[],{}]')
        t.join(timeout=3.0)
        assert "r" in result_box
        text, version = result_box["r"]
        assert text == "hello\nworld"
        assert version == 7
    finally:
        s.close()


# -------- emit frame shape ------------------------------------------------


def test_emit_writes_event_frame_with_ack_id():
    """`emit` writes a `5:N+::` event frame and resolves on the matching ack."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws)
        s.await_join_project(timeout=3.0)
        result_box = {}
        t = threading.Thread(
            target=lambda: result_box.setdefault(
                "r", s.emit("ping", [{"x": 1}], timeout=3.0)
            )
        )
        t.start()
        deadline = time.time() + 3.0
        while not ws.sent and time.time() < deadline:
            time.sleep(0.01)
        assert ws.sent
        frame = ws.sent[-1]
        # Expected shape: 5:1+::{"name":"ping","args":[{"x":1}]}
        assert frame.startswith("5:1+::")
        payload = json.loads(frame[len("5:1+::") :])
        assert payload == {"name": "ping", "args": [{"x": 1}]}
        ws.feed('6:::1+[null,"ok"]')
        t.join(timeout=3.0)
        assert result_box["r"] == ["ok"]
    finally:
        s.close()


# -------- ack error propagation -------------------------------------------


def test_ack_with_non_null_error_raises():
    """An ack payload whose first element is non-null surfaces as `OtUpdateError`."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws)
        s.await_join_project(timeout=3.0)
        result_box = {}

        def _go():
            try:
                s.emit("bad", [], timeout=3.0)
            except OtUpdateError as e:
                result_box["err"] = e

        t = threading.Thread(target=_go)
        t.start()
        deadline = time.time() + 3.0
        while not ws.sent and time.time() < deadline:
            time.sleep(0.01)
        ws.feed('6:::1+["server-said-no"]')
        t.join(timeout=3.0)
        assert "err" in result_box
        assert "server-said-no" in str(result_box["err"])
    finally:
        s.close()


# -------- apply_ot_update_and_wait: confirmation via sender echo ----------


def test_apply_ot_update_resolves_on_sender_echo_and_returns_v_plus_one():
    """Ignores collab broadcasts, resolves on the sender echo, returns v+1."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws, public_id="me")
        s.await_join_project(timeout=3.0)
        result_box = {}

        def _go():
            result_box["v"] = s.apply_ot_update_and_wait(
                "doc-1", [{"p": 0, "i": "x"}], 4, timeout=3.0
            )

        t = threading.Thread(target=_go)
        t.start()
        deadline = time.time() + 3.0
        # Wait for emit
        while not ws.sent and time.time() < deadline:
            time.sleep(0.01)
        # Ack the applyOtUpdate (enqueue)
        ws.feed("6:::1+[null]")
        # A collaborator broadcast (has 'op') must NOT resolve our submit.
        collab = {
            "name": "otUpdateApplied",
            "args": [{"doc": "doc-1", "v": 99, "op": [{"p": 0, "i": "z"}], "meta": {}}],
        }
        ws.feed("5:::" + json.dumps(collab))
        # Submit should still be pending; sleep a touch and assert not done
        time.sleep(0.1)
        assert "v" not in result_box
        # Now feed the sender-shape echo (no 'op')
        sender = {"name": "otUpdateApplied", "args": [{"doc": "doc-1", "v": 4}]}
        ws.feed("5:::" + json.dumps(sender))
        t.join(timeout=3.0)
        assert result_box["v"] == 5  # event.v + 1
    finally:
        s.close()


def test_apply_ot_update_ot_update_error_raises():
    """An `otUpdateError` frame fails the pending submit with `OtUpdateError`."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws)
        s.await_join_project(timeout=3.0)
        result_box = {}

        def _go():
            try:
                s.apply_ot_update_and_wait(
                    "doc-1", [{"p": 0, "i": "x"}], 0, timeout=3.0
                )
            except OtUpdateError as e:
                result_box["err"] = e

        t = threading.Thread(target=_go)
        t.start()
        deadline = time.time() + 3.0
        while not ws.sent and time.time() < deadline:
            time.sleep(0.01)
        ws.feed("6:::1+[null]")
        ws.feed(
            "5:::" + json.dumps({"name": "otUpdateError", "args": [{"code": "TooBig"}]})
        )
        t.join(timeout=3.0)
        assert "err" in result_box
        assert "TooBig" in str(result_box["err"])
    finally:
        s.close()


# -------- heartbeat -------------------------------------------------------


def test_server_heartbeat_is_echoed():
    """Server-sent `2::` heartbeats are echoed back so the connection stays alive."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws)
        s.await_join_project(timeout=3.0)
        ws.feed("2::")
        # Allow reader to process and echo
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(f == "2::" for f in ws.sent):
                break
            time.sleep(0.02)
        assert any(f == "2::" for f in ws.sent), f"no echoed heartbeat in {ws.sent!r}"
    finally:
        s.close()


# -------- server error frame (type 7) marks session unusable --------------


def test_type_7_frame_fails_pending_and_marks_unusable():
    """A type-7 server error fails pending emits and blocks subsequent emits."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws)
        s.await_join_project(timeout=3.0)
        result_box = {}

        def _go():
            try:
                s.emit("anything", [], timeout=3.0)
            except SocketClosed as e:
                result_box["err"] = e

        t = threading.Thread(target=_go)
        t.start()
        deadline = time.time() + 3.0
        while not ws.sent and time.time() < deadline:
            time.sleep(0.01)
        ws.feed("7:::session-expired")
        t.join(timeout=3.0)
        assert "err" in result_box

        # New emits should fail immediately
        with pytest.raises(SocketClosed):
            s.emit("x", [], timeout=1.0)
    finally:
        s.close()


# -------- shutdown via close() unblocks reader ----------------------------


def test_close_while_blocked_in_recv_unblocks_reader():
    """`close()` aborts a recv() that is blocked on an empty inbound queue."""
    ws = FakeSocket()
    s = _make_session(ws)
    try:
        _feed_join_project(ws)
        s.await_join_project(timeout=3.0)
        # Reader is now blocked in recv() on an empty inbound queue.
        # close() must unblock it (via abort + fail pending).
    finally:
        t0 = time.time()
        s.close()
        elapsed = time.time() - t0
        assert elapsed < 5.5, f"close() did not return in time ({elapsed:.2f}s)"
        assert s._reader is not None
        assert not s._reader.is_alive(), "reader thread did not exit after close()"


def test_close_fails_pending_futures():
    """`close()` fails any in-flight emit futures with `SocketClosed`."""
    ws = FakeSocket()
    s = _make_session(ws)
    _feed_join_project(ws)
    s.await_join_project(timeout=3.0)
    result_box = {}

    def _go():
        try:
            s.emit("pending", [], timeout=10.0)
        except SocketClosed as e:
            result_box["err"] = e

    t = threading.Thread(target=_go)
    t.start()
    deadline = time.time() + 3.0
    while not ws.sent and time.time() < deadline:
        time.sleep(0.01)
    s.close()
    t.join(timeout=3.0)
    assert "err" in result_box
