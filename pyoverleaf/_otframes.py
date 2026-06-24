"""Frame-parsing primitives for the Socket.IO 0.9 wire protocol.

Pulled out of `_otsession.py` so the session orchestration module stays
focused on lifecycle / dispatch logic. Pure functions; no I/O.

Wire format: `<type>:<id>:<endpoint>:<data>`
  0 disconnect | 1 connect | 2 heartbeat | 3 message | 4 json
  5 event { name, args } | 6 ack `<id>+<json_array>` | 7 error
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from concurrent.futures import Future

_FRAME_RE = re.compile(r"^(\d+):([^:]*):([^:]*):?([\s\S]*)$")


class SocketClosed(RuntimeError):
    """The underlying websocket was closed before the operation completed."""


def parse_frame(frame: str) -> tuple[str, str, str, str] | None:
    """Parse a Socket.IO 0.9 frame into (type, id, endpoint, data).

    Returns None for unparseable frames; callers should ignore those.
    """
    if not frame:
        return None
    m = _FRAME_RE.match(frame)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


EventMatcher = Callable[[dict[str, Any]], bool]


class EventWaiter:
    """Pairs a Future with a predicate used to claim matching event payloads."""

    __slots__ = ("future", "matcher")

    def __init__(self, future: Future[dict[str, Any]], matcher: EventMatcher) -> None:
        self.future = future
        self.matcher = matcher
