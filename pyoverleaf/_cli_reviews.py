"""CLI groups for review-panel operations: `comments` + `changes`.

The `register(...)` helper attaches both subcommand groups to the
top-level `pyoverleaf` Click group. Project-id resolution and the
shared `--host` option come from `_cli_common`.
"""

from __future__ import annotations

import json
import sys

import click

from . import Api
from ._cli_common import host_option, resolve_project_id


def register(parent: click.Group) -> None:
    """Attach `comments` and `changes` subcommand groups to `parent`."""
    _register_comments(parent)
    _register_changes(parent)


# ---- comments group ---------------------------------------------------------


def _register_comments(parent: click.Group) -> None:
    @parent.group(
        "comments",
        help="List/reply/resolve/reopen review-panel comment threads.",
    )
    def comments_group() -> None:
        """Review-panel comment-thread subcommands."""

    @comments_group.command("list", help="List comment threads. Output: JSON.")
    @click.argument("path", type=str)
    @click.option("--include-resolved", is_flag=True, help="Include resolved threads.")
    @host_option
    def _list(path: str, include_resolved: bool, host: str) -> None:
        """List comment threads, optionally including resolved ones."""
        api = Api(host=host)
        api.login_from_browser()
        if "/" in path:
            project, sub = path.split("/", 1)
            doc_filter = sub or None
        else:
            project, doc_filter = path, None
        project_id = resolve_project_id(api, project)
        threads = api.list_comments(
            project_id,
            doc_path_filter=doc_filter,
            include_resolved=include_resolved,
        )
        click.echo(json.dumps([t.to_json_dict() for t in threads], ensure_ascii=False))

    @comments_group.command("reply", help="Post a reply to a comment thread.")
    @click.argument("project", type=str)
    @click.argument("thread_id", type=str)
    @click.option("-m", "--message", required=True, help="Reply text.")
    @host_option
    def _reply(project: str, thread_id: str, message: str, host: str) -> None:
        """Post `message` as a reply to `thread_id` in `project`."""
        api = Api(host=host)
        api.login_from_browser()
        project_id = resolve_project_id(api, project)
        click.echo(json.dumps(api.reply_to_comment(project_id, thread_id, message)))

    @comments_group.command("resolve", help="Mark a thread as resolved.")
    @click.argument("project", type=str)
    @click.argument("thread_id", type=str)
    @host_option
    def _resolve(project: str, thread_id: str, host: str) -> None:
        """Mark `thread_id` as resolved in `project`."""
        api = Api(host=host)
        api.login_from_browser()
        project_id = resolve_project_id(api, project)
        click.echo(json.dumps(api.resolve_comment(project_id, thread_id)))

    @comments_group.command("reopen", help="Reopen a previously-resolved thread.")
    @click.argument("project", type=str)
    @click.argument("thread_id", type=str)
    @host_option
    def _reopen(project: str, thread_id: str, host: str) -> None:
        """Reopen the previously-resolved `thread_id` in `project`."""
        api = Api(host=host)
        api.login_from_browser()
        project_id = resolve_project_id(api, project)
        click.echo(json.dumps(api.reopen_comment(project_id, thread_id)))


# ---- changes group ----------------------------------------------------------


def _register_changes(parent: click.Group) -> None:
    @parent.group(
        "changes",
        help="List/accept/reject tracked changes (review-panel suggestions).",
    )
    def changes_group() -> None:
        """Review-panel tracked-change subcommands."""

    @changes_group.command("list", help="List tracked changes. Output: JSON.")
    @click.argument("path", type=str)
    @host_option
    def _list(path: str, host: str) -> None:
        """List tracked changes for a project, optionally filtered by doc path."""
        api = Api(host=host)
        api.login_from_browser()
        if "/" in path:
            project, sub = path.split("/", 1)
            doc_filter = sub or None
        else:
            project, doc_filter = path, None
        project_id = resolve_project_id(api, project)
        items = api.list_tracked_changes(project_id, doc_path_filter=doc_filter)
        click.echo(json.dumps([c.to_json_dict() for c in items], ensure_ascii=False))

    @changes_group.command("accept", help="Accept one or more tracked changes.")
    @click.argument("project", type=str)
    @click.argument("change_ids", type=str, nargs=-1, required=True)
    @host_option
    def _accept(project: str, change_ids: tuple[str, ...], host: str) -> None:
        """Accept the listed tracked changes in `project`."""
        api = Api(host=host)
        api.login_from_browser()
        project_id = resolve_project_id(api, project)
        result = api.accept_tracked_changes(project_id, list(change_ids))
        click.echo(json.dumps(result, ensure_ascii=False))
        if result.get("unknown"):
            sys.exit(1)

    @changes_group.command(
        "reject",
        help=(
            "Reject one or more tracked changes (constructs inverse OT ops "
            "client-side and submits with u:true)."
        ),
    )
    @click.argument("project", type=str)
    @click.argument("change_ids", type=str, nargs=-1, required=True)
    @host_option
    def _reject(project: str, change_ids: tuple[str, ...], host: str) -> None:
        """Reject the listed tracked changes in `project`."""
        api = Api(host=host)
        api.login_from_browser()
        project_id = resolve_project_id(api, project)
        result = api.reject_tracked_changes(project_id, list(change_ids))
        click.echo(json.dumps(result, ensure_ascii=False))
        if result.get("missing"):
            sys.exit(1)
