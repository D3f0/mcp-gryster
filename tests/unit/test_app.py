from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from mcp_gryster.app import create_app
from mcp_gryster.artifacts import ArtifactStore
from mcp_gryster.config import Settings, SettingsError
from mcp_gryster.grist.adapter import GristAdapter
from mcp_gryster.grist.policy import Policy
from mcp_gryster.grist.registry import MethodRegistry


@pytest.mark.asyncio
async def test_create_app_configures_strict_masked_server_and_lifespan_components(
    tmp_path: Path,
) -> None:
    settings = Settings(workspace_root=tmp_path)
    app = create_app(settings)

    assert app.strict_input_validation is True
    assert app._mask_error_details is True
    async with app._lifespan(app) as context:
        assert context["settings"] is settings
        assert isinstance(context["registry"], MethodRegistry)
        assert isinstance(context["policy"], Policy)
        assert isinstance(context["artifacts"], ArtifactStore)
        assert isinstance(context["adapter"], GristAdapter)
        assert callable(context["adapter_factory"])

    async with Client(app) as client:
        tools = await client.list_tools()
    assert "grist.call" in {tool.name for tool in tools}


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
