"""Immutable models for revisioned widget projects."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp_gryster.widget.paths import normalize_authored_path, validate_project_id, validate_revision


class WidgetColumn(BaseModel):
    """One Grist widget column declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    optional: bool = False
    allow_multiple: bool = Field(default=False, alias="allowMultiple")


class WidgetAuthor(BaseModel):
    """Optional public author metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    email: str | None = None


class WidgetMetadata(BaseModel):
    """Validated input used to create a widget project."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    project_id: str
    widget_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    entrypoint: str = "index.html"
    access_level: Literal["none", "read table", "full"] = "none"
    columns: tuple[WidgetColumn, ...] = ()
    authors: tuple[WidgetAuthor, ...] = ()

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, value: str) -> str:
        return validate_project_id(value)

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint(cls, value: str) -> str:
        path = normalize_authored_path(f"src/{value}")
        return path.removeprefix("src/")

    def to_widget_json(self) -> bytes:
        """Serialize canonical schema-version-one widget metadata."""
        data: dict[str, Any] = {
            "accessLevel": self.access_level,
            "columns": [column.model_dump(by_alias=True) for column in self.columns],
            "description": self.description,
            "entrypoint": self.entrypoint,
            "name": self.name,
            "schemaVersion": 1,
            "widgetId": self.widget_id,
        }
        if self.authors:
            data["authors"] = [
                author.model_dump(exclude_none=True, by_alias=True) for author in self.authors
            ]
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


class FileUpdate(BaseModel):
    """One complete authored file replacement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    content: bytes

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return normalize_authored_path(value)


class ProjectSnapshot(BaseModel):
    """Stable description of one visible immutable project revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    revision: str
    files: tuple[str, ...]

    @field_validator("project_id")
    @classmethod
    def _project_id(cls, value: str) -> str:
        return validate_project_id(value)

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_revision(value)

    @field_validator("files")
    @classmethod
    def _files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_authored_path(value) for value in values)
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("files must be unique and sorted")
        return normalized


class FixtureColumn(BaseModel):
    """One explicitly allowlisted fixture column."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    type: str = Field(min_length=1)


class FixtureSource(BaseModel):
    """Non-sensitive identifiers describing a fixture's source."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    doc_id: str = Field(alias="docId", min_length=1)
    table_id: str = Field(alias="tableId", min_length=1)
    captured_at: str = Field(alias="capturedAt", min_length=1)


class FixtureRecord(BaseModel):
    """One fixture record containing only explicitly selected fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int | str
    fields: dict[str, Any]


class PreviewFixture(BaseModel):
    """Validated client-provided fixture committed into a project revision."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    fixture_id: str = Field(alias="fixtureId", min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    expected_revision: str = Field(alias="expectedRevision")
    source: FixtureSource
    columns: tuple[FixtureColumn, ...] = Field(min_length=1)
    records: tuple[FixtureRecord, ...]
    selected_record_id: int | str | None = Field(default=None, alias="selectedRecordId")
    mappings: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    include_attachment_metadata: bool = Field(default=False, alias="includeAttachmentMetadata")
    access_level: Literal["none", "read table", "full"] = Field(alias="accessLevel")

    @field_validator("expected_revision")
    @classmethod
    def _expected_revision(cls, value: str) -> str:
        return validate_revision(value)


class GristFixtureRequest(BaseModel):
    """Request to derive a fixture from policy-checked Grist reads."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    project_id: str = Field(alias="projectId")
    expected_revision: str = Field(alias="expectedRevision")
    fixture_id: str = Field(alias="fixtureId", min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    doc_id: str = Field(alias="docId", min_length=1)
    table_id: str = Field(alias="tableId", min_length=1)
    columns: tuple[str, ...] = Field(min_length=1)
    selected_record_id: int | str | None = Field(default=None, alias="selectedRecordId")
    mappings: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    access_level: Literal["none", "read table", "full"] = Field(alias="accessLevel")
    include_attachment_metadata: bool = Field(default=False, alias="includeAttachmentMetadata")
    team_id: str = Field(default="", alias="teamId")

    @field_validator("project_id")
    @classmethod
    def _request_project_id(cls, value: str) -> str:
        return validate_project_id(value)

    @field_validator("expected_revision")
    @classmethod
    def _request_revision(cls, value: str) -> str:
        return validate_revision(value)

    @field_validator("columns")
    @classmethod
    def _unique_columns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("columns must be non-empty and unique")
        return values


class FixtureSummary(BaseModel):
    """Value-free summary of a committed fixture and resulting project revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_id: str
    revision: str
    digest: str
    schema_version: int
    row_count: int
    column_count: int
    source_doc_id: str
    source_table_id: str

    @field_validator("revision")
    @classmethod
    def _summary_revision(cls, value: str) -> str:
        return validate_revision(value)
