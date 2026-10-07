from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from mcp.shared.exceptions import McpError
from mcp_gryster.app import create_app
from mcp_gryster.config import Settings
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.preview import PreviewManager
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.tools import _read_authored_file


async def _call(client: Client[Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    assert not result.is_error
    assert result.structured_content is not None
    return result.structured_content

async def _assert_resource_denied(client: Client[Any], uri: str) -> None:
    try:
        contents = await client.read_resource(uri)
    except McpError:
        return
    assert contents == []


@pytest.mark.asyncio
async def test_widget_mcp_surface_completes_project_fixture_validate_preview_package_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(workspace_root=tmp_path / "workspace")
    app = create_app(settings)
    monkeypatch.setattr(PreviewManager, "start", _fake_start)
    monkeypatch.setattr(PreviewManager, "stop", _fake_stop)

    async with Client(app) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert {
            "widget.projects.create",
            "widget.projects.list",
            "widget.projects.describe",
            "widget.projects.read",
            "widget.projects.apply",
            "widget.fixtures.create",
            "widget.fixtures.from_grist",
            "widget.validate",
            "widget.preview.start",
            "widget.preview.stop",
            "widget.package",
        }.issubset(tools)

        created = await _call(
            client,
            "widget.projects.create",
            {
                "metadata": {
                    "project_id": "flow-widget",
                    "widget_id": "com.example.flow",
                    "name": "Flow Widget",
                    "access_level": "none",
                }
            },
        )
        revision = created["revision"]
        applied = await _call(
            client,
            "widget.projects.apply",
            {
                "project_id": "flow-widget",
                "expected_revision": revision,
                "files": [
                    {
                        "path": "src/index.html",
                        "content": (
                            "<!doctype html>"
                            "<script src='https://docs.getgrist.com/grist-plugin-api.js'></script>"
                            "<script src='app.js'></script>"
                        ),
                    },
                    {"path": "src/app.js", "content": "grist.ready();\n"},
                ],
                "delete": [],
            },
        )
        revision = applied["revision"]
        fixture = await _call(
            client,
            "widget.fixtures.create",
            {
                "project_id": "flow-widget",
                "fixture": {
                    "schemaVersion": 1,
                    "fixtureId": "sample",
                    "expectedRevision": revision,
                    "source": {
                        "docId": "doc",
                        "tableId": "Table1",
                        "capturedAt": "2026-10-06T12:00:00Z",
                    },
                    "columns": [{"id": "Name", "label": "Name", "type": "Text"}],
                    "records": [{"id": 1, "fields": {"Name": "private value"}}],
                    "selectedRecordId": 1,
                    "mappings": {"name": "Name"},
                    "options": {},
                    "accessLevel": "none",
                },
            },
        )
        revision = fixture["revision"]
        preview = await _call(
            client,
            "widget.preview.start",
            {"project_id": "flow-widget", "mode": "fixture", "fixture_id": "sample"},
        )
        assert preview["revision"] == revision
        validation = await _call(
            client,
            "widget.validate",
            {"project_id": "flow-widget", "fixture_id": "sample"},
        )
        assert validation["packageable"] is True
        package = await _call(
            client,
            "widget.package",
            {
                "project_id": "flow-widget",
                "expected_revision": revision,
                "public_base_url": "https://widgets.example",
                "fixture_id": "sample",
            },
        )
        assert package["revision"] == revision
        assert package["artifact"]["name"] == f"flow-widget-{revision}.zip"
        artifact = await client.read_resource(
            f"widget://dist/{package['artifact']['name']}"
        )
        assert artifact[0].blob is not None
        assert await _call(client, "widget.preview.stop", {"session_id": preview["session_id"]}) == {
            "session_id": preview["session_id"],
            "stopped": True,
        }


@pytest.mark.asyncio
async def test_widget_package_reports_stale_revision_without_private_details(
    tmp_path: Path,
) -> None:
    app = create_app(Settings(workspace_root=tmp_path / "workspace"))
    async with Client(app) as client:
        await _call(
            client,
            "widget.projects.create",
            {
                "metadata": {
                    "project_id": "stale-widget",
                    "widget_id": "com.example.stale",
                    "name": "Stale Widget",
                }
            },
        )
        arguments = {
            "project_id": "stale-widget",
            "expected_revision": "0" * 64,
            "public_base_url": "https://widgets.example",
        }
        first = await client.call_tool(
            "widget.package", arguments, raise_on_error=False
        )
        second = await client.call_tool(
            "widget.package", arguments, raise_on_error=False
        )

    assert first.is_error
    assert second.is_error
    assert str(first.content) == str(second.content)
    assert str(tmp_path) not in str(first.content)

@pytest.mark.asyncio
async def test_widget_resources_expose_authored_files_without_fixture_enumeration_or_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(workspace_root=tmp_path / "workspace")
    store = ProjectStore(settings)
    app = create_app(settings)
    async with Client(app) as client:
        created = await _call(
            client,
            "widget.projects.create",
            {
                "metadata": {
                    "project_id": "resource-widget",
                    "widget_id": "com.example.resource",
                    "name": "Resource Widget",
                }
            },
        )
        revision = created["revision"]
        fixture = await _call(
            client,
            "widget.fixtures.create",
            {
                "project_id": "resource-widget",
                "fixture": {
                    "schemaVersion": 1,
                    "fixtureId": "secret",
                    "expectedRevision": revision,
                    "source": {"docId": "doc", "tableId": "Table1", "capturedAt": "now"},
                    "columns": [{"id": "Name", "label": "Name", "type": "Text"}],
                    "records": [{"id": 1, "fields": {"Name": "never expose"}}],
                    "mappings": {},
                    "options": {},
                    "accessLevel": "none",
                },
            },
        )
        revision = fixture["revision"]
        project = await client.read_resource("widget://projects/resource-widget")
        project_data = json.loads(project[0].text)
        assert project_data["files"] == ["src/index.html", "widget.json"]
        authored = await client.read_resource("widget://projects/resource-widget/files/src/index.html")
        assert "<!doctype html>" in authored[0].text
        assert authored[0].mimeType == "text/plain"

        denied_paths = (
            "fixtures/secret.json",
            "../widget.json",
            "src/%2E%2E/widget.json",
        )
        for path in denied_paths:
            with pytest.raises(McpGrysterError) as denied:
                _read_authored_file(store, "resource-widget", path)
            assert denied.value.code == "PATH_NOT_ALLOWED"

        with monkeypatch.context() as expected_errors:
            expected_errors.setattr(
                logging.getLogger("fastmcp.server.server"), "disabled", True
            )
            for uri in (
                "widget://projects/resource-widget/files/fixtures/secret.json",
                "widget://projects/resource-widget/files/../widget.json",
            ):
                await _assert_resource_denied(client, uri)

        # AnyUrl canonicalizes the encoded dot segment before MCP dispatch, so the
        # server receives the safe canonical widget.json resource rather than the
        # authored malicious spelling. The direct reader assertions above prove
        # that spelling itself remains denied and no fixture content is exposed.
        canonicalized = await client.read_resource(
            "widget://projects/resource-widget/files/src/%2E%2E/widget.json"
        )
        assert canonicalized[0].text == (
            store.project_path("resource-widget")
            / "revisions"
            / revision
            / "widget.json"
        ).read_text()
        assert "never expose" not in canonicalized[0].text

        artifact_root = settings.workspace_root / "artifacts" / "widget"
        outside = tmp_path / "outside.zip"
        outside.write_bytes(b"private")
        artifact_root.mkdir(parents=True)
        (artifact_root / "linked.zip").symlink_to(outside)
        with monkeypatch.context() as expected_errors:
            expected_errors.setattr(
                logging.getLogger("fastmcp.server.server"), "disabled", True
            )
            await _assert_resource_denied(client, "widget://dist/linked.zip")

        validation = await client.read_resource("widget://projects/resource-widget/validation")
        assert json.loads(validation[0].text)["revision"] == revision
        methods = await client.read_resource("widget://grist/methods")
        method_data = json.loads(methods[0].text)
        assert method_data["pygrister_version"] == "0.10.0"
        assert method_data["methods"]


async def _fake_start(
    self: PreviewManager, project_id: str, mode: str, fixture_id: str | None = None
) -> Any:
    from mcp_gryster.widget.preview import PreviewSession

    snapshot = self._store.read(project_id)
    self._evidence.record_success(project_id, snapshot.revision, fixture_id)
    return PreviewSession(
        session_id="fake-session",
        url="http://127.0.0.1/fake",
        emulator_version="test",
        project_id=project_id,
        revision=snapshot.revision,
        fixture_id=fixture_id,
        _statuses=[],
    )


async def _fake_stop(self: PreviewManager, session_id: str) -> Any:
    from mcp_gryster.widget.preview import StopPreviewResult

    return StopPreviewResult(session_id=session_id, stopped=True)
