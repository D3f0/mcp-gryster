"""Atomic immutable storage for authored widget project revisions."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import time
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, overload

from mcp_gryster.config import Settings
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.models import FileUpdate, ProjectSnapshot, WidgetMetadata
from mcp_gryster.widget.paths import (
    normalize_authored_path,
    validate_project_id,
    validate_revision,
)

_STARTER_INDEX = b"<!doctype html>\n<html><body></body></html>\n"
_STATE_DIRECTORY = ".mcp-gryster"
_LOCK_ACQUIRE_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.01


class ProjectStore:
    """Manage complete immutable authored trees behind one atomic revision pointer."""

    @overload
    def __init__(
        self,
        workspace: Settings,
        *,
        max_file_bytes: int | None = None,
        max_files: int = 256,
        max_aggregate_bytes: int | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self,
        workspace: str | Path,
        *,
        max_file_bytes: int | None = None,
        max_files: int = 256,
        max_aggregate_bytes: int | None = None,
    ) -> None: ...

    def __init__(
        self,
        workspace: str | Path | Settings,
        *,
        max_file_bytes: int | None = None,
        max_files: int = 256,
        max_aggregate_bytes: int | None = None,
    ) -> None:
        if isinstance(workspace, Settings):
            workspace_path: str | Path = workspace.workspace_root
            if max_file_bytes is None:
                max_file_bytes = workspace.max_file_bytes
            if max_aggregate_bytes is None:
                max_aggregate_bytes = workspace.max_aggregate_bytes
        else:
            workspace_path = workspace
        if max_file_bytes is None:
            max_file_bytes = 5 * 1024 * 1024
        if max_aggregate_bytes is None:
            max_aggregate_bytes = 25 * 1024 * 1024
        if min(max_file_bytes, max_files, max_aggregate_bytes) <= 0:
            raise ValueError("project limits must be positive")
        requested = Path(workspace_path).expanduser().absolute()
        _reject_symlink_components(requested)
        try:
            requested.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise _path_error() from exc
        _require_directory(requested)
        self._workspace = requested.resolve(strict=True)
        self._root = self._workspace / _STATE_DIRECTORY / "projects"
        _safe_mkdir_tree(self._workspace, self._root)
        self._max_file_bytes = max_file_bytes
        self._max_files = max_files
        self._max_aggregate_bytes = max_aggregate_bytes
        self._lock = RLock()
        self._recover_staging()

    @property
    def workspace(self) -> Path:
        """Return the canonical workspace used to validate internal destinations."""
        return self._workspace

    def project_path(self, project_id: str) -> Path:
        """Return the internal path for a validated project identifier."""
        return self._root / validate_project_id(project_id)

    def create(self, metadata: WidgetMetadata | Mapping[str, Any]) -> ProjectSnapshot:
        """Create a project with canonical metadata and a starter HTML entrypoint."""
        if not isinstance(metadata, WidgetMetadata):
            metadata = WidgetMetadata.model_validate(metadata)
        project_id = validate_project_id(metadata.project_id)
        entrypoint = normalize_authored_path(f"src/{metadata.entrypoint}")
        files = {
            entrypoint: _STARTER_INDEX,
            "widget.json": metadata.to_widget_json(),
        }
        self._validate_file_set(files)
        project = self.project_path(project_id)
        _safe_mkdir_tree(self._root, project)
        with self._lock, _project_lock(project):
            _safe_mkdir_tree(project, project / "revisions")
            pointer = project / "current.json"
            try:
                pointer_mode = pointer.lstat().st_mode
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(pointer_mode) or stat.S_ISREG(pointer_mode):
                    raise McpGrysterError("INVALID_ARGUMENT", "Widget project already exists")
                raise _path_error()
            revision = _revision_digest(files)
            self._commit_revision(project, revision, files, previous=None)
            self._replace_pointer(project, revision)
            return self._snapshot(project_id, revision, tuple(sorted(files)))

    def read(self, project_id: str) -> ProjectSnapshot:
        """Read the visible snapshot after validating its pointer and immutable tree."""
        project_id = validate_project_id(project_id)
        with self._lock:
            project = self.project_path(project_id)
            revision = self._read_pointer(project)
            files = self._read_revision(project, revision)
            return self._snapshot(project_id, revision, tuple(sorted(files)))

    def list(self) -> tuple[ProjectSnapshot, ...]:
        """List valid visible projects in deterministic identifier order."""
        with self._lock:
            snapshots: list[ProjectSnapshot] = []
            try:
                entries = sorted(self._root.iterdir(), key=lambda entry: entry.name)
            except OSError as exc:
                raise _path_error() from exc
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                try:
                    validate_project_id(entry.name)
                except McpGrysterError:
                    continue
                if not (entry / "current.json").exists():
                    continue
                snapshots.append(self.read(entry.name))
            return tuple(snapshots)

    def apply_files(
        self,
        project_id: str,
        expected_revision: str,
        files: Sequence[FileUpdate] | Mapping[str, bytes],
        delete: Sequence[str],
    ) -> ProjectSnapshot:
        """Atomically replace a validated batch against exactly one visible revision."""
        project_id = validate_project_id(project_id)
        expected_revision = validate_revision(expected_revision)
        updates = _normalize_updates(files)
        deletions = tuple(normalize_authored_path(path) for path in delete)
        if len(set(deletions)) != len(deletions):
            raise McpGrysterError("INVALID_ARGUMENT", "Delete paths must be unique")
        overlap = set(updates).intersection(deletions)
        if overlap:
            raise McpGrysterError("INVALID_ARGUMENT", "Update and delete paths conflict")

        project = self.project_path(project_id)
        with self._lock, _project_lock(project):
            current_revision = self._read_pointer(project)
            if current_revision != expected_revision:
                raise McpGrysterError(
                    "REVISION_CONFLICT",
                    "Project revision does not match",
                    {"current_revision": current_revision},
                )
            current = self._read_revision(project, current_revision)
            resulting = dict(current)
            resulting.update(updates)
            for path in deletions:
                resulting.pop(path, None)
            self._validate_file_set(resulting)
            revision = _revision_digest(resulting)
            if revision == current_revision:
                return self._snapshot(project_id, revision, tuple(sorted(resulting)))
            self._commit_revision(
                project,
                revision,
                resulting,
                previous=(current_revision, current),
            )
            self._replace_pointer(project, revision)
            return self._snapshot(project_id, revision, tuple(sorted(resulting)))

    @contextmanager
    def hold_current_revision(
        self, project_id: str, expected_revision: str
    ) -> Generator[None, None, None]:
        """Hold the project advisory lock while an exact visible revision is committed."""
        project_id = validate_project_id(project_id)
        expected_revision = validate_revision(expected_revision)
        project = self.project_path(project_id)
        with self._lock, _project_lock(project):
            current_revision = self._read_pointer(project)
            if current_revision != expected_revision:
                raise McpGrysterError(
                    "REVISION_CONFLICT",
                    "Project revision does not match",
                    {"current_revision": current_revision},
                )
            yield

    def materialize(
        self,
        project_id: str,
        revision: str,
        destination: str | Path,
        *,
        include_paths: Sequence[str] | None = None,
    ) -> None:
        """Copy all or an explicit trusted file selection from one validated revision."""
        project_id = validate_project_id(project_id)
        revision = validate_revision(revision)
        destination_path = _canonical_destination(Path(destination).expanduser())
        with self._lock:
            project = self.project_path(project_id)
            current_revision = self._read_pointer(project)
            if revision != current_revision:
                raise McpGrysterError(
                    "REVISION_CONFLICT",
                    "Project revision does not match",
                    {"current_revision": current_revision},
                )
            files = self._read_revision(project, revision)
            if include_paths is not None:
                normalized_paths = tuple(normalize_authored_path(path) for path in include_paths)
                if len(normalized_paths) != len(set(normalized_paths)):
                    raise McpGrysterError("INVALID_ARGUMENT", "Materialization paths must be unique")
                if any(path not in files for path in normalized_paths):
                    raise McpGrysterError(
                        "INVALID_ARGUMENT", "Materialization path is not in the project revision"
                    )
                files = {path: files[path] for path in normalized_paths}
            self._validate_destination(destination_path)
            if destination_path.exists() or destination_path.is_symlink():
                raise _path_error()
            _safe_mkdir_tree(self._workspace, destination_path.parent)
            self._validate_destination(destination_path)
            staging = destination_path.parent / (
                f".{destination_path.name}.materialize-{secrets.token_hex(8)}"
            )
            try:
                staging.mkdir(mode=0o700)
                for authored_path, content in files.items():
                    target = staging.joinpath(*authored_path.split("/"))
                    _safe_mkdir_tree(staging, target.parent)
                    _write_new_file(target, content)
                _fsync_tree(staging)
                self._validate_destination(destination_path)
                if destination_path.exists() or destination_path.is_symlink():
                    raise _path_error()
                os.replace(staging, destination_path)
                _fsync_directory(destination_path.parent)
            except McpGrysterError:
                raise
            except OSError as exc:
                raise _path_error() from exc
            finally:
                _remove_tree_no_follow(staging)

    def _validate_destination(self, destination: Path) -> None:
        try:
            destination.relative_to(self._workspace)
        except ValueError as exc:
            raise _path_error() from exc
        try:
            destination.relative_to(self._root)
        except ValueError:
            pass
        else:
            raise _path_error()
        _reject_symlink_components(destination.parent)

    def _validate_file_set(self, files: Mapping[str, bytes]) -> None:
        if len(files) > self._max_files:
            raise _limit_error()
        total = 0
        for path, content in files.items():
            normalize_authored_path(path)
            if not isinstance(content, bytes):
                raise McpGrysterError("INVALID_ARGUMENT", "File content must be bytes")
            if len(content) > self._max_file_bytes:
                raise _limit_error()
            total += len(content)
            if total > self._max_aggregate_bytes:
                raise _limit_error()

    def _commit_revision(
        self,
        project: Path,
        revision: str,
        files: Mapping[str, bytes],
        *,
        previous: tuple[str, Mapping[str, bytes]] | None,
    ) -> None:
        revisions = project / "revisions"
        final = revisions / revision
        if final.exists() or final.is_symlink():
            if final.is_symlink() or not final.is_dir():
                raise McpGrysterError("VALIDATION_FAILED", "Stored project revision is invalid")
            if _revision_digest(self._read_tree(final)) != revision:
                raise McpGrysterError("VALIDATION_FAILED", "Stored project revision is invalid")
            return
        staging = revisions / f".staging-{secrets.token_hex(16)}"
        previous_root = revisions / previous[0] if previous is not None else None
        previous_files = previous[1] if previous is not None else {}
        try:
            staging.mkdir(mode=0o700)
            for authored_path in sorted(files):
                target = staging.joinpath(*authored_path.split("/"))
                _safe_mkdir_tree(staging, target.parent)
                linked = False
                if (
                    previous_root is not None
                    and previous_files.get(authored_path) == files[authored_path]
                ):
                    source = previous_root.joinpath(*authored_path.split("/"))
                    _require_regular_file(source)
                    try:
                        os.link(source, target, follow_symlinks=False)
                    except OSError:
                        try:
                            target.unlink(missing_ok=True)
                        except OSError:
                            pass
                    else:
                        linked = True
                if not linked:
                    _write_new_file(target, files[authored_path])
            if _revision_digest(self._read_tree(staging)) != revision:
                raise McpGrysterError("VALIDATION_FAILED", "Staged project revision is invalid")
            _fsync_tree(staging)
            os.replace(staging, final)
            _fsync_directory(revisions)
        finally:
            _remove_tree_no_follow(staging)

    def _replace_pointer(self, project: Path, revision: str) -> None:
        validate_revision(revision)
        temporary = project / f".current-{secrets.token_hex(16)}.tmp"
        payload = (json.dumps({"revision": revision}, sort_keys=True) + "\n").encode()
        try:
            _write_new_file(temporary, payload)
            os.replace(temporary, project / "current.json")
            _fsync_directory(project)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_pointer(self, project: Path) -> str:
        pointer = project / "current.json"
        try:
            _require_regular_file(pointer)
            raw = _read_regular_file(pointer)
            parsed = json.loads(raw)
        except McpGrysterError as exc:
            if exc.code == "PROJECT_NOT_FOUND":
                raise McpGrysterError(
                    "PROJECT_NOT_FOUND", "Widget project was not found"
                ) from exc
            raise McpGrysterError(
                "VALIDATION_FAILED", "Project state pointer is invalid"
            ) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpGrysterError(
                "VALIDATION_FAILED", "Project state pointer is invalid"
            ) from exc
        if not isinstance(parsed, dict) or set(parsed) != {"revision"}:
            raise McpGrysterError("VALIDATION_FAILED", "Project state pointer is invalid")
        revision = parsed["revision"]
        if not isinstance(revision, str):
            raise McpGrysterError("VALIDATION_FAILED", "Project state pointer is invalid")
        try:
            return validate_revision(revision)
        except McpGrysterError as exc:
            raise McpGrysterError(
                "VALIDATION_FAILED", "Project state pointer is invalid"
            ) from exc

    def _read_revision(self, project: Path, revision: str) -> dict[str, bytes]:
        revision = validate_revision(revision)
        root = project / "revisions" / revision
        files = self._read_tree(root)
        if _revision_digest(files) != revision:
            raise McpGrysterError("VALIDATION_FAILED", "Stored project revision is invalid")
        self._validate_file_set(files)
        return files

    def _read_tree(self, root: Path) -> dict[str, bytes]:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            root_descriptor = os.open(root, flags)
        except FileNotFoundError as exc:
            raise McpGrysterError(
                "PROJECT_NOT_FOUND", "Widget project was not found"
            ) from exc
        except OSError as exc:
            raise _path_error() from exc
        try:
            if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
                raise _path_error()
            files: dict[str, bytes] = {}
            _read_directory(root_descriptor, (), files)
            return files
        finally:
            os.close(root_descriptor)

    def _snapshot(
        self, project_id: str, revision: str, files: tuple[str, ...]
    ) -> ProjectSnapshot:
        return ProjectSnapshot(project_id=project_id, revision=revision, files=files)

    def _recover_staging(self) -> None:
        try:
            projects = tuple(self._root.iterdir())
        except OSError as exc:
            raise _path_error() from exc
        for project in projects:
            if project.is_symlink() or not project.is_dir():
                continue
            revisions = project / "revisions"
            if revisions.is_symlink() or not revisions.is_dir():
                continue
            for candidate in revisions.iterdir():
                if candidate.name.startswith(".staging-"):
                    _remove_tree_no_follow(candidate)


@contextmanager
def _project_lock(project: Path) -> Generator[None, None, None]:
    lock = project / ".write.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise _path_error() from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _path_error()
        deadline = time.monotonic() + _LOCK_ACQUIRE_SECONDS
        while not _try_advisory_lock(descriptor):
            if time.monotonic() >= deadline:
                raise McpGrysterError("REVISION_CONFLICT", "Project update is busy")
            time.sleep(_LOCK_RETRY_SECONDS)
        try:
            yield
        finally:
            _release_advisory_lock(descriptor)
    finally:
        os.close(descriptor)


def _try_advisory_lock(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(  # type: ignore[attr-defined]
                descriptor,
                msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                1,
            )
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release_advisory_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(  # type: ignore[attr-defined]
            descriptor,
            msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
            1,
        )
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _canonical_destination(destination: Path) -> Path:
    if ".." in destination.parts:
        raise _path_error()
    absolute = destination if destination.is_absolute() else Path.cwd() / destination
    _reject_symlink_components(absolute.parent)
    try:
        parent = absolute.parent.resolve(strict=False)
    except OSError as exc:
        raise _path_error() from exc
    return parent / absolute.name


def _normalize_updates(
    updates: Sequence[FileUpdate] | Mapping[str, bytes],
) -> dict[str, bytes]:
    items: Iterable[tuple[str, bytes]]
    if isinstance(updates, Mapping):
        items = updates.items()
    else:
        items = ((update.path, update.content) for update in updates)
    normalized: dict[str, bytes] = {}
    for path, content in items:
        canonical = normalize_authored_path(path)
        if canonical in normalized:
            raise McpGrysterError("INVALID_ARGUMENT", "Update paths must be unique")
        normalized[canonical] = content
    return normalized


def _revision_digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        encoded_path = path.encode()
        content = files[path]
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _read_directory(
    directory_descriptor: int,
    relative_parts: tuple[str, ...],
    files: dict[str, bytes],
) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = flags
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    try:
        names = sorted(os.listdir(directory_descriptor))
        for name in names:
            if (
                name in {"", ".", ".."}
                or "/" in name
                or "\\" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
            ):
                raise _path_error()
            entry_stat = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if stat.S_ISLNK(entry_stat.st_mode):
                raise _path_error()
            if stat.S_ISDIR(entry_stat.st_mode):
                child_descriptor = os.open(name, directory_flags, dir_fd=directory_descriptor)
                try:
                    _read_directory(child_descriptor, (*relative_parts, name), files)
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise _path_error()
            file_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
            try:
                if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                    raise _path_error()
                relative = "/".join((*relative_parts, name))
                canonical = normalize_authored_path(relative)
                with os.fdopen(file_descriptor, "rb", closefd=False) as stream:
                    files[canonical] = stream.read()
            finally:
                os.close(file_descriptor)
    except McpGrysterError:
        raise
    except OSError as exc:
        raise _path_error() from exc


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise _path_error()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _write_new_file(path: Path, content: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _safe_mkdir_tree(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise _path_error() from exc
    current = root
    _require_directory(current)
    for part in relative.parts:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _require_directory(current)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            if current.exists() or current.is_symlink():
                raise _path_error()
            continue
        except OSError as exc:
            raise _path_error() from exc
        if stat.S_ISLNK(mode):
            raise _path_error()


def _require_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise McpGrysterError("PROJECT_NOT_FOUND", "Widget project was not found") from exc
    except OSError as exc:
        raise _path_error() from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise _path_error()


def _require_regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise McpGrysterError("PROJECT_NOT_FOUND", "Widget project was not found") from exc
    except OSError as exc:
        raise _path_error() from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise _path_error()


def _fsync_tree(root: Path) -> None:
    for directory, _, _ in os.walk(root, topdown=False, followlinks=False):
        _fsync_directory(Path(directory))


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree_no_follow(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError:
        return
    try:
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
    except OSError:
        pass


def _path_error() -> McpGrysterError:
    return McpGrysterError("PATH_NOT_ALLOWED", "Widget project path is not allowed")


def _limit_error() -> McpGrysterError:
    return McpGrysterError("LIMIT_EXCEEDED", "Widget project exceeds configured limits")
