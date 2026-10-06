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

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        yield {
            "settings": settings,
            "registry": registry,
            "policy": policy,
            "artifacts": artifacts,
            "adapter_factory": adapter_factory,
            "adapter": adapter,
        }

    app = FastMCP(
        "mcp-gryster",
        instructions="Policy-controlled Pygrister operations and custom-widget authoring.",
        lifespan=lifespan,
        strict_input_validation=True,
        mask_error_details=True,
    )
    register_grist_tools(app, adapter)
    return app


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower().removesuffix(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
