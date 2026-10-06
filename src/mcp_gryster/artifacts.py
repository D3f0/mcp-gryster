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
        if not _supports_dir_fd():
            return self._put_bytes_portable(kind, name, relative, data)
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

    def _put_bytes_portable(
        self, kind: str, name: str, relative: Path, data: bytes
    ) -> Artifact:
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
            _ensure_contained(self._root, parent)
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


def _supports_dir_fd() -> bool:
    return (
        os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )


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
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


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
