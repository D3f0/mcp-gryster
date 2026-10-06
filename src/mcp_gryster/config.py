"""Environment-backed server configuration with secure defaults."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator


class SettingsError(ValueError):
    """Raised when server configuration is invalid or unsafe."""


class DomainAccess(StrEnum):
    """Maximum access granted to a Grist API domain."""

    NONE = "none"
    READ = "read"
    WRITE = "write"


class DomainPolicy(BaseModel):
    """Allow-list policy for every exposed Grist domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organizations: DomainAccess = DomainAccess.NONE
    workspaces: DomainAccess = DomainAccess.NONE
    documents: DomainAccess = DomainAccess.NONE
    schema_access: DomainAccess = Field(default=DomainAccess.NONE, alias="schema")
    records: DomainAccess = DomainAccess.READ
    access: DomainAccess = DomainAccess.NONE
    attachments: DomainAccess = DomainAccess.NONE
    webhooks: DomainAccess = DomainAccess.NONE
    sql: DomainAccess = DomainAccess.NONE
    identity: DomainAccess = DomainAccess.NONE
    service_accounts: DomainAccess = DomainAccess.NONE
    administration: DomainAccess = DomainAccess.NONE


_DEFAULT_REDACTION_REGEXES = (
    r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+",
    r"(?i)(api[-_ ]?key\s*[=:]\s*)[^\s,;]+",
    r"(?i)(token\s*[=:]\s*)[^\s,;]+",
)
_GRIST_KEYS = (
    "GRIST_API_KEY",
    "GRIST_SELF_MANAGED",
    "GRIST_SELF_MANAGED_HOME",
    "GRIST_SELF_MANAGED_SINGLE_ORG",
    "GRIST_SERVER_PROTOCOL",
    "GRIST_API_SERVER",
    "GRIST_API_ROOT",
    "GRIST_TEAM_SITE",
    "GRIST_WORKSPACE_ID",
    "GRIST_DOC_ID",
    "GRIST_RAISE_ERROR",
    "GRIST_SAFEMODE",
)

_POLICY_DOMAINS = frozenset(
    field.alias or name for name, field in DomainPolicy.model_fields.items()
)


class Settings(BaseModel):
    """Validated process settings loaded from an explicit environment mapping."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: Path
    transport: Literal["stdio", "streamable-http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, le=65_535)
    preview_timeout_seconds: float = Field(default=30.0, gt=0)
    max_file_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_aggregate_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_fixture_rows: int = Field(default=1_000, gt=0)
    policy: DomainPolicy = Field(default_factory=DomainPolicy)
    grist_config: Mapping[str, str] = Field(default_factory=dict)
    redaction_regexes: tuple[str, ...] = _DEFAULT_REDACTION_REGEXES
    http_auth_token: SecretStr | None = None

    @property
    def workspace(self) -> Path:
        """Compatibility name used by the command-line layer."""
        return self.workspace_root

    @field_validator("redaction_regexes")
    @classmethod
    def _validate_redaction_regexes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            re.compile(value)
        return values

    @classmethod
    def load(cls, env: Mapping[str, str]) -> Settings:
        """Load settings solely from *env*, without consulting or changing ``os.environ``."""
        source = dict(env)
        workspace_value = source.get("MCP_GRYSTER_WORKSPACE", ".mcp-gryster")
        workspace = Path(workspace_value).expanduser().resolve()

        policy = _load_policy(source.get("MCP_GRYSTER_POLICY"))
        redaction_regexes = _load_redaction_regexes(
            source.get("MCP_GRYSTER_REDACTION_REGEXES")
        )
        grist_config = MappingProxyType(_load_grist_config(source))
        auth_token = source.get("MCP_GRYSTER_HTTP_AUTH_TOKEN") or source.get(
            "MCP_GRYSTER_HTTP_BEARER_TOKEN"
        )
        values: dict[str, Any] = {
            "workspace_root": workspace,
            "transport": source.get("MCP_GRYSTER_TRANSPORT", "stdio"),
            "host": source.get("MCP_GRYSTER_HOST", "127.0.0.1"),
            "port": source.get("MCP_GRYSTER_PORT", "8000"),
            "preview_timeout_seconds": source.get(
                "MCP_GRYSTER_PREVIEW_TIMEOUT_SECONDS",
                source.get("MCP_GRYSTER_PREVIEW_TIMEOUT", "30"),
            ),
            "max_file_bytes": source.get("MCP_GRYSTER_MAX_FILE_BYTES", str(5 * 1024 * 1024)),
            "max_aggregate_bytes": source.get(
                "MCP_GRYSTER_MAX_AGGREGATE_BYTES", str(25 * 1024 * 1024)
            ),
            "max_fixture_rows": source.get("MCP_GRYSTER_MAX_FIXTURE_ROWS", "1000"),
            "policy": policy,
            "grist_config": grist_config,
            "redaction_regexes": redaction_regexes,
            "http_auth_token": auth_token,
        }

        try:
            settings = cls.model_validate(values)
        except (ValidationError, re.error) as exc:
            raise SettingsError(_settings_error_message(exc)) from exc

        if (
            settings.transport == "streamable-http"
            and not _is_loopback(settings.host)
            and settings.http_auth_token is None
        ):
            raise SettingsError(
                "HTTP authentication is required when binding Streamable HTTP "
                "to a non-loopback host"
            )

        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SettingsError("workspace could not be created") from exc
        if not workspace.is_dir():
            raise SettingsError("workspace must be a directory")
        return settings


def _load_policy(raw: str | None) -> DomainPolicy:
    if raw is None or not raw.strip():
        return DomainPolicy()
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or set(decoded) != _POLICY_DOMAINS:
            raise TypeError
        return DomainPolicy.model_validate(decoded)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise SettingsError("policy must define only valid domains and access levels") from exc



def _load_grist_config(source: Mapping[str, str]) -> dict[str, str]:
    overrides = {key: source[key] for key in _GRIST_KEYS if source.get(key)}
    raw = source.get("MCP_GRYSTER_GRIST_CONFIG")
    if raw is None or not raw.strip():
        return overrides
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError("Grist configuration overrides must be a JSON object") from exc
    if not isinstance(decoded, dict) or not all(
        key in _GRIST_KEYS and isinstance(value, str) and value
        for key, value in decoded.items()
    ):
        raise SettingsError("Grist configuration overrides contain an invalid key or value")
    overrides.update(decoded)
    return overrides

def _load_redaction_regexes(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return _DEFAULT_REDACTION_REGEXES
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError("redaction regexes must be a JSON array of strings") from exc
    if not isinstance(decoded, list) or not all(isinstance(value, str) for value in decoded):
        raise SettingsError("redaction regexes must be a JSON array of strings")
    return (*_DEFAULT_REDACTION_REGEXES, *decoded)


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower().removesuffix(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _settings_error_message(exc: ValidationError | re.error) -> str:
    if isinstance(exc, ValidationError):
        locations = {str(part) for error in exc.errors() for part in error["loc"]}
        numeric = {
            "port",
            "preview_timeout_seconds",
            "max_file_bytes",
            "max_aggregate_bytes",
            "max_fixture_rows",
        }
        if locations & numeric:
            return "port, timeout, and limits must be positive valid numbers"
    return "settings are invalid"
