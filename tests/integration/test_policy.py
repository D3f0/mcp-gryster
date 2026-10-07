from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

from mcp_gryster.grist.adapter import GristAdapter

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.integration
async def test_read_policy_denies_add_before_adapter_or_http(
    integration_read_mcp_client: Client[Any],
    grist_ids: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_id = grist_ids["table_id"]
    before = await integration_read_mcp_client.call_tool(
        "grist.records.list", {"table_id": table_id}
    )
    assert not before.is_error
    assert before.structured_content is not None

    execute_calls = 0
    original_execute = GristAdapter._execute_call

    def spy_execute(self: GristAdapter, *args: Any, **kwargs: Any) -> Any:
        nonlocal execute_calls
        execute_calls += 1
        return original_execute(self, *args, **kwargs)

    monkeypatch.setattr(GristAdapter, "_execute_call", spy_execute)
    denied = await integration_read_mcp_client.call_tool(
        "grist.records.add",
        {"table_id": table_id, "records": [{"Name": "Must Not Exist"}]},
        raise_on_error=False,
    )
    monkeypatch.setattr(GristAdapter, "_execute_call", original_execute)


    assert denied.is_error
    assert execute_calls == 0

    after = await integration_read_mcp_client.call_tool(
        "grist.records.list", {"table_id": table_id}
    )
    assert not after.is_error
    assert after.structured_content == before.structured_content
