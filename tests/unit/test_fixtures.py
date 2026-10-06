from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mcp_gryster.config import Settings
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.grist.models import GristCallResult
from mcp_gryster.widget.fixtures import FixtureManager
from mcp_gryster.widget.models import WidgetMetadata
from mcp_gryster.widget.projects import ProjectStore


class FakeAdapter:
    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, arguments: dict[str, Any]) -> Any:
        if method not in {"list_tables", "list_cols", "list_records"}:
            raise AssertionError(f"unexpected adapter route: {method}")
        self.calls.append((method, arguments))
        return self.results[method]


def _settings(
    tmp_path: Path,
    *,
    max_rows: int = 3,
    max_bytes: int = 4096,
    redactions: tuple[str, ...] = (r"(?i)secret",),
) -> Settings:
    return Settings(
        workspace_root=tmp_path / "workspace",
        max_fixture_rows=max_rows,
        max_file_bytes=max_bytes,
        max_aggregate_bytes=max_bytes * 4,
        redaction_regexes=redactions,
    )


def _store(settings: Settings) -> ProjectStore:
    return ProjectStore(
        settings.workspace_root,
        max_file_bytes=settings.max_file_bytes * 4,
        max_aggregate_bytes=settings.max_aggregate_bytes * 4,
    )


def _project(store: ProjectStore) -> str:
    return store.create(
        WidgetMetadata(
            project_id="safe-widget",
            widget_id="example-widget",
            name="Example Widget",
        )
    ).revision


def _fixture_bytes(store: ProjectStore, revision: str, fixture_id: str = "preview") -> bytes:
    return (
        store.project_path("safe-widget")
        / "revisions"
        / revision
        / "fixtures"
        / f"{fixture_id}.json"
    ).read_bytes()


def _client_fixture(revision: str, **updates: Any) -> dict[str, Any]:
    fixture: dict[str, Any] = {
        "schemaVersion": 1,
        "fixtureId": "preview",
        "expectedRevision": revision,
        "source": {
            "docId": "doc-1",
            "tableId": "Table1",
            "capturedAt": "2026-10-06T12:00:00Z",
        },
        "columns": [{"id": "Name", "label": "Name", "type": "Text"}],
        "records": [{"id": 1, "fields": {"Name": "Ada"}}],
        "selectedRecordId": 1,
        "mappings": {"name": "Name"},
        "options": {"includeAttachmentMetadata": False},
        "accessLevel": "read table",
    }
    fixture.update(updates)
    return fixture


def _grist_results(*, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "list_tables": GristCallResult(
            status_code=200,
            data=[{"id": "Table1", "fields": {"primaryViewId": 1}}],
        ),
        "list_cols": GristCallResult(
            status_code=200,
            data=[
                {"id": "Name", "fields": {"label": "Name", "type": "Text"}},
                {"id": "Private", "fields": {"label": "Private", "type": "Text"}},
            ],
        ),
        "list_records": GristCallResult(
            status_code=200,
            data=records
            if records is not None
            else [{"id": 1, "Name": "Ada", "Private": "never serialize me"}],
        ),
    }


def _grist_request(revision: str, **updates: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "projectId": "safe-widget",
        "expectedRevision": revision,
        "fixtureId": "preview",
        "docId": "doc-1",
        "tableId": "Table1",
        "columns": ["Name"],
        "selectedRecordId": 1,
        "mappings": {"name": "Name"},
        "options": {},
        "accessLevel": "read table",
    }
    request.update(updates)
    return request


def test_client_fixture_commits_against_expected_revision_and_returns_safe_summary(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    initial = _project(store)
    manager = FixtureManager(store, FakeAdapter({}), settings)

    summary = manager.create("safe-widget", _client_fixture(initial))

    assert summary.revision != initial
    assert summary.fixture_id == "preview"
    assert summary.schema_version == 1
    assert summary.row_count == 1
    assert summary.column_count == 1
    assert summary.source_doc_id == "doc-1"
    assert summary.source_table_id == "Table1"
    assert summary.digest
    assert "Ada" not in summary.model_dump_json()
    assert store.read("safe-widget").revision == summary.revision


def test_columns_are_a_required_non_empty_allowlist(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    revision = _project(store)
    manager = FixtureManager(store, FakeAdapter({}), settings)

    for fixture in (
        _client_fixture(revision, columns=[]),
        _client_fixture(revision) | {"columns": None},
    ):
        with pytest.raises(McpGrysterError) as caught:
            manager.create("safe-widget", fixture)
        assert caught.value.code == "VALIDATION_FAILED"
    assert store.read("safe-widget").revision == revision


@pytest.mark.asyncio
async def test_grist_fixture_serializes_only_requested_columns_and_normalized_wrappers(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    revision = _project(store)
    wrapped = {
        method: {"status_code": result.status_code, "data": result.data}
        for method, result in _grist_results().items()
    }
    adapter = FakeAdapter(wrapped)
    manager = FixtureManager(
        store,
        adapter,
        settings,
        clock=lambda: datetime(2026, 10, 6, 12, tzinfo=UTC),
    )

    summary = await manager.create_from_grist(_grist_request(revision))
    payload = json.loads(_fixture_bytes(store, summary.revision))

    assert payload["source"] == {
        "capturedAt": "2026-10-06T12:00:00Z",
        "docId": "doc-1",
        "tableId": "Table1",
    }
    assert payload["columns"] == [{"id": "Name", "label": "Name", "type": "Text"}]
    assert payload["records"] == [{"fields": {"Name": "Ada"}, "id": 1}]
    assert b"Private" not in _fixture_bytes(store, summary.revision)
    assert b"never serialize me" not in _fixture_bytes(store, summary.revision)
    assert adapter.calls[-1] == (
        "list_records",
        {
            "table_id": "Table1",
            "filter": None,
            "sort": "",
            "limit": 4,
            "hidden": False,
            "doc_id": "doc-1",
            "team_id": "",
        },
    )


@pytest.mark.asyncio
async def test_attachment_columns_require_opt_in_and_store_only_identifiers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    initial = _project(store)
    results = _grist_results(records=[{"id": 1, "Photo": ["L", 7, 8]}])
    results["list_cols"] = GristCallResult(
        status_code=200,
        data=[{"id": "Photo", "fields": {"label": "Photo", "type": "Attachments"}}],
    )
    manager = FixtureManager(store, FakeAdapter(results), settings)

    with pytest.raises(McpGrysterError) as caught:
        await manager.create_from_grist(_grist_request(initial, columns=["Photo"]))
    assert caught.value.code == "INVALID_ARGUMENT"
    assert store.read("safe-widget").revision == initial

    summary = await manager.create_from_grist(
        _grist_request(
            initial,
            columns=["Photo"],
            includeAttachmentMetadata=True,
        )
    )
    payload = json.loads(_fixture_bytes(store, summary.revision))
    assert payload["records"] == [{"fields": {"Photo": [7, 8]}, "id": 1}]


@pytest.mark.asyncio
async def test_column_names_and_matching_values_are_redacted(tmp_path: Path) -> None:
    settings = _settings(tmp_path, redactions=(r"(?i)ssn", r"\b\d{3}-\d{2}-\d{4}\b"))
    store = _store(settings)
    revision = _project(store)
    results = _grist_results(
        records=[{"id": 1, "Name": "call 123-45-6789", "SSN": "000-00-0000"}]
    )
    results["list_cols"] = GristCallResult(
        status_code=200,
        data=[
            {"id": "Name", "fields": {"label": "Name", "type": "Text"}},
            {"id": "SSN", "fields": {"label": "SSN", "type": "Text"}},
        ],
    )
    manager = FixtureManager(store, FakeAdapter(results), settings)

    summary = await manager.create_from_grist(
        _grist_request(revision, columns=["Name", "SSN"])
    )
    payload = json.loads(_fixture_bytes(store, summary.revision))

    assert payload["records"] == [
        {"fields": {"Name": "[REDACTED]", "SSN": "[REDACTED]"}, "id": 1}
    ]


@pytest.mark.asyncio
async def test_row_and_byte_limits_are_checked_before_commit(tmp_path: Path) -> None:
    row_settings = _settings(tmp_path / "rows", max_rows=1)
    row_store = _store(row_settings)
    row_revision = _project(row_store)
    row_manager = FixtureManager(
        row_store,
        FakeAdapter(_grist_results(records=[{"id": 1, "Name": "a"}, {"id": 2, "Name": "b"}])),
        row_settings,
    )

    with pytest.raises(McpGrysterError) as caught_rows:
        await row_manager.create_from_grist(_grist_request(row_revision))
    assert caught_rows.value.code == "LIMIT_EXCEEDED"
    assert row_store.read("safe-widget").revision == row_revision

    byte_settings = _settings(tmp_path / "bytes", max_bytes=200)
    byte_store = _store(byte_settings)
    byte_revision = _project(byte_store)
    byte_manager = FixtureManager(byte_store, FakeAdapter({}), byte_settings)
    with pytest.raises(McpGrysterError) as caught_bytes:
        byte_manager.create(
            "safe-widget",
            _client_fixture(
                byte_revision,
                records=[{"id": 1, "fields": {"Name": "x" * 300}}],
            ),
        )
    assert caught_bytes.value.code == "LIMIT_EXCEEDED"
    assert byte_store.read("safe-widget").revision == byte_revision


def test_fixture_metadata_rejects_authentication_secrets(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        redactions=(
            r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+",
            r"(?i)(api[-_ ]?key\s*[=:]\s*)[^\s,;]+",
        ),
    )
    store = _store(settings)
    revision = _project(store)
    manager = FixtureManager(store, FakeAdapter({}), settings)

    for fixture in (
        _client_fixture(revision, options={"note": "Authorization: Bearer private"}),
        _client_fixture(revision, mappings={"API key=private": "Name"}),
    ):
        with pytest.raises(McpGrysterError) as caught:
            manager.create("safe-widget", fixture)
        assert caught.value.code == "VALIDATION_FAILED"
    assert store.read("safe-widget").revision == revision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "malformed"),
    [
        ("list_tables", {"tables": [{"id": "Table1"}], "unexpected": True}),
        ("list_cols", [{"id": "Name", "type": "Text"}]),
        ("list_records", [{"fields": {"Name": "Ada"}}]),
        ("list_records", {"status_code": 200, "data": [], "extra": "not allowed"}),
    ],
)
async def test_grist_shapes_fail_closed_before_commit(
    tmp_path: Path, method: str, malformed: Any
) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    revision = _project(store)
    results = _grist_results()
    results[method] = malformed
    manager = FixtureManager(store, FakeAdapter(results), settings)

    with pytest.raises(McpGrysterError) as caught:
        await manager.create_from_grist(_grist_request(revision))

    assert caught.value.code == "GRIST_REQUEST_FAILED"
    assert store.read("safe-widget").revision == revision


def test_recursive_values_have_depth_bounds(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    revision = _project(store)
    value: Any = "bottom"
    for _ in range(25):
        value = [value]
    manager = FixtureManager(store, FakeAdapter({}), settings)

    with pytest.raises(McpGrysterError) as caught:
        manager.create(
            "safe-widget",
            _client_fixture(revision, records=[{"id": 1, "fields": {"Name": value}}]),
        )

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert store.read("safe-widget").revision == revision


def test_stale_expected_revision_does_not_overwrite_fixture(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = _store(settings)
    initial = _project(store)
    manager = FixtureManager(store, FakeAdapter({}), settings)
    current = manager.create("safe-widget", _client_fixture(initial, fixtureId="first"))

    with pytest.raises(McpGrysterError) as caught:
        manager.create("safe-widget", _client_fixture(initial, fixtureId="second"))

    assert caught.value.code == "REVISION_CONFLICT"
    assert caught.value.details == {"current_revision": current.revision}


def test_source_materialization_selector_excludes_fixtures() -> None:
    assert FixtureManager.source_materialization_paths(
        ("fixtures/preview.json", "src/index.html", "widget.json")
    ) == ("src/index.html", "widget.json")


def test_client_fixture_redacts_records_and_restricts_attachment_metadata(tmp_path: Path) -> None:
    settings = _settings(tmp_path, redactions=(r"(?i)ssn", r"\b\d{3}-\d{2}-\d{4}\b"))
    store = _store(settings)
    revision = _project(store)
    manager = FixtureManager(store, FakeAdapter({}), settings)
    fixture = _client_fixture(
        revision,
        columns=[
            {"id": "Name", "label": "Name", "type": "Text"},
            {"id": "SSN", "label": "SSN", "type": "Text"},
            {"id": "Photo", "label": "Photo", "type": "Attachments"},
        ],
        records=[
            {
                "id": 1,
                "fields": {
                    "Name": "call 123-45-6789",
                    "SSN": "000-00-0000",
                    "Photo": ["L", 7, 8],
                },
            }
        ],
    )

    with pytest.raises(McpGrysterError) as caught:
        manager.create("safe-widget", fixture)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert store.read("safe-widget").revision == revision

    fixture["includeAttachmentMetadata"] = True
    summary = manager.create("safe-widget", fixture)
    payload = json.loads(_fixture_bytes(store, summary.revision))
    assert payload["records"] == [
        {
            "fields": {
                "Name": "[REDACTED]",
                "Photo": [7, 8],
                "SSN": "[REDACTED]",
            },
            "id": 1,
        }
    ]
