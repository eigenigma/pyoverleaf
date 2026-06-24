"""WebSocket connection setup for Overleaf's Socket.IO 0.9 endpoint.

Pulled out of `_webapi.py` so the API surface module stays under the
project's per-file size budget. `open_socket()` is called via a delegating
`Api._open_socket` method.

The `api._*` accesses below are intentional: this module is an internal
collaborator of the `Api` class, which is deliberately split across sibling
modules to stay under the per-file size budget. The `# noqa: SLF001`
markers acknowledge that the `_`-prefix here means "not for external
callers", not "not for sibling modules".
"""

from __future__ import annotations

import ssl
import time
import urllib.parse
from base64 import b64encode
from typing import TYPE_CHECKING, Any

from websocket import create_connection

if TYPE_CHECKING:
    from requests import Session

    from ._webapi import Api


def _build_cookie_header(session: Session, dot_host: str) -> tuple[str, dict[str, str]]:
    """Build the cookie string + filtered HTTP headers for the upgrade request.

    Returns `(cookies, headers)` where `cookies` is the joined cookie header
    value (any pre-existing `Cookie` header on `session.headers` is folded
    into it) and `headers` is a fresh dict with the `Cookie` entry stripped.
    """
    cookies = "; ".join(
        f"{c.name}={c.value}" for c in session.cookies if c.domain.endswith(dot_host)
    )
    headers = dict(session.headers)
    for header, value in list(headers.items()):
        if header.lower() == "cookie":
            if cookies:
                cookies += "; "
            cookies += value
            del headers[header]
            break
    return cookies, headers


def _apply_basic_auth(session: Session, headers: dict[str, str]) -> None:
    """Inject a Basic-auth header into `headers` if `session.auth` is set."""
    if "Authorization" in headers or session.auth is None:
        return
    if not isinstance(session.auth, tuple):  # pragma: no cover
        raise ValueError("Only basic authentication is supported")  # noqa: TRY004
    raw = f"{session.auth[0]}:{session.auth[1]}".encode()
    headers["Authorization"] = "Basic " + b64encode(raw).decode("utf-8")


def _client_cert_sslopt(session: Session) -> dict[str, Any] | None:
    """Translate `session.cert` into a websocket-client `sslopt` fragment."""
    if isinstance(session.cert, tuple):
        return {"certfile": session.cert[0], "keyfile": session.cert[1]}
    if session.cert:
        return {"certfile": session.cert}
    return None


def _proxy_kwargs(session: Session, socket_url: str) -> dict[str, Any]:
    """Translate `session.proxies` into websocket-client `http_proxy_*` kwargs."""
    if not session.proxies:
        return {}
    if socket_url.startswith("ws://"):
        proxy_url = session.proxies.get("ws", session.proxies.get("http"))
    else:
        proxy_url = session.proxies.get("wss", session.proxies.get("https"))
    if not proxy_url:
        return {}
    parsed = urllib.parse.urlparse(
        proxy_url if "://" in proxy_url else "scheme://" + proxy_url
    )
    return {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
        "http_proxy_auth": (
            (parsed.username, parsed.password)
            if parsed.username or parsed.password
            else None
        ),
    }


def _merge_tls_verify(session: Session, kwargs: dict[str, Any]) -> None:
    """Fold `session.verify` into `kwargs['sslopt']` (created if absent)."""
    if isinstance(session.verify, str):
        kwargs.setdefault("sslopt", {})["ca_certs"] = session.verify
    elif not session.verify:
        kwargs["sslopt"] = {"cert_reqs": ssl.CERT_NONE}


def open_socket(api: Api, project_id: str) -> Any:
    """Open an authenticated Socket.IO 0.9 websocket to the Overleaf endpoint.

    Handshakes via HTTP, then upgrades to WebSocket carrying the session
    cookie. Returns the websocket-client connection.
    """
    api._assert_session_initialized()  # noqa: SLF001
    time_now = int(time.time() * 1000)
    session = api._get_session()  # noqa: SLF001
    host = api._host  # noqa: SLF001
    request_kwargs: dict[str, Any] = api._request_kwargs  # noqa: SLF001
    r = session.get(
        f"https://{host}/socket.io/1/?projectId={project_id}&t={time_now}",
        **request_kwargs,
    )
    r.raise_for_status()
    socket_id = r.content.decode("utf-8").split(":")[0]
    socket_url = (
        f"wss://{host}/socket.io/1/websocket/{socket_id}?projectId={project_id}"
    )

    dot_host = f".{host.removeprefix('www.')}"
    cookies, headers = _build_cookie_header(session, dot_host)
    _apply_basic_auth(session, headers)

    kwargs: dict[str, Any] = {}
    cert_sslopt = _client_cert_sslopt(session)
    if cert_sslopt is not None:
        kwargs["sslopt"] = cert_sslopt
    kwargs.update(_proxy_kwargs(session, socket_url))
    _merge_tls_verify(session, kwargs)

    kwargs["header"] = headers
    kwargs["cookie"] = cookies
    kwargs["enable_multithread"] = True
    if "timeout" in request_kwargs:
        kwargs["timeout"] = request_kwargs["timeout"]
    return create_connection(socket_url, **kwargs)
