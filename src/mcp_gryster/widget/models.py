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
