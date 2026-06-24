"""Unit tests for pyoverleaf._figupload + the fig-upload CLI command."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from pyoverleaf import Api
from pyoverleaf.__main__ import main as cli_main
from pyoverleaf._figupload import (
    _resolve_or_create_folder,
    fig_upload,
    includegraphics_snippet,
)
from pyoverleaf._models import Project, ProjectFile, ProjectFolder


def _folder(id_: str, name: str, children=()) -> ProjectFolder:
    f = ProjectFolder(id=id_, name=name)
    f.children = list(children)
    return f


def _file(id_: str, name: str) -> ProjectFile:
    return ProjectFile(id=id_, name=name, created=None)


# ---- includegraphics_snippet ------------------------------------------------


def test_includegraphics_default_width():
    r"""Default width emits a `\linewidth` includegraphics snippet."""
    assert (
        includegraphics_snippet("figures/x.pdf", width="\\linewidth")
        == "\\includegraphics[width=\\linewidth]{figures/x.pdf}"
    )


def test_includegraphics_custom_width():
    """A caller-supplied width string is inserted verbatim."""
    assert (
        includegraphics_snippet("a.png", width="0.5\\linewidth")
        == "\\includegraphics[width=0.5\\linewidth]{a.png}"
    )


# ---- _resolve_or_create_folder ----------------------------------------------


def test_resolve_existing_folder(monkeypatch: pytest.MonkeyPatch):
    """Resolving an existing folder returns its id without creating anything."""
    figures = _folder("fig-id", "figures")
    root = _folder("root", "rootFolder", [figures])
    api = Api()
    api._session_initialized = True
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: root, raising=True)
    assert (
        _resolve_or_create_folder(api, "p", ["figures"], create_parents=False)
        == "fig-id"
    )


def test_resolve_missing_folder_no_create_raises(monkeypatch: pytest.MonkeyPatch):
    """Without create_parents, a missing folder must raise FileNotFoundError."""
    root = _folder("root", "rootFolder", [])
    api = Api()
    api._session_initialized = True
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: root, raising=True)
    with pytest.raises(FileNotFoundError):
        _resolve_or_create_folder(api, "p", ["missing"], create_parents=False)


def test_resolve_creates_parents(monkeypatch: pytest.MonkeyPatch):
    """With create_parents, missing folders are created and the new id is returned."""
    root = _folder("root", "rootFolder", [])
    created: list[tuple[str, str, str]] = []

    api = Api()
    api._session_initialized = True
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: root, raising=True)

    def _create(self, project_id, parent_id, name):
        created.append((project_id, parent_id, name))
        new = _folder(f"new-{name}", name)
        # Mirror server side-effect: append to parent so subsequent walks find it.
        if parent_id == "root":
            root.children.append(new)
        return new

    monkeypatch.setattr(Api, "project_create_folder", _create, raising=True)
    folder_id = _resolve_or_create_folder(api, "p", ["nested-a"], create_parents=True)
    assert folder_id == "new-nested-a"
    assert created == [("p", "root", "nested-a")]


# ---- fig_upload --------------------------------------------------------------


def test_fig_upload_default_remote_path(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Default remote path puts the file under `figures/<basename>` of the project."""
    figures = _folder("fig-id", "figures")
    root = _folder("root", "rootFolder", [figures])
    local = tmp_path / "img.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\n")
    api = Api()
    api._session_initialized = True
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: root, raising=True)

    captured: list[dict] = []

    def _upload(self, project_id, folder_id, file_name, file_content):
        captured.append(
            {
                "project_id": project_id,
                "folder_id": folder_id,
                "file_name": file_name,
                "size": len(file_content),
            }
        )
        return _file("new-img", file_name)

    monkeypatch.setattr(Api, "project_upload_file", _upload, raising=True)

    resolved, uploaded = fig_upload(api, "p", str(local))
    assert resolved == "figures/img.png"
    assert uploaded.id == "new-img"
    assert captured == [
        {
            "project_id": "p",
            "folder_id": "fig-id",
            "file_name": "img.png",
            "size": 8,
        }
    ]


def test_fig_upload_explicit_remote_path_nested(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """An explicit nested remote_path creates parents and uploads to that path."""
    root = _folder("root", "rootFolder", [])
    local = tmp_path / "graph.pdf"
    local.write_bytes(b"%PDF-1.4")
    api = Api()
    api._session_initialized = True
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: root, raising=True)
    monkeypatch.setattr(
        Api,
        "project_create_folder",
        lambda self, pid, parent, name: _folder(f"new-{name}", name),
        raising=True,
    )
    monkeypatch.setattr(
        Api,
        "project_upload_file",
        lambda self, pid, fid, fname, data: _file("uploaded", fname),
        raising=True,
    )
    resolved, _ = fig_upload(
        api, "p", str(local), remote_path="figures/results/graph.pdf"
    )
    assert resolved == "figures/results/graph.pdf"


def test_fig_upload_rejects_missing_local_file():
    """A nonexistent local path must raise FileNotFoundError before any upload."""
    api = Api()
    api._session_initialized = True
    with pytest.raises(FileNotFoundError):
        fig_upload(api, "p", "/nonexistent/path/img.png")


def test_fig_upload_rejects_bare_remote_path(tmp_path):
    """A remote_path with no filename component must be rejected."""
    api = Api()
    api._session_initialized = True
    local = tmp_path / "x.png"
    local.write_bytes(b"data")
    with pytest.raises(ValueError, match="remote_path must include a filename"):
        fig_upload(api, "p", str(local), remote_path="/")


# ---- CLI ---------------------------------------------------------------------


def _stub_login_and_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Api, "login_from_browser", lambda self: None, raising=True)
    proj = Project(
        id="p1",
        name="proj",
        last_updated="",
        access_level="owner",
        source="owner",
        archived=False,
        trashed=False,
    )
    monkeypatch.setattr(Api, "get_projects", lambda self, **kw: [proj], raising=True)


def test_cli_fig_upload_help():
    """`fig-upload --help` must surface the `--bare` and `--width` flags."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["fig-upload", "--help"])
    assert r.exit_code == 0
    assert "--bare" in r.output
    assert "--width" in r.output


def test_cli_fig_upload_default_emits_includegraphics(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    r"""Default CLI invocation prints an `\includegraphics` snippet."""
    _stub_login_and_projects(monkeypatch)
    local = tmp_path / "small.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\n")
    captured: list[dict] = []

    def _stub(api, project_id, local_path, *, remote_path=None, create_parents=True):
        captured.append(
            {
                "project_id": project_id,
                "local_path": local_path,
                "remote_path": remote_path,
                "create_parents": create_parents,
            }
        )
        return ("figures/small.png", _file("new-id", "small.png"))

    monkeypatch.setattr("pyoverleaf._figupload.fig_upload", _stub, raising=True)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["fig-upload", "proj", str(local)])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "\\includegraphics[width=\\linewidth]{figures/small.png}"
    assert captured == [
        {
            "project_id": "p1",
            "local_path": str(local),
            "remote_path": None,
            "create_parents": True,
        }
    ]


def test_cli_fig_upload_bare(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """`--bare` prints just the resolved remote path, no LaTeX wrapper."""
    _stub_login_and_projects(monkeypatch)
    local = tmp_path / "small.png"
    local.write_bytes(b"data")
    monkeypatch.setattr(
        "pyoverleaf._figupload.fig_upload",
        lambda *a, **kw: ("figures/small.png", _file("id", "small.png")),
        raising=True,
    )
    runner = CliRunner()
    r = runner.invoke(cli_main, ["fig-upload", "--bare", "proj", str(local)])
    assert r.exit_code == 0
    assert r.output.strip() == "figures/small.png"


def test_cli_fig_upload_custom_width(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """`--width` propagates verbatim into the emitted includegraphics snippet."""
    _stub_login_and_projects(monkeypatch)
    local = tmp_path / "x.pdf"
    local.write_bytes(b"%PDF")
    monkeypatch.setattr(
        "pyoverleaf._figupload.fig_upload",
        lambda *a, **kw: ("figures/x.pdf", _file("id", "x.pdf")),
        raising=True,
    )
    runner = CliRunner()
    r = runner.invoke(
        cli_main, ["fig-upload", "--width", "0.5\\linewidth", "proj", str(local)]
    )
    assert r.exit_code == 0
    assert r.output.strip() == "\\includegraphics[width=0.5\\linewidth]{figures/x.pdf}"


def test_cli_fig_upload_missing_local_file_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """A nonexistent local file must surface as a nonzero CLI exit code."""
    _stub_login_and_projects(monkeypatch)
    runner = CliRunner()
    r = runner.invoke(cli_main, ["fig-upload", "proj", "/no/such/file.png"])
    assert r.exit_code != 0
