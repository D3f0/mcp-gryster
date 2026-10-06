from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest
from playwright.async_api import async_playwright, expect

from mcp_gryster.widget.models import FileUpdate, WidgetMetadata
from mcp_gryster.widget.preview import PreviewManager
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.validation import PreviewEvidenceStore


def _request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            normalized = {key.lower(): value for key, value in response.headers.items()}
            return response.status, response.read(), normalized
    except urllib.error.HTTPError as exc:
        normalized = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, exc.read(), normalized


def _store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(
        tmp_path / "workspace",
        max_file_bytes=32_768,
        max_files=20,
        max_aggregate_bytes=131_072,
    )


def _project(
    store: ProjectStore,
    *,
    project_id: str = "preview-widget",
    script: bytes | None = None,
) -> str:
    created = store.create(
        WidgetMetadata(
            project_id=project_id,
            widget_id=f"com.example.{project_id}",
            name="Preview Widget",
            access_level="full",
            columns=[],
        )
    )
    source = script or b"document.body.dataset.loaded = 'yes';\n"
    fixture = {
        "schemaVersion": 1,
        "fixtureId": "sample",
        "source": {
            "docId": "doc-private",
            "tableId": "Table1",
            "capturedAt": "2026-10-06T12:00:00Z",
        },
        "columns": [{"id": "Name", "label": "Name", "type": "Text"}],
        "records": [
            {"id": 1, "fields": {"Name": "First fixture value"}},
            {"id": 2, "fields": {"Name": "Selected fixture value"}},
        ],
        "selectedRecordId": 2,
        "mappings": {"displayName": "Name"},
        "options": {"theme": "fixture-only-value"},
        "accessLevel": "full",
    }
    updated = store.apply_files(
        project_id,
        created.revision,
        [
            FileUpdate(
                path="src/index.html",
                content=(
                    b"<!doctype html><html><head><link rel='stylesheet' href='style.css'></head>"
                    b"<body><output id='rendered'>waiting</output><script src='app.js'></script>"
                    b"</body></html>"
                ),
            ),
            FileUpdate(path="src/app.js", content=source),
            FileUpdate(path="src/style.css", content=b"output { color: green; }\n"),
            FileUpdate(path="src/blob.unknown", content=b"not served"),
            FileUpdate(
                path="fixtures/sample.json",
                content=(json.dumps(fixture, separators=(",", ":")) + "\n").encode(),
            ),
        ],
        [],
    )
    return updated.revision


@pytest.mark.asyncio
async def test_fixture_preview_is_capability_scoped_and_stops_cleanly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _project(store)
    _project(store, project_id="other-widget")
    evidence = PreviewEvidenceStore()
    manager = PreviewManager(store, evidence)

    session = await manager.start("preview-widget", "fixture", "sample")
    parsed = urlsplit(session.url)
    token = parsed.path.split("/")[2]
    origin = f"{parsed.scheme}://{parsed.netloc}"

    assert parsed.hostname == "127.0.0.1"
    assert parsed.port not in {None, 0}
    assert len(token) >= 43
    assert session.project_id == "preview-widget"
    assert session.revision
    assert session.fixture_id == "sample"
    assert session.emulator_version
    assert session.status == ()

    status, harness, headers = await asyncio.to_thread(_request, session.url)
    assert status == 200
    assert b"<iframe" in harness
    assert b"Preview Widget" not in harness
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["content-type"].startswith("text/html")

    widget_url = f"{origin}/t/{token}/widget/index.html"
    status, widget, _ = await asyncio.to_thread(_request, widget_url)
    assert status == 200
    assert b"grist-bridge.js" in widget
    assert b"<output id='rendered'>" in widget

    status, _, headers = await asyncio.to_thread(
        _request, f"{origin}/t/{token}/widget/style.css"
    )
    assert status == 200
    assert headers["content-type"].startswith("text/css")

    denied_urls = (
        f"{origin}/",
        f"{origin}/t/{token}/widget/",
        f"{origin}/t/{token}/widget/{quote('../widget.json', safe='')}",
        f"{origin}/t/{token}/widget/{quote('../../other-widget/src/index.html', safe='')}",
    )
    for denied_url in denied_urls:
        status, _, _ = await asyncio.to_thread(_request, denied_url)
        assert status == 404
    status, _, _ = await asyncio.to_thread(
        _request, f"{origin}/t/{token}/widget/blob.unknown"
    )
    assert status == 415

    status, _, _ = await asyncio.to_thread(
        _request, session.url, headers={"Host": "attacker.example"}
    )
    assert status == 403
    status, _, _ = await asyncio.to_thread(
        _request,
        f"{origin}/t/{token}/status",
        method="POST",
        body=b'{"kind":"ready"}',
        headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
    )
    assert status == 403
    status, _, _ = await asyncio.to_thread(
        _request,
        f"{origin}/t/{token}/status",
        method="POST",
        body=b'{"kind":"ready"}',
        headers={"Content-Type": "application/json", "Origin": origin},
    )
    assert status == 204

    status, _, _ = await asyncio.to_thread(
        _request,
        f"{origin}/t/{token}/status",
        method="POST",
        body=b'{"kind":"handshake"}',
        headers={"Content-Type": "application/json", "Origin": origin},
    )
    assert status == 409
    assert not evidence.has_success("preview-widget", session.revision, "sample")

    stopped = await manager.stop(session.session_id)
    assert stopped.stopped is True
    with pytest.raises(urllib.error.URLError):
        await asyncio.to_thread(_request, session.url)
    assert (await manager.stop(session.session_id)).stopped is False


@pytest.mark.asyncio
async def test_live_preview_serves_only_unmodified_authored_static(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _project(store)
    manager = PreviewManager(store, PreviewEvidenceStore())
    session = await manager.start("preview-widget", "live")
    parsed = urlsplit(session.url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    token = parsed.path.split("/")[2]

    status, html, _ = await asyncio.to_thread(_request, session.url)
    assert status == 200
    assert b"grist-bridge.js" not in html
    for private_route in ("harness", "grist-bridge.js", "fixture", "status"):
        status, _, _ = await asyncio.to_thread(
            _request, f"{origin}/t/{token}/{private_route}"
        )
        assert status == 404

    status, authored_script, _ = await asyncio.to_thread(
        _request, f"{origin}/t/{token}/widget/app.js"
    )
    assert status == 200
    assert authored_script == b"document.body.dataset.loaded = 'yes';\n"

    await manager.stop_all()


@pytest.mark.asyncio
async def test_browser_bridge_dispatches_in_order_renders_selection_and_records_evidence(
    tmp_path: Path,
) -> None:
    script = b"""
const rendered = document.querySelector('#rendered');
grist.onOptions(options => { rendered.dataset.theme = options.theme; });
grist.onRecords(records => { rendered.dataset.count = String(records.length); });
grist.onRecord(record => { rendered.textContent = record.displayName; });
grist.ready();
"""
    store = _store(tmp_path)
    revision = _project(store, script=script)
    evidence = PreviewEvidenceStore()
    manager = PreviewManager(store, evidence)
    session = await manager.start("preview-widget", "fixture", "sample")

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.goto(session.url)
            frame = page.frame_locator("#widget-frame")
            rendered = frame.locator("#rendered")
            await rendered.wait_for(state="visible")
            await expect(rendered).to_have_text("Selected fixture value")
            await expect(frame.locator("body")).to_have_attribute(
                "data-mcp-gryster-handshake", "complete"
            )
            await browser.close()
    finally:
        await manager.stop_all()

    assert [status.kind for status in session.status] == [
        "callback",
        "callback",
        "callback",
        "ready",
        "event",
        "event",
        "event",
        "handshake",
    ]
    assert [status.method for status in session.status] == [
        "onOptions",
        "onRecords",
        "onRecord",
        None,
        "options",
        "records",
        "selectedRecord",
        None,
    ]
    assert evidence.has_success("preview-widget", revision, "sample")


@pytest.mark.asyncio
async def test_bridge_table_access_and_unsupported_members_are_enforced(tmp_path: Path) -> None:
    script = b"""
grist.onRecord(async record => {
  const table = grist.getTable();
  const before = await table.getRecords();
  await table.update({id: record.id, fields: {Name: 'changed in memory'}});
  const after = await table.getRecord(record.id);
  document.querySelector('#rendered').textContent = before.length + ':' + after.fields.Name;
  try { grist.notARealMember(); } catch (error) {
    document.body.dataset.errorName = error.name;
  }
});
grist.ready();
"""
    store = _store(tmp_path)
    revision = _project(store, script=script)
    evidence = PreviewEvidenceStore()
    manager = PreviewManager(store, evidence)
    session = await manager.start("preview-widget", "fixture", "sample")

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.goto(session.url)
            frame = page.frame_locator("#widget-frame")
            await expect(frame.locator("#rendered")).to_have_text("2:changed in memory")
            await expect(frame.locator("body")).to_have_attribute(
                "data-mcp-gryster-unsupported", "notARealMember"
            )
            await browser.close()
    finally:
        await manager.stop_all()

    unsupported = [status for status in session.status if status.kind == "unsupported"]
    assert [(status.method, status.error_class) for status in unsupported] == [
        ("notARealMember", "GristPreviewUnsupportedError")
    ]
    assert not evidence.has_success("preview-widget", revision, "sample")


@pytest.mark.asyncio
async def test_runtime_failure_status_omits_authored_message_and_never_records_success(
    tmp_path: Path,
) -> None:
    secret = "Selected fixture value must never appear in status"
    store = _store(tmp_path)
    revision = _project(store, script=f'throw new TypeError("{secret}");\n'.encode())
    evidence = PreviewEvidenceStore()
    manager = PreviewManager(store, evidence)
    session = await manager.start("preview-widget", "fixture", "sample")

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.goto(session.url)
            frame = page.frame_locator("#widget-frame")
            await expect(frame.locator("body")).to_have_attribute(
                "data-mcp-gryster-error", "runtime"
            )
            await browser.close()
    finally:
        await manager.stop_all()

    runtime = next(status for status in session.status if status.kind == "uncaught")
    assert runtime.error_class == "TypeError"
    assert runtime.category == "runtime"
    assert runtime.location is not None
    assert runtime.location.startswith("app.js:1:")
    assert secret not in repr(session.status)
    assert "fixture" not in repr(runtime).lower()
    assert not evidence.has_success("preview-widget", revision, "sample")

    assert not evidence.has_success("preview-widget", revision, "sample")
