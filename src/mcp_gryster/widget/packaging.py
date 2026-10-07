"""Deterministic deployment packaging for validated widget revisions."""

from __future__ import annotations

import io
import json
import secrets
import shutil
from pathlib import Path
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcp_gryster.artifacts import Artifact, ArtifactStore
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.fixtures import FixtureManager
from mcp_gryster.widget.models import WidgetMetadata
from mcp_gryster.widget.paths import validate_revision
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.validation import WidgetValidator

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o100644


class PackageResult(BaseModel):
    """Content-addressed result of packaging one immutable project revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    revision: str
    artifact: Artifact
    manifest: Artifact
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WidgetPackager:
    """Validate, source-materialize, and emit deterministic widget artifacts."""

    def __init__(
        self,
        store: ProjectStore,
        fixtures: FixtureManager,
        validator: WidgetValidator,
        artifacts: ArtifactStore,
    ) -> None:
        self._store = store
        self._fixtures = fixtures
        self._validator = validator
        self._artifacts = artifacts

    def package(
        self,
        project_id: str,
        public_base_url: str,
        *,
        expected_revision: str | None = None,
        fixture_id: str | None = None,
    ) -> PackageResult:
        """Package exactly one current, validated, successfully previewed revision."""
        snapshot = self._store.read(project_id)
        revision = snapshot.revision
        if expected_revision is not None and validate_revision(expected_revision) != revision:
            raise _revision_conflict(revision)
        report = self._validator.validate(project_id, fixture_id)
        if report.revision != revision:
            raise _revision_conflict(self._store.read(project_id).revision)
        if not report.packageable:
            raise McpGrysterError("PACKAGE_FAILED", "Widget project revision is not packageable")

        destination = self._store.workspace / f".package-{secrets.token_hex(16)}"
        try:
            self._fixtures.materialize_sources(project_id, revision, destination)
            current = self._store.read(project_id)
            if current.revision != revision:
                raise _revision_conflict(current.revision)
            metadata = _load_metadata(destination / "widget.json", project_id)
            archive_bytes = _archive_sources(destination / "src")
            manifest_bytes = _manifest(metadata, project_id, public_base_url)
            current = self._store.read(project_id)
            if current.revision != revision:
                raise _revision_conflict(current.revision)
            stem = f"{project_id}-{revision}"
            artifact = self._artifacts.put_bytes("widget", f"{stem}.zip", archive_bytes)
            manifest = self._artifacts.put_bytes("widget", f"{stem}.json", manifest_bytes)
        finally:
            shutil.rmtree(destination, ignore_errors=True)

        return PackageResult(
            project_id=project_id,
            revision=revision,
            artifact=artifact,
            manifest=manifest,
            sha256=artifact.sha256,
            manifest_sha256=manifest.sha256,
        )


def _load_metadata(path: Path, project_id: str) -> WidgetMetadata:
    try:
        raw = json.loads(path.read_bytes())
        if not isinstance(raw, dict):
            raise TypeError
        if raw.get("schemaVersion") != 1:
            raise ValueError
        return WidgetMetadata.model_validate(
            {
                "project_id": project_id,
                "widget_id": raw.get("widgetId"),
                "name": raw.get("name"),
                "description": raw.get("description", ""),
                "entrypoint": raw.get("entrypoint"),
                "access_level": raw.get("accessLevel"),
                "columns": raw.get("columns", []),
                "authors": raw.get("authors", []),
            }
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise McpGrysterError("PACKAGE_FAILED", "Widget metadata is invalid") from exc


def _archive_sources(source_root: Path) -> bytes:
    try:
        files = tuple(
            sorted(
                path
                for path in source_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
        )
    except OSError as exc:
        raise McpGrysterError("PACKAGE_FAILED", "Widget package could not be generated") from exc
    output = io.BytesIO()
    try:
        with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                relative = path.relative_to(source_root).as_posix()
                info = ZipInfo(relative, date_time=_FIXED_ZIP_TIME)
                info.compress_type = ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = _FILE_MODE << 16
                archive.writestr(info, path.read_bytes())
    except OSError as exc:
        raise McpGrysterError("PACKAGE_FAILED", "Widget package could not be generated") from exc
    return output.getvalue()


def _manifest(metadata: WidgetMetadata, project_id: str, public_base_url: str) -> bytes:
    base = _public_base_url(public_base_url)
    path = "/".join(
        (
            base.path.rstrip("/"),
            quote(project_id, safe=""),
            quote(metadata.entrypoint, safe="/"),
        )
    )
    url = urlunsplit((base.scheme, base.netloc, path, "", ""))
    data: dict[str, object] = {
        "accessLevel": metadata.access_level,
        "columns": [column.model_dump(by_alias=True) for column in metadata.columns],
        "description": metadata.description,
        "name": metadata.name,
        "published": True,
        "url": url,
        "widgetId": metadata.widget_id,
    }
    if metadata.authors:
        data["authors"] = [author.model_dump(exclude_none=True) for author in metadata.authors]
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _public_base_url(value: str) -> SplitResult:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise McpGrysterError("INVALID_ARGUMENT", "Public base URL is invalid")
    return parsed


def _revision_conflict(current_revision: str) -> McpGrysterError:
    return McpGrysterError(
        "REVISION_CONFLICT",
        "Project revision does not match",
        {"current_revision": current_revision},
    )
