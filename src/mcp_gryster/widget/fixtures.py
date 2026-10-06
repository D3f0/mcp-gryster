"""Privacy-bounded preview fixtures stored in revisioned widget projects."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from pydantic import ValidationError

from mcp_gryster.config import Settings
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.grist.adapter import GristAdapter
from mcp_gryster.grist.models import GristCallResult, JsonValue
from mcp_gryster.widget.models import (
    FixtureColumn,
    FixtureRecord,
    FixtureSource,
    FixtureSummary,
    GristFixtureRequest,
    PreviewFixture,
)
from mcp_gryster.widget.projects import ProjectStore

_REDACTED = "[REDACTED]"
_MAX_JSON_DEPTH = 20
_ATTACHMENT_TYPES = frozenset({"Attachment", "Attachments"})
_SECRET_METADATA = re.compile(
    r"(?i)(?:authorization\s*:\s*(?:bearer\s+)?\S+|api[-_ ]?key\s*[=:]\s*\S+)"
)


class _Adapter(Protocol):
    async def call(self, method: str, arguments: dict[str, JsonValue]) -> GristCallResult: ...


class FixtureManager:
    """Validate, redact, bound, and atomically commit preview fixtures."""

    def __init__(
        self,
        store: ProjectStore,
        adapter: GristAdapter | _Adapter,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._max_rows = settings.max_fixture_rows
        self._max_bytes = settings.max_file_bytes
        self._redactions = tuple(re.compile(pattern) for pattern in settings.redaction_regexes)
        self._clock = clock or (lambda: datetime.now(UTC))
    @staticmethod
    def source_materialization_paths(files: Sequence[str]) -> tuple[str, ...]:
        """Select authored source and widget metadata while excluding private fixtures."""
        return tuple(path for path in files if path == "widget.json" or path.startswith("src/"))

    def create(
        self,
        project_id: str,
        fixture: PreviewFixture | Mapping[str, Any],
    ) -> FixtureSummary:
        """Commit a validated client fixture against its expected project revision."""
        parsed = self._validate_fixture(fixture)
        self._validate_metadata(parsed)
        sanitized = self._sanitize_client_fixture(parsed)
        self._validate_fixture_shape(sanitized)
        return self._commit(project_id, sanitized)

    async def create_from_grist(
        self,
        request: GristFixtureRequest | Mapping[str, Any],
    ) -> FixtureSummary:
        """Read only requested Grist data through the adapter and commit a safe fixture."""
        parsed = self._validate_request(request)
        self._validate_metadata(parsed)

        tables = await self._call("list_tables", {"doc_id": parsed.doc_id, "team_id": parsed.team_id})
        self._require_table(tables, parsed.table_id)
        columns_data = await self._call(
            "list_cols",
            {
                "table_id": parsed.table_id,
                "hidden": False,
                "doc_id": parsed.doc_id,
                "team_id": parsed.team_id,
            },
        )
        available_columns = self._parse_columns(columns_data)
        selected_columns = self._select_columns(
            available_columns,
            parsed.columns,
            parsed.include_attachment_metadata,
        )
        records_data = await self._call(
            "list_records",
            {
                "table_id": parsed.table_id,
                "filter": None,
                "sort": "",
                "limit": self._max_rows + 1,
                "hidden": False,
                "doc_id": parsed.doc_id,
                "team_id": parsed.team_id,
            },
        )
        records = self._select_records(
            records_data,
            selected_columns,
            parsed.include_attachment_metadata,
        )
        captured_at = self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        fixture = PreviewFixture(
            schemaVersion=1,
            fixtureId=parsed.fixture_id,
            expectedRevision=parsed.expected_revision,
            source=FixtureSource(
                docId=parsed.doc_id,
                tableId=parsed.table_id,
                capturedAt=captured_at,
            ),
            columns=selected_columns,
            records=records,
            selectedRecordId=parsed.selected_record_id,
            mappings=parsed.mappings,
            options=parsed.options
            | {"includeAttachmentMetadata": parsed.include_attachment_metadata},
            accessLevel=parsed.access_level,
        )
        self._validate_fixture_shape(fixture)
        return self._commit(parsed.project_id, fixture)

    async def _call(self, method: str, arguments: dict[str, JsonValue]) -> JsonValue:
        try:
            result = await self._adapter.call(method, arguments)
        except McpGrysterError:
            raise
        except Exception as exc:
            raise self._grist_shape_error() from exc
        if isinstance(result, GristCallResult):
            return result.data
        if isinstance(result, Mapping) and set(result) == {"status_code", "data"}:
            status = result["status_code"]
            if (
                not isinstance(status, int)
                or isinstance(status, bool)
                or status < 200
                or status >= 300
            ):
                raise self._grist_shape_error()
            return cast(JsonValue, result["data"])
        raise self._grist_shape_error()

    @staticmethod
    def _require_table(data: JsonValue, table_id: str) -> None:
        rows = FixtureManager._object_list(data)
        for row in rows:
            if set(row) == {"id", "fields"} and row["id"] == table_id and isinstance(row["fields"], dict):
                return
        raise FixtureManager._grist_shape_error()

    @staticmethod
    def _parse_columns(data: JsonValue) -> dict[str, FixtureColumn]:
        columns: dict[str, FixtureColumn] = {}
        for row in FixtureManager._object_list(data):
            if set(row) != {"id", "fields"}:
                raise FixtureManager._grist_shape_error()
            column_id = row["id"]
            fields = row["fields"]
            if not isinstance(column_id, str) or not isinstance(fields, dict):
                raise FixtureManager._grist_shape_error()
            if not {"label", "type"}.issubset(fields):
                raise FixtureManager._grist_shape_error()
            label, column_type = fields["label"], fields["type"]
            if not isinstance(label, str) or not isinstance(column_type, str):
                raise FixtureManager._grist_shape_error()
            try:
                columns[column_id] = FixtureColumn(id=column_id, label=label, type=column_type)
            except ValidationError as exc:
                raise FixtureManager._grist_shape_error() from exc
        return columns

    @staticmethod
    def _select_columns(
        available: Mapping[str, FixtureColumn],
        requested: Sequence[str],
        include_attachment_metadata: bool,
    ) -> tuple[FixtureColumn, ...]:
        selected: list[FixtureColumn] = []
        for column_id in requested:
            column = available.get(column_id)
            if column is None:
                raise McpGrysterError("INVALID_ARGUMENT", "Requested fixture column is invalid")
            if column.type in _ATTACHMENT_TYPES and not include_attachment_metadata:
                raise McpGrysterError(
                    "INVALID_ARGUMENT", "Attachment fixture columns require explicit metadata opt-in"
                )
            selected.append(column)
        return tuple(selected)

    def _select_records(
        self,
        data: JsonValue,
        columns: Sequence[FixtureColumn],
        include_attachment_metadata: bool,
    ) -> tuple[FixtureRecord, ...]:
        rows = self._object_list(data)
        if len(rows) > self._max_rows:
            raise self._limit_error()
        selected: list[FixtureRecord] = []
        for row in rows:
            record_id = row.get("id")
            if not isinstance(record_id, (int, str)) or isinstance(record_id, bool):
                raise self._grist_shape_error()
            fields: dict[str, Any] = {}
            for column in columns:
                if column.id not in row:
                    raise self._grist_shape_error()
                value: Any = row[column.id]
                if column.type in _ATTACHMENT_TYPES and include_attachment_metadata:
                    value = self._attachment_ids(value)
                fields[column.id] = self._redact_value(column.id, value)
            selected.append(FixtureRecord(id=record_id, fields=fields))
        return tuple(selected)

    def _sanitize_client_fixture(self, fixture: PreviewFixture) -> PreviewFixture:
        columns = {column.id: column for column in fixture.columns}
        include_attachment_metadata = fixture.options.get("includeAttachmentMetadata") is True
        if any(
            column.type in _ATTACHMENT_TYPES for column in fixture.columns
        ) and not include_attachment_metadata:
            raise McpGrysterError(
                "INVALID_ARGUMENT", "Attachment fixture columns require explicit metadata opt-in"
            )
        records: list[FixtureRecord] = []
        for record in fixture.records:
            fields: dict[str, Any] = {}
            for column_id, value in record.fields.items():
                column = columns.get(column_id)
                if column is None:
                    raise McpGrysterError("VALIDATION_FAILED", "Fixture is invalid")
                if column.type in _ATTACHMENT_TYPES:
                    value = self._attachment_ids(value)
                fields[column_id] = self._redact_value(column_id, value)
            records.append(FixtureRecord(id=record.id, fields=fields))
        return fixture.model_copy(update={"records": tuple(records)})

    def _validate_fixture_shape(self, fixture: PreviewFixture) -> None:
        if len(fixture.records) > self._max_rows:
            raise self._limit_error()
        allowed = {column.id for column in fixture.columns}
        if len(allowed) != len(fixture.columns):
            raise McpGrysterError("VALIDATION_FAILED", "Fixture is invalid")
        for record in fixture.records:
            if not set(record.fields).issubset(allowed):
                raise McpGrysterError("VALIDATION_FAILED", "Fixture is invalid")
            for value in record.fields.values():
                self._validate_json_value(value, depth=0)
        self._validate_json_value(fixture.mappings, depth=0)
        self._validate_json_value(fixture.options, depth=0)

    def _commit(self, project_id: str, fixture: PreviewFixture) -> FixtureSummary:
        payload = self._serialize(fixture)
        digest = hashlib.sha256(payload).hexdigest()
        snapshot = self._store.apply_files(
            project_id,
            fixture.expected_revision,
            {f"fixtures/{fixture.fixture_id}.json": payload},
            [],
        )
        return FixtureSummary(
            fixture_id=fixture.fixture_id,
            revision=snapshot.revision,
            digest=digest,
            schema_version=fixture.schema_version,
            row_count=len(fixture.records),
            column_count=len(fixture.columns),
            source_doc_id=fixture.source.doc_id,
            source_table_id=fixture.source.table_id,
        )

    def _serialize(self, fixture: PreviewFixture) -> bytes:
        data = fixture.model_dump(
            by_alias=True,
            exclude={"expected_revision"},
            mode="json",
        )
        payload = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(payload) > self._max_bytes:
            raise self._limit_error()
        return payload

    def _redact_value(self, column_name: str, value: JsonValue) -> JsonValue:
        if any(pattern.search(column_name) for pattern in self._redactions):
            return _REDACTED
        if isinstance(value, str) and any(pattern.search(value) for pattern in self._redactions):
            return _REDACTED
        if isinstance(value, list):
            return [self._redact_value(column_name, item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_value(column_name, item) for key, item in value.items()}
        return value

    @staticmethod
    def _attachment_ids(value: JsonValue) -> list[int]:
        if not isinstance(value, list):
            raise FixtureManager._grist_shape_error()
        raw_ids = value[1:] if value and value[0] == "L" else value
        if any(not isinstance(item, int) or isinstance(item, bool) for item in raw_ids):
            raise FixtureManager._grist_shape_error()
        return cast(list[int], raw_ids)

    @staticmethod
    def _object_list(data: JsonValue) -> list[dict[str, JsonValue]]:
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise FixtureManager._grist_shape_error()
        return cast(list[dict[str, JsonValue]], data)

    @staticmethod
    def _validate_fixture(fixture: PreviewFixture | Mapping[str, Any]) -> PreviewFixture:
        try:
            return fixture if isinstance(fixture, PreviewFixture) else PreviewFixture.model_validate(fixture)
        except ValidationError as exc:
            raise McpGrysterError("VALIDATION_FAILED", "Fixture is invalid") from exc

    @staticmethod
    def _validate_request(
        request: GristFixtureRequest | Mapping[str, Any],
    ) -> GristFixtureRequest:
        try:
            return (
                request
                if isinstance(request, GristFixtureRequest)
                else GristFixtureRequest.model_validate(request)
            )
        except ValidationError as exc:
            raise McpGrysterError("VALIDATION_FAILED", "Fixture request is invalid") from exc

    @staticmethod
    def _validate_metadata(value: PreviewFixture | GristFixtureRequest) -> None:
        metadata = value.model_dump(
            by_alias=True,
            exclude={"records", "expected_revision"},
            mode="json",
        )
        encoded = json.dumps(metadata, sort_keys=True)
        if _SECRET_METADATA.search(encoded):
            raise McpGrysterError("VALIDATION_FAILED", "Fixture metadata is invalid")

    @staticmethod
    def _validate_json_value(value: Any, *, depth: int) -> None:
        if depth > _MAX_JSON_DEPTH:
            raise FixtureManager._limit_error()
        if value is None or isinstance(value, (bool, int, float, str)):
            return
        if isinstance(value, list):
            for item in value:
                FixtureManager._validate_json_value(item, depth=depth + 1)
            return
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            for item in value.values():
                FixtureManager._validate_json_value(item, depth=depth + 1)
            return
        raise McpGrysterError("VALIDATION_FAILED", "Fixture contains a non-JSON value")

    @staticmethod
    def _limit_error() -> McpGrysterError:
        return McpGrysterError("LIMIT_EXCEEDED", "Fixture exceeds configured limits")

    @staticmethod
    def _grist_shape_error() -> McpGrysterError:
        return McpGrysterError("GRIST_REQUEST_FAILED", "The Grist request failed")
