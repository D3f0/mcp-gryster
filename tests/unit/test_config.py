from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_gryster.config import DomainAccess, Settings, SettingsError

DOMAINS = (
    "organizations",
    "workspaces",
    "documents",
    "schema",
    "records",
    "access",
    "attachments",
    "webhooks",
    "sql",
    "identity",
    "service_accounts",
    "administration",
)


def test_settings_use_safe_policy_and_numeric_defaults(tmp_path: Path) -> None:
    settings = Settings.load({"MCP_GRYSTER_WORKSPACE": str(tmp_path / "workspace")})

    assert settings.policy.identity is DomainAccess.NONE
    assert settings.policy.service_accounts is DomainAccess.NONE
    assert settings.policy.administration is DomainAccess.NONE
    assert settings.policy.records is DomainAccess.READ
    assert settings.port > 0
    assert settings.preview_timeout_seconds > 0
    assert settings.max_file_bytes > 0
    assert settings.max_aggregate_bytes > 0
    assert settings.max_fixture_rows > 0
    assert settings.workspace.is_dir()
    assert settings.workspace.is_absolute()


def test_settings_parse_complete_policy_and_reject_unknown_domains(tmp_path: Path) -> None:
    policy = {domain: "none" for domain in DOMAINS}
    policy["documents"] = "write"
    settings = Settings.load(
        {
            "MCP_GRYSTER_WORKSPACE": str(tmp_path),
            "MCP_GRYSTER_POLICY": json.dumps(policy),
        }
    )

    incomplete = {domain: "none" for domain in DOMAINS if domain != "sql"}
    with pytest.raises(SettingsError, match="policy"):
        Settings.load(
            {
                "MCP_GRYSTER_WORKSPACE": str(tmp_path),
                "MCP_GRYSTER_POLICY": json.dumps(incomplete),
            }
        )
    assert settings.policy.documents is DomainAccess.WRITE

    policy["unknown"] = "read"
    with pytest.raises(SettingsError, match="policy"):
        Settings.load(
            {
                "MCP_GRYSTER_WORKSPACE": str(tmp_path),
                "MCP_GRYSTER_POLICY": json.dumps(policy),
            }
        )


def test_settings_reject_non_loopback_http_without_authentication(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="authentication"):
        Settings.load(
            {
                "MCP_GRYSTER_WORKSPACE": str(tmp_path),
                "MCP_GRYSTER_TRANSPORT": "streamable-http",
                "MCP_GRYSTER_HOST": "0.0.0.0",
            }
        )


def test_settings_accept_authenticated_non_loopback_http(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "MCP_GRYSTER_WORKSPACE": str(tmp_path),
            "MCP_GRYSTER_TRANSPORT": "streamable-http",
            "MCP_GRYSTER_HOST": "0.0.0.0",
            "MCP_GRYSTER_HTTP_AUTH_TOKEN": "a-long-random-test-token",
        }
    )
    assert settings.http_auth_token is not None
    assert settings.http_auth_token.get_secret_value() == "a-long-random-test-token"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("MCP_GRYSTER_PORT", "0"),
        ("MCP_GRYSTER_PREVIEW_TIMEOUT_SECONDS", "-1"),
        ("MCP_GRYSTER_MAX_FILE_BYTES", "0"),
        ("MCP_GRYSTER_MAX_AGGREGATE_BYTES", "-1"),
        ("MCP_GRYSTER_MAX_FIXTURE_ROWS", "0"),
    ],
)
def test_settings_reject_non_positive_numeric_values(
    tmp_path: Path, key: str, value: str
) -> None:
    with pytest.raises(SettingsError, match="positive"):
        Settings.load({"MCP_GRYSTER_WORKSPACE": str(tmp_path), key: value})


def test_settings_load_does_not_read_or_mutate_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_GRYSTER_PORT", "9999")
    supplied = {"MCP_GRYSTER_WORKSPACE": str(tmp_path), "MCP_GRYSTER_PORT": "8123"}
    before = dict(os.environ)

    settings = Settings.load(supplied)

    assert settings.port == 8123
    assert dict(os.environ) == before


def test_settings_models_are_frozen(tmp_path: Path) -> None:
    settings = Settings.load({"MCP_GRYSTER_WORKSPACE": str(tmp_path)})
    with pytest.raises(ValidationError):
        settings.port = 9999  # type: ignore[misc]
    with pytest.raises(ValidationError):
        settings.policy.records = DomainAccess.WRITE  # type: ignore[misc]
