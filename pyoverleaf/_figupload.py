r"""Upload a binary asset to a project and emit a LaTeX include snippet.

`fig_upload` resolves (and optionally creates) the remote folder path,
streams local file bytes through `Api.project_upload_file`, and returns
the resolved remote path. The CLI prints either the bare path or a
`\includegraphics` fragment ready to pipe.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._models import ProjectFile, ProjectFolder


def _resolve_or_create_folder(
    api: Any,
    project_id: str,
    parts: list[str],
    *,
    create_parents: bool,
) -> str:
    """Walk to the folder identified by `parts`; return its folder_id.

    When `create_parents=True`, any missing intermediate folders are
    created (`mkdir -p`-style). When False, missing folders raise
    `FileNotFoundError` carrying the path that failed to resolve.
    """
    root: ProjectFolder = api.project_get_files(project_id)
    current = root
    walked: list[str] = []
    for part in parts:
        walked.append(part)
        match = None
        for child in current.children:
            if child.name == part and getattr(child, "type", None) == "folder":
                match = child
                break
        if match is None:
            if not create_parents:
                raise FileNotFoundError(
                    f"remote folder does not exist: {'/'.join(walked)}"
                )
            match = api.project_create_folder(project_id, current.id, part)
        current = match
    return current.id


def fig_upload(
    api: Any,
    project_id: str,
    local_path: str,
    *,
    remote_path: str | None = None,
    create_parents: bool = True,
) -> tuple[str, ProjectFile]:
    """Upload `local_path` to `remote_path` inside `project_id`.

    `remote_path` defaults to `figures/<basename>`. Returns the resolved
    remote path (slash-joined, no leading slash) and the
    `ProjectFile` the server returned.
    """
    local = Path(local_path)
    if not local.is_file():
        raise FileNotFoundError(f"local file does not exist: {local_path}")

    if remote_path is None:
        remote_path = f"figures/{local.name}"
    remote_path = remote_path.replace("\\", "/").lstrip("/")
    parts = [p for p in remote_path.split("/") if p]
    if not parts:
        raise ValueError("remote_path must include a filename")
    *folder_parts, filename = parts

    folder_id = _resolve_or_create_folder(
        api, project_id, folder_parts, create_parents=create_parents
    )
    data = local.read_bytes()
    uploaded = api.project_upload_file(project_id, folder_id, filename, data)
    return "/".join(parts), uploaded


def includegraphics_snippet(remote_path: str, *, width: str) -> str:
    r"""Return a `\includegraphics[width=...]{path}` fragment.

    `width` is included verbatim - callers pick `\linewidth`,
    `0.5\linewidth`, `3cm`, etc.
    """
    return f"\\includegraphics[width={width}]{{{remote_path}}}"
