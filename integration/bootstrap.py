from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, OpenerDirector, Request, build_opener, urlopen

from pygrister.api import GristApi  # type: ignore[import-untyped]
from pygrister.exceptions import GristApiException  # type: ignore[import-untyped]
from requests.exceptions import RequestException

_EMAIL = "integration@example.test"
_SENSITIVE_OUTPUT = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;\]\}\"']+"),
    re.compile(r"(?i)((?:api[-_ ]?key|token)\s*[=:]\s*)[^\s,;\]\}\"']+"),
    re.compile(r'(?i)("(?:api_key|key|token)"\s*:\s*")[^"]+(\")'),
)
_API_KEY_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_INTERVAL = 0.5


def _sanitize_error(value: object) -> str:
    sanitized = str(value)
    for pattern in _SENSITIVE_OUTPUT:
        sanitized = pattern.sub(
            lambda match: f"{match.group(1)}[REDACTED]{match.group(2) if match.lastindex == 2 else ''}",
            sanitized,
        )
    return sanitized


def _wait_until_ready(
    base_url: str, timeout: float = 120.0, retry_interval: float = _RETRY_INTERVAL
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urlopen(f"{base_url}/status?ready=1", timeout=min(5, timeout)) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, TimeoutError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Grist did not become ready")
        time.sleep(retry_interval)


def _open_when_ready(
    opener: OpenerDirector,
    request: str | Request,
    deadline: float,
    retry_interval: float,
) -> Any:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Grist login did not become ready")
        try:
            return opener.open(request, timeout=min(10, remaining))
        except HTTPError as exc:
            if exc.code not in _TRANSIENT_HTTP_STATUSES:
                raise
            exc.close()
        except (URLError, TimeoutError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Grist login did not become ready")
        time.sleep(min(retry_interval, remaining))


def _create_api_key(
    base_url: str, timeout: float = 120.0, retry_interval: float = _RETRY_INTERVAL
) -> str:
    deadline = time.monotonic() + timeout
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    with _open_when_ready(opener, f"{base_url}/", deadline, retry_interval):
        pass
    query = urlencode({"username": _EMAIL, "name": "Integration User", "next": base_url})
    with _open_when_ready(
        opener, f"{base_url}/test/login?{query}", deadline, retry_interval
    ):
        pass
    request = Request(
        f"{base_url}/api/profile/apikey",
        data=json.dumps({"force": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open_when_ready(opener, request, deadline, retry_interval) as response:
        payload = response.read().decode("utf-8").strip()
    if payload.startswith('"'):
        decoded = json.loads(payload)
        if not isinstance(decoded, str):
            raise RuntimeError("Grist returned an invalid API key")
        key = decoded
    else:
        key = payload
    if _API_KEY_PATTERN.fullmatch(key) is None:
        raise RuntimeError("Grist returned an invalid API key")
    return key


def _expect_success(operation: str, response: tuple[int, object]) -> object:
    status, data = response
    if not 200 <= status < 300:
        raise RuntimeError(f"{operation} failed with status {status}")
    return data


def _select_organization(organizations: object, team_site: str) -> dict[str, object]:
    if not isinstance(organizations, list):
        raise TypeError("Grist returned an invalid organization list")
    matches = [
        organization
        for organization in organizations
        if isinstance(organization, dict)
        and (organization.get("domain") == team_site or organization.get("name") == team_site)
    ]
    if len(matches) != 1 or "id" not in matches[0]:
        raise RuntimeError("Expected exactly one matching Grist organization")
    return matches[0]


def _create_dataset(base_url: str, api_key: str) -> dict[str, object]:
    team_site = "integration"
    config = {
        "GRIST_API_KEY": api_key,
        "GRIST_SELF_MANAGED": "Y",
        "GRIST_SELF_MANAGED_HOME": base_url,
        "GRIST_SELF_MANAGED_SINGLE_ORG": "Y",
        "GRIST_SERVER_PROTOCOL": "http://",
        "GRIST_API_SERVER": "localhost",
        "GRIST_API_ROOT": "api",
        "GRIST_TEAM_SITE": team_site,
        "GRIST_WORKSPACE_ID": "0",
        "GRIST_DOC_ID": "bootstrap",
        "GRIST_RAISE_ERROR": "Y",
        "GRIST_SAFEMODE": "N",
    }
    api = GristApi(config=config)
    organizations = _expect_success("list organizations", api.list_team_sites())
    organization = _select_organization(organizations, team_site)
    org_id = organization["id"]
    workspace_id = _expect_success("create workspace", api.add_workspace("Integration Workspace"))
    doc_id = _expect_success(
        "create document", api.add_doc("Integration Document", ws_id=workspace_id)
    )
    api.update_config(
        {"GRIST_WORKSPACE_ID": str(workspace_id), "GRIST_DOC_ID": str(doc_id)}
    )
    tables = _expect_success(
        "create People table",
        api.add_tables(
            [
                {
                    "id": "People",
                    "columns": [
                        {"id": "Name", "fields": {"label": "Name", "type": "Text"}},
                        {"id": "Email", "fields": {"label": "Email", "type": "Text"}},
                        {"id": "Secret", "fields": {"label": "Secret", "type": "Text"}},
                        {
                            "id": "Attachment",
                            "fields": {"label": "Attachment", "type": "Attachments"},
                        },
                    ],
                }
            ]
        ),
    )
    if not isinstance(tables, list) or not tables:
        raise RuntimeError("Grist returned an invalid table list")
    table_id = tables[0]
    _expect_success(
        "seed People records",
        api.add_records(
            table_id,
            [
                {
                    "Name": "Ada Lovelace",
                    "Email": "ada@example.test",
                    "Secret": "analytical-engine",
                },
                {
                    "Name": "Grace Hopper",
                    "Email": "grace@example.test",
                    "Secret": "compiler",
                },
            ],
        ),
    )
    return {
        "org_id": org_id,
        "workspace_id": workspace_id,
        "doc_id": doc_id,
        "table_id": table_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    try:
        _wait_until_ready(base_url)
        api_key = _create_api_key(base_url)
        ids = _create_dataset(base_url, api_key)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"api_key": api_key, **ids}, stream)
        os.chmod(args.output, 0o600)
    except (
        GristApiException,
        HTTPError,
        OSError,
        RequestException,
        RuntimeError,
        TimeoutError,
        TypeError,
        URLError,
        ValueError,
    ) as exc:
        print(
            f"bootstrap failed: {type(exc).__name__}: {_sanitize_error(exc)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
