from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from fastmcp import Client
from mcp.types import TextResourceContents

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _call(client: Client[Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    assert not result.is_error
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.integration
async def test_fixture_captures_only_explicit_columns_and_redacts_sensitive_values(
    integration_mcp_client: Client[Any],
    grist_ids: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = integration_mcp_client
    project_id = f"integration-widget-{uuid.uuid4().hex[:8]}"
    created = await _call(
        client,
        "widget.projects.create",
        {
            "metadata": {
                "project_id": project_id,
                "widget_id": "com.example.integration",
                "name": "Integration Widget",
                "access_level": "read table",
            }
        },
    )
    fixture = await _call(
        client,
        "widget.fixtures.create_from_grist",
        {
            "project_id": project_id,
            "expected_revision": created["revision"],
            "fixture_id": "people",
            "doc_id": grist_ids["doc_id"],
            "table_id": grist_ids["table_id"],
            "columns": ["Name", "Email", "Secret"],
            "access_level": "read table",
        },
    )

    project = await client.read_resource(f"widget://projects/{project_id}")
    assert isinstance(project[0], TextResourceContents)
    project_data = json.loads(project[0].text)
    assert project_data["files"] == ["src/index.html", "widget.json"]

    with monkeypatch.context() as expected_error:
        expected_error.setattr(logging.getLogger("fastmcp.server.server"), "disabled", True)
        denied = await client.call_tool(
            "widget.projects.read",
            {"project_id": project_id, "path": "fixtures/people.json"},
            raise_on_error=False,
        )
    assert denied.is_error

    described = await _call(client, "widget.projects.describe", {"project_id": project_id})
    assert "fixtures/people.json" in described["files"]
    assert fixture["revision"] == described["revision"]

    fixture_path = (
        grist_ids["workspace_root"]
        / ".mcp-gryster"
        / "projects"
        / project_id
        / "revisions"
        / fixture["revision"]
        / "fixtures"
        / "people.json"
    )
    contents = json.loads(fixture_path.read_text())
    assert [column["id"] for column in contents["columns"]] == ["Name", "Email", "Secret"]
    assert all("Attachment" not in record["fields"] for record in contents["records"])
    assert contents["options"]["includeAttachmentMetadata"] is False
    assert all(record["fields"]["Email"] == "[REDACTED]" for record in contents["records"])
    assert all(record["fields"]["Secret"] == "[REDACTED]" for record in contents["records"])
    assert {record["fields"]["Name"] for record in contents["records"]} == {
        "Ada Lovelace",
        "Grace Hopper",
    }
