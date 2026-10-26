from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _call(client: Client[Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    assert not result.is_error
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.integration
async def test_typed_and_generic_reads_use_real_grist(
    integration_mcp_client: Client[Any], grist_ids: dict[str, Any]
) -> None:
    client = integration_mcp_client
    organizations = await _call(client, "grist.orgs.list", {})
    workspaces = await _call(client, "grist.workspaces.list", {})
    documents = await _call(
        client, "grist.documents.list", {"workspace_id": grist_ids["workspace_id"]}
    )
    document = await _call(client, "grist.documents.get", {})
    tables = await _call(client, "grist.tables.list", {})
    columns = await _call(client, "grist.columns.list", {"table_id": grist_ids["table_id"]})
    records = await _call(client, "grist.records.list", {"table_id": grist_ids["table_id"]})
    generic = await _call(
        client,
        "grist.call",
        {"method": "see_team", "arguments": {"team_id": "current"}},
    )

    assert any(org["id"] == grist_ids["org_id"] for org in organizations["data"])
    assert any(workspace["id"] == grist_ids["workspace_id"] for workspace in workspaces["data"])
    assert any(doc["id"] == grist_ids["doc_id"] for doc in documents["data"])
    assert document["data"]["id"] == grist_ids["doc_id"]
    assert any(table["id"] == grist_ids["table_id"] for table in tables["data"])
    assert {column["id"] for column in columns["data"]} >= {
        "Name",
        "Email",
        "Secret",
        "Attachment",
    }
    assert {(record["Name"], record["Email"]) for record in records["data"]} == {
        ("Ada Lovelace", "ada@example.test"),
        ("Grace Hopper", "grace@example.test"),
    }
    assert generic["data"]["id"] == grist_ids["org_id"]


@pytest.mark.integration
async def test_write_policy_allows_add_and_delete_and_restores_count(
    integration_mcp_client: Client[Any], grist_ids: dict[str, Any]
) -> None:
    client = integration_mcp_client
    table_id = grist_ids["table_id"]
    before = await _call(client, "grist.records.list", {"table_id": table_id})
    added = await _call(
        client,
        "grist.records.add",
        {
            "table_id": table_id,
            "records": [
                {
                    "Name": "Temporary Person",
                    "Email": "temporary@example.test",
                    "Secret": "temporary-secret",
                }
            ],
        },
    )
    row_id = added["data"][0]
    after_add = await _call(client, "grist.records.list", {"table_id": table_id})
    await _call(client, "grist.records.delete", {"table_id": table_id, "rows": [row_id]})
    after_delete = await _call(client, "grist.records.list", {"table_id": table_id})

    assert len(after_add["data"]) == len(before["data"]) + 1
    assert any(record["id"] == row_id for record in after_add["data"])
    assert len(after_delete["data"]) == len(before["data"])
    assert all(record["id"] != row_id for record in after_delete["data"])
