from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

from mcp_gryster.app import create_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.integration
async def test_fresh_app_client_reuses_running_grist(
    integration_settings: Any, grist_ids: dict[str, Any]
) -> None:
    first_app = create_app(integration_settings)
    async with Client(first_app) as first_client:
        first = await first_client.call_tool(
            "grist.records.list", {"table_id": grist_ids["table_id"]}
        )
    second_app = create_app(integration_settings)
    async with Client(second_app) as second_client:
        second = await second_client.call_tool(
            "grist.records.list", {"table_id": grist_ids["table_id"]}
        )

    assert not first.is_error
    assert not second.is_error
    assert second.structured_content == first.structured_content
