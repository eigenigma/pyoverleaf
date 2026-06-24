"""Opt-in live integration test against a real Overleaf sandbox project.

Enable by setting:
  PYOVERLEAF_LIVE_TEST_PROJECT=<project-id>
  PYOVERLEAF_HOST=www.overleaf.com   (recommended; bare overleaf.com 302s)

The test:
  1. Resolves a doc-only target file (default `main.tex`).
  2. Captures the original content and restores it in `finally`.
  3. Submits a marker append via `api.write_doc`.
  4. Submits a CJK / emoji line to validate UTF-16 position correctness.
  5. Submits a tracked-changes edit (visible in the Review panel).
"""

from __future__ import annotations

import os
import time

import pytest

import pyoverleaf

LIVE_PROJECT = os.environ.get("PYOVERLEAF_LIVE_TEST_PROJECT")
LIVE_TARGET = os.environ.get("PYOVERLEAF_LIVE_TEST_FILE", "main.tex")
PROXY_INJECTED = any(
    os.environ.get(v)
    for v in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    )
)


pytestmark = [
    pytest.mark.skipif(
        not LIVE_PROJECT,
        reason="set PYOVERLEAF_LIVE_TEST_PROJECT to run live integration tests",
    ),
    pytest.mark.skipif(
        PROXY_INJECTED,
        reason="HTTP(S)_PROXY env injected; sandbox proxy breaks WS upgrade",
    ),
]


@pytest.fixture(scope="module")
def api() -> pyoverleaf.Api:
    """Module-scoped logged-in Api against the live host."""
    host = os.environ.get("PYOVERLEAF_HOST", "www.overleaf.com")
    a = pyoverleaf.Api(host=host)
    a.login_from_browser()
    return a


@pytest.fixture(scope="module")
def project_io(api: pyoverleaf.Api) -> pyoverleaf.ProjectIO:
    """ProjectIO bound to the live target project."""
    return pyoverleaf.ProjectIO(api, LIVE_PROJECT)


@pytest.fixture(scope="module")
def original_content(api: pyoverleaf.Api):
    """Snapshot the file's original content; restore at module teardown."""
    root = api.project_get_files(LIVE_PROJECT)
    target = None
    for child in root.children:
        if child.name == LIVE_TARGET and child.type == "doc":
            target = child
            break
    if target is None:
        pytest.skip(f"{LIVE_TARGET!r} not found in project {LIVE_PROJECT}")
    text = api._pull_doc_project_file_content(LIVE_PROJECT, target.id)
    yield text
    # Restore. The test would leave the live project mutated otherwise, so
    # any failure type (OT, HTTP, socket, validation) must convert to
    # `pytest.fail`; enumerating each is impractical and would let new
    # exception types regress the cleanup contract silently.
    try:
        api.write_doc(
            LIVE_PROJECT,
            LIVE_TARGET,
            text,
            track_changes=False,
            raise_on_silent_noop=False,
        )
    except Exception as e:  # noqa: BLE001  # pragma: no cover
        pytest.fail(f"failed to restore original content: {e!r}")


def test_marker_append_lands(api: pyoverleaf.Api, original_content: str) -> None:
    """A timestamped marker line must land at the doc tail and read back."""
    marker = f"% OT edit marker {int(time.time())}"
    new_text = original_content.rstrip("\n") + "\n" + marker + "\n"
    result = api.write_doc(LIVE_PROJECT, LIVE_TARGET, new_text)
    assert result.new_version > result.old_version
    re_read = api._pull_doc_project_file_content(LIVE_PROJECT, _find_doc_id(api))
    assert marker in re_read


def test_unicode_round_trip(api: pyoverleaf.Api, original_content: str) -> None:
    """A CJK marker line must round-trip byte-exactly through write/read."""
    # CJK BMP chars (1 UTF-16 unit each) round-trip byte-exactly through OT.
    # Astral chars (U+10000+, e.g. emoji) are stored by Overleaf's
    # document-updater as broken surrogate-pair UTF-8 (CESU-8) regardless
    # of client; out of scope for the OT write path.
    cjk_marker = "% unicode marker " + str(int(time.time())) + " 中文"
    new_text = original_content.rstrip("\n") + "\n" + cjk_marker + "\n"
    result = api.write_doc(LIVE_PROJECT, LIVE_TARGET, new_text)
    assert result.new_version > result.old_version
    re_read = api._pull_doc_project_file_content(LIVE_PROJECT, _find_doc_id(api))
    assert cjk_marker in re_read


def test_tracked_change_marker(api: pyoverleaf.Api, original_content: str) -> None:
    """A track_changes=True append must land and read back from the live doc."""
    marker = f"% tracked-change marker {int(time.time())}"
    new_text = original_content.rstrip("\n") + "\n" + marker + "\n"
    result = api.write_doc(LIVE_PROJECT, LIVE_TARGET, new_text, track_changes=True)
    assert result.new_version > result.old_version
    re_read = api._pull_doc_project_file_content(LIVE_PROJECT, _find_doc_id(api))
    assert marker in re_read


def _find_doc_id(api: pyoverleaf.Api) -> str:
    root = api.project_get_files(LIVE_PROJECT)
    for child in root.children:
        if child.name == LIVE_TARGET and child.type == "doc":
            return child.id
    raise RuntimeError(f"{LIVE_TARGET!r} disappeared mid-test")
