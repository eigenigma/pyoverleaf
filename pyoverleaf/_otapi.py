"""High-level OT operations wired on top of `Api`.

These are module-level functions rather than methods so the `Api` class
file stays under the per-file size budget; `Api` exposes thin delegating
wrappers (`apply_ot_update`, `write_doc`) that call into here.

Two entry points:

  - `apply_ot_update(api, project_id, doc_id, ops, version, ...)`: low-level,
    caller-built ops. Returns the server-confirmed new version.

  - `write_doc(api, project_id, file_path, new_content, ...)`: high-level.
    Resolves the path, diffs against the live doc, submits, and verifies the
    result. Raises `SilentNoOpError` by default on a server-transformed no-op.

The `api._*` accesses below are intentional: this module is an internal
collaborator of the `Api` class, which is deliberately split across sibling
modules to stay under the per-file size budget. The `# noqa: SLF001`
markers acknowledge that the `_`-prefix here means "not for external
callers", not "not for sibling modules".
"""

from __future__ import annotations

import contextlib
from typing import Any

from ._models import (
    DryRunResult,
    FindReplaceResult,
    ProjectFile,
    ProjectFolder,
    WriteResult,
)
from ._ot import MultipleMatchesError, OtError, SilentNoOpError, text_to_ops
from ._otsession import OtSession
from ._webapi import Api


def _affected_lines(text: str, ops: list[dict[str, Any]]) -> list[int]:
    """Return sorted unique 1-based line numbers in `text` touched by `ops`.

    Each op's `p` is a UTF-16 code-unit offset; we walk `text` in UTF-16
    units to map back to the line containing each position. A delete that
    spans multiple lines reports every line it overlaps.
    """
    if not ops:
        return []
    line_starts_utf16: list[int] = [0]
    units = 0
    for ch in text:
        units += 1 if ord(ch) < 0x10000 else 2
        if ch == "\n":
            line_starts_utf16.append(units)
    total_units = units

    def line_of(p: int) -> int:
        lo, hi = 0, len(line_starts_utf16) - 1
        if p >= line_starts_utf16[hi]:
            return hi + 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts_utf16[mid] <= p:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    affected: set[int] = set()
    for op in ops:
        p = int(op["p"])
        if "i" in op:
            affected.add(line_of(min(p, total_units)))
        else:
            text_d = op.get("d", "")
            span = sum(1 if ord(ch) < 0x10000 else 2 for ch in text_d)
            start = min(p, total_units)
            end = min(p + span, total_units)
            affected.add(line_of(start))
            affected.add(line_of(end))
    return sorted(affected)


def _validate_ops(ops: list[dict[str, Any]]) -> None:
    if not isinstance(ops, list):
        raise TypeError("ops must be a list")
    if not ops:
        raise ValueError("ops must be non-empty for apply_ot_update")
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise TypeError(f"ops[{i}] must be a dict")
        p = op.get("p")
        if not isinstance(p, int) or p < 0:
            raise ValueError(f"ops[{i}].p must be a non-negative int, got {p!r}")
        has_i = isinstance(op.get("i"), str)
        has_d = isinstance(op.get("d"), str)
        if has_i == has_d:
            raise ValueError(
                f"ops[{i}] must have exactly one of 'i' (str) or 'd' (str)"
            )


def _open_ot_session(api: Any, project_id: str) -> OtSession:
    """Reuse `Api._open_socket` to build an OT session.

    Heartbeat interval defaults to the Socket.IO 0.9 baseline of 60s; the
    existing `_open_socket` does not surface the handshake `hb` field, and
    overriding it requires only that we send heartbeats often enough.
    """
    ws = api._open_socket(project_id)  # noqa: SLF001
    session = OtSession(ws, heartbeat_interval=60.0)
    session.start()
    try:
        session.await_join_project()
    except Exception:
        session.close()
        raise
    return session


def _find_in_folder(
    folder: ProjectFolder, path: str
) -> tuple[ProjectFile | None, ProjectFolder | None]:
    """Resolve `path` (slash-separated) inside a `ProjectFolder` tree.

    Returns (file, parent_folder). Either may be None if not found.
    """
    parts = [p for p in path.replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        return None, folder
    current: ProjectFolder = folder
    for idx, part in enumerate(parts):
        is_last = idx == len(parts) - 1
        match = None
        for child in current.children:
            if child.name == part:
                match = child
                break
        if match is None:
            return None, current
        if is_last:
            if isinstance(match, ProjectFolder):
                return None, match
            return match, current
        if not isinstance(match, ProjectFolder):
            return None, current
        current = match
    return None, current


def apply_ot_update(
    api: Any,
    project_id: str,
    doc_id: str,
    ops: list[dict[str, Any]],
    version: int,
    *,
    track_changes: bool = False,
    user_id: str = "",
    timeout: float = 15.0,
) -> int:
    """Submit caller-built ops via the OT path. Returns the new version."""
    _validate_ops(ops)
    if not isinstance(version, int) or version < 0:
        raise ValueError(f"version must be a non-negative int, got {version!r}")

    session = _open_ot_session(api, project_id)
    try:
        # joinDoc registers our membership so the document-updater routes
        # the sender echo back to us. We discard the text/version here; the
        # caller supplied the baseline they want to submit against.
        session.join_doc(doc_id, timeout=timeout)
        new_v = session.apply_ot_update_and_wait(
            doc_id,
            ops,
            version,
            track_changes=track_changes,
            user_id=user_id,
            timeout=timeout,
        )
        with contextlib.suppress(Exception):
            session.leave_doc(doc_id)
        return new_v
    finally:
        session.close()


def write_doc(
    api: Any,
    project_id: str,
    file_path: str,
    new_content: str,
    *,
    track_changes: bool = False,
    raise_on_silent_noop: bool = True,
    dry_run: bool = False,
    timeout: float = 15.0,
) -> WriteResult | DryRunResult:
    """Resolve `file_path`, diff against the live doc, submit, verify.

    When `dry_run=True`, the live doc is joined to read its current text
    and version, the diff is computed, and a `DryRunResult` is returned
    without issuing `applyOtUpdate`.
    """
    if not isinstance(new_content, str):
        raise TypeError("new_content must be a str (UTF-8 text)")

    root = api.project_get_files(project_id)
    target, _parent = _find_in_folder(root, file_path)
    if target is None:
        raise FileNotFoundError(f"no such file in project: {file_path!r}")
    if target.type != "doc":
        raise OtError(
            f"OT writes require a doc file; {file_path!r} has type {target.type!r}"
        )

    session = _open_ot_session(api, project_id)
    try:
        pre_text, baseline_version = session.join_doc(target.id, timeout=timeout)
        ops = text_to_ops(pre_text, new_content)
        if dry_run:
            with contextlib.suppress(Exception):
                session.leave_doc(target.id)
            return DryRunResult(
                baseline_version=baseline_version,
                ops=list(ops),
                affects_lines=_affected_lines(pre_text, ops),
            )
        if not ops:
            with contextlib.suppress(Exception):
                session.leave_doc(target.id)
            return WriteResult(
                old_version=baseline_version,
                new_version=baseline_version,
                silent_no_op=False,
            )
        new_version = session.apply_ot_update_and_wait(
            target.id,
            ops,
            baseline_version,
            track_changes=track_changes,
            timeout=timeout,
        )
        with contextlib.suppress(Exception):
            session.leave_doc(target.id)
    finally:
        session.close()

    server_text = api._pull_doc_project_file_content(project_id, target.id)  # noqa: SLF001
    silent = bool(ops) and server_text == pre_text
    if silent and raise_on_silent_noop:
        raise SilentNoOpError(
            f"submit on {file_path!r} produced no observable change "
            f"(baseline v={baseline_version}, ack v={new_version})"
        )
    return WriteResult(
        old_version=baseline_version,
        new_version=new_version,
        silent_no_op=silent,
    )


def find_and_replace(  # noqa: PLR0913 - documented public API; arg shape is the contract
    api: Any,
    project_id: str,
    file_path: str,
    find: str,
    replace: str,
    *,
    count: int | None = None,
    expect_unique: bool = True,
    track_changes: bool = False,
    dry_run: bool = False,
    timeout: float = 15.0,
) -> FindReplaceResult | DryRunResult:
    """Literal find-and-replace on a doc via the OT path.

    Pulls the current doc text, replaces matches of the literal `find`
    string with `replace`, and submits the result through `write_doc`.
    Collab-safety is inherited from `write_doc`.

    Safety: by default (`expect_unique=True`, `count=None`), more than
    one match raises `MultipleMatchesError` to prevent accidental bulk
    edits when the caller meant to fix a single occurrence. Either pass
    `count=N` to take the first N matches explicitly, or
    `expect_unique=False` to opt into replace-all.

    Returns FindReplaceResult(replacements, old_version, new_version).
    When no occurrences are found, no socket session is opened and both
    versions are None.
    """
    if not isinstance(find, str) or not isinstance(replace, str):
        raise TypeError("find and replace must both be str")
    if not find:
        raise ValueError("find must be non-empty")
    if count is not None and (not isinstance(count, int) or count < 0):
        raise ValueError(f"count must be a non-negative int or None, got {count!r}")

    root = api.project_get_files(project_id)
    target, _parent = _find_in_folder(root, file_path)
    if target is None:
        raise FileNotFoundError(f"no such file in project: {file_path!r}")
    if target.type != "doc":
        raise OtError(
            f"OT writes require a doc file; {file_path!r} has type {target.type!r}"
        )

    pre_text = api._pull_doc_project_file_content(project_id, target.id)  # noqa: SLF001
    occurrences = pre_text.count(find)
    if occurrences == 0:
        if dry_run:
            return DryRunResult(
                baseline_version=0, ops=[], affects_lines=[], replacements=0
            )
        return FindReplaceResult(replacements=0, old_version=None, new_version=None)
    if expect_unique and count is None and occurrences > 1:
        raise MultipleMatchesError(find, occurrences)
    if count is None or count >= occurrences:
        new_text = pre_text.replace(find, replace)
        replacements = occurrences
    else:
        new_text = pre_text.replace(find, replace, count)
        replacements = count

    result = write_doc(
        api,
        project_id,
        file_path,
        new_text,
        track_changes=track_changes,
        raise_on_silent_noop=True,
        dry_run=dry_run,
        timeout=timeout,
    )
    if dry_run:
        assert isinstance(result, DryRunResult)
        return DryRunResult(
            baseline_version=result.baseline_version,
            ops=result.ops,
            affects_lines=result.affects_lines,
            replacements=replacements,
        )
    assert isinstance(result, WriteResult)
    return FindReplaceResult(
        replacements=replacements,
        old_version=result.old_version,
        new_version=result.new_version,
    )


def _api_apply_ot_update(
    self: Api,
    project_id: str,
    doc_id: str,
    ops: list[dict[str, Any]],
    version: int,
    *,
    track_changes: bool = False,
    user_id: str = "",
    timeout: float = 15.0,
) -> int:
    return apply_ot_update(
        self,
        project_id,
        doc_id,
        ops,
        version,
        track_changes=track_changes,
        user_id=user_id,
        timeout=timeout,
    )


def _api_write_doc(
    self: Api,
    project_id: str,
    file_path: str,
    new_content: str,
    *,
    track_changes: bool = False,
    raise_on_silent_noop: bool = True,
    dry_run: bool = False,
    timeout: float = 15.0,
) -> WriteResult | DryRunResult:
    return write_doc(
        self,
        project_id,
        file_path,
        new_content,
        track_changes=track_changes,
        raise_on_silent_noop=raise_on_silent_noop,
        dry_run=dry_run,
        timeout=timeout,
    )


def _api_find_and_replace(  # noqa: PLR0913 - mirrors find_and_replace public API
    self: Api,
    project_id: str,
    file_path: str,
    find: str,
    replace: str,
    *,
    count: int | None = None,
    expect_unique: bool = True,
    track_changes: bool = False,
    dry_run: bool = False,
    timeout: float = 15.0,
) -> FindReplaceResult | DryRunResult:
    return find_and_replace(
        self,
        project_id,
        file_path,
        find,
        replace,
        count=count,
        expect_unique=expect_unique,
        track_changes=track_changes,
        dry_run=dry_run,
        timeout=timeout,
    )


Api.apply_ot_update = _api_apply_ot_update
Api.write_doc = _api_write_doc
Api.find_and_replace = _api_find_and_replace
