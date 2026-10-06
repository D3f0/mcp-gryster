"""Safe, durable storage for generated binary artifacts."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mcp_gryster.errors import McpGrysterError


class Artifact(BaseModel):
    """Description of a stored artifact."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: Path
    size_bytes: int = Field(ge=0)


class ArtifactStore:
    """Store artifacts beneath one canonical root without following symlinks."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._root.is_dir():
            raise McpGrysterError("PATH_NOT_ALLOWED", "Artifact root is not a directory")

    def put_bytes(self, kind: str, name: str, data: bytes) -> Artifact:
        """Atomically and durably write bytes under ``root/kind/name``."""
        relative = _validated_relative_path(kind, name)
        target = self._root.joinpath(relative)
        parent = target.parent
        _create_safe_directories(self._root, parent)
        _ensure_contained(self._root, parent)
        _ensure_no_symlink_components(self._root, parent)
        if target.is_symlink():
            raise _path_error()

        temporary = parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _ensure_no_symlink_components(self._root, parent)
            if target.is_symlink():
                raise _path_error()
            os.replace(temporary, target)
            _fsync_directory(parent)
        except McpGrysterError:
            raise
        except OSError as exc:
            raise McpGrysterError("PATH_NOT_ALLOWED", "Artifact path is not allowed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

        return Artifact(kind=kind, name=name, path=target, size_bytes=len(data))


def _validated_relative_path(kind: str, name: str) -> Path:
    for value in (kind, name):
        path = Path(value)
        if (
            not value
            or path.is_absolute()
            or bool(path.anchor)
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise _path_error()
    return Path(kind) / Path(name)


def _create_safe_directories(root: Path, parent: Path) -> None:
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise _path_error() from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise _path_error()


def _ensure_contained(root: Path, path: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise _path_error() from exc


def _ensure_no_symlink_components(root: Path, parent: Path) -> None:
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise _path_error()
        except OSError as exc:
            raise _path_error() from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _path_error() -> McpGrysterError:
    return McpGrysterError("PATH_NOT_ALLOWED", "Artifact path is not allowed")
