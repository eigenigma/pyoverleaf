"""File-like helpers that map Overleaf project files onto Python IO.

`ProjectIO` is a thin `os`-style facade over the Overleaf web API: it can
`open`, `mkdir`, `listdir`, `remove`, and check `exists` against project
files exactly as if the project were a local directory. `ProjectBytesIO`
backs the `open` call with a `BytesIO` buffer that flushes back to the
server via `Api.project_upload_file`.

For collaboration-safe edits to live docs prefer `write_doc` /
`find_and_replace` (OT path) over `open(..., "w")` (whole-file upload).
"""

import io
import os
import pathlib
from collections.abc import Callable
from typing import IO

from ._models import (
    DryRunResult,
    FindReplaceResult,
    ProjectFile,
    ProjectFolder,
    WriteResult,
)
from ._webapi import Api


class ProjectBytesIO(io.BytesIO):
    """`BytesIO` backed by an Overleaf project file.

    On `flush`/`close`, the in-memory buffer is uploaded back to the
    project through the caller-supplied `update_file` callback. Mode
    semantics mirror the built-in `open`: `"r"` preloads server content,
    `"w"` starts empty, `"a"` preserves existing bytes as a prefix.
    """

    def __init__(
        self,
        api: Api,
        project_id: str,
        file: ProjectFile | None = None,
        mode: str = "r",
        update_file: Callable[[bytes], ProjectFile] | None = None,
    ) -> None:
        """Initialize the buffer, optionally seeding it from the server.

        Args:
            api: The Overleaf API client.
            project_id: The id of the project the file belongs to.
            file: The existing project file, if any.
            mode: Standard Python file mode (`"r"`, `"w"`, `"a"`, ...).
            update_file: Callback that uploads the final bytes back to
                the project and returns the resulting `ProjectFile`.
        """
        self._api = api
        self._project_id = project_id
        self._file = file
        self._mode = mode
        self._update_file = update_file
        self._prefix_bytes: bytes | None = None
        init_bytes = b""
        if file is not None and "w" not in mode:
            init_bytes = self._api.project_download_file(self._project_id, self._file)
        if "a" in mode:
            self._prefix_bytes = init_bytes
            init_bytes = b""
        super().__init__(init_bytes)

    def writable(self) -> bool:
        """Return True if the file was opened in a writable mode."""
        return "w" in self._mode or "a" in self._mode or "+" in self._mode

    def readable(self) -> bool:
        """Return True if the file was opened in a readable mode."""
        return "r" in self._mode or "+" in self._mode

    def flush(self) -> None:
        """Flush the buffer; upload the contents if writable."""
        super().flush()
        if self.writable():
            data = self.getvalue()
            if self._prefix_bytes is not None:
                data = self._prefix_bytes + data
            assert self._update_file is not None
            self._file = self._update_file(data)

    def close(self) -> None:
        """Flush pending writes and close the underlying buffer."""
        self.flush()
        super().close()


class ProjectIO:
    """`os`-style facade for navigating and mutating an Overleaf project.

    Caches the root folder listing on first use; later mutations through
    this instance (e.g. `mkdir`) update the cache in place, but external
    edits made by collaborators are not picked up until a new instance
    is constructed.
    """

    def __init__(self, api: "Api", project_id: str) -> None:
        """Bind the facade to a specific project.

        Args:
            api: The Overleaf API client.
            project_id: The id of the target project.
        """
        self._api = api
        self._project_id = project_id
        self._cached_project_files: ProjectFolder | None = None

    def _project_files(self) -> ProjectFolder:
        if self._cached_project_files is None:
            self._cached_project_files = self._api.project_get_files(self._project_id)
        return self._cached_project_files

    def _find(self, path: pathlib.PurePath | str) -> ProjectFolder | ProjectFile | None:
        current_pointer: ProjectFolder | ProjectFile = self._project_files()
        path = pathlib.PurePath(path)
        for part in path.parts:
            if not isinstance(current_pointer, ProjectFolder):
                return None
            for child in current_pointer.children:
                if child.name == part:
                    current_pointer = child
                    break
            else:
                return None
        return current_pointer

    def exists(self, path: pathlib.PurePath | str) -> bool:
        """Check if a file exists in the project.

        Args:
            path: The path to the file.

        Returns:
            True if the file exists, else False.
        """
        return self._find(path) is not None

    def open(
        self,
        path: pathlib.PurePath | str,
        mode: str = "r",
        encoding: str | None = None,
    ) -> IO:
        """Open a file in the project.

        Args:
            path: The path to the file.
            mode: The mode to open the file in.
            encoding: The encoding to use when the file is not opened
                in binary mode.

        Returns:
            A file-like object.
        """
        assert mode in [
            "r",
            "w",
            "a",
            "r+",
            "w+",
            "a+",
            "rb",
            "wb",
            "ab",
            "rb+",
            "wb+",
            "ab+",
        ]
        binary = "b" in mode

        assert_file_exists = True
        if "r" in mode and "+" in mode:
            # Create file if it doesn't exist
            assert_file_exists = False
        elif "w" in mode:
            assert_file_exists = False

        # Find the handles
        parent_path = pathlib.PurePath(path).parent
        folder = self._project_files()
        for part in parent_path.parts:
            for child in folder.children:
                if child.name == part and child.type == "folder":
                    folder = child
                    break
            else:
                raise FileNotFoundError("No such file or directory: " + str(path))

        folder_id = folder.id
        file = None
        filename = os.path.split(path)[-1]
        for child in folder.children:
            if child.name == filename and child.type != "folder":
                file = child
                break
        if file is None and assert_file_exists:
            raise FileNotFoundError("No such file or directory: " + str(path))

        def update_file(data: bytes) -> ProjectFile:
            return self._api.project_upload_file(
                self._project_id, folder_id, filename, data
            )

        bytes_io = ProjectBytesIO(self._api, self._project_id, file, mode, update_file)
        if not binary:
            return io.TextIOWrapper(bytes_io, encoding=encoding)
        return bytes_io

    def mkdir(
        self,
        path: pathlib.PurePath | str,
        exist_ok: bool = False,
        *,
        parents: bool = False,
    ) -> None:
        """Create a directory in the project.

        Args:
            path: The path to the directory.
            exist_ok: If True, no exception will be raised if the
                directory already exists.
            parents: If True, all parent directories will be created if
                they don't exist.
        """
        path = pathlib.PurePath(path)
        current_pointer = self._project_files()
        for i, part in enumerate(path.parts):
            for child in current_pointer.children:
                if child.name == part:
                    if child.type != "folder":
                        raise FileExistsError("Cannot create directory: " + str(path))
                    current_pointer = child
                    if i == len(path.parts) - 1 and not exist_ok:
                        raise FileExistsError("Cannot create directory: " + str(path))
                    break
            else:
                if i < len(path.parts) - 1 and not parents:
                    raise FileNotFoundError("No such file or directory: " + str(path))
                current_pointer = self._api.project_create_folder(
                    self._project_id, current_pointer.id, part
                )

    def listdir(self, path: pathlib.PurePath | str) -> list[str]:
        """List the contents of a directory in the project.

        Args:
            path: The path to the directory.

        Returns:
            A list of the contents of the directory.
        """
        directory = self._find(path)
        if directory is None:
            raise FileNotFoundError("No such file or directory: " + str(path))
        return [child.name for child in directory.children]

    def write_doc(
        self,
        path: pathlib.PurePath | str,
        content: str,
        *,
        track_changes: bool = False,
        raise_on_silent_noop: bool = True,
        dry_run: bool = False,
        timeout: float = 15.0,
    ) -> WriteResult | DryRunResult:
        """Submit a collaboration-safe OT write to an existing doc.

        Unlike `open(..., "w")` (which whole-file-uploads), this routes
        through Overleaf's `applyOtUpdate` socket channel so concurrent
        edits by live collaborators are not clobbered. Pass
        `dry_run=True` to receive a `DryRunResult` describing the ops
        instead of sending them.
        """
        return self._api.write_doc(
            self._project_id,
            str(path),
            content,
            track_changes=track_changes,
            raise_on_silent_noop=raise_on_silent_noop,
            dry_run=dry_run,
            timeout=timeout,
        )

    def find_and_replace(
        self,
        path: pathlib.PurePath | str,
        find: str,
        replace: str,
        *,
        count: int | None = None,
        expect_unique: bool = True,
        track_changes: bool = False,
        dry_run: bool = False,
        timeout: float = 15.0,
    ) -> FindReplaceResult | DryRunResult:
        """Literal find-and-replace on a doc via the OT path.

        By default (`expect_unique=True`, `count=None`) raises
        `MultipleMatchesError` when `find` matches more than once, to
        prevent accidental bulk edits. Pass `count=N` for the first N or
        `expect_unique=False` to opt into replace-all. Collab-safety is
        inherited from `write_doc`. Pass `dry_run=True` to receive a
        `DryRunResult` preview without sending.
        """
        return self._api.find_and_replace(
            self._project_id,
            str(path),
            find,
            replace,
            count=count,
            expect_unique=expect_unique,
            track_changes=track_changes,
            dry_run=dry_run,
            timeout=timeout,
        )

    def remove(self, path: pathlib.PurePath | str, missing_ok: bool = False) -> None:
        """Remove a file/directory from the project.

        Args:
            path: The path to the file.
            missing_ok: If True, silently return when the path does not
                exist instead of raising `FileNotFoundError`.
        """
        entity = self._find(path)
        if entity is None:
            if missing_ok:
                return
            raise FileNotFoundError("No such file or directory: " + str(path))
        self._api.project_delete_entity(self._project_id, entity)
