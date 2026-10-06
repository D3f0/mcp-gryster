"""Validated models for the checked-in Pygrister method registry."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Domain = Literal[
    "service_accounts",
    "identity",
    "organizations",
    "workspaces",
    "documents",
    "schema",
    "records",
    "attachments",
    "webhooks",
    "sql",
    "access",
    "administration",
]
Effect = Literal["read", "write"]
ResultKind = Literal["json", "binary", "file", "paginated"]


class ParameterSpec(BaseModel):
    """The dispatch-relevant portion of a Python method parameter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required: bool
    default: Any = None


class MethodSpec(BaseModel):
    """Classification and call contract for one remote Grist operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    domain: Domain
    effect: Effect
    parameters: dict[str, ParameterSpec]
    result_kind: ResultKind
    typed_tool: bool = False
    supported: bool = True

    @field_validator("parameters")
    @classmethod
    def validate_parameter_names(
        cls, parameters: dict[str, ParameterSpec]
    ) -> dict[str, ParameterSpec]:
        if any(not name.isidentifier() or name.startswith("_") for name in parameters):
            raise ValueError("parameter names must be public Python identifiers")
        return parameters
