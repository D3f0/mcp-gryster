"""Typed FastMCP tools and read-only resources for the widget workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

from mcp_gryster.errors import McpGrysterError
from mcp_gryster.grist.registry import MethodRegistry
from mcp_gryster.widget.fixtures import FixtureManager
from mcp_gryster.widget.models import (
    FileUpdate,
    FixtureSummary,
    GristFixtureRequest,
    PreviewFixture,
    ProjectSnapshot,
    WidgetMetadata,
)
from mcp_gryster.widget.packaging import PackageResult, WidgetPackager
from mcp_gryster.widget.paths import normalize_authored_path
from mcp_gryster.widget.preview import PreviewManager, PreviewSession, StopPreviewResult
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.validation import ValidationReport, WidgetValidator


def register_widget_tools(
    mcp: FastMCP,
    *,
    store: ProjectStore,
    fixtures: FixtureManager,
    validator: WidgetValidator,
    previews: PreviewManager,
    packager: WidgetPackager,
    registry: MethodRegistry,
) -> None:
    """Register the complete project-to-package widget authoring surface."""

    @mcp.tool(name="widget.projects.create")
    def create_project(metadata: WidgetMetadata) -> ProjectSnapshot:
        return store.create(metadata)

    @mcp.tool(name="widget.projects.list")
    def list_projects() -> tuple[ProjectSnapshot, ...]:
        return store.list()

    @mcp.tool(name="widget.projects.describe")
    def describe_project(project_id: str) -> ProjectSnapshot:
        return store.read(project_id)

    @mcp.tool(name="widget.projects.read")
    def read_project_file(project_id: str, path: str) -> str:
        return _read_authored_file(store, project_id, path).decode("utf-8")

    @mcp.tool(name="widget.projects.apply")
    def apply_project(
        project_id: str,
        expected_revision: str,
        files: tuple[FileUpdate, ...] = (),
        delete: tuple[str, ...] = (),
    ) -> ProjectSnapshot:
        return store.apply_files(project_id, expected_revision, files, delete)

    @mcp.tool(name="widget.fixtures.create")
    def create_fixture(project_id: str, fixture: PreviewFixture) -> FixtureSummary:
        return fixtures.create(project_id, fixture)

    @mcp.tool(name="widget.fixtures.from_grist")
    async def create_fixture_from_grist(
        project_id: str,
        expected_revision: str,
        fixture_id: str,
        doc_id: str,
        table_id: str,
        columns: tuple[str, ...],
        access_level: Literal["none", "read table", "full"],
        selected_record_id: int | str | None = None,
        mappings: dict[str, str | tuple[str, ...]] | None = None,
        options: dict[str, Any] | None = None,
        include_attachment_metadata: bool = False,
        team_id: str = "",
    ) -> FixtureSummary:
        request = GristFixtureRequest.model_validate(
            {
                "project_id": project_id,
                "expected_revision": expected_revision,
                "fixture_id": fixture_id,
                "doc_id": doc_id,
                "table_id": table_id,
                "columns": columns,
                "selected_record_id": selected_record_id,
                "mappings": mappings or {},
                "options": options or {},
                "access_level": access_level,
                "include_attachment_metadata": include_attachment_metadata,
                "team_id": team_id,
            }
        )
        return await fixtures.create_from_grist(request)

    @mcp.tool(name="widget.validate")
    def validate_widget(
        project_id: str, fixture_id: str | None = None
    ) -> ValidationReport:
        return validator.validate(project_id, fixture_id)

    @mcp.tool(name="widget.preview.start")
    async def start_preview(
        project_id: str,
        mode: Literal["fixture", "live"],
        fixture_id: str | None = None,
    ) -> PreviewSession:
        return await previews.start(project_id, mode, fixture_id)

    @mcp.tool(name="widget.preview.stop")
    async def stop_preview(session_id: str) -> StopPreviewResult:
        return await previews.stop(session_id)

    @mcp.tool(name="widget.package")
    def package_widget(
        project_id: str,
        expected_revision: str,
        public_base_url: str,
        fixture_id: str | None = None,
    ) -> PackageResult:
        return packager.package(
            project_id,
            public_base_url,
            expected_revision=expected_revision,
            fixture_id=fixture_id,
        )

    @mcp.resource(
        "widget://projects/{project_id}",
        name="widget-project",
        mime_type="application/json",
    )
    def project_resource(project_id: str) -> str:
        snapshot = store.read(project_id)
        safe = snapshot.model_copy(
            update={"files": FixtureManager.source_materialization_paths(snapshot.files)}
        )
        return _json(safe.model_dump(mode="json"))

    @mcp.resource(
        "widget://projects/{project_id}/files/{path*}",
        name="widget-project-file",
        mime_type="text/plain",
    )
    def project_file_resource(project_id: str, path: str) -> str:
        try:
            return _read_authored_file(store, project_id, path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise McpGrysterError(
                "VALIDATION_FAILED", "Widget project file is not valid UTF-8"
            ) from exc

    @mcp.resource(
        "widget://projects/{project_id}/validation{?fixture_id}",
        name="widget-validation",
        mime_type="application/json",
    )
    def validation_resource(project_id: str, fixture_id: str | None = None) -> str:
        return validator.validate(project_id, fixture_id).model_dump_json()

    @mcp.resource(
        "widget://dist/{name}",
        name="widget-dist",
        mime_type="application/octet-stream",
    )
    def dist_resource(name: str) -> bytes:
        return _read_dist(store, name)

    @mcp.resource(
        "widget://grist/methods",
        name="grist-method-registry",
        mime_type="application/json",
    )
    def method_registry_resource() -> str:
        return registry.model_dump_json()


def _read_authored_file(store: ProjectStore, project_id: str, path: str) -> bytes:
    normalized = normalize_authored_path(path)
    if normalized.startswith("fixtures/"):
        raise McpGrysterError("PATH_NOT_ALLOWED", "Widget project path is not allowed")
    snapshot = store.read(project_id)
    if normalized not in snapshot.files:
        raise McpGrysterError("PATH_NOT_ALLOWED", "Widget project path is not allowed")
    root = store.project_path(project_id) / "revisions" / snapshot.revision
    candidate = root.joinpath(*normalized.split("/"))
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError
        return candidate.read_bytes()
    except OSError as exc:
        raise McpGrysterError("PATH_NOT_ALLOWED", "Widget project path is not allowed") from exc


def _read_dist(store: ProjectStore, name: str) -> bytes:
    if not name or Path(name).name != name or name.startswith("."):
        raise McpGrysterError("PATH_NOT_ALLOWED", "Artifact path is not allowed")
    root = store.workspace / "artifacts" / "widget"
    path = root / name
    try:
        if root.is_symlink() or path.is_symlink() or not path.is_file():
            raise OSError
        return path.read_bytes()
    except OSError as exc:
        raise McpGrysterError("PATH_NOT_ALLOWED", "Artifact path is not allowed") from exc


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))

