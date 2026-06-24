"""Shared FakeSocket harness used by the OT integration test modules.

The real `websocket._core.WebSocket` interface is wide; we only need the
methods `OtSession` calls: `settimeout`, `send`, `recv`, `close`,
`abort`. Each test feeds inbound frames and asserts on `sent`.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._inbound: deque[Any] = deque()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._timeout: float = 5.0
        self._closed = False

    def settimeout(self, t: float) -> None:
        self._timeout = float(t)

    def send(self, frame: Any) -> None:
        if self._closed:
            raise WebSocketConnectionClosedException("closed")
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8")
        self.sent.append(frame)

    def recv(self) -> str:
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
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def abort(self) -> None:
        self.close()

    def feed(self, frame: str) -> None:
        with self._cv:
            self._inbound.append(frame)
            self._cv.notify_all()


def feed_join_project(fake: FakeSocket) -> None:
    """Feed a synthetic joinProjectResponse frame onto `fake`."""
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


def wait_for_send(fake: FakeSocket, prefix: str, timeout: float = 3.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for f in fake.sent:
            if f.startswith(prefix):
                return f
        time.sleep(0.01)
    raise AssertionError(f"no send starting with {prefix!r} in {fake.sent!r}")
