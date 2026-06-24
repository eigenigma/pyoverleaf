"""CLI smoke tests for `pyoverleaf` (write + patch).

Uses `click.testing.CliRunner`. `Api` calls are stubbed at the module
boundary so we don't touch the network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from click.testing import CliRunner

from pyoverleaf import (
    Api,
    FindReplaceResult,
    MultipleMatchesError,
    SilentNoOpError,
    WriteResult,
)
from pyoverleaf.__main__ import main as cli_main
from pyoverleaf._models import Project, ProjectFile, ProjectFolder

if TYPE_CHECKING:
    import pytest


def _stub_login_and_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `login_from_browser` and `get_projects` to return a single fake project."""
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


def test_patch_help_lists_command():
    """`--help` lists both the `patch` and `write` subcommands."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["--help"])
    assert r.exit_code == 0
    assert "patch" in r.output
    assert "write" in r.output


def test_patch_subcommand_help():
    """`patch --help` advertises OT semantics and the `--track`/`--no-track` flags."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["patch", "--help"])
    assert r.exit_code == 0
    assert "OT" in r.output or "collab" in r.output.lower()
    assert "--track" in r.output
    assert "--no-track" in r.output


def test_patch_success(monkeypatch: pytest.MonkeyPatch):
    """`patch` calls `write_doc` with tracking-on default, prints v-old -> v-new."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub_write(
        self,
        project_id,
        file_path,
        new_content,
        *,
        track_changes=False,
        raise_on_silent_noop=True,
        dry_run=False,
        timeout=15.0,
    ):
        captured.append(
            {
                "project_id": project_id,
                "file_path": file_path,
                "new_content": new_content,
                "track_changes": track_changes,
                "raise_on_silent_noop": raise_on_silent_noop,
            }
        )
        return WriteResult(old_version=4, new_version=5, silent_no_op=False)

    monkeypatch.setattr(Api, "write_doc", _stub_write, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["patch", "proj/main.tex"], input="hello world\n")
    assert r.exit_code == 0, r.output
    assert "v4 -> v5" in (r.stderr or r.output)
    assert captured == [
        {
            "project_id": "p1",
            "file_path": "main.tex",
            "new_content": "hello world\n",
            "track_changes": True,
            "raise_on_silent_noop": True,
        }
    ]


def test_patch_no_track_flag(monkeypatch: pytest.MonkeyPatch):
    """`--no-track` flips `track_changes` to False when calling `write_doc`."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub_write(
        self,
        project_id,
        file_path,
        new_content,
        *,
        track_changes=False,
        raise_on_silent_noop=True,
        dry_run=False,
        timeout=15.0,
    ):
        captured.append({"track_changes": track_changes})
        return WriteResult(old_version=1, new_version=2, silent_no_op=False)

    monkeypatch.setattr(Api, "write_doc", _stub_write, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["patch", "--no-track", "proj/main.tex"], input="x")
    assert r.exit_code == 0, r.output
    assert captured == [{"track_changes": False}]


def test_patch_silent_noop_exits_nonzero(monkeypatch: pytest.MonkeyPatch):
    """`patch` exits 2 and reports `silent no-op` when `write_doc` raises it."""
    _stub_login_and_projects(monkeypatch)

    def _stub_write(
        self,
        project_id,
        file_path,
        new_content,
        *,
        track_changes=False,
        raise_on_silent_noop=True,
        dry_run=False,
        timeout=15.0,
    ):
        raise SilentNoOpError("op was nullified")

    monkeypatch.setattr(Api, "write_doc", _stub_write, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["patch", "proj/main.tex"], input="x")
    assert r.exit_code == 2, r.output
    # The error message is on stderr; CliRunner captures both into r.output
    assert "silent no-op" in (r.stderr or r.output)


def test_write_still_uses_upload_path(monkeypatch: pytest.MonkeyPatch):
    """Regression: legacy `write` must not start using OT."""
    _stub_login_and_projects(monkeypatch)
    root = ProjectFolder(id="root", name="rootFolder")
    doc = ProjectFile(id="doc-1", name="main.tex", created=None)
    doc.type = "doc"
    root.children = [doc]
    monkeypatch.setattr(Api, "project_get_files", lambda self, pid: root, raising=True)

    upload_calls: list[dict] = []

    def _upload(self, project_id, folder_id, file_name, file_content):
        upload_calls.append(
            {
                "project_id": project_id,
                "folder_id": folder_id,
                "file_name": file_name,
                "size": len(file_content),
            }
        )
        return ProjectFile(id="doc-1", name=file_name, created=None)

    monkeypatch.setattr(Api, "project_upload_file", _upload, raising=True)

    write_doc_calls: list[dict] = []

    def _stub_write(self, *args: Any, **kwargs: Any):
        write_doc_calls.append({"args": args, "kwargs": kwargs})
        return WriteResult(old_version=0, new_version=0, silent_no_op=False)

    monkeypatch.setattr(Api, "write_doc", _stub_write, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["write", "proj/main.tex"], input="hello")
    assert r.exit_code == 0, r.output
    assert upload_calls, "write should hit project_upload_file"
    assert write_doc_calls == [], "write must not route through OT path"


# ----- replace command --------------------------------------------------------


def test_replace_help_lists_command():
    """`--help` advertises the `replace` subcommand."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["--help"])
    assert r.exit_code == 0
    assert "replace" in r.output


def test_replace_subcommand_help():
    """`replace --help` advertises --find/--replace/--count/--track/--no-track flags."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["replace", "--help"])
    assert r.exit_code == 0
    assert "--find" in r.output
    assert "--replace" in r.output
    assert "--count" in r.output
    assert "--track" in r.output
    assert "--no-track" in r.output


def test_replace_success(monkeypatch: pytest.MonkeyPatch):
    """`replace` forwards arguments to `find_and_replace` and prints v-old -> v-new."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub_far(  # noqa: PLR0913 - must mirror Api.find_and_replace signature
        self,
        project_id,
        file_path,
        find,
        replace,
        *,
        count=None,
        expect_unique=True,
        track_changes=False,
        dry_run=False,
        timeout=15.0,
    ):
        captured.append(
            {
                "project_id": project_id,
                "file_path": file_path,
                "find": find,
                "replace": replace,
                "count": count,
                "expect_unique": expect_unique,
                "track_changes": track_changes,
            }
        )
        return FindReplaceResult(replacements=1, old_version=4, new_version=5)

    monkeypatch.setattr(Api, "find_and_replace", _stub_far, raising=False)

    runner = CliRunner()
    r = runner.invoke(
        cli_main,
        ["replace", "proj/main.tex", "-f", "foo", "-r", "FOO"],
    )
    assert r.exit_code == 0, r.output
    assert captured == [
        {
            "project_id": "p1",
            "file_path": "main.tex",
            "find": "foo",
            "replace": "FOO",
            "count": None,
            "expect_unique": True,
            "track_changes": True,
        }
    ]
    out = r.stderr or r.output
    assert "replaced 1" in out
    assert "v4 -> v5" in out


def test_replace_all_flag_disables_safety(monkeypatch: pytest.MonkeyPatch):
    """`--all` disables `expect_unique` and leaves `count` unset."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub_far(  # noqa: PLR0913 - must mirror Api.find_and_replace signature
        self,
        project_id,
        file_path,
        find,
        replace,
        *,
        count=None,
        expect_unique=True,
        track_changes=False,
        dry_run=False,
        timeout=15.0,
    ):
        captured.append({"expect_unique": expect_unique, "count": count})
        return FindReplaceResult(replacements=5, old_version=1, new_version=2)

    monkeypatch.setattr(Api, "find_and_replace", _stub_far, raising=False)
    runner = CliRunner()
    r = runner.invoke(
        cli_main,
        ["replace", "--all", "proj/main.tex", "-f", "foo", "-r", "FOO"],
    )
    assert r.exit_code == 0, r.output
    assert captured == [{"expect_unique": False, "count": None}]


def test_replace_multi_match_default_exits_three(monkeypatch: pytest.MonkeyPatch):
    """`replace` exits 3 and reports `ambiguous` on a `MultipleMatchesError`."""
    _stub_login_and_projects(monkeypatch)

    def _stub_far(self, *a: Any, **kw: Any):
        raise MultipleMatchesError("foo", 3)

    monkeypatch.setattr(Api, "find_and_replace", _stub_far, raising=False)
    runner = CliRunner()
    r = runner.invoke(cli_main, ["replace", "proj/main.tex", "-f", "foo", "-r", "FOO"])
    assert r.exit_code == 3, r.output
    out = r.stderr or r.output
    assert "ambiguous" in out


def test_replace_count_and_track_changes(monkeypatch: pytest.MonkeyPatch):
    """`-n N --no-track` sets `count=N, track_changes=False` on `find_and_replace`."""
    _stub_login_and_projects(monkeypatch)
    captured: list[dict] = []

    def _stub_far(  # noqa: PLR0913 - must mirror Api.find_and_replace signature
        self,
        project_id,
        file_path,
        find,
        replace,
        *,
        count=None,
        expect_unique=True,
        track_changes=False,
        dry_run=False,
        timeout=15.0,
    ):
        captured.append({"count": count, "track_changes": track_changes})
        return FindReplaceResult(replacements=1, old_version=1, new_version=2)

    monkeypatch.setattr(Api, "find_and_replace", _stub_far, raising=False)

    runner = CliRunner()
    r = runner.invoke(
        cli_main,
        ["replace", "proj/main.tex", "-f", "x", "-r", "Y", "-n", "1", "--no-track"],
    )
    assert r.exit_code == 0, r.output
    assert captured == [{"count": 1, "track_changes": False}]


def test_replace_no_occurrences_exits_nonzero(monkeypatch: pytest.MonkeyPatch):
    """`replace` exits 1 and prints `no occurrences` when nothing matched."""
    _stub_login_and_projects(monkeypatch)

    def _stub_far(self, *a: Any, **kw: Any):
        return FindReplaceResult(replacements=0, old_version=None, new_version=None)

    monkeypatch.setattr(Api, "find_and_replace", _stub_far, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["replace", "proj/main.tex", "-f", "x", "-r", "y"])
    assert r.exit_code == 1, r.output
    out = r.stderr or r.output
    assert "no occurrences" in out


def test_replace_silent_noop_exits_two(monkeypatch: pytest.MonkeyPatch):
    """`replace` exits 2 and reports `silent no-op` on `SilentNoOpError`."""
    _stub_login_and_projects(monkeypatch)

    def _stub_far(self, *a: Any, **kw: Any):
        raise SilentNoOpError("server transformed away")

    monkeypatch.setattr(Api, "find_and_replace", _stub_far, raising=False)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["replace", "proj/main.tex", "-f", "x", "-r", "y"])
    assert r.exit_code == 2, r.output
    out = r.stderr or r.output
    assert "silent no-op" in out


# ----- snapshot command -------------------------------------------------------


def test_snapshot_help_lists_command():
    """`--help` advertises the `snapshot` subcommand."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["--help"])
    assert r.exit_code == 0
    assert "snapshot" in r.output


def test_snapshot_subcommand_help():
    """`snapshot --help` advertises the `--output`/`-o` flag and the manifest."""
    runner = CliRunner()
    r = runner.invoke(cli_main, ["snapshot", "--help"])
    assert r.exit_code == 0
    assert "--output" in r.output or "-o" in r.output
    assert "manifest" in r.output.lower()


def test_snapshot_writes_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """End-to-end CLI: stubs hit build_snapshot, output is written to -o."""
    _stub_login_and_projects(monkeypatch)
    out_path = tmp_path / "snap.zip"
    payload = b"PK\x05\x06" + b"\x00" * 18  # empty-zip EOCD signature
    captured: list[dict] = []

    def _stub_build(api, project_id):
        captured.append({"project_id": project_id})
        return payload

    monkeypatch.setattr(
        "pyoverleaf._snapshot.build_snapshot", _stub_build, raising=True
    )

    runner = CliRunner()
    r = runner.invoke(cli_main, ["snapshot", "proj", "-o", str(out_path)])
    assert r.exit_code == 0, r.output
    assert out_path.read_bytes() == payload
    assert captured == [{"project_id": "p1"}]


def test_snapshot_missing_project_raises(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """`snapshot` exits non-zero when the requested project name doesn't exist."""
    monkeypatch.setattr(Api, "login_from_browser", lambda self: None, raising=True)
    monkeypatch.setattr(Api, "get_projects", lambda self, **kw: [], raising=True)

    runner = CliRunner()
    r = runner.invoke(cli_main, ["snapshot", "nope", "-o", str(tmp_path / "x.zip")])
    assert r.exit_code != 0
