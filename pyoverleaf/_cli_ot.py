"""CLI commands for Overleaf's OT (Operational Transform) write channel.

Houses `patch` and `replace` — the two commands that route through
`Api.write_doc` / `Api.find_and_replace` and therefore land as
collab-safe (optionally tracked) edits. Pulled out of `__main__.py`
to keep that module focused on routing + filesystem-style commands.
"""

from __future__ import annotations

import json
import sys

import click

from . import Api, DryRunResult, MultipleMatchesError, SilentNoOpError
from ._cli_common import get_io_and_path, host_option


def register(parent: click.Group) -> None:
    """Attach the `patch` and `replace` commands to the parent group."""
    _register_patch(parent)
    _register_replace(parent)


def _register_patch(parent: click.Group) -> None:
    @parent.command(
        "patch",
        help=(
            "Patch an existing doc via Overleaf's OT channel "
            "(collab-safe; preserves concurrent edits). Reads stdin as UTF-8."
        ),
    )
    @click.argument("path", type=str)
    @click.option(
        "--track/--no-track",
        "track_changes",
        default=True,
        show_default=True,
        help=(
            "Submit as a tracked change (visible in Overleaf's Review panel). "
            "Default is ON so collaborators always see agent edits in review; "
            "pass --no-track to write directly without review."
        ),
    )
    @click.option(
        "--dry-run",
        "dry_run",
        is_flag=True,
        help=(
            "Print the OT ops as JSON to stdout without sending. "
            "Format: {baseline_version, ops, affects_lines}."
        ),
    )
    @host_option
    def patch(path: str, track_changes: bool, dry_run: bool, host: str) -> None:
        """Submit stdin as a UTF-8 OT update against the doc at `path`."""
        api = Api(host=host)
        api.login_from_browser()
        _io, local_path, project_id = get_io_and_path(api, path)
        content = sys.stdin.buffer.read().decode("utf-8")
        try:
            result = api.write_doc(
                project_id,
                local_path,
                content,
                track_changes=track_changes,
                dry_run=dry_run,
            )
        except SilentNoOpError as e:
            click.echo(f"silent no-op: {e}", err=True)
            sys.exit(2)
        if isinstance(result, DryRunResult):
            click.echo(json.dumps(result.to_json_dict(), ensure_ascii=False))
            return
        click.echo(
            f"v{result.old_version} -> v{result.new_version}"
            + (" (silent no-op)" if result.silent_no_op else ""),
            err=True,
        )


def _register_replace(parent: click.Group) -> None:
    @parent.command(
        "replace",
        help=(
            "Literal find-and-replace in an existing doc via Overleaf's OT "
            "channel (collab-safe; preserves concurrent edits)."
        ),
    )
    @click.argument("path", type=str)
    @click.option(
        "-f", "--find", "find_str", required=True, help="Literal string to search for."
    )
    @click.option(
        "-r", "--replace", "replace_str", required=True, help="Replacement string."
    )
    @click.option(
        "-n",
        "--count",
        type=int,
        default=None,
        help=(
            "Replace at most N occurrences. Implies opt-in to multi-match; "
            "without -n or --all a multi-match raises an error."
        ),
    )
    @click.option(
        "--all",
        "all_matches",
        is_flag=True,
        help=(
            "Allow replacing every occurrence. Without this flag (and without "
            "-n), more than one match is treated as ambiguous and rejected."
        ),
    )
    @click.option(
        "--track/--no-track",
        "track_changes",
        default=True,
        show_default=True,
        help=(
            "Submit as a tracked change (visible in Overleaf's Review panel). "
            "Default is ON so collaborators always see agent edits in review; "
            "pass --no-track to write directly without review."
        ),
    )
    @click.option(
        "--dry-run",
        "dry_run",
        is_flag=True,
        help=(
            "Print the OT ops as JSON to stdout without sending. "
            "Format: {baseline_version, ops, affects_lines, replacements}. "
            "Multi-match safety (--all / -n) still applies before the preview."
        ),
    )
    @host_option
    def replace(
        path: str,
        find_str: str,
        replace_str: str,
        count: int | None,
        all_matches: bool,
        track_changes: bool,
        dry_run: bool,
        host: str,
    ) -> None:
        """Run a literal find-and-replace OT update against the doc at `path`."""
        api = Api(host=host)
        api.login_from_browser()
        _io, local_path, project_id = get_io_and_path(api, path)
        try:
            result = api.find_and_replace(
                project_id,
                local_path,
                find_str,
                replace_str,
                count=count,
                expect_unique=not all_matches,
                track_changes=track_changes,
                dry_run=dry_run,
            )
        except MultipleMatchesError as e:
            click.echo(f"ambiguous: {e}", err=True)
            sys.exit(3)
        except SilentNoOpError as e:
            click.echo(f"silent no-op: {e}", err=True)
            sys.exit(2)
        if isinstance(result, DryRunResult):
            if result.replacements == 0:
                click.echo("no occurrences found; nothing replaced", err=True)
                sys.exit(1)
            click.echo(json.dumps(result.to_json_dict(), ensure_ascii=False))
            return
        if result.replacements == 0:
            click.echo("no occurrences found; nothing replaced", err=True)
            sys.exit(1)
        click.echo(
            f"replaced {result.replacements} occurrence(s); "
            f"v{result.old_version} -> v{result.new_version}",
            err=True,
        )
