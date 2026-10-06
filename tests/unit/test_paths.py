from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.paths import normalize_authored_path, validate_project_id


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/tmp/widget.json",
        r"C:\tmp\widget.json",
        r"\\server\share\widget.json",
        "src/../widget.json",
        "src//index.html",
        "src/index.html/",
        "src/./index.html",
        "src/bad\name.js",
        "src/bad\x00name.js",
        "src/bad\x1fname.js",
        "dist/index.html",
        ".widget-state.json",
        "fixtures/not_json.txt",
        "fixtures/a/nested.json",
    ],
)
def test_rejects_noncanonical_or_server_owned_authored_paths(path: str) -> None:
    with pytest.raises(McpGrysterError) as caught:
        normalize_authored_path(path)

    assert caught.value.code == "PATH_NOT_ALLOWED"


@pytest.mark.parametrize(
    "path",
    [
        "widget.json",
        "src/index.html",
        "src/assets/widget.js",
        "fixtures/sample-fixture.json",
    ],
)
def test_accepts_only_canonical_authored_paths(path: str) -> None:
    assert normalize_authored_path(path) == path


@pytest.mark.parametrize(
    "project_id",
    ["", "ab", "Upper", "has_underscore", "-leading", "a" * 64],
)
def test_rejects_invalid_project_ids(project_id: str) -> None:
    with pytest.raises(McpGrysterError) as caught:
        validate_project_id(project_id)

    assert caught.value.code == "INVALID_ARGUMENT"


def test_accepts_valid_project_id() -> None:
    assert validate_project_id("example-widget-1") == "example-widget-1"


def test_store_rejects_symlinked_workspace_ancestor(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")

    from mcp_gryster.widget.projects import ProjectStore

    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.symlink_to(outside, target_is_directory=True)

    with pytest.raises(McpGrysterError) as caught:
        ProjectStore(workspace)

    assert caught.value.code == "PATH_NOT_ALLOWED"
