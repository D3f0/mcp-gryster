from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

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
_SESSION_FAILED = False
_ACTIVE_PROJECT: str | None = None
_SECRET_FILE: Path | None = None




def _run_compose(
    project: str,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
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
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def _published_base_url(result: Any) -> str:
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or len(lines) != 1:
        raise RuntimeError("Docker Compose did not publish a usable Grist port")
    match = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})", lines[0])
    if match is None or not 1 <= int(match.group(1)) <= 65_535:
        raise RuntimeError("Docker Compose did not publish a usable Grist port")
    return f"http://{lines[0]}"


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
    fallback = tuple(filter(None, (os.environ.get("GRIST_API_KEY"),)))
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    return tuple(
        value for key, value in payload.items() if "key" in key.lower() and isinstance(value, str)
    )


def _diagnostic_output(project: str, *arguments: str) -> str:
    try:
        return _run_compose(project, *arguments, check=False).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<unavailable: {type(exc).__name__}>\n"


def _print_compose_diagnostics(project: str, secrets: tuple[str, ...]) -> None:
    status = _diagnostic_output(project, "ps", "--all")
    logs = _diagnostic_output(project, "logs", "--no-color", "--tail", "200")
    print(
        "\nGrist Compose status:\n"
        + _sanitize(status, secrets)
        + "\nGrist Compose logs (last 200 lines):\n"
        + _sanitize(logs, secrets),
        file=sys.stderr,
    )


def _node_failed(node: Any) -> bool:
    return any(
        bool(getattr(getattr(node, f"rep_{phase}", None), "failed", False))
        for phase in ("setup", "call", "teardown")
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Iterator[None]:
    global _SESSION_FAILED
    outcome = yield
    report = cast(Any, outcome).get_result()
    setattr(item, f"rep_{report.when}", report)
    _SESSION_FAILED = _SESSION_FAILED or bool(report.failed)
    if (
        report.failed
        and report.when in {"setup", "teardown"}
        and _ACTIVE_PROJECT is not None
    ):
        secrets = _read_secrets(_SECRET_FILE) if _SECRET_FILE is not None else ()
        _print_compose_diagnostics(_ACTIVE_PROJECT, secrets)


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

def _stop_compose(
    project: str, secrets: tuple[str, ...], *, preserve_failure: bool
) -> None:
    try:
        result = _run_compose(
            project,
            "down",
            "--volumes",
            "--remove-orphans",
            check=False,
        )
        teardown_failed = result.returncode != 0
    except (OSError, subprocess.SubprocessError):
        teardown_failed = True

    if not teardown_failed:
        return

    _print_compose_diagnostics(project, secrets)
    if preserve_failure:
        print("Docker Compose teardown failed", file=sys.stderr)
    else:
        pytest.fail("Docker Compose teardown failed")



@pytest.fixture(scope="session")
def grist_secret_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("grist-diagnostics") / "secrets.json"


@pytest.fixture(scope="session")
def compose_project(grist_secret_file: Path) -> Iterator[str]:
    global _ACTIVE_PROJECT, _SECRET_FILE
    if not _docker_available():
        message = "Docker is required for Grist integration tests"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    project = f"mcp-gryster-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    failed = False
    _ACTIVE_PROJECT = project
    _SECRET_FILE = grist_secret_file
    try:
        _run_compose(project, "up", "--detach", "--wait")
        yield project
    except BaseException:
        failed = True
        _print_compose_diagnostics(project, _read_secrets(grist_secret_file))
        raise
    finally:
        try:
            _stop_compose(
                project,
                _read_secrets(grist_secret_file),
                preserve_failure=failed or _SESSION_FAILED,
            )
        finally:
            grist_secret_file.unlink(missing_ok=True)
            _ACTIVE_PROJECT = None
            _SECRET_FILE = None


@pytest.fixture(scope="session")
def grist_base_url(compose_project: str) -> str:
    result = _run_compose(compose_project, "port", "grist", "8484", check=False)
    try:
        return _published_base_url(result)
    except RuntimeError:
        _print_compose_diagnostics(compose_project, ())
        raise


@pytest.fixture(autouse=True)
def compose_failure_diagnostics(
    request: pytest.FixtureRequest,
    compose_project: str,
    grist_secret_file: Path,
) -> Iterator[None]:
    yield
    if _node_failed(request.node):
        _print_compose_diagnostics(compose_project, _read_secrets(grist_secret_file))


@pytest.fixture(scope="session")
def grist_bootstrap(
    grist_base_url: str,
    grist_secret_file: Path,
    compose_project: str,
) -> dict[str, Any]:
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
        _print_compose_diagnostics(compose_project, _read_secrets(grist_secret_file))
        pytest.fail("Grist bootstrap failed:\n" + _sanitize(result.stdout))
    assert output.stat().st_mode & 0o777 == 0o600
    raw_payload = json.loads(output.read_text())
    if not isinstance(raw_payload, dict) or any(
        not isinstance(key, str) for key in raw_payload
    ):
        _print_compose_diagnostics(compose_project, _read_secrets(grist_secret_file))
        pytest.fail("Grist bootstrap returned invalid data")
    return cast(dict[str, Any], raw_payload)


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
async def integration_mcp_client(
    integration_settings: Settings,
) -> AsyncIterator[Client[Any]]:
    async with Client(create_app(integration_settings)) as client:
        yield client


@pytest.fixture
async def integration_read_mcp_client(
    grist_base_url: str, grist_api_key: str, grist_ids: dict[str, Any]
) -> AsyncIterator[Client[Any]]:
    settings = _settings(
        base_url=grist_base_url,
        api_key=grist_api_key,
        ids=grist_ids,
        records=DomainAccess.READ,
    )
    async with Client(create_app(settings)) as client:
        yield client
