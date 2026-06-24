"""Shared helpers for the CLI sub-modules (`__main__`, `_cli_ot`, `_cli_reviews`).

Carries the `--host` decorator, project-id resolution by name, and the
`<project>/<local path>` argument splitter used by every command that
operates on a project file. Centralized here so the three CLI modules
don't each grow their own copy.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import click

from . import Api, ProjectIO

F = TypeVar("F", bound=Callable[..., object])


def host_option(func: F) -> F:
    """Attach the shared `--host` option (envvar `PYOVERLEAF_HOST`)."""
    return click.option(
        "--host",
        default="overleaf.com",
        envvar="PYOVERLEAF_HOST",
        help=(
            "The domain of the overleaf instance. If not given, the value of "
            "env var PYOVERLEAF_HOST, else default overleaf.com."
        ),
    )(func)


def resolve_project_id(api: Api, project_name: str) -> str:
    """Return the project id whose name matches `project_name`.

    Raises:
        FileNotFoundError: if no project with that name exists.
    """
    for p in api.get_projects():
        if p.name == project_name:
            return p.id
    raise FileNotFoundError(f"Project '{project_name}' not found.")


def split_project_and_subpath(path: str) -> tuple[str, str]:
    """Split a `<project>/<local path>` argument into its two parts.

    Raises:
        click.BadParameter: if `path` does not contain a `/`.
    """
    if "/" not in path:
        raise click.BadParameter("Path must be in the format <project>/<local path>.")
    stripped = path.removeprefix("/")
    project, *rest = stripped.split("/", 1)
    local_path = rest[0] if rest else ""
    return project, local_path


def get_io_and_path(api: Api, path: str) -> tuple[ProjectIO, str, str]:
    """Resolve a `<project>/<local path>` arg into `(io, local_path, project_id)`."""
    project, local_path = split_project_and_subpath(path)
    project_id = resolve_project_id(api, project)
    io = ProjectIO(api, project_id)
    return io, local_path, project_id
