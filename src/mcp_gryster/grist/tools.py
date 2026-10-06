"""Domain-focused FastMCP tools backed exclusively by :class:`GristAdapter`."""

from __future__ import annotations

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from mcp_gryster.grist.adapter import GristAdapter
from mcp_gryster.grist.models import GristCallResult, JsonValue

type JsonObject = dict[str, JsonValue]


class GristCallRequest(BaseModel):
    """A controlled call to one checked-in registry method."""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(min_length=1)
    arguments: JsonObject = Field(default_factory=dict)


async def _call(adapter: GristAdapter, method: str, **arguments: object) -> GristCallResult:
    return await adapter.call(method, arguments)  # type: ignore[arg-type]


def register_grist_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    """Register the supported typed and registry-only Grist tool surface."""
    _register_organization_tools(mcp, adapter)
    _register_workspace_and_document_tools(mcp, adapter)
    _register_schema_tools(mcp, adapter)
    _register_record_tools(mcp, adapter)
    _register_sql_tools(mcp, adapter)
    _register_access_tools(mcp, adapter)
    _register_attachment_tools(mcp, adapter)
    _register_webhook_tools(mcp, adapter)
    _register_catalog_tools(mcp, adapter)
    _register_generic_call(mcp, adapter)


def _register_organization_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.orgs.list")
    async def list_organizations() -> GristCallResult:
        """List Grist organizations available to the configured account."""
        return await _call(adapter, "list_team_sites")


def _register_workspace_and_document_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.workspaces.list")
    async def list_workspaces(team_id: str = "") -> GristCallResult:
        """List workspaces, optionally overriding the configured team."""
        return await _call(adapter, "list_workspaces", team_id=team_id)

    @mcp.tool(name="grist.documents.list")
    async def list_documents(workspace_id: int = 0) -> GristCallResult:
        """List documents in one workspace under document-read policy."""
        return await adapter.call_typed("documents.list", {"ws_id": workspace_id})

    @mcp.tool(name="grist.documents.get")
    async def get_document(doc_id: str = "", team_id: str = "") -> GristCallResult:
        """Get one document, with optional document and team overrides."""
        return await _call(adapter, "see_doc", doc_id=doc_id, team_id=team_id)


def _register_schema_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.tables.list")
    async def list_tables(doc_id: str = "", team_id: str = "") -> GristCallResult:
        """List tables in a document."""
        return await _call(adapter, "list_tables", doc_id=doc_id, team_id=team_id)

    @mcp.tool(name="grist.columns.list")
    async def list_columns(
        table_id: str,
        hidden: bool = False,
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """List columns in a table, optionally including hidden columns."""
        return await _call(
            adapter,
            "list_cols",
            table_id=table_id,
            hidden=hidden,
            doc_id=doc_id,
            team_id=team_id,
        )


def _register_record_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.records.list")
    async def list_records(
        table_id: str,
        filter: JsonObject | None = None,
        sort: str = "",
        limit: int = 0,
        hidden: bool = False,
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """List table records with Pygrister filtering, sorting, and visibility controls."""
        return await _call(
            adapter,
            "list_records",
            table_id=table_id,
            filter=filter,
            sort=sort,
            limit=limit,
            hidden=hidden,
            doc_id=doc_id,
            team_id=team_id,
        )

    @mcp.tool(name="grist.records.add")
    async def add_records(
        table_id: str,
        records: list[JsonObject],
        noparse: bool = False,
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """Add records to a table, optionally disabling Pygrister conversion."""
        return await _call(
            adapter,
            "add_records",
            table_id=table_id,
            records=records,
            noparse=noparse,
            doc_id=doc_id,
            team_id=team_id,
        )

    @mcp.tool(name="grist.records.update")
    async def update_records(
        table_id: str,
        records: list[JsonObject],
        noparse: bool = False,
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """Update records in a table, optionally disabling Pygrister conversion."""
        return await _call(
            adapter,
            "update_records",
            table_id=table_id,
            records=records,
            noparse=noparse,
            doc_id=doc_id,
            team_id=team_id,
        )

    @mcp.tool(name="grist.records.upsert")
    async def upsert_records(
        table_id: str,
        records: list[JsonObject],
        noparse: bool = False,
        onmany: str = "first",
        noadd: bool = False,
        noupdate: bool = False,
        allow_empty_require: bool = False,
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """Upsert records with all Pygrister matching and mutation controls."""
        return await _call(
            adapter,
            "add_update_records",
            table_id=table_id,
            records=records,
            noparse=noparse,
            onmany=onmany,
            noadd=noadd,
            noupdate=noupdate,
            allow_empty_require=allow_empty_require,
            doc_id=doc_id,
            team_id=team_id,
        )

    @mcp.tool(name="grist.records.delete")
    async def delete_records(
        table_id: str,
        rows: list[int],
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """Delete record row identifiers from a table."""
        return await _call(
            adapter,
            "delete_rows",
            table_id=table_id,
            rows=rows,
            doc_id=doc_id,
            team_id=team_id,
        )


def _register_sql_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.sql.query")
    async def query_sql(
        sql: str,
        qargs: list[JsonValue] | None = None,
        timeout: int = 1000,
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """Run parameterized SQL against a document with a bounded Pygrister timeout."""
        return await _call(
            adapter,
            "run_sql_with_args",
            sql=sql,
            qargs=qargs or [],
            timeout=timeout,
            doc_id=doc_id,
            team_id=team_id,
        )


def _register_access_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.access.document.list")
    async def list_document_access(doc_id: str = "", team_id: str = "") -> GristCallResult:
        """List users and access levels for a document."""
        return await _call(adapter, "list_doc_users", doc_id=doc_id, team_id=team_id)

    @mcp.tool(name="grist.access.document.update")
    async def update_document_access(
        users: dict[str, str],
        max: str = "owners",
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """Update document users while constraining the maximum access role."""
        return await _call(
            adapter,
            "update_doc_users",
            users=users,
            max=max,
            doc_id=doc_id,
            team_id=team_id,
        )


def _register_attachment_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.attachments.list")
    async def list_attachments(
        filter: JsonObject | None = None,
        sort: str = "",
        limit: int = 0,
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """List document attachments with Pygrister filter and sort controls."""
        return await _call(
            adapter,
            "list_attachments",
            filter=filter,
            sort=sort,
            limit=limit,
            doc_id=doc_id,
            team_id=team_id,
        )

    @mcp.tool(name="grist.attachments.upload")
    async def upload_attachment(
        filename: str,
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """Upload one workspace-owned file as a document attachment."""
        return await _call(
            adapter,
            "upload_attachment",
            filename=filename,
            doc_id=doc_id,
            team_id=team_id,
        )


def _register_webhook_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.webhooks.list")
    async def list_webhooks(doc_id: str = "", team_id: str = "") -> GristCallResult:
        """List webhooks configured for a document."""
        return await _call(adapter, "list_webhooks", doc_id=doc_id, team_id=team_id)

    @mcp.tool(name="grist.webhooks.create")
    async def create_webhooks(
        webhooks: list[JsonObject],
        doc_id: str = "",
        team_id: str = "",
    ) -> GristCallResult:
        """Create one or more webhooks for a document."""
        return await _call(
            adapter,
            "add_webhooks",
            webhooks=webhooks,
            doc_id=doc_id,
            team_id=team_id,
        )


def _register_catalog_tools(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.catalog.widgets.list")
    async def list_widgets() -> GristCallResult:
        """List widgets from the configured Grist installation catalog."""
        return await _call(adapter, "list_widgets")


def _register_generic_call(mcp: FastMCP, adapter: GristAdapter) -> None:
    @mcp.tool(name="grist.call")
    async def generic_call(
        method: str, arguments: JsonObject | None = None
    ) -> GristCallResult:
        """Call one checked-in registry method under the same validation and policy controls."""
        request = GristCallRequest(method=method, arguments=arguments or {})
        return await adapter.call(request.method, request.arguments)
