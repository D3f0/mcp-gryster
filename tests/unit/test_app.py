from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from mcp_gryster.app import create_app
from mcp_gryster.artifacts import ArtifactStore
from mcp_gryster.config import Settings, SettingsError
from mcp_gryster.grist.adapter import GristAdapter
from mcp_gryster.grist.policy import Policy
from mcp_gryster.grist.registry import MethodRegistry
from mcp_gryster.widget.fixtures import FixtureManager
from mcp_gryster.widget.packaging import WidgetPackager
from mcp_gryster.widget.preview import PreviewManager
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.validation import PreviewEvidenceStore, WidgetValidator


@pytest.mark.asyncio
async def test_create_app_configures_strict_masked_server_and_lifespan_components(
    tmp_path: Path,
) -> None:
    settings = Settings(workspace_root=tmp_path)
    app = create_app(settings)

    assert app.strict_input_validation is True
    assert app._mask_error_details is True
    preview_manager: PreviewManager | None = None
    async with app._lifespan(app) as context:
        assert context["settings"] is settings
        assert isinstance(context["registry"], MethodRegistry)
        assert isinstance(context["policy"], Policy)
        assert isinstance(context["artifacts"], ArtifactStore)
        assert isinstance(context["adapter"], GristAdapter)
        assert callable(context["adapter_factory"])
        assert isinstance(context["project_store"], ProjectStore)
        assert isinstance(context["preview_evidence"], PreviewEvidenceStore)
        assert isinstance(context["preview_manager"], PreviewManager)
        assert isinstance(context["fixture_manager"], FixtureManager)
        assert isinstance(context["widget_validator"], WidgetValidator)
        assert isinstance(context["widget_packager"], WidgetPackager)
        preview_manager = context["preview_manager"]
        preview_manager.stop_all = AsyncMock()

    assert preview_manager is not None
    preview_manager.stop_all.assert_awaited_once_with()

    async with Client(app) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools}
    assert "grist.call" in names
    assert "widget.package" in names


def test_create_app_refuses_non_loopback_even_with_configured_bearer_token(
    tmp_path: Path,
) -> None:
    settings = Settings(
        workspace_root=tmp_path,
        transport="streamable-http",
        host="0.0.0.0",
        http_auth_token="configured-but-not-enforced",
    )

    with pytest.raises(SettingsError, match="non-loopback"):
        create_app(settings)
