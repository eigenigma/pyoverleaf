"""Synchronous Socket.IO 0.9 OT session for Overleaf.

`OtSession` wraps an already-connected `websocket-client` socket with one
reader thread that owns every `recv()` call, dispatches ack and event
frames to pending `concurrent.futures.Future` objects, and emits / consumes
heartbeats. The session is request/response: open, join doc(s), emit
`applyOtUpdate`, await sender `otUpdateApplied` echo, close. Callers should
treat the session as single-use after `close()`.

Confirmation model: the ack on `applyOtUpdate` proves enqueue, not apply.
The applied result arrives as an `otUpdateApplied` event. The sender echo
has shape {v, doc} (no `op` field); collaborator broadcasts carry the full
transformed `update` with an `op` field. We disambiguate by shape and only
resolve the pending submit on the sender echo.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket as _stdsocket
import threading
from concurrent.futures import Future
from typing import Any

from websocket import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
)

from ._ot import (
    OtUpdateError,
    decode_packed_utf8,
    generate_id_seed,
)
from ._otframes import (
    EventMatcher,
    EventWaiter,
    SocketClosed,
    parse_frame,
)

_log = logging.getLogger(__name__)

__all__ = ["OtSession", "SocketClosed"]


class OtSession:
    """Synchronous OT session on top of an open websocket-client socket.

    Construction is cheap; call `start()` to spawn the reader+heartbeat
    threads, then `await_join_project()` once before any other emit.
    """

    def __init__(
        self,
        ws: Any,
        *,
        heartbeat_interval: float = 60.0,
        public_id_hint: str | None = None,
    ) -> None:
        self._ws = ws
        self._heartbeat_interval = max(15.0, heartbeat_interval)
        # Read timeout short enough that `close()` never blocks much on recv.
        self._read_timeout = min(max(1.0, self._heartbeat_interval / 4.0), 5.0)
        with contextlib.suppress(Exception):
            self._ws.settimeout(self._read_timeout)

        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._next_ack_id = 1
        self._pending_acks: dict[int, Future[list[Any]]] = {}
        self._event_waiters: dict[str, list[EventWaiter]] = {}
        self._closing = threading.Event()
        self._closed = False
        self._unusable = False

        self._reader: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None

        # Captured from joinProjectResponse.
        self.public_id: str | None = public_id_hint
        self.root_folder: dict[str, Any] | None = None
        self.permissions_level: str | None = None
        self.protocol_version: int | None = None
        self.project: dict[str, Any] | None = None
        self._join_project_future: Future[dict[str, Any]] = Future()

    # ------- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._reader is not None:
            return
        self._reader = threading.Thread(
            target=self._reader_loop, name="ot-session-reader", daemon=True
        )
        self._reader.start()
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="ot-session-heartbeat", daemon=True
        )
        self._heartbeat.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing.set()
        self._fail_all(SocketClosed("session closed"))
        # Wake any blocked recv() on the reader. All branches are
        # best-effort cleanup; any error during shutdown is intentionally
        # swallowed so we never block close().
        abort = getattr(self._ws, "abort", None)
        if callable(abort):
            with contextlib.suppress(OSError, RuntimeError):
                abort()
        else:
            sock = getattr(self._ws, "sock", None)
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(_stdsocket.SHUT_RDWR)
            with contextlib.suppress(OSError, RuntimeError):
                self._ws.close()
        if self._reader is not None:
            self._reader.join(timeout=5.0)
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=2.0)

    # ------- public emit / wait surface ----------------------------------

    def await_join_project(self, timeout: float = 15.0) -> dict[str, Any]:
        return self._join_project_future.result(timeout=timeout)

    def emit(
        self,
        name: str,
        args: list[Any],
        *,
        timeout: float = 15.0,
    ) -> list[Any]:
        """Send a `5:<id>+::{name,args}` event and block on its ack."""
        with self._lock:
            if self._unusable or self._closed:
                raise SocketClosed("session is closed or unusable")
            ack_id = self._next_ack_id
            self._next_ack_id += 1
            fut: Future[list[Any]] = Future()
            self._pending_acks[ack_id] = fut

        # ensure_ascii=False keeps astral code points as their UTF-8 bytes
        # on the wire instead of \uXXXX-escaped surrogate pairs (the latter
        # round-trip through Overleaf as mojibake U+FFFD pairs).
        body = json.dumps({"name": name, "args": args}, ensure_ascii=False)
        frame = f"5:{ack_id}+::{body}"
        try:
            self._send(frame)
        except Exception as e:
            with self._lock:
                self._pending_acks.pop(ack_id, None)
            raise SocketClosed(f"send failed: {e!r}") from e

        try:
            return fut.result(timeout=timeout)
        finally:
            with self._lock:
                self._pending_acks.pop(ack_id, None)

    def wait_for_event(
        self,
        name: str,
        matcher: EventMatcher,
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Block until an event `name` arrives where `matcher(payload)` is True."""
        fut: Future[dict[str, Any]] = Future()
        waiter = EventWaiter(fut, matcher)
        with self._lock:
            if self._unusable or self._closed:
                raise SocketClosed("session is closed or unusable")
            self._event_waiters.setdefault(name, []).append(waiter)
        try:
            return fut.result(timeout=timeout)
        finally:
            self._drop_waiter(name, waiter)

    def join_doc(self, doc_id: str, *, timeout: float = 15.0) -> tuple[str, int]:
        """Emit joinDoc; return (text, baseline_version).

        joinDoc ack carries `[null, docLines, version, updates, ranges]`.
        Each line arrives Latin-1-encoded UTF-8 bytes; decode for the caller.
        """
        result = self.emit("joinDoc", [doc_id, {"encodeRanges": True}], timeout=timeout)
        if len(result) < 2:
            raise OtUpdateError(f"joinDoc returned unexpected ack shape: {result!r}")
        doc_lines = result[0] or []
        version = result[1] if len(result) >= 2 else 0
        text = "\n".join(decode_packed_utf8(line) for line in doc_lines)
        return text, int(version)

    def leave_doc(self, doc_id: str, *, timeout: float = 5.0) -> None:
        # Best-effort; closing the socket implicitly leaves the doc, so any
        # failure here is recoverable and we deliberately swallow it.
        with contextlib.suppress(SocketClosed, OtUpdateError, TimeoutError):
            self.emit("leaveDoc", [doc_id], timeout=timeout)

    def apply_ot_update_and_wait(
        self,
        doc_id: str,
        ops: list[dict[str, Any]],
        version: int,
        *,
        track_changes: bool = False,
        user_id: str = "",
        timeout: float = 15.0,
    ) -> int:
        """Submit an OT update and block until the server applies it.

        Returns the post-edit version (event.v + 1). The ack on
        `applyOtUpdate` only proves enqueue; we wait for the sender-shape
        `otUpdateApplied` event (`{v, doc}` with no `op`). Raises
        `OtUpdateError` if the server emits `otUpdateError`.
        """
        applied_fut: Future[dict[str, Any]] = Future()
        error_fut: Future[dict[str, Any]] = Future()
        applied_waiter = EventWaiter(
            applied_fut,
            lambda ev: ev.get("doc") == doc_id and "op" not in ev,
        )
        error_waiter = EventWaiter(error_fut, lambda _ev: True)
        with self._lock:
            if self._unusable or self._closed:
                raise SocketClosed("session is closed or unusable")
            self._event_waiters.setdefault("otUpdateApplied", []).append(applied_waiter)
            self._event_waiters.setdefault("otUpdateError", []).append(error_waiter)

        meta: dict[str, Any] = {"source": self.public_id or "", "user_id": user_id}
        if track_changes:
            meta["tc"] = generate_id_seed()
        update = {"doc": doc_id, "op": ops, "v": int(version), "meta": meta}

        try:
            self.emit("applyOtUpdate", [doc_id, update], timeout=timeout)
            return self._wait_applied(applied_fut, error_fut, version, doc_id, timeout)
        finally:
            self._drop_waiter("otUpdateApplied", applied_waiter)
            self._drop_waiter("otUpdateError", error_waiter)

    # ------- internals ----------------------------------------------------

    def _wait_applied(
        self,
        applied_fut: Future[dict[str, Any]],
        error_fut: Future[dict[str, Any]],
        version: int,
        doc_id: str,
        timeout: float,
    ) -> int:
        deadline_remaining = timeout
        step = 0.25
        while True:
            if error_fut.done():
                raise OtUpdateError(error_fut.result())
            if applied_fut.done():
                payload = applied_fut.result()
                return int(payload.get("v", version)) + 1
            if deadline_remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for otUpdateApplied on doc {doc_id}"
                )
            try:
                payload = applied_fut.result(timeout=step)
                return int(payload.get("v", version)) + 1
            except TimeoutError:
                pass
            except Exception as err:
                if error_fut.done():
                    raise OtUpdateError(error_fut.result()) from err
                raise
            deadline_remaining -= step

    def _send(self, frame: str) -> None:
        with self._send_lock:
            self._ws.send(frame)

    def _drop_waiter(self, name: str, waiter: EventWaiter) -> None:
        with self._lock:
            lst = self._event_waiters.get(name) or []
            if waiter in lst:
                lst.remove(waiter)
            if not lst:
                self._event_waiters.pop(name, None)

    def _reader_loop(self) -> None:
        try:
            while not self._closing.is_set():
                try:
                    raw = self._ws.recv()
                except WebSocketTimeoutException:
                    continue
                except (WebSocketConnectionClosedException, OSError):
                    break
                if not raw:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                self._handle_frame(raw)
        finally:
            self._fail_all(SocketClosed("reader exited"))

    def _heartbeat_loop(self) -> None:
        interval = max(15.0, self._heartbeat_interval - 5.0)
        while not self._closing.wait(timeout=interval):
            if self._closing.is_set() or self._closed or self._unusable:
                return
            # If sending the keepalive fails, the socket is gone; exit
            # quietly and let close() / reader-exit handle teardown.
            try:
                self._send("2::")
            except (OSError, WebSocketConnectionClosedException):
                return

    def _handle_frame(self, frame: str) -> None:
        parsed = parse_frame(frame)
        if parsed is None:
            return
        ftype, _fid, _fendpoint, data = parsed
        if ftype in ("0", "7"):
            with self._lock:
                self._unusable = True
            self._fail_all(SocketClosed(f"server frame type {ftype}: {data}"))
            return
        if ftype == "1":
            return
        if ftype == "2":
            with contextlib.suppress(Exception):
                self._send("2::")
            return
        if ftype == "5":
            self._handle_event(data)
            return
        if ftype == "6":
            self._handle_ack(data)
            return

    def _handle_event(self, data: str) -> None:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return
        name = obj.get("name")
        if not name:
            return
        args = obj.get("args") or []
        payload: dict[str, Any] = args[0] if args and isinstance(args[0], dict) else {}
        if name == "joinProjectResponse" and not self._join_project_future.done():
            self._capture_join_project(payload)
            with contextlib.suppress(Exception):
                self._join_project_future.set_result(payload)
        self._dispatch_event(name, payload)

    def _capture_join_project(self, payload: dict[str, Any]) -> None:
        self.public_id = payload.get("publicId") or self.public_id
        self.permissions_level = payload.get("permissionsLevel")
        self.protocol_version = payload.get("protocolVersion")
        proj = payload.get("project") or {}
        self.project = proj
        root = proj.get("rootFolder") or []
        self.root_folder = root[0] if root else None

    def _handle_ack(self, data: str) -> None:
        # Two ack shapes in the wild: `<id>+<json>` and bare `<id>` (no
        # payload). The minimal `6:::<id>` form is what Overleaf returns
        # for fire-and-forget calls like leaveDoc / applyOtUpdate.
        plus = data.find("+")
        if plus >= 0:
            id_str = data[:plus]
            rest = data[plus + 1 :]
        else:
            id_str = data
            rest = ""
        try:
            ack_id = int(id_str)
        except ValueError:
            return
        arr: list[Any] = []
        if rest:
            try:
                parsed = json.loads(rest)
            except json.JSONDecodeError:
                parsed = [rest]
            arr = parsed if isinstance(parsed, list) else [parsed]
        with self._lock:
            fut = self._pending_acks.pop(ack_id, None)
        if fut is None:
            return
        if not arr:
            with contextlib.suppress(Exception):
                fut.set_result([])
            return
        err = arr[0]
        if err:
            with contextlib.suppress(Exception):
                fut.set_exception(OtUpdateError(err))
        else:
            with contextlib.suppress(Exception):
                fut.set_result(arr[1:])

    def _dispatch_event(self, name: str, payload: dict[str, Any]) -> None:
        with self._lock:
            waiters = list(self._event_waiters.get(name) or [])
        for w in waiters:
            try:
                if not w.matcher(payload):
                    continue
            except Exception:  # noqa: BLE001 - user matcher must not crash reader thread
                _log.exception("OT event matcher raised; skipping waiter")
                continue
            if not w.future.done():
                with contextlib.suppress(Exception):
                    w.future.set_result(payload)

    def _fail_all(self, exc: BaseException) -> None:
        with self._lock:
            acks = list(self._pending_acks.values())
            self._pending_acks.clear()
            waiters: list[EventWaiter] = []
            for lst in self._event_waiters.values():
                waiters.extend(lst)
            self._event_waiters.clear()
            jp = self._join_project_future
        for fut in acks:
            if not fut.done():
                with contextlib.suppress(Exception):
                    fut.set_exception(exc)
        for w in waiters:
            if not w.future.done():
                with contextlib.suppress(Exception):
                    w.future.set_exception(exc)
        if jp is not None and not jp.done():
            with contextlib.suppress(Exception):
                jp.set_exception(exc)
