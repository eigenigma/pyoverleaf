"""Shared HTTP helpers + doc-tree walk for review-panel modules.

Used by `_comments` and `_otapi_reviews` (tracked changes). Keeps the
`/project/{pid}/...` JSON GET/POST surface in one place so the comments
and changes modules only encode their route/payload shapes.

The `api._*` accesses below are intentional: this module is an internal
collaborator of the `Api` class, which is deliberately split across
sibling modules to stay under the per-file size budget. The `# noqa:
SLF001` markers acknowledge that the `_`-prefix here means "not for
external callers", not "not for sibling modules".
"""

from __future__ import annotations

import json
from typing import Any

from ._models import ProjectFolder


def doc_paths_by_id(root: ProjectFolder) -> dict[str, str]:
    """Walk a folder tree, build `{doc_id: slash-path}`.

    Both comments and tracked-changes resolve `doc_id` to a human path
    for surfacing in CLI output.
    """
    out: dict[str, str] = {}

    def walk(folder: ProjectFolder, prefix: str) -> None:
        for child in folder.children:
            if isinstance(child, ProjectFolder):
                sub_prefix = f"{prefix}{child.name}/" if prefix else f"{child.name}/"
                walk(child, sub_prefix)
            elif getattr(child, "type", "file") == "doc":
                out[child.id] = f"{prefix}{child.name}"

    walk(root, "")
    return out


def get_json(api: Any, path: str) -> Any:
    """GET `https://{host}/project/{path}` and parse JSON."""
    api._assert_session_initialized()  # noqa: SLF001
    host = api._host  # noqa: SLF001
    r = api._get_session().get(  # noqa: SLF001
        f"https://{host}/project/{path}",
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
        **api._request_kwargs,  # noqa: SLF001
    )
    r.raise_for_status()
    return json.loads(r.content)


def post_json(api: Any, project_id: str, path: str, body: dict) -> Any:
    """POST JSON to `https://{host}/project/{project_id}/{path}`."""
    api._assert_session_initialized()  # noqa: SLF001
    host = api._host  # noqa: SLF001
    r = api._get_session().post(  # noqa: SLF001
        f"https://{host}/project/{project_id}/{path}",
        json=body,
        headers={
            "Referer": f"https://{host}/project/{project_id}",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "x-csrf-token": api._get_csrf_token(project_id),  # noqa: SLF001
        },
        **api._request_kwargs,  # noqa: SLF001
    )
    r.raise_for_status()
    if not r.content:
        return None
    try:
        return json.loads(r.content)
    except json.JSONDecodeError:
        return None
