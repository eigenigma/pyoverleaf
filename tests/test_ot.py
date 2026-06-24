"""Unit tests for pyoverleaf._ot."""

from __future__ import annotations

import re

import pytest

from pyoverleaf._ot import (
    OtDeleteMismatch,
    OtError,
    OtUpdateError,
    OtVersionConflict,
    SilentNoOpError,
    decode_packed_utf8,
    generate_id_seed,
    text_to_ops,
)


def _apply_ops_js(text: str, ops):
    """Reference op-applier operating on a list of UTF-16 code units.

    Mirrors how the Overleaf server applies ops to a JavaScript string.
    """
    encoded = text.encode("utf-16-le")
    units = [encoded[i : i + 2] for i in range(0, len(encoded), 2)]
    for op in ops:
        p = op["p"]
        if "i" in op:
            ins = op["i"].encode("utf-16-le")
            ins_units = [ins[i : i + 2] for i in range(0, len(ins), 2)]
            units = units[:p] + ins_units + units[p:]
        elif "d" in op:
            d = op["d"].encode("utf-16-le")
            d_units = [d[i : i + 2] for i in range(0, len(d), 2)]
            actual = units[p : p + len(d_units)]
            assert actual == d_units, (
                f"delete mismatch at p={p}: {actual!r} vs {d_units!r}"
            )
            units = units[:p] + units[p + len(d_units) :]
    return b"".join(units).decode("utf-16-le")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("", ""),
        ("abc", "abc"),
        ("", "hello"),
        ("hello", ""),
        ("hello", "world"),
        ("hello", "hello world"),
        ("hello world", "hi world"),
        ("one\ntwo\nthree", "one\nTWO\nthree"),
        ("abcdef", "abXcdYef"),
        ("the quick brown fox", "the slow brown fox jumps"),
        ("CJK test: ni hao", "CJK test: ni hao ma"),
        ("zhong wen ce shi", "ce shi zhong wen"),
    ],
)
def test_text_to_ops_round_trip(old, new):
    """text_to_ops output, applied to `old`, must reproduce `new` exactly."""
    ops = text_to_ops(old, new)
    assert _apply_ops_js(old, ops) == new


def test_text_to_ops_no_op():
    """Identical inputs must produce no ops at all."""
    assert text_to_ops("same", "same") == []
    assert text_to_ops("", "") == []


def test_text_to_ops_utf16_astral_emoji_position():
    """Insert position past an astral char counts UTF-16 units, not code points."""
    # 'a' = 1 UTF-16 unit; emoji = 2 UTF-16 units (surrogate pair).
    # An insertion after the emoji must be at p=3, not p=2 (code-point count).
    ops = text_to_ops("a\U0001f600b", "a\U0001f600Xb")
    assert ops == [{"p": 3, "i": "X"}]


def test_text_to_ops_utf16_astral_round_trip():
    """Round-trip across an astral character must reproduce the new text exactly."""
    old = "abc\U0001f680def"
    new = "abc\U0001f680X\U0001f680def"
    ops = text_to_ops(old, new)
    assert _apply_ops_js(old, ops) == new


def test_text_to_ops_delete_emoji_position():
    """Deleting an astral char emits delete-only ops, no insertions."""
    # Deleting the emoji itself: position 1, deleted string is the emoji
    # (2 UTF-16 units), and what remains is 'a' + 'b' at positions 0 and 1.
    ops = text_to_ops("a\U0001f600b", "ab")
    assert _apply_ops_js("a\U0001f600b", ops) == "ab"
    assert all("i" not in op for op in ops)


def test_generate_id_seed_format():
    """generate_id_seed returns an 18-char lowercase hex string."""
    seed = generate_id_seed()
    assert isinstance(seed, str)
    assert len(seed) == 18
    assert re.fullmatch(r"[0-9a-f]{18}", seed) is not None
    # hex-parseable as a single integer
    int(seed, 16)


def test_generate_id_seed_distinct_calls():
    """Repeated generate_id_seed calls must yield distinct values."""
    seeds = {generate_id_seed() for _ in range(8)}
    # urandom-driven machine field randomises across calls; collisions are
    # astronomically unlikely.
    assert len(seeds) >= 4


@pytest.mark.parametrize(
    "raw",
    [
        "hello",
        "ASCII only line",
        "ni hao shi jie",
        "rocket here too",
        "mixed ASCII and CJK ni hao",
        "",
    ],
)
def test_decode_packed_utf8_round_trip(raw):
    """decode_packed_utf8 reverses the Latin-1 packing Overleaf applies on the wire."""
    # Simulate the wire-format: take the UTF-8 bytes of `raw`, decode as
    # Latin-1 to get the "packed" string Overleaf hands us via JSON, then
    # check our decoder restores the original.
    packed = raw.encode("utf-8").decode("latin-1")
    assert decode_packed_utf8(packed) == raw


def test_exception_hierarchy():
    """OT exceptions must form the documented subclass tree."""
    assert issubclass(OtError, RuntimeError)
    assert issubclass(OtUpdateError, OtError)
    assert issubclass(OtVersionConflict, OtUpdateError)
    assert issubclass(OtDeleteMismatch, OtUpdateError)
    assert issubclass(SilentNoOpError, OtError)


def test_ot_update_error_carries_server_message():
    """OtUpdateError must expose the raw server payload via server_message."""
    err = OtUpdateError({"code": "TooBig"})
    assert err.server_message == {"code": "TooBig"}
    assert "TooBig" in str(err)

    err_none = OtUpdateError()
    assert err_none.server_message is None
    assert "otUpdateError" in str(err_none)
