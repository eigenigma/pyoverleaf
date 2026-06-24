"""Public entry point for the pyoverleaf package.

Imports `_otapi` for its side effect of attaching `apply_ot_update` /
`write_doc` to `Api`, and calls `_otapi_reviews.attach(Api)` to bolt on
the tracked-changes / comments methods. After import the `Api` class
exposes the full surface listed in `__all__`.
"""

from . import _otapi as _otapi  # side-effect: attaches OT methods to `Api`
from . import _otapi_reviews
from ._io import ProjectBytesIO, ProjectIO
from ._models import (
    CommentMessage,
    CommentThread,
    DryRunResult,
    FindReplaceResult,
    Project,
    ProjectFile,
    ProjectFolder,
    TrackedChange,
    User,
    WriteResult,
)
from ._ot import (
    MultipleMatchesError,
    OtDeleteMismatch,
    OtError,
    OtUpdateError,
    OtVersionConflict,
    SilentNoOpError,
)
from ._webapi import Api

_otapi_reviews.attach(Api)

__all__ = [
    "Api",
    "CommentMessage",
    "CommentThread",
    "DryRunResult",
    "FindReplaceResult",
    "MultipleMatchesError",
    "OtDeleteMismatch",
    "OtError",
    "OtUpdateError",
    "OtVersionConflict",
    "Project",
    "ProjectBytesIO",
    "ProjectFile",
    "ProjectFolder",
    "ProjectIO",
    "SilentNoOpError",
    "TrackedChange",
    "User",
    "WriteResult",
]
