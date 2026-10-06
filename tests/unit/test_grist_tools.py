from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP

from mcp_gryster.grist.models import GristCallResult
from mcp_gryster.grist.tools import register_grist_tools


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, arguments: dict[str, Any]) -> GristCallResult:
        self.calls.append((method, arguments))
        return GristCallResult(status_code=200, data={"method": method, "arguments": arguments})


@pytest.fixture
def server_and_adapter() -> tuple[FastMCP, RecordingAdapter]:
    server = FastMCP("test", strict_input_validation=True)
    adapter = RecordingAdapter()
    register_grist_tools(server, adapter)  # type: ignore[arg-type]
    return server, adapter


@pytest.mark.asyncio
async def test_registers_complete_closed_typed_surface(
    server_and_adapter: tuple[FastMCP, RecordingAdapter],
) -> None:
    server, _ = server_and_adapter
    expected = {
        "grist.orgs.list",
        "grist.workspaces.list",
        "grist.documents.list",
        "grist.documents.get",
        "grist.tables.list",
        "grist.columns.list",
        "grist.records.list",
        "grist.records.add",
        "grist.records.update",
        "grist.records.upsert",
        "grist.records.delete",
        "grist.sql.query",
        "grist.access.document.list",
        "grist.access.document.update",
        "grist.attachments.list",
        "grist.attachments.upload",
        "grist.webhooks.list",
        "grist.webhooks.create",
        "grist.catalog.widgets.list",
        "grist.call",
    }

    async with Client(server) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools} == expected
    for tool in tools:
        assert tool.description
        assert tool.inputSchema.get("additionalProperties") is False
        assert tool.outputSchema is not None
        assert tool.outputSchema.get("additionalProperties") is False


@pytest.mark.asyncio
async def test_record_tools_delegate_all_controls_to_adapter(
    server_and_adapter: tuple[FastMCP, RecordingAdapter],
) -> None:
    server, adapter = server_and_adapter
    arguments = {
        "table_id": "Table1",
        "records": [{"require": {"Email": "a@example.com"}, "fields": {"Name": "A"}}],
        "noparse": True,
        "onmany": "all",
        "noadd": True,
        "noupdate": False,
        "allow_empty_require": True,
        "doc_id": "doc-override",
        "team_id": "team-override",
    }

    async with Client(server) as client:
        result = await client.call_tool("grist.records.upsert", arguments)

    assert not result.is_error
    assert adapter.calls == [("add_update_records", arguments)]
    assert result.structured_content == {
        "status_code": 200,
        "data": {"method": "add_update_records", "arguments": arguments},
    }


@pytest.mark.asyncio
async def test_list_filters_and_sql_arguments_are_preserved(
    server_and_adapter: tuple[FastMCP, RecordingAdapter],
) -> None:
    server, adapter = server_and_adapter
    async with Client(server) as client:
        await client.call_tool(
            "grist.records.list",
            {
                "table_id": "T",
                "filter": {"Active": [True]},
                "sort": "-Name",
                "limit": 25,
                "hidden": True,
                "doc_id": "D",
                "team_id": "TEAM",
            },
        )
        await client.call_tool(
            "grist.sql.query",
            {"sql": "select ?", "qargs": [1], "timeout": 321, "doc_id": "D", "team_id": "T"},
        )

    assert adapter.calls == [
        (
            "list_records",
            {
                "table_id": "T",
                "filter": {"Active": [True]},
                "sort": "-Name",
                "limit": 25,
                "hidden": True,
                "doc_id": "D",
                "team_id": "TEAM",
            },
        ),
        (
            "run_sql_with_args",
            {"sql": "select ?", "qargs": [1], "timeout": 321, "doc_id": "D", "team_id": "T"},
        ),
    ]


@pytest.mark.asyncio
async def test_documents_list_maps_to_workspace_detail(
    server_and_adapter: tuple[FastMCP, RecordingAdapter],
) -> None:
    server, adapter = server_and_adapter
    async with Client(server) as client:
        await client.call_tool("grist.documents.list", {"workspace_id": 42})

    assert adapter.calls == [("see_workspace", {"ws_id": 42})]


@pytest.mark.asyncio
async def test_generic_call_is_registry_shaped_and_rejects_extra_fields(
    server_and_adapter: tuple[FastMCP, RecordingAdapter],
) -> None:
    server, adapter = server_and_adapter
    async with Client(server) as client:
        result = await client.call_tool(
            "grist.call", {"method": "see_doc", "arguments": {"doc_id": "D"}}
        )
        invalid = await client.call_tool(
            "grist.call",
            {"method": "see_doc", "arguments": {}, "path": "/api/docs/private"},
            raise_on_error=False,
        )

    assert not result.is_error
    assert invalid.is_error
    assert adapter.calls == [("see_doc", {"doc_id": "D"})]
