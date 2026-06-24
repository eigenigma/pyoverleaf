"""Project snapshot: augment a download-project zip with per-doc metadata.

`build_snapshot` is the orchestrator the CLI calls. `enrich_zip` and
`walk_docs` are pure helpers exposed for unit testing.

Manifest layout (a single file `.pyoverleaf/manifest.json` added inside
the zip):

    {
      "main.tex": {"doc_id": "abc...", "version": 12, "ranges": {...}},
      "chapters/intro.tex": {...},
      ...
    }
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import TYPE_CHECKING, Any

from ._models import ProjectFile, ProjectFolder

if TYPE_CHECKING:
    from collections.abc import Iterator

MANIFEST_PATH = ".pyoverleaf/manifest.json"


def walk_docs(
    folder: ProjectFolder, prefix: str = ""
) -> Iterator[tuple[str, ProjectFile]]:
    """Yield `(full_path, ProjectFile)` for every doc-type file in the tree.

    Subfolders are recursed; binary files (`type != "doc"`) are skipped.
    Folder names are joined with `/`. The root folder's own name is NOT
    part of the prefix - paths are relative to project root.
    """
    for child in folder.children:
        if isinstance(child, ProjectFolder):
            sub_prefix = f"{prefix}{child.name}/" if prefix else f"{child.name}/"
            yield from walk_docs(child, sub_prefix)
        elif getattr(child, "type", "file") == "doc":
            yield f"{prefix}{child.name}", child


def enrich_zip(orig_zip_bytes: bytes, manifest: dict[str, Any]) -> bytes:
    """Add `.pyoverleaf/manifest.json` to a copy of `orig_zip_bytes`.

    Returns a new zip that contains every entry of `orig_zip_bytes` plus
    `.pyoverleaf/manifest.json` carrying `manifest` as JSON. Original
    entries are copied byte-for-byte (same CRC, same compression). If
    `MANIFEST_PATH` is already present in the original zip it's replaced.
    """
    out_buf = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(orig_zip_bytes), "r") as src,
        zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for info in src.infolist():
            if info.filename == MANIFEST_PATH:
                continue
            dst.writestr(info, src.read(info.filename))
        dst.writestr(
            MANIFEST_PATH,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        )
    return out_buf.getvalue()


def build_snapshot(api: Any, project_id: str) -> bytes:
    """Download `project_id` as a zip, then enrich with the manifest.

    For each doc in the project tree, this does one joinDoc socket round
    trip to collect `(version, ranges)`. Serialized — O(N docs) wall
    clock. Acceptable for v1; a batched mode is future work.
    """
    root = api.project_get_files(project_id)
    manifest: dict[str, dict[str, Any]] = {}
    for path, doc in walk_docs(root):
        _text, version, ranges = api._pull_doc_snapshot(project_id, doc.id)  # noqa: SLF001
        manifest[path] = {
            "doc_id": doc.id,
            "version": version,
            "ranges": ranges,
        }
    orig_zip = api.download_project(project_id)
    return enrich_zip(orig_zip, manifest)
