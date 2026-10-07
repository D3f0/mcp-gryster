from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from mcp_gryster.app import create_app
from mcp_gryster.config import DomainAccess, DomainPolicy, Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "integration" / "compose.yml"
_KEY_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;\]\}\"']+"),
    re.compile(r"(?i)((?:api[-_ ]?key|token)\s*[=:]\s*)[^\s,;\]\}\"']+"),
    re.compile(r'(?i)("(?:api_key|key|token)"\s*:\s*")[^"]+(\")'),
)


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run_compose(
    project: str,
    port: int,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GRIST_PORT"] = str(port)
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(_COMPOSE_FILE),
            *arguments,
        ],
        cwd=_REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def _sanitize(text: str, secrets: tuple[str, ...] = ()) -> str:
    sanitized = text
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    for pattern in _KEY_PATTERNS:
        sanitized = pattern.sub(
            lambda match: f"{match.group(1)}[REDACTED]{match.group(2) if match.lastindex == 2 else ''}",
            sanitized,
        )
    return sanitized

def _read_secrets(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return tuple(filter(None, (os.environ.get("GRIST_API_KEY"),)))
    return tuple(
        value for key, value in payload.items() if "key" in key.lower() and isinstance(value, str)
    )


def _print_compose_diagnostics(project: str, port: int, secrets: tuple[str, ...]) -> None:
    status = _run_compose(project, port, "ps", "--all", check=False).stdout
    logs = _run_compose(
        project, port, "logs", "--no-color", "--tail", "200", check=False
    ).stdout
    print(
        "\nGrist Compose status:\n"
        + _sanitize(status, secrets)
        + "\nGrist Compose logs (last 200 lines):\n"
        + _sanitize(logs, secrets),
        file=sys.stderr,
    )


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


@pytest.fixture(scope="session")
def grist_secret_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("grist-diagnostics") / "secrets.json"


@pytest.fixture(scope="session")
def grist_base_url(grist_secret_file: Path) -> Iterator[str]:
    if not _docker_available():
        message = "Docker is required for Grist integration tests"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    project = f"mcp-gryster-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    port = _available_port()
    base_url = f"http://127.0.0.1:{port}"
    secret_file = grist_secret_file
    failed = False
    try:
        _run_compose(project, port, "up", "--detach", "--wait")
        yield base_url
    except BaseException:
        failed = True
        secrets = _read_secrets(secret_file)
        _print_compose_diagnostics(project, port, secrets)
        raise
    finally:
        down = _run_compose(
            project,
            port,
            "down",
            "--volumes",
            "--remove-orphans",
            check=False,
        )
        if down.returncode != 0 and not failed:
            secrets = _read_secrets(secret_file)
            _print_compose_diagnostics(project, port, secrets)
            pytest.fail("Docker Compose teardown failed")
        secret_file.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def grist_bootstrap(grist_base_url: str, grist_secret_file: Path) -> dict[str, Any]:
    output = grist_secret_file
    environment = os.environ.copy()
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-m",
            "integration.bootstrap",
            "--base-url",
            grist_base_url,
            "--output",
            str(output),
        ],
        cwd=_REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("Grist bootstrap failed:\n" + _sanitize(result.stdout))
    assert output.stat().st_mode & 0o777 == 0o600
    payload = json.loads(output.read_text())
    return payload


@pytest.fixture(scope="session")
def grist_api_key(grist_bootstrap: dict[str, Any]) -> str:
    return str(grist_bootstrap["api_key"])


@pytest.fixture(scope="session")
def grist_ids(
    grist_bootstrap: dict[str, Any], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    return {
        key: value for key, value in grist_bootstrap.items() if key != "api_key"
    } | {"workspace_root": tmp_path_factory.mktemp("mcp-gryster-integration")}


def _settings(
    *,
    base_url: str,
    api_key: str,
    ids: dict[str, Any],
    records: DomainAccess,
) -> Settings:
    return Settings(
        workspace_root=ids["workspace_root"],
        policy=DomainPolicy(
            organizations=DomainAccess.READ,
            workspaces=DomainAccess.READ,
            documents=DomainAccess.READ,
            schema=DomainAccess.READ,
            records=records,
        ),
        grist_config={
            "GRIST_API_KEY": api_key,
            "GRIST_SELF_MANAGED": "Y",
            "GRIST_SELF_MANAGED_HOME": base_url,
            "GRIST_SELF_MANAGED_SINGLE_ORG": "Y",
            "GRIST_SERVER_PROTOCOL": "http://",
            "GRIST_API_SERVER": "localhost",
            "GRIST_API_ROOT": "api",
            "GRIST_TEAM_SITE": "integration",
            "GRIST_WORKSPACE_ID": str(ids["workspace_id"]),
            "GRIST_DOC_ID": str(ids["doc_id"]),
        },
        redaction_regexes=(
            r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+",
            r"(?i)(api[-_ ]?key\s*[=:]\s*)[^\s,;]+",
            r"(?i)(token\s*[=:]\s*)[^\s,;]+",
            r"(?i)^email$",
            r"(?i)^secret$",
        ),
    )


@pytest.fixture(scope="session")
def integration_settings(
    grist_base_url: str, grist_api_key: str, grist_ids: dict[str, Any]
) -> Settings:
    return _settings(
        base_url=grist_base_url,
        api_key=grist_api_key,
        ids=grist_ids,
        records=DomainAccess.WRITE,
    )


@pytest.fixture
async def integration_mcp_client(integration_settings: Settings) -> Iterator[Client[Any]]:
    async with Client(create_app(integration_settings)) as client:
        yield client


@pytest.fixture
async def integration_read_mcp_client(
    grist_base_url: str, grist_api_key: str, grist_ids: dict[str, Any]
) -> Iterator[Client[Any]]:
    settings = _settings(
        base_url=grist_base_url,
        api_key=grist_api_key,
        ids=grist_ids,
        records=DomainAccess.READ,
    )
    async with Client(create_app(settings)) as client:
        yield client
