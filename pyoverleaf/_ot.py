"""Pure OT building blocks for the Overleaf ShareJS text protocol.

I/O-free helpers used by the synchronous OT write path. All public surface
here is deterministic, side-effect-free, and safe to call from any thread.
"""

from __future__ import annotations

import os
import time

import diff_match_patch

ShareJsOp = dict[str, int | str]


_DMP = diff_match_patch.diff_match_patch()
_DIFF_DELETE = -1
_DIFF_EQUAL = 0
_DIFF_INSERT = 1


def _utf16_units(text: str) -> int:
    """Length of `text` in UTF-16 code units.

    Overleaf ShareJS op positions (`p`) are JavaScript string offsets, which
    are UTF-16 code units. A character outside the BMP (e.g. an emoji) spans
    two UTF-16 code units (one surrogate pair), but only one Python code
    point. Each UTF-16 code unit is exactly two bytes in UTF-16-LE encoding.
    """
    return len(text.encode("utf-16-le")) // 2


def text_to_ops(old_text: str, new_text: str) -> list[ShareJsOp]:
    """Convert `old_text` to `new_text` as a ShareJS-style op list.

    Each op operates on the result of applying all preceding ops in the
    list, matching Overleaf's `applyOtUpdate` semantics. Op positions are
    expressed in UTF-16 code units (JavaScript string indices), not Python
    code points.
    """
    if old_text == new_text:
        return []
    diffs = _DMP.diff_main(old_text, new_text)
    _DMP.diff_cleanupSemantic(diffs)
    ops: list[ShareJsOp] = []
    pos = 0
    for kind, chunk in diffs:
        if kind == _DIFF_EQUAL:
            pos += _utf16_units(chunk)
        elif kind == _DIFF_INSERT:
            ops.append({"p": pos, "i": chunk})
            pos += _utf16_units(chunk)
        elif kind == _DIFF_DELETE:
            ops.append({"p": pos, "d": chunk})
            # cursor stays; next op anchors at the same position.
    return ops


def generate_id_seed() -> str:
    """Generate the 18-hex-char `meta.tc` seed that enables tracked changes.

    Format mirrors Overleaf's `ranges-tracker.generateIdSeed`:
    ts(8 hex) + machine(6 hex) + pid(4 hex). The server's RangesTracker uses
    this as the deterministic seed for per-op change IDs when `meta.tc` is
    set.
    """
    ts = format(int(time.time()) & 0xFFFFFFFF, "08x")
    machine = format(int.from_bytes(os.urandom(3), "big"), "06x")
    pid = format(os.getpid() & 0xFFFF, "04x")
    return ts + machine + pid


def decode_packed_utf8(line: str) -> str:
    """Decode a docLine that arrived as Latin-1-encoded UTF-8 bytes.

    Overleaf serializes doc lines as a JSON array of strings, but the bytes
    inside each string are the raw UTF-8 bytes of the line, interpreted as
    Latin-1 code points by the JSON layer. Round-tripping through
    `latin-1` -> `utf-8` restores the original text.
    """
    return line.encode("latin-1").decode("utf-8")


class OtError(RuntimeError):
    """Base class for all OT write failures."""


class OtUpdateError(OtError):
    """Server emitted `otUpdateError` or an ack with a non-null error.

    `server_message` carries the raw server-side description when available.
    """

    def __init__(self, server_message: object = None) -> None:
        super().__init__(
            str(server_message) if server_message is not None else "otUpdateError"
        )
        self.server_message = server_message


class OtVersionConflict(OtUpdateError):
    """Server reported the submitted base version is stale beyond merge."""


class OtDeleteMismatch(OtUpdateError):
    """A `d:` op's text did not match the document at the given position."""


class SilentNoOpError(OtError):
    """Submitted ops were non-empty but the server's post-edit text is unchanged.

    Usually means the op was transformed away by a concurrent collaborator
    update, or the op collapsed to a no-op under server-side OT.
    """


class MultipleMatchesError(OtError):
    """A find-and-replace call found more matches than the caller asked for.

    Raised when `expect_unique=True` (the default) and `count` is unset and
    the literal find string occurs more than once. Carries the match
    `count` so callers can decide how to disambiguate.
    """

    def __init__(self, find: str, occurrences: int) -> None:
        super().__init__(
            f"found {occurrences} matches for {find!r}; "
            f"pass expect_unique=False (or --all on CLI) to replace them all, "
            f"or pass count=N to replace the first N"
        )
        self.find = find
        self.occurrences = occurrences
