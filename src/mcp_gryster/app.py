"""FastMCP server composition for mcp-gryster."""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from pygrister.api import GristApi  # type: ignore[import-untyped]

from mcp_gryster.artifacts import ArtifactStore
from mcp_gryster.audit import JsonLineAuditSink
from mcp_gryster.config import Settings, SettingsError
from mcp_gryster.grist.adapter import GristAdapter
from mcp_gryster.grist.policy import Policy
from mcp_gryster.grist.registry import load_registry
from mcp_gryster.grist.tools import register_grist_tools
from mcp_gryster.widget.fixtures import FixtureManager
from mcp_gryster.widget.packaging import WidgetPackager
from mcp_gryster.widget.preview import PreviewManager
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.tools import register_widget_tools
from mcp_gryster.widget.validation import PreviewEvidenceStore, WidgetValidator


def create_app(settings: Settings) -> FastMCP:
    """Create the strict, policy-controlled MCP server for validated settings."""
    if settings.transport == "streamable-http" and not _is_loopback(settings.host):
        raise SettingsError(
            "non-loopback Streamable HTTP is unavailable because bearer authentication "
            "cannot be safely enforced"
        )

    registry = load_registry()
    registry.assert_compatible(GristApi)
    policy = Policy(settings.policy)
    artifacts = ArtifactStore(settings.workspace_root / "artifacts")
    audit = JsonLineAuditSink(settings.workspace_root / "audit.jsonl")

    def adapter_factory() -> GristAdapter:
        return GristAdapter(registry, policy, artifacts, settings, audit)

    adapter = adapter_factory()
    project_store = ProjectStore(settings)
    preview_evidence = PreviewEvidenceStore()
    preview_manager = PreviewManager(project_store, preview_evidence)
    fixture_manager = FixtureManager(project_store, adapter, settings)
    widget_validator = WidgetValidator(project_store, preview_evidence)
    widget_packager = WidgetPackager(
        project_store,
        fixture_manager,
        widget_validator,
        artifacts,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {
                "settings": settings,
                "registry": registry,
                "policy": policy,
                "artifacts": artifacts,
                "adapter_factory": adapter_factory,
                "adapter": adapter,
                "project_store": project_store,
                "preview_evidence": preview_evidence,
                "preview_manager": preview_manager,
                "fixture_manager": fixture_manager,
                "widget_validator": widget_validator,
                "widget_packager": widget_packager,
            }
        finally:
            await preview_manager.stop_all()

    app = FastMCP(
        "mcp-gryster",
        instructions="Policy-controlled Pygrister operations and custom-widget authoring.",
        lifespan=lifespan,
        strict_input_validation=True,
        mask_error_details=True,
    )
    register_grist_tools(app, adapter)
    register_widget_tools(
        app,
        store=project_store,
        fixtures=fixture_manager,
        validator=widget_validator,
        previews=preview_manager,
        packager=widget_packager,
        registry=registry,
    )
    return app


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower().removesuffix(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
