"""Top-level Click entry point for the `pyoverleaf` CLI.

Wires file-system style commands (`ls`, `read`, `write`, `rm`, ...)
onto a shared `pyoverleaf` group, then delegates the OT-channel
commands (`patch`, `replace`) to `_cli_ot` and the review-panel
commands (`comments`, `changes`) to `_cli_reviews`.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click

from . import Api
from ._cli_common import get_io_and_path, host_option, resolve_project_id
from ._cli_ot import register as _register_ot
from ._cli_reviews import register as _register_reviews


@click.group()
def main() -> None:
    """Root group for the `pyoverleaf` CLI."""


@main.command("ls", help="List projects or files in a project")
@click.argument("path", type=str, default=".")
@host_option
def list_projects_and_files(path: str, host: str) -> None:
    """List projects, or files inside a project when `path` is `<project>/...`."""
    api = Api(host=host)
    api.login_from_browser()
    if not path or path in {".", "/"}:
        projects = api.get_projects()
        click.echo("\n".join(project.name for project in projects))
        return
    io, local_path, _project_id = get_io_and_path(api, path)
    files = io.listdir(local_path)
    click.echo("\n".join(files))


@main.command("mkdir", help="Create a directory in a project")
@click.option(
    "-p",
    "--parents",
    is_flag=True,
    help="Create parent directories if they don't exist.",
)
@host_option
@click.argument("path", type=str)
def make_directory(path: str, parents: bool, host: str) -> None:
    """Create directory `path` in the target project."""
    api = Api(host=host)
    api.login_from_browser()
    io, local_path, _project_id = get_io_and_path(api, path)
    io.mkdir(local_path, parents=parents, exist_ok=parents)


@main.command(
    "read", help="Reads the file in a project and writes to the standard output"
)
@click.argument("path", type=str)
@host_option
def read(path: str, host: str) -> None:
    """Stream the project file at `path` to stdout as raw bytes."""
    api = Api(host=host)
    api.login_from_browser()
    io, local_path, _project_id = get_io_and_path(api, path)
    with io.open(local_path, "rb") as f:
        shutil.copyfileobj(f, sys.stdout.buffer)


@main.command(
    "write", help="Reads the standard input and writes to the file in a project"
)
@click.argument("path", type=str)
@host_option
def write(path: str, host: str) -> None:
    """Write stdin bytes to the project file at `path` (upload path, not OT)."""
    api = Api(host=host)
    api.login_from_browser()
    io, local_path, _project_id = get_io_and_path(api, path)
    with io.open(local_path, "wb+") as f:
        shutil.copyfileobj(sys.stdin.buffer, f)


@main.command("rm", help="Remove file or folder from a project")
@click.argument("path", type=str)
@host_option
def remove(path: str, host: str) -> None:
    """Delete the file or folder at `path` from the project."""
    api = Api(host=host)
    api.login_from_browser()
    io, local_path, _project_id = get_io_and_path(api, path)
    io.remove(local_path)


@main.command(
    "download-project", help="Download project as a zip file to the specified path."
)
@click.argument("project", type=str)
@click.argument("output_path", type=str)
@host_option
def download_project(project: str, output_path: str, host: str) -> None:
    """Download project `project` as a zip file to `output_path`."""
    api = Api(host=host)
    api.login_from_browser()
    project_id = resolve_project_id(api, project)
    api.download_project(project_id, output_path)
    click.echo("Project downloaded to " + output_path)


@main.command(
    "fig-upload",
    help=(
        "Upload a binary asset (figure/PDF/image) to a project. Prints "
        "a ready-to-pipe \\includegraphics{...} fragment to stdout."
    ),
)
@click.argument("project", type=str)
@click.argument(
    "local_path", type=click.Path(exists=True, dir_okay=False, readable=True)
)
@click.option(
    "--remote-path",
    "remote_path",
    default=None,
    help="Remote path inside the project. Defaults to figures/<basename>.",
)
@click.option(
    "--bare",
    is_flag=True,
    help="Print only the remote path instead of the \\includegraphics fragment.",
)
@click.option(
    "--width",
    default="\\linewidth",
    help="Width spec for \\includegraphics. Default: \\linewidth.",
)
@click.option(
    "--no-create-parents",
    is_flag=True,
    help="Fail if the remote folder does not already exist.",
)
@host_option
def fig_upload_cmd(
    project: str,
    local_path: str,
    remote_path: str | None,
    bare: bool,
    width: str,
    no_create_parents: bool,
    host: str,
) -> None:
    r"""Upload a figure to `project` and print an `\includegraphics` snippet."""
    from ._figupload import fig_upload, includegraphics_snippet

    api = Api(host=host)
    api.login_from_browser()
    project_id = resolve_project_id(api, project)
    resolved_path, _ = fig_upload(
        api,
        project_id,
        local_path,
        remote_path=remote_path,
        create_parents=not no_create_parents,
    )
    if bare:
        click.echo(resolved_path)
    else:
        click.echo(includegraphics_snippet(resolved_path, width=width))


@main.command(
    "snapshot",
    help=(
        "Download project as a zip enriched with .pyoverleaf/manifest.json "
        "carrying per-doc {doc_id, version, ranges} (tracked changes + "
        "comments)."
    ),
)
@click.argument("project", type=str)
@click.option(
    "-o",
    "--output",
    "output_path",
    required=True,
    type=click.Path(dir_okay=False, writable=True),
    help="Path to write the enriched zip.",
)
@host_option
def snapshot(project: str, output_path: str, host: str) -> None:
    """Build a manifest-enriched zip of `project` and write to `output_path`."""
    from ._snapshot import build_snapshot

    api = Api(host=host)
    api.login_from_browser()
    project_id = resolve_project_id(api, project)
    data = build_snapshot(api, project_id)
    Path(output_path).write_bytes(data)
    click.echo(f"Snapshot written to {output_path} ({len(data)} bytes)", err=True)


_register_ot(main)
_register_reviews(main)


if __name__ == "__main__":
    main()
