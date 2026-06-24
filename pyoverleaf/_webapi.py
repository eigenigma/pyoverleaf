"""Overleaf web API client.

`Api` speaks Overleaf's HTTP and Socket.IO endpoints — the same channel
the official editor uses — and returns the typed objects from
`_models`. All write paths require a prior `login_from_*` call; reads
that hit the dashboard or a project page do too.

The OT-write surface (`apply_ot_update`, `write_doc`, `find_and_replace`)
and tracked-changes / comments methods are attached at package import
time by `_otapi` and `_otapi_reviews`; they live on the same `Api`
instance even though they are not declared in this file.
"""

try:
    import http.cookiejar as cookielib
except ImportError:
    import cookielib  # type: ignore[no-redef]
import json
from pathlib import Path
from typing import Any, Literal, overload

import browser_cookie3 as browsercookie
import requests
from bs4 import BeautifulSoup

from ._models import Project, ProjectFile, ProjectFolder, Tag
from ._websocket import open_socket as _open_socket_impl


class Api:
    """HTTP + Socket.IO client for an Overleaf instance.

    Construct, call `login_from_browser` (or `login_from_cookies`), then
    drive the project/file methods. One instance corresponds to one
    Overleaf account session against one `host`; cookies are stored on
    the instance and shared across calls. Not thread-safe.
    """

    def __init__(
        self,
        *,
        timeout: int = 16,
        proxies: dict[str, str] | None = None,
        ssl_verify: bool = True,
        host: str = "www.overleaf.com",
    ) -> None:
        """Configure the client without performing any network I/O.

        Args:
            timeout: Per-request timeout in seconds for the underlying
                `requests.Session`.
            proxies: Optional mapping of `requests`-style proxy URLs.
            ssl_verify: Whether to verify TLS certificates; turn off for
                self-hosted Overleaf with self-signed certs.
            host: Overleaf hostname (defaults to the public service).
        """
        self._session_initialized = False
        self._cookies: cookielib.CookieJar | None = None
        self._request_kwargs = {"timeout": timeout}
        self._proxies = proxies
        self._ssl_verify = ssl_verify
        self._csrf_cache: tuple[str, str] | None = None
        self._host = host

    def get_projects(
        self, *, trashed: bool = False, archived: bool = False
    ) -> list[Project]:
        """List projects visible on the dashboard.

        Scrapes the `ol-prefetchedProjectsBlob` / `ol-tags` meta tags
        from `GET /` (the same payload the editor's project list reads).
        Each project is annotated with its tags as a side effect.

        Args:
            trashed: Whether to include trashed projects.
            archived: Whether to include archived projects.

        Returns:
            A list of projects.
        """
        self._assert_session_initialized()
        r = self._get_session().get(f"https://{self._host}/", **self._request_kwargs)
        r.raise_for_status()
        content = BeautifulSoup(r.content, features="html.parser")

        projects_meta = content.find("meta", {"name": "ol-prefetchedProjectsBlob"})
        if projects_meta is None:
            raise RuntimeError(
                "Failed to fetch projects. Please ensure that you are logged"
                " into Overleaf in your browser and that your session is valid."
            )

        data = projects_meta.get("content")
        data = json.loads(data)
        projects = []
        for project_data in data["projects"]:
            proj = Project.from_data(project_data)
            if not trashed and proj.trashed:
                continue
            if not archived and proj.archived:
                continue
            projects.append(proj)

        # Add tags to projects
        tags_meta = content.find("meta", {"name": "ol-tags"})
        if tags_meta is None:
            raise RuntimeError(
                "Failed to fetch tags. Please ensure that you are logged"
                " into Overleaf in your browser and that your session is valid."
            )

        tags = tags_meta.get("content")
        tags = json.loads(tags)
        proj_map = {proj.id: proj for proj in projects}
        for tag_data in tags:
            tag = Tag.from_data(tag_data)
            for project_id in tag_data["project_ids"]:
                if project_id in proj_map:
                    project = proj_map[project_id]
                    if not hasattr(project, "tags"):
                        project.tags = []
                    project.tags.append(tag)
        return projects

    @overload
    def download_project(self, project_id: str) -> bytes: ...

    @overload
    def download_project(self, project_id: str, output_path: str) -> None: ...

    def download_project(
        self, project_id: str, output_path: str | None = None
    ) -> bytes | None:
        """Download a project as a zip via `GET /project/{id}/download/zip`.

        Args:
            project_id: The id of the project to download.
            output_path: Where to save the zip. When `None`, the bytes
                are returned instead of written to disk.

        Returns:
            The zipped project if `output_path` is `None`, else `None`.
        """
        self._assert_session_initialized()
        r = self._get_session().get(
            f"https://{self._host}/project/{project_id}/download/zip",
            **self._request_kwargs,
        )
        r.raise_for_status()
        if output_path is not None:
            Path(output_path).write_bytes(r.content)
            return None
        return r.content

    def project_get_files(self, project_id: str) -> ProjectFolder:
        """Get the root folder tree for a project.

        Opens a Socket.IO session, waits for `joinProjectResponse`, and
        returns its `rootFolder[0]` as a `ProjectFolder`.

        Args:
            project_id: The id of the project.

        Returns:
            The root directory of the project.
        """
        data = None
        socket = self._open_socket(project_id)
        while True:
            line = socket.recv()
            if line.startswith("7:"):
                # Unauthorized. TODO: handle this.
                raise RuntimeError("Could not get project files.")
            if line.startswith("5:"):
                break
        data = json.loads(line[len("5:") :].lstrip(":"))

        # Parse the data
        assert data["name"] == "joinProjectResponse"
        data = data["args"][0]
        assert len(data["project"]["rootFolder"]) == 1
        return ProjectFolder.from_data(data["project"]["rootFolder"][0])

    def project_create_folder(
        self, project_id: str, parent_folder_id: str, folder_name: str
    ) -> ProjectFolder:
        """Create a folder via `POST /project/{id}/folder`.

        Args:
            project_id: The id of the project.
            parent_folder_id: The id of the parent folder.
            folder_name: The name of the folder.

        Returns:
            The newly created `ProjectFolder`.
        """
        self._assert_session_initialized()
        r = self._get_session().post(
            f"https://{self._host}/project/{project_id}/folder",
            json={"parent_folder_id": parent_folder_id, "name": folder_name},
            **self._request_kwargs,
            headers={
                "Referer": f"https://{self._host}/project/{project_id}",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "x-csrf-token": self._get_csrf_token(project_id),
            },
        )
        r.raise_for_status()
        return ProjectFolder.from_data(json.loads(r.content))

    def project_upload_file(
        self, project_id: str, folder_id: str, file_name: str, file_content: bytes
    ) -> ProjectFile:
        """Upload a file via `POST /project/{id}/upload?folder_id={fid}`.

        Whole-file upload — overwrites any existing file with the same
        name in the target folder. For collaboration-safe edits to live
        docs use `write_doc` (OT path) instead.

        Args:
            project_id: The id of the project.
            folder_id: The id of the folder to upload to.
            file_name: The name of the file.
            file_content: The raw bytes to upload.

        Returns:
            The `ProjectFile` returned by the server.
        """
        mime = "application/octet-stream"
        self._assert_session_initialized()
        r = self._get_session().post(
            f"https://{self._host}/project/{project_id}/upload?folder_id={folder_id}",
            files={
                "relativePath": (None, "null"),
                "name": (None, file_name),
                "type": (None, mime),
                "qqfile": (file_name, file_content, mime),
            },
            **self._request_kwargs,
            headers={
                "Referer": f"https://{self._host}/project/{project_id}",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "x-csrf-token": self._get_csrf_token(project_id),
            },
        )
        r.raise_for_status()
        response = json.loads(r.content)
        return ProjectFile(
            response["entity_id"],
            name=file_name,
            created=None,
            type=response["entity_type"],
        )

    @overload
    def project_download_file(self, project_id: str, file: ProjectFile) -> bytes: ...

    @overload
    def project_download_file(
        self, project_id: str, file: ProjectFile, output_path: str
    ) -> None: ...

    def project_download_file(
        self, project_id: str, file: ProjectFile, output_path: str | None = None
    ) -> bytes | None:
        """Download a project file.

        Static binaries use `GET /project/{id}/file/{file_id}`; docs are
        pulled over the OT socket via `_pull_doc_project_file_content`
        and returned as UTF-8 bytes.

        Args:
            project_id: The id of the project.
            file: The file to download.
            output_path: Where to save the bytes. When `None`, the bytes
                are returned instead of written to disk.

        Returns:
            The file bytes if `output_path` is `None`, else `None`.
        """
        self._assert_session_initialized()
        if file.type == "file":
            r = self._get_session().get(
                f"https://{self._host}/project/{project_id}/file/{file.id}",
                **self._request_kwargs,
            )
            r.raise_for_status()
            if output_path is not None:
                Path(output_path).write_bytes(r.content)
                return None
            return r.content
        if file.type == "doc":
            return self._pull_doc_project_file_content(project_id, file.id).encode(
                "utf-8"
            )
        raise ValueError(f"Unknown file type: {file.type}")

    @overload
    def project_delete_entity(
        self, project_id: str, entity: ProjectFile | ProjectFolder
    ) -> None: ...

    @overload
    def project_delete_entity(
        self,
        project_id: str,
        entity: str,
        entity_type: Literal["file", "doc", "folder"],
    ) -> None: ...

    def project_delete_entity(
        self,
        project_id: str,
        entity: ProjectFile | ProjectFolder | str,
        entity_type: Literal["file", "doc", "folder"] | None = None,
    ) -> None:
        """Delete a file/folder/doc via `DELETE /project/{id}/{type}/{id}`.

        Args:
            project_id: The id of the project.
            entity: Either a `ProjectFile`/`ProjectFolder` (in which
                case `entity_type` is inferred), or the raw entity id
                as a string.
            entity_type: Required when `entity` is a string id; one of
                `"file"`, `"doc"`, `"folder"`.
        """
        if entity_type is None:
            assert isinstance(entity, ProjectFile | ProjectFolder)
            entity_type = entity.type
            entity = entity.id
        else:
            assert isinstance(entity, str)
        self._assert_session_initialized()
        r = self._get_session().delete(
            f"https://{self._host}/project/{project_id}/{entity_type}/{entity}",
            json={},
            **self._request_kwargs,
            headers={
                "Referer": f"https://{self._host}/project/{project_id}",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "x-csrf-token": self._get_csrf_token(project_id),
            },
        )
        r.raise_for_status()

    def login_from_browser(self) -> None:
        """Login to Overleaf using the default browser's cookies."""
        cookies = browsercookie.load()
        self.login_from_cookies(cookies)

    @overload
    def login_from_cookies(self, cookies: dict[str, str]) -> None: ...

    @overload
    def login_from_cookies(self, cookies: cookielib.CookieJar) -> None: ...

    def login_from_cookies(self, cookies: dict[str, str] | cookielib.CookieJar) -> None:
        """Install session cookies on the client.

        Accepts either a name->value dict (which is wrapped into a
        CookieJar scoped to `self._host`) or a CookieJar; only cookies
        matching the configured host are retained.

        Args:
            cookies: Either a dict of cookie name -> value or a
                `cookielib.CookieJar`.
        """
        dot_host = self._host
        if dot_host[:4] == "www.":
            dot_host = f".{self._host.removeprefix('www.')}"

        if not isinstance(cookies, cookielib.CookieJar):
            assert isinstance(cookies, dict)
            cookies_jar = cookielib.CookieJar()
            for name, value in cookies.items():
                cookies_jar.set_cookie(
                    requests.cookies.create_cookie(name, value, domain=dot_host)
                )
            cookies = cookies_jar

        assert isinstance(cookies, cookielib.CookieJar)
        self._cookies = cookielib.CookieJar()
        for cookie in cookies:
            if cookie.domain.endswith(dot_host):
                self._cookies.set_cookie(cookie)
        self._session_initialized = True

    def _pull_doc_project_file_content(self, project_id: str, file_id: str) -> str:
        text, _version, _ranges = self._pull_doc_joindoc_ack(project_id, file_id)
        return text

    def _pull_doc_snapshot(
        self, project_id: str, file_id: str
    ) -> "tuple[str, int, dict]":
        """Pull a doc's text, version, and ranges (tracked changes + comments).

        Sibling to `_pull_doc_project_file_content` that returns the full
        joinDoc ack payload. Versions and ranges are needed by the
        `snapshot` CLI command but not by the existing text-only callers.
        """
        return self._pull_doc_joindoc_ack(project_id, file_id)

    def _pull_doc_joindoc_ack(
        self, project_id: str, file_id: str
    ) -> "tuple[str, int, dict]":
        socket = None
        try:
            socket = self._open_socket(project_id)

            while True:
                line = socket.recv()
                if line.startswith("7:"):
                    raise RuntimeError("Could not get project files.")
                if line.startswith("5:"):
                    break
            socket.send(b'5:1+::{"name":"clientTracking.getConnectedUsers"}')

            socket.send(
                f'5:2+::{{"name": "joinDoc", "args":'
                f' ["{file_id}", {{"encodeRanges": true}}]}}'.encode()
            )
            while True:
                line = socket.recv()
                if line.startswith("7:"):
                    raise RuntimeError("Could not get project files.")
                if line.startswith("6:::2+"):
                    break
            data = line[6:]

            socket.send(f'5:3+::{{"name": "leaveDoc", "args": ["{file_id}"]}}'.encode())
            while True:
                line = socket.recv()
                if line.startswith("7:"):
                    raise RuntimeError("Could not get project files.")
                if line.startswith("6:::3+"):
                    break
        finally:
            if socket is not None:
                socket.close()
                socket = None

        from ._ot import decode_packed_utf8

        ack = json.loads(data)
        doc_lines = ack[1] if len(ack) > 1 else []
        version = int(ack[2]) if len(ack) > 2 and ack[2] is not None else 0
        ranges = ack[4] if len(ack) > 4 and isinstance(ack[4], dict) else {}
        text = "\n".join(decode_packed_utf8(line) for line in doc_lines)
        return text, version, ranges

    def _get_session(self) -> requests.Session:
        self._assert_session_initialized()
        http_session = requests.Session()
        http_session.cookies = self._cookies
        http_session.proxies = self._proxies
        http_session.verify = self._ssl_verify
        return http_session

    def _assert_session_initialized(self) -> None:
        if not self._session_initialized:
            raise RuntimeError("Must call api.login_*() before using the api")

    def _get_csrf_token(self, project_id: str) -> str:
        self._assert_session_initialized()
        # First we pull the csrf token
        if self._csrf_cache is not None and self._csrf_cache[0] == project_id:
            return self._csrf_cache[1]
        r = self._get_session().get(
            f"https://{self._host}/project/{project_id}", **self._request_kwargs
        )
        r.raise_for_status()
        content = BeautifulSoup(r.content, features="html.parser")
        token = content.find("meta", {"name": "ol-csrfToken"}).get("content")
        self._csrf_cache = (project_id, token)
        return token

    def _open_socket(self, project_id: str) -> Any:
        return _open_socket_impl(self, project_id)
