"""Unit tests for pyoverleaf._snapshot."""

from __future__ import annotations

import io
import json
import zipfile
from typing import TYPE_CHECKING

from pyoverleaf import Api
from pyoverleaf._models import ProjectFile, ProjectFolder
from pyoverleaf._snapshot import (
    MANIFEST_PATH,
    build_snapshot,
    enrich_zip,
    walk_docs,
)

if TYPE_CHECKING:
    import pytest


def _doc(id_: str, name: str) -> ProjectFile:
    f = ProjectFile(id=id_, name=name, created=None)
    f.type = "doc"
    return f


def _binary(id_: str, name: str) -> ProjectFile:
    return ProjectFile(id=id_, name=name, created=None)


def _folder(name: str, children=()) -> ProjectFolder:
    f = ProjectFolder(id=f"folder-{name}", name=name)
    f.children = list(children)
    return f


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


# ---- enrich_zip ------------------------------------------------------------


def test_enrich_zip_preserves_original_entries():
    """enrich_zip must keep every original entry alongside the new manifest."""
    orig = _make_zip({"main.tex": b"hello", "chapters/intro.tex": b"intro"})
    out = enrich_zip(orig, {"main.tex": {"doc_id": "abc", "version": 1, "ranges": {}}})
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = set(z.namelist())
        assert "main.tex" in names
        assert "chapters/intro.tex" in names
        assert MANIFEST_PATH in names
        assert z.read("main.tex") == b"hello"
        assert z.read("chapters/intro.tex") == b"intro"


def test_enrich_zip_writes_manifest_json():
    """The injected manifest must round-trip through JSON intact."""
    orig = _make_zip({"main.tex": b"hello"})
    manifest = {
        "main.tex": {
            "doc_id": "abc",
            "version": 7,
            "ranges": {"comments": [], "changes": []},
        }
    }
    out = enrich_zip(orig, manifest)
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        loaded = json.loads(z.read(MANIFEST_PATH))
        assert loaded == manifest


def test_enrich_zip_replaces_existing_manifest():
    """A pre-existing manifest entry must be overwritten, not duplicated."""
    orig = _make_zip({"main.tex": b"hello", MANIFEST_PATH: b"stale"})
    out = enrich_zip(orig, {"main.tex": {"doc_id": "d", "version": 1, "ranges": {}}})
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert json.loads(z.read(MANIFEST_PATH)) == {
            "main.tex": {"doc_id": "d", "version": 1, "ranges": {}}
        }


# ---- walk_docs -------------------------------------------------------------


def test_walk_docs_skips_binary_files():
    """walk_docs must yield only doc-type children, not binary file refs."""
    root = _folder("root", [_doc("d1", "main.tex"), _binary("b1", "image.png")])
    found = list(walk_docs(root))
    assert [p for p, _ in found] == ["main.tex"]


def test_walk_docs_recurses_subfolders():
    """walk_docs must descend into nested folders and join names with `/`."""
    root = _folder(
        "root",
        [
            _doc("d1", "main.tex"),
            _folder(
                "chapters",
                [_doc("d2", "intro.tex"), _doc("d3", "methods.tex")],
            ),
            _folder("figures", [_binary("f1", "graph.pdf")]),
        ],
    )
    paths = [p for p, _ in walk_docs(root)]
    assert paths == ["main.tex", "chapters/intro.tex", "chapters/methods.tex"]


def test_walk_docs_handles_nested_empty_folders():
    """Empty folders at any depth must not emit phantom entries."""
    root = _folder("root", [_folder("empty", [_folder("nested", [])])])
    assert list(walk_docs(root)) == []


# ---- build_snapshot --------------------------------------------------------


def test_build_snapshot_with_mocked_api(monkeypatch: pytest.MonkeyPatch):
    """End-to-end: tree walk → snapshot pulls → zip enrichment."""
    api = Api()
    api._session_initialized = True

    root = _folder(
        "root",
        [
            _doc("d1", "main.tex"),
            _folder("chapters", [_doc("d2", "intro.tex")]),
        ],
    )

    fake_ranges_d1 = {"comments": [{"id": "c1"}], "changes": []}
    fake_ranges_d2 = {"comments": [], "changes": [{"id": "ch1"}]}
    snapshot_data = {
        "d1": ("main body", 10, fake_ranges_d1),
        "d2": ("intro body", 5, fake_ranges_d2),
    }

    orig_zip = _make_zip(
        {"main.tex": b"main body", "chapters/intro.tex": b"intro body"}
    )

    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: root, raising=True)
    monkeypatch.setattr(
        Api,
        "_pull_doc_snapshot",
        lambda self, pid, fid: snapshot_data[fid],
        raising=True,
    )
    monkeypatch.setattr(
        Api, "download_project", lambda self, pid: orig_zip, raising=True
    )

    out = build_snapshot(api, "p1")
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = set(z.namelist())
        assert names == {"main.tex", "chapters/intro.tex", MANIFEST_PATH}
        manifest = json.loads(z.read(MANIFEST_PATH))
        assert manifest == {
            "main.tex": {"doc_id": "d1", "version": 10, "ranges": fake_ranges_d1},
            "chapters/intro.tex": {
                "doc_id": "d2",
                "version": 5,
                "ranges": fake_ranges_d2,
            },
        }


def test_build_snapshot_empty_project(monkeypatch: pytest.MonkeyPatch):
    """A project with no docs must still emit a zip carrying an empty manifest."""
    api = Api()
    api._session_initialized = True
    root = _folder("root", [])
    orig_zip = _make_zip({})

    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: root, raising=True)
    monkeypatch.setattr(
        Api,
        "_pull_doc_snapshot",
        lambda self, pid, fid: ("", 0, {}),
        raising=True,
    )
    monkeypatch.setattr(
        Api, "download_project", lambda self, pid: orig_zip, raising=True
    )

    out = build_snapshot(api, "p1")
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert json.loads(z.read(MANIFEST_PATH)) == {}
