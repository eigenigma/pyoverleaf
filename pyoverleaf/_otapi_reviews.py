"""Tracked-changes operations: list / accept (HTTP) and reject (client OT).

- `list` walks `GET /project/{pid}/ranges` and shapes entries into
  `TrackedChange` objects, with `doc_path` enrichment.
- `accept` groups change_ids by doc and POSTs
  `/project/{pid}/doc/{did}/changes/accept`.
- `reject` cannot use a server endpoint - it builds inverse OT ops with
  `u:true` and submits them over the same socket channel that `write_doc`
  uses, with NO `meta.tc` (we are un-tracking, not creating new tracked
  changes).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from ._models import TrackedChange
from ._review_http import doc_paths_by_id, get_json, post_json

if TYPE_CHECKING:
    from collections.abc import Iterable


def _tracked_change_from(
    doc_id: str, doc_path: str | None, raw: dict
) -> TrackedChange | None:
    """Convert one raw `ranges.changes[]` entry to a `TrackedChange`.

    Returns None if the entry has neither `i` nor `d` (malformed).
    """
    op = raw.get("op") or {}
    if "i" in op:
        kind = "insert"
        text = str(op.get("i") or "")
    elif "d" in op:
        kind = "delete"
        text = str(op.get("d") or "")
    else:
        return None
    metadata = raw.get("metadata") or {}
    return TrackedChange(
        change_id=str(raw.get("id") or ""),
        doc_id=doc_id,
        doc_path=doc_path,
        kind=kind,
        position=int(op.get("p", 0)),
        text=text,
        user_id=metadata.get("user_id"),
        timestamp=metadata.get("ts"),
    )


def list_tracked_changes(
    api: Any,
    project_id: str,
    *,
    doc_path_filter: str | None = None,
) -> list[TrackedChange]:
    """Enumerate tracked-change suggestions across every doc in the project."""
    root = api.project_get_files(project_id)
    doc_paths = doc_paths_by_id(root)

    ranges = get_json(api, f"{project_id}/ranges") or []
    out: list[TrackedChange] = []
    for entry in ranges:
        doc_id = entry.get("id")
        if not doc_id:
            continue
        for raw in (entry.get("ranges") or {}).get("changes") or []:
            tc = _tracked_change_from(doc_id, doc_paths.get(doc_id), raw)
            if tc is not None:
                out.append(tc)

    if doc_path_filter:
        needle = doc_path_filter.lower()
        out = [c for c in out if (c.doc_path or "").lower().find(needle) >= 0]
    return out


def _index_changes_by_id(
    ranges_entries: Iterable[dict],
) -> dict[str, tuple[str, dict]]:
    """Build `{change_id: (doc_id, raw_change)}` over all docs."""
    out: dict[str, tuple[str, dict]] = {}
    for entry in ranges_entries:
        doc_id = entry.get("id")
        if not doc_id:
            continue
        for raw in (entry.get("ranges") or {}).get("changes") or []:
            cid = raw.get("id")
            if cid:
                out[cid] = (doc_id, raw)
    return out


def accept_tracked_changes(api: Any, project_id: str, change_ids: list[str]) -> dict:
    """Accept one or more tracked changes.

    Resolves each `change_id` to its `doc_id` via `GET /ranges`, groups by
    doc, and sends `POST /project/{pid}/doc/{did}/changes/accept` per
    affected doc. Returns a summary with the per-doc counts plus any
    `unknown` change_ids that were not found in the live ranges.
    """
    if not change_ids:
        return {"accepted": 0, "docs": [], "unknown": []}

    ranges = get_json(api, f"{project_id}/ranges") or []
    index = _index_changes_by_id(ranges)

    grouped: dict[str, list[str]] = {}
    unknown: list[str] = []
    for cid in change_ids:
        hit = index.get(cid)
        if hit is None:
            unknown.append(cid)
            continue
        grouped.setdefault(hit[0], []).append(cid)

    docs_summary: list[dict] = []
    for doc_id, ids in grouped.items():
        post_json(api, project_id, f"doc/{doc_id}/changes/accept", {"change_ids": ids})
        docs_summary.append({"doc_id": doc_id, "count": len(ids)})

    return {
        "accepted": sum(d["count"] for d in docs_summary),
        "docs": docs_summary,
        "unknown": unknown,
    }


def _inverse_op_for(raw: dict) -> dict | None:
    """Inverse OT op for a tracked-change entry, with the `u:true` flag.

    A tracked-insert (`{p, i}`) inverts to `{p, d:text, u:true}` and a
    tracked-delete (`{p, d}`) inverts to `{p, i:text, u:true}`. The
    `u:true` flag tells the server's RangesTracker to clear the existing
    tracked-change entry instead of creating a new one.
    """
    op = raw.get("op") or {}
    if "i" in op:
        return {"p": int(op.get("p", 0)), "d": str(op.get("i") or ""), "u": True}
    if "d" in op:
        return {"p": int(op.get("p", 0)), "i": str(op.get("d") or ""), "u": True}
    return None


def reject_tracked_changes(api: Any, project_id: str, change_ids: list[str]) -> dict:
    """Reject one or more tracked changes via client-side inverse OT.

    For each affected doc: opens an OT session, joins the doc to learn
    the live version, builds inverse ops with `u:true`, sorts them by
    position **descending** so earlier ops don't shift later positions,
    and submits one `applyOtUpdate`. `meta.tc` is NOT set - we are
    un-tracking, not creating new tracked changes.
    """
    if not change_ids:
        return {"rejected": 0, "docs": [], "missing": []}

    from ._otapi import _open_ot_session

    ranges = get_json(api, f"{project_id}/ranges") or []
    index = _index_changes_by_id(ranges)

    grouped: dict[str, list[dict]] = {}
    missing: list[str] = []
    for cid in change_ids:
        hit = index.get(cid)
        if hit is None:
            missing.append(cid)
            continue
        grouped.setdefault(hit[0], []).append(hit[1])

    docs_summary: list[dict] = []
    for doc_id, raw_changes in grouped.items():
        inverses: list[dict] = []
        for raw in raw_changes:
            inv = _inverse_op_for(raw)
            if inv is not None:
                inverses.append(inv)
        inverses.sort(key=lambda op: int(op["p"]), reverse=True)

        session = _open_ot_session(api, project_id)
        try:
            _text, baseline_version = session.join_doc(doc_id, timeout=15.0)
            session.apply_ot_update_and_wait(
                doc_id, inverses, baseline_version, timeout=15.0
            )
            with contextlib.suppress(Exception):
                session.leave_doc(doc_id)
        finally:
            session.close()
        docs_summary.append({"doc_id": doc_id, "count": len(inverses)})

    return {
        "rejected": sum(d["count"] for d in docs_summary),
        "docs": docs_summary,
        "missing": missing,
    }


def _api_list_tracked_changes(
    self: Any,
    project_id: str,
    *,
    doc_path_filter: str | None = None,
) -> list[TrackedChange]:
    return list_tracked_changes(self, project_id, doc_path_filter=doc_path_filter)


def _api_accept_tracked_changes(
    self: Any, project_id: str, change_ids: list[str]
) -> dict:
    return accept_tracked_changes(self, project_id, change_ids)


def _api_reject_tracked_changes(
    self: Any, project_id: str, change_ids: list[str]
) -> dict:
    return reject_tracked_changes(self, project_id, change_ids)


def attach(api_cls: Any) -> None:
    """Attach tracked-changes methods AND comments methods to the Api class.

    Comments live in `_comments` now; we route the attachment through
    here so `__init__` keeps the single `_otapi_reviews.attach(Api)`
    side-effect import.
    """
    from . import _comments

    _comments.attach(api_cls)
    api_cls.list_tracked_changes = _api_list_tracked_changes
    api_cls.accept_tracked_changes = _api_accept_tracked_changes
    api_cls.reject_tracked_changes = _api_reject_tracked_changes


__all__ = [
    "accept_tracked_changes",
    "attach",
    "list_tracked_changes",
    "reject_tracked_changes",
]
