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
        parent_fd: int | None = None
        temporary_name = f".{relative.name}.{secrets.token_hex(8)}.tmp"
        try:
            parent_fd = _open_parent(self._root, relative.parts[:-1])
            target_name = relative.name
            try:
                target_stat = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(target_stat.st_mode):
                    raise _path_error()

            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            path_stat = os.stat(target.parent, follow_symlinks=False)
            descriptor_stat = os.fstat(parent_fd)
            if (path_stat.st_dev, path_stat.st_ino) != (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ):
                raise _path_error()
            os.fsync(parent_fd)
        except McpGrysterError:
            raise
        except OSError as exc:
            raise McpGrysterError("PATH_NOT_ALLOWED", "Artifact path is not allowed") from exc
        finally:
            if parent_fd is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except OSError:
                    pass
                finally:
                    os.close(parent_fd)

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


def _open_parent(root: Path, parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    current_fd = os.open(root, flags)
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
                next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _path_error() -> McpGrysterError:
    return McpGrysterError("PATH_NOT_ALLOWED", "Artifact path is not allowed")
