"""Dataclasses describing Overleaf API entities.

Extracted from `_webapi.py` so the API surface module stays under the
project's per-file size budget. No behavior change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class User:
    id: str
    email: str
    first_name: str
    last_name: str | None

    @classmethod
    def from_data(cls, data: dict) -> User:
        return cls(
            id=data["id"],
            email=data["email"],
            first_name=data["firstName"],
            last_name=data.get("lastName"),
        )


@dataclass
class Tag:
    id: str
    name: str
    color: str | None

    @classmethod
    def from_data(cls, data: dict) -> Tag:
        return cls(
            id=data["_id"],
            name=data["name"],
            color=data.get("color"),
        )


@dataclass
class Project:
    id: str
    name: str
    last_updated: str
    access_level: str
    source: str
    archived: bool
    trashed: bool
    owner: User | None = None
    last_updated_by: User | None = None
    tags: list[Tag] | None = field(default_factory=list)

    @classmethod
    def from_data(cls, data: dict) -> Project:
        out = cls(
            id=data["id"],
            name=data["name"],
            last_updated=data["lastUpdated"],
            access_level=data["accessLevel"],
            source=data["source"],
            archived=data["archived"],
            trashed=data["trashed"],
        )

        owner_data = data.get("owner")
        if owner_data is not None:
            out.owner = User.from_data(owner_data)

        last_updated_by_data = data.get("lastUpdatedBy")
        if last_updated_by_data is not None:
            out.last_updated_by = User.from_data(last_updated_by_data)

        return out


@dataclass
class ProjectFile:
    id: str
    name: str
    created: str | None
    type: Literal["file", "doc"] = "file"

    @classmethod
    def from_data(cls, data: dict) -> ProjectFile:
        return cls(
            id=data["_id"],
            name=data["name"],
            created=data.get("created"),
        )

    def __str__(self) -> str:
        return self.name


@dataclass
class ProjectFolder:
    id: str
    name: str
    children: list[ProjectFile | ProjectFolder] = field(default_factory=list)

    @classmethod
    def from_data(cls, data: dict) -> ProjectFolder:
        out = cls(
            id=data["_id"],
            name=data["name"],
        )
        for child in data["folders"]:
            out.children.append(ProjectFolder.from_data(child))

        for child in data["fileRefs"]:
            out.children.append(ProjectFile.from_data(child))

        for child in data["docs"]:
            doc = ProjectFile.from_data(child)
            doc.type = "doc"
            out.children.append(doc)
        return out

    def __str__(self) -> str:
        out = self.name + ":"
        for child in self.children:
            child_str = str(child)
            out += "\n"
            for line in child_str.splitlines(keepends=True):
                out += "  " + line
        return out

    @property
    def type(self) -> str:
        return "folder"


@dataclass(frozen=True)
class WriteResult:
    """Outcome of an OT write submitted through `Api.write_doc`.

    Fields:
      old_version: the baseline version the diff was computed against.
      new_version: server-confirmed post-edit version (event.v + 1).
      silent_no_op: True when a non-empty op set produced a post-edit
        document identical to the pre-edit document (server-side OT
        nullified our op).
    """

    old_version: int
    new_version: int
    silent_no_op: bool


@dataclass(frozen=True)
class FindReplaceResult:
    """Outcome of an OT find-and-replace submitted through `Api.find_and_replace`.

    Fields:
      replacements: count of literal `find` occurrences replaced.
      old_version: pre-edit doc version (or None when replacements == 0).
      new_version: post-edit doc version (or None when replacements == 0).
    """

    replacements: int
    old_version: int | None
    new_version: int | None


@dataclass(frozen=True)
class TrackedChange:
    """A single tracked-change entry (insert or delete suggestion).

    `kind` is "insert" when the change adds text (`op.i`) and "delete"
    when it removes text (`op.d`). `text` is the affected text body.
    `change_id` is what the accept/reject endpoints use to identify it.
    """

    change_id: str
    doc_id: str
    doc_path: str | None
    kind: str
    position: int
    text: str
    user_id: str | None
    timestamp: str | None

    def to_json_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "doc_id": self.doc_id,
            "doc_path": self.doc_path,
            "kind": self.kind,
            "position": self.position,
            "text": self.text,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class CommentMessage:
    """One message inside a review-panel comment thread."""

    id: str
    content: str
    timestamp: int | None
    user_id: str | None
    user_name: str | None


@dataclass(frozen=True)
class CommentThread:
    """A review-panel comment thread, cross-referenced with its doc anchor."""

    thread_id: str
    doc_id: str | None
    doc_path: str | None
    quoted_text: str | None
    position: int | None
    resolved: bool
    messages: list[CommentMessage]

    def to_json_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "doc_id": self.doc_id,
            "doc_path": self.doc_path,
            "quoted_text": self.quoted_text,
            "position": self.position,
            "resolved": self.resolved,
            "messages": [
                {
                    "id": m.id,
                    "content": m.content,
                    "timestamp": m.timestamp,
                    "user_id": m.user_id,
                    "user_name": m.user_name,
                }
                for m in self.messages
            ],
        }


@dataclass(frozen=True)
class DryRunResult:
    """Preview of what an OT write would emit, without sending it.

    Fields:
      baseline_version: live doc version the ops were computed against.
      ops: ShareJS op list that would be submitted.
      affects_lines: 1-based line numbers in the baseline doc touched by ops.
      replacements: only set by `find_and_replace(dry_run=True)`; None
        for `write_doc(dry_run=True)`.
    """

    baseline_version: int
    ops: list[dict]
    affects_lines: list[int]
    replacements: int | None = None

    def to_json_dict(self) -> dict:
        out: dict = {
            "baseline_version": self.baseline_version,
            "ops": list(self.ops),
            "affects_lines": list(self.affects_lines),
        }
        if self.replacements is not None:
            out["replacements"] = self.replacements
        return out
