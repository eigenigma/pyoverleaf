"""Comment-thread operations over Overleaf's HTTP REST surface.

Three routes are involved:
- `GET /project/{pid}/threads` - thread bodies
- `GET /project/{pid}/ranges` - thread anchors (doc_id, position, quoted text)
- `POST /project/{pid}/thread/{tid}/messages` - reply (project-scoped chat store)
- `POST /project/{pid}/doc/{did}/thread/{tid}/resolve|reopen` - doc-scoped
  resolve/reopen (the project-scoped variant 404s).
"""

from __future__ import annotations

from typing import Any

from ._models import CommentMessage, CommentThread
from ._review_http import doc_paths_by_id, get_json, post_json


def _user_display_name(user: dict | None) -> str | None:
    if not isinstance(user, dict):
        return None
    first = user.get("first_name") or ""
    last = user.get("last_name") or ""
    full = f"{first} {last}".strip()
    if full:
        return full
    return user.get("email") or None


def _comment_message_from(raw: dict) -> CommentMessage:
    return CommentMessage(
        id=str(raw.get("id") or raw.get("_id") or ""),
        content=str(raw.get("content") or ""),
        timestamp=int(raw["timestamp"])
        if isinstance(raw.get("timestamp"), (int, float))
        else None,
        user_id=raw.get("user_id") or (raw.get("user") or {}).get("id"),
        user_name=_user_display_name(raw.get("user")),
    )


def list_comments(
    api: Any,
    project_id: str,
    *,
    doc_path_filter: str | None = None,
    include_resolved: bool = False,
) -> list[CommentThread]:
    """Return enriched `CommentThread` objects for every thread in the project.

    Cross-references `GET /threads` (thread bodies) with `GET /ranges`
    (anchor positions). Threads with no surviving anchor in `/ranges`
    are still returned but have `doc_id`/`position`/`quoted_text=None`.
    """
    root = api.project_get_files(project_id)
    doc_paths = doc_paths_by_id(root)

    threads = get_json(api, f"{project_id}/threads") or {}
    ranges = get_json(api, f"{project_id}/ranges") or []

    anchors: dict[str, tuple[str, int, str]] = {}
    for entry in ranges:
        doc_id = entry.get("id")
        for c in (entry.get("ranges") or {}).get("comments") or []:
            op = c.get("op") or {}
            t = op.get("t")
            if t and doc_id:
                anchors[t] = (doc_id, int(op.get("p", 0)), str(op.get("c", "")))

    out: list[CommentThread] = []
    for thread_id, thread in threads.items():
        if not isinstance(thread, dict):
            continue
        anchor = anchors.get(thread_id)
        doc_id = anchor[0] if anchor else None
        position = anchor[1] if anchor else None
        quoted = anchor[2] if anchor else None
        messages = [_comment_message_from(m) for m in (thread.get("messages") or [])]
        out.append(
            CommentThread(
                thread_id=thread_id,
                doc_id=doc_id,
                doc_path=doc_paths.get(doc_id) if doc_id else None,
                quoted_text=quoted,
                position=position,
                resolved=bool(thread.get("resolved")),
                messages=messages,
            )
        )

    if not include_resolved:
        out = [t for t in out if not t.resolved]
    if doc_path_filter:
        needle = doc_path_filter.lower()
        out = [t for t in out if (t.doc_path or "").lower().find(needle) >= 0]
    out.sort(
        key=lambda t: (t.messages[-1].timestamp or 0) if t.messages else 0,
        reverse=True,
    )
    return out


def reply_to_comment(api: Any, project_id: str, thread_id: str, content: str) -> dict:
    """Post a reply message to an existing thread.

    Goes to the project-scoped chat-store route; the doc anchor is not
    needed (overleaf stores thread message history per-thread).
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    post_json(api, project_id, f"thread/{thread_id}/messages", {"content": content})
    return {"thread_id": thread_id, "content_length": len(content)}


def _doc_id_for_thread(api: Any, project_id: str, thread_id: str) -> str:
    """Resolve the doc_id a thread is anchored to by walking `/ranges`.

    `resolve` and `reopen` go through the doc-scoped chat router on the
    Overleaf web server (`/project/{pid}/doc/{did}/thread/{tid}/...`).
    The project-scoped route used by `reply` does NOT serve them and
    returns 404. Looking the doc up from `/ranges` keeps the CLI surface
    `(project_id, thread_id)` shaped while routing correctly underneath.
    """
    ranges = get_json(api, f"{project_id}/ranges") or []
    for entry in ranges:
        doc_id = entry.get("id")
        if not doc_id:
            continue
        for c in (entry.get("ranges") or {}).get("comments") or []:
            if (c.get("op") or {}).get("t") == thread_id:
                return doc_id
    raise ValueError(f"thread {thread_id} has no live anchor in project {project_id}")


def resolve_comment(api: Any, project_id: str, thread_id: str) -> dict:
    doc_id = _doc_id_for_thread(api, project_id, thread_id)
    post_json(api, project_id, f"doc/{doc_id}/thread/{thread_id}/resolve", {})
    return {"thread_id": thread_id, "resolved": True}


def reopen_comment(api: Any, project_id: str, thread_id: str) -> dict:
    doc_id = _doc_id_for_thread(api, project_id, thread_id)
    post_json(api, project_id, f"doc/{doc_id}/thread/{thread_id}/reopen", {})
    return {"thread_id": thread_id, "resolved": False}


def _api_list_comments(
    self: Any,
    project_id: str,
    *,
    doc_path_filter: str | None = None,
    include_resolved: bool = False,
) -> list[CommentThread]:
    return list_comments(
        self,
        project_id,
        doc_path_filter=doc_path_filter,
        include_resolved=include_resolved,
    )


def _api_reply_to_comment(
    self: Any, project_id: str, thread_id: str, content: str
) -> dict:
    return reply_to_comment(self, project_id, thread_id, content)


def _api_resolve_comment(self: Any, project_id: str, thread_id: str) -> dict:
    return resolve_comment(self, project_id, thread_id)


def _api_reopen_comment(self: Any, project_id: str, thread_id: str) -> dict:
    return reopen_comment(self, project_id, thread_id)


def attach(api_cls: Any) -> None:
    api_cls.list_comments = _api_list_comments
    api_cls.reply_to_comment = _api_reply_to_comment
    api_cls.resolve_comment = _api_resolve_comment
    api_cls.reopen_comment = _api_reopen_comment


__all__ = [
    "attach",
    "list_comments",
    "reopen_comment",
    "reply_to_comment",
    "resolve_comment",
]
