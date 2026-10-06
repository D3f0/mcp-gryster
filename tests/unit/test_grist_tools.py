from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastmcp import Client, FastMCP

from mcp_gryster.grist.models import GristCallResult
from mcp_gryster.grist.tools import register_grist_tools
from mcp_gryster.artifacts import ArtifactStore
from mcp_gryster.audit import AuditEvent, AuditSink
from mcp_gryster.config import DomainAccess, DomainPolicy, Settings
from mcp_gryster.grist.adapter import GristAdapter
from mcp_gryster.grist.models import MethodSpec, ParameterSpec
from mcp_gryster.grist.policy import Policy
from mcp_gryster.grist.registry import MethodRegistry


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, arguments: dict[str, Any]) -> GristCallResult:
        self.calls.append((method, arguments))
        return GristCallResult(status_code=200, data={"method": method, "arguments": arguments})

    async def call_typed(self, route: str, arguments: dict[str, Any]) -> GristCallResult:
        assert route == "documents.list"
        return await self.call("see_workspace", arguments)


@pytest.fixture
def server_and_adapter() -> tuple[FastMCP, RecordingAdapter]:
    server = FastMCP("test", strict_input_validation=True)
    adapter = RecordingAdapter()
    register_grist_tools(server, adapter)  # type: ignore[arg-type]
    return server, adapter


class RecordingAudit(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class WorkspaceApi:
    configs: ClassVar[list[dict[str, str]]] = []

    def __init__(self, config: dict[str, str]) -> None:
        type(self).configs.append(config)

    def see_workspace(self, ws_id: int = 0) -> tuple[int, object]:
        return 200, {
            "id": ws_id,
            "name": "Private workspace metadata",
            "docs": [{"id": "doc-1", "name": "Allowed document"}],
        }


def documents_server(
    tmp_path: Path, *, documents: DomainAccess, workspaces: DomainAccess
) -> FastMCP:
    method = MethodSpec(
        name="see_workspace",
        domain="workspaces",
        effect="read",
        parameters={"ws_id": ParameterSpec(required=False, default=0)},
        result_kind="json",
    )
    registry = MethodRegistry.model_construct(pygrister_version="0.10.0", methods=(method,))
    settings = Settings(
        workspace_root=tmp_path,
        policy=DomainPolicy(documents=documents, workspaces=workspaces),
    )
    adapter = GristAdapter(
        registry,
        Policy(settings.policy),
        ArtifactStore(tmp_path / "artifacts"),
        settings,
        RecordingAudit(),
        api_factory=WorkspaceApi,
    )
    server = FastMCP("test", strict_input_validation=True)
    register_grist_tools(server, adapter)
    return server


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
async def test_documents_list_denies_when_documents_are_disabled(
    tmp_path: Path,
) -> None:
    server = documents_server(
        tmp_path, documents=DomainAccess.NONE, workspaces=DomainAccess.READ
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "grist.documents.list", {"workspace_id": 42}, raise_on_error=False
        )

    assert result.is_error


@pytest.mark.asyncio
async def test_documents_list_uses_document_policy_and_returns_only_documents(
    tmp_path: Path,
) -> None:
    server = documents_server(
        tmp_path, documents=DomainAccess.READ, workspaces=DomainAccess.NONE
    )

    async with Client(server) as client:
        result = await client.call_tool("grist.documents.list", {"workspace_id": 42})

    assert not result.is_error
    assert result.structured_content == {
        "status_code": 200,
        "data": [{"id": "doc-1", "name": "Allowed document"}],
    }


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
