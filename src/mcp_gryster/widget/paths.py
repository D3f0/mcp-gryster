"""Validation for authored project paths and identifiers."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

from mcp_gryster.errors import McpGrysterError

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_FIXTURE_FILE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.json$")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[a-zA-Z]:[\\/]|\\\\)")
_REVISION = re.compile(r"^[0-9a-f]{64}$")


def validate_project_id(project_id: str) -> str:
    """Return a project ID only when it is safe as one directory component."""
    if _PROJECT_ID.fullmatch(project_id) is None:
        raise McpGrysterError("INVALID_ARGUMENT", "Project ID is invalid")
    return project_id


def validate_revision(revision: str) -> str:
    """Validate an exact lowercase SHA-256 revision identifier."""
    if _REVISION.fullmatch(revision) is None:
        raise McpGrysterError("INVALID_ARGUMENT", "Project revision is invalid")
    return revision


def normalize_authored_path(value: str) -> str:
    """Validate and return the canonical POSIX spelling of an authored file path."""
    if (
        not value
        or value.startswith("/")
        or _WINDOWS_ABSOLUTE.match(value)
        or "\\" in value
        or "//" in value
        or value.endswith("/")
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise _path_error()

    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise _path_error()
    if path.as_posix() != value:
        raise _path_error()

    parts = path.parts
    allowed = value == "widget.json"
    if len(parts) >= 2 and parts[0] == "src":
        allowed = True
    elif len(parts) == 2 and parts[0] == "fixtures":
        allowed = _FIXTURE_FILE.fullmatch(parts[1]) is not None
    if not allowed:
        raise _path_error()
    return path.as_posix()


def _path_error() -> McpGrysterError:
    return McpGrysterError("PATH_NOT_ALLOWED", "Widget project path is not allowed")
