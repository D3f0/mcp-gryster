from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

_BOOTSTRAP_PATH = Path(__file__).resolve().parents[2] / "integration" / "bootstrap.py"
_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location("_integration_bootstrap", _BOOTSTRAP_PATH)
assert _BOOTSTRAP_SPEC is not None and _BOOTSTRAP_SPEC.loader is not None
_BOOTSTRAP = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
_BOOTSTRAP_SPEC.loader.exec_module(_BOOTSTRAP)
_create_api_key = _BOOTSTRAP._create_api_key
_wait_until_ready = _BOOTSTRAP._wait_until_ready
_select_organization = _BOOTSTRAP._select_organization


class _BootstrapHandler(BaseHTTPRequestHandler):
    counts: dict[str, int]
    scenarios: dict[str, list[tuple[int, dict[str, str], bytes]]]
    requests: list[tuple[str, str | None]]

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _respond(self) -> None:
        self.requests.append((self.path, self.headers.get("Cookie")))
        path = self.path.split("?", 1)[0]
        self.counts[path] = self.counts.get(path, 0) + 1
        responses = self.scenarios[path]
        status, headers, body = responses.pop(0) if len(responses) > 1 else responses[0]
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        if "Connection" not in headers:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


@pytest.fixture
def bootstrap_server() -> Iterator[
    tuple[
        str,
        dict[str, int],
        dict[str, list[tuple[int, dict[str, str], bytes]]],
        list[tuple[str, str | None]],
    ]
]:
    counts: dict[str, int] = {}
    scenarios: dict[str, list[tuple[int, dict[str, str], bytes]]] = {}
    requests: list[tuple[str, str | None]] = []
    handler = type(
        "BootstrapHandler",
        (_BootstrapHandler,),
        {"counts": counts, "scenarios": scenarios, "requests": requests},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", counts, scenarios, requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_wait_until_ready_uses_full_readiness_probe(
    bootstrap_server: tuple[
        str,
        dict[str, int],
        dict[str, list[tuple[int, dict[str, str], bytes]]],
        list[tuple[str, str | None]],
    ],
) -> None:
    base_url, counts, scenarios, requests = bootstrap_server
    scenarios["/status"] = [
        (500, {}, b"Grist is alive (ready not ok)."),
        (200, {}, b"Grist is alive (ready ok)."),
    ]

    _wait_until_ready(base_url, timeout=1, retry_interval=0.01)

    assert counts == {"/status": 2}
    assert requests[0][0] == "/status?ready=1"



def test_select_organization_ignores_personal_site() -> None:
    organizations = [
        {"id": 5, "name": "Personal", "domain": "docs-5"},
        {"id": 7, "name": "Integration", "domain": "integration"},
    ]

    assert _select_organization(organizations, "integration") == organizations[1]

def test_create_api_key_retries_transient_responses_and_keeps_session_cookie(
    bootstrap_server: tuple[
        str,
        dict[str, int],
        dict[str, list[tuple[int, dict[str, str], bytes]]],
        list[tuple[str, str | None]],
    ],
) -> None:
    base_url, counts, scenarios, requests = bootstrap_server
    scenarios["/"] = [
        (503, {}, b"starting"),
        (200, {"Set-Cookie": "session=ready; Path=/"}, b"ok"),
    ]
    scenarios["/test/login"] = [
        (502, {}, b"starting"),
        (200, {"Set-Cookie": "auth=logged-in; Path=/"}, b"ok"),
    ]
    scenarios["/api/profile/apikey"] = [
        (429, {}, b"busy"),
        (504, {}, b"busy"),
        (200, {"Content-Type": "text/html; charset=utf-8"}, b"a" * 40),
    ]

    assert _create_api_key(base_url, timeout=1, retry_interval=0.01) == "a" * 40
    assert counts == {"/": 2, "/test/login": 2, "/api/profile/apikey": 3}
    assert requests[2][1] == "session=ready"
    assert set((requests[-1][1] or "").split("; ")) == {"session=ready", "auth=logged-in"}


def test_create_api_key_accepts_json_string_for_compatibility(
    bootstrap_server: tuple[
        str,
        dict[str, int],
        dict[str, list[tuple[int, dict[str, str], bytes]]],
        list[tuple[str, str | None]],
    ],
) -> None:
    base_url, _counts, scenarios, _requests = bootstrap_server
    scenarios["/"] = [(200, {"Set-Cookie": "session=ready; Path=/"}, b"ok")]
    scenarios["/test/login"] = [
        (200, {"Set-Cookie": "auth=logged-in; Path=/"}, b"ok")
    ]
    scenarios["/api/profile/apikey"] = [
        (200, {"Content-Type": "application/json"}, json.dumps("b" * 40).encode())
    ]

    assert _create_api_key(base_url, timeout=1, retry_interval=0.01) == "b" * 40


def test_create_api_key_does_not_retry_permanent_client_errors(
    bootstrap_server: tuple[
        str,
        dict[str, int],
        dict[str, list[tuple[int, dict[str, str], bytes]]],
        list[tuple[str, str | None]],
    ],
) -> None:
    base_url, counts, scenarios, _requests = bootstrap_server
    scenarios["/"] = [(200, {"Set-Cookie": "session=ready; Path=/"}, b"ok")]
    scenarios["/test/login"] = [
        (200, {"Set-Cookie": "auth=logged-in; Path=/"}, b"ok")
    ]
    scenarios["/api/profile/apikey"] = [(401, {}, b"unauthorized")]

    with pytest.raises(HTTPError) as error:
        _create_api_key(base_url, timeout=1, retry_interval=0.01)

    assert error.value.code == 401
    assert counts["/api/profile/apikey"] == 1
