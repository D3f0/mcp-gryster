"""Capability-scoped loopback previews for authored widgets."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import quote

import uvicorn
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.models import StoredPreviewFixture, WidgetMetadata
from mcp_gryster.widget.paths import normalize_authored_path
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.validation import PreviewEvidenceStore

_EMULATOR_VERSION = "1"
_ALLOWED_MIME = MappingProxyType(
    {
        ".css": "text/css; charset=utf-8",
        ".gif": "image/gif",
        ".html": "text/html; charset=utf-8",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json",
        ".mjs": "text/javascript; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }
)
_SAFE_METHOD = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.-]{0,79}$")
_SAFE_LOCATION = re.compile(r"^[A-Za-z0-9._-]{1,100}:[0-9]{1,7}:[0-9]{1,7}$")


class PreviewStatus(BaseModel):
    """One sanitized emulator lifecycle event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["ready", "callback", "event", "unsupported", "uncaught", "handshake"]
    method: str | None = None
    error_class: str | None = None
    category: str | None = None
    location: str | None = None


@dataclass(frozen=True, slots=True)
class PreviewSession:
    """A running preview endpoint and its value-free status stream."""

    session_id: str
    url: str
    emulator_version: str
    project_id: str
    revision: str
    fixture_id: str | None
    _statuses: list[PreviewStatus] = field(repr=False, compare=False)

    @property
    def status(self) -> tuple[PreviewStatus, ...]:
        return tuple(self._statuses)


@dataclass(frozen=True, slots=True)
class StopPreviewResult:
    """Idempotent preview stop result."""

    session_id: str
    stopped: bool


@dataclass(slots=True)
class _Runtime:
    session: PreviewSession
    token: str
    origin: str
    widget_origin: str
    status_nonce: str
    mode: Literal["fixture", "live"]
    source_root: Path
    entrypoint: str
    fixture: StoredPreviewFixture | None
    server: uvicorn.Server
    task: asyncio.Task[None]
    evidence: PreviewEvidenceStore
    successful: bool = False
    failed: bool = False
    bridge_available: bool = True
    smoke_task: asyncio.Task[None] | None = None
    smoke_interval_seconds: float = 30.0

class PreviewManager:
    """Own isolated loopback servers for fixture and live widget previews."""

    def __init__(
        self,
        store: ProjectStore,
        evidence: PreviewEvidenceStore,
        preview_timeout_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._evidence = evidence
        self._preview_timeout_seconds = preview_timeout_seconds
        self._sessions: dict[str, _Runtime] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        project_id: str,
        mode: Literal["fixture", "live"],
        fixture_id: str | None = None,
    ) -> PreviewSession:
        """Start one preview on an ephemeral IPv4 loopback port."""
        if mode not in {"fixture", "live"}:
            raise McpGrysterError("INVALID_ARGUMENT", "Preview mode is invalid")
        if mode == "fixture" and fixture_id is None:
            raise McpGrysterError("INVALID_ARGUMENT", "Fixture preview requires a fixture")
        if mode == "live" and fixture_id is not None:
            raise McpGrysterError("INVALID_ARGUMENT", "Live preview does not accept a fixture")

        snapshot = self._store.read(project_id)
        project_revision = self._store.project_path(project_id) / "revisions" / snapshot.revision
        metadata = self._load_metadata(project_revision, project_id)
        source_root = project_revision / "src"
        entrypoint = metadata.entrypoint
        fixture: StoredPreviewFixture | None = None
        if fixture_id is not None:
            fixture = self._load_fixture(project_revision, fixture_id)

        session_id = secrets.token_urlsafe(24)
        token = secrets.token_urlsafe(32)
        status_nonce = secrets.token_urlsafe(32)
        statuses: list[PreviewStatus] = []
        session = PreviewSession(
            session_id=session_id,
            url="",
            emulator_version=_EMULATOR_VERSION,
            project_id=project_id,
            revision=snapshot.revision,
            fixture_id=fixture_id,
            _statuses=statuses,
        )
        holder: dict[str, _Runtime] = {}
        app = self._build_app(holder)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            access_log=False,
            server_header=False,
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(), name=f"preview-{session_id}")
        try:
            await self._wait_started(server, task)
            port = self._server_port(server)
            origin = f"http://127.0.0.1:{port}"
            widget_origin = f"http://localhost:{port}"
            entry_url = (
                f"{origin}/t/{token}/harness"
                if mode == "fixture"
                else f"{origin}/t/{token}/widget/{quote(entrypoint, safe='/')}"
            )
            session = PreviewSession(
                session_id=session_id,
                url=entry_url,
                emulator_version=_EMULATOR_VERSION,
                project_id=project_id,
                revision=snapshot.revision,
                fixture_id=fixture_id,
                _statuses=statuses,
            )
            runtime = _Runtime(
                session=session,
                token=token,
                origin=origin,
                widget_origin=widget_origin,
                status_nonce=status_nonce,
                mode=mode,
                source_root=source_root,
                entrypoint=entrypoint,
                fixture=fixture,
                server=server,
                task=task,
                evidence=self._evidence,
                smoke_interval_seconds=self._preview_timeout_seconds,
            )
            holder["runtime"] = runtime
            async with self._lock:
                self._sessions[session_id] = runtime
            return session
        except BaseException:
            await self._shutdown(server, task)
            raise

    async def stop(self, session_id: str) -> StopPreviewResult:
        """Stop one preview, invalidating its capability URL."""
        async with self._lock:
            runtime = self._sessions.pop(session_id, None)
        if runtime is None:
            return StopPreviewResult(session_id=session_id, stopped=False)
        await self._stop_runtime(runtime)
        return StopPreviewResult(session_id=session_id, stopped=True)

    async def stop_all(self) -> None:
        """Stop every server owned by this manager."""
        async with self._lock:
            runtimes = tuple(self._sessions.values())
            self._sessions.clear()
        if runtimes:
            await asyncio.gather(*(self._stop_runtime(runtime) for runtime in runtimes))

    async def _stop_runtime(self, runtime: _Runtime) -> None:
        await _cancel_smoke(runtime)
        await self._shutdown(runtime.server, runtime.task)


    @staticmethod
    async def _shutdown(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except TimeoutError:
            server.force_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    async def _wait_started(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("preview server stopped before startup")
            await asyncio.sleep(0.005)

    @staticmethod
    def _server_port(server: uvicorn.Server) -> int:
        servers = getattr(server, "servers", None)
        if not servers or not servers[0].sockets:
            raise RuntimeError("preview server did not bind a socket")
        return int(servers[0].sockets[0].getsockname()[1])

    @staticmethod
    def _load_metadata(project_revision: Path, project_id: str) -> WidgetMetadata:
        try:
            raw = json.loads((project_revision / "widget.json").read_bytes())
            if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
                raise ValueError
            return WidgetMetadata.model_validate(
                {
                    "project_id": project_id,
                    "widget_id": raw.get("widgetId"),
                    "name": raw.get("name"),
                    "description": raw.get("description", ""),
                    "entrypoint": raw.get("entrypoint", "index.html"),
                    "access_level": raw.get("accessLevel", "none"),
                    "columns": raw.get("columns", []),
                    "authors": raw.get("authors", []),
                }
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise McpGrysterError("INVALID_ARGUMENT", "Widget metadata is invalid") from exc

    @staticmethod
    def _load_fixture(project_revision: Path, fixture_id: str) -> StoredPreviewFixture:
        if fixture_id in {"", ".", ".."} or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in fixture_id
        ):
            raise McpGrysterError("INVALID_ARGUMENT", "Fixture ID is invalid")
        path = project_revision / "fixtures" / f"{fixture_id}.json"
        try:
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError
            fixture = StoredPreviewFixture.model_validate_json(path.read_bytes())
        except FileNotFoundError as exc:
            raise McpGrysterError("INVALID_ARGUMENT", "Fixture does not exist") from exc
        if fixture.fixture_id != fixture_id:
            raise McpGrysterError("INVALID_ARGUMENT", "Fixture ID does not match fixture content")
        return fixture

    @staticmethod
    def _build_app(holder: dict[str, _Runtime]) -> Starlette:
        async def dispatch(request: Request) -> Response:
            runtime = holder.get("runtime")
            if runtime is None:
                return Response(status_code=503)
            host_kind = _host_kind(request, runtime)
            if host_kind is None:
                return Response(status_code=403)
            if request.path_params["token"] != runtime.token:
                return Response(status_code=404)
            route = request.path_params["route"]
            if runtime.mode == "live":
                if host_kind == "harness" and route.startswith("widget/"):
                    return _asset_response(runtime, route.removeprefix("widget/"), inject_bridge=False)
                return Response(status_code=404)
            if host_kind == "harness":
                if route == "harness" and request.method == "GET":
                    return _harness_response(runtime)
                if route == "grist-bridge.js" and request.method == "GET":
                    if not runtime.bridge_available:
                        return Response(status_code=410, headers=_security_headers())
                    runtime.bridge_available = False
                    return _bridge_response(runtime)
                if route == "fixture" and request.method == "GET":
                    if not _origin_allowed(request, runtime):
                        return Response(status_code=403)
                    return _fixture_response(runtime)
                if route in {"fixture", "status"} and request.method == "OPTIONS":
                    return _cors_preflight_response(request, runtime)
                if route == "status" and request.method == "POST":
                    return await _status_response(request, runtime)
                return Response(status_code=404)
            if route.startswith("widget/") and request.method == "GET":
                return _asset_response(runtime, route.removeprefix("widget/"), inject_bridge=True)
            return Response(status_code=404)

        return Starlette(
            routes=[Route("/t/{token}/{route:path}", dispatch, methods=["GET", "POST", "OPTIONS"])]
        )


def _security_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
    }


def _host_kind(request: Request, runtime: _Runtime) -> Literal["harness", "widget"] | None:
    host = request.headers.get("host", "")
    if host == urlsplit_netloc(runtime.origin):
        return "harness"
    if host == urlsplit_netloc(runtime.widget_origin):
        return "widget"
    return None


def urlsplit_netloc(url: str) -> str:
    return url.removeprefix("http://")


def _origin_allowed(request: Request, runtime: _Runtime) -> bool:
    return request.headers.get("origin") == runtime.widget_origin

def _cors_headers(runtime: _Runtime) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": runtime.widget_origin,
        "Access-Control-Allow-Headers": "Content-Type, X-Mcp-Gryster-Status",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Vary": "Origin",
    }


def _cors_preflight_response(request: Request, runtime: _Runtime) -> Response:
    if not _origin_allowed(request, runtime):
        return Response(status_code=403)
    return Response(status_code=204, headers={**_security_headers(), **_cors_headers(runtime)})


def _harness_response(runtime: _Runtime) -> Response:
    template = (
        files("mcp_gryster.widget.static")
        .joinpath("harness.html")
        .read_text(encoding="utf-8")
    )
    widget_url = (
        f"{runtime.widget_origin}/t/{runtime.token}/widget/{quote(runtime.entrypoint, safe='/')}"
    )
    body = template.replace("__WIDGET_URL__", widget_url)
    headers = _security_headers()
    headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
        "frame-src 'self'", f"frame-src {runtime.widget_origin}"
    )
    return HTMLResponse(body, headers=headers)


def _bridge_response(runtime: _Runtime) -> Response:
    template = (
        files("mcp_gryster.widget.static")
        .joinpath("grist-bridge.js")
        .read_text(encoding="utf-8")
    )
    content = (
        template.replace("__REPORT_ORIGIN__", runtime.origin)
        .replace("__STATUS_NONCE__", runtime.status_nonce)
        .encode()
    )
    headers = {
        **_security_headers(),
        "Content-Type": "text/javascript; charset=utf-8",
    }
    return Response(content, media_type=None, headers=headers)


def _fixture_response(runtime: _Runtime) -> Response:
    if runtime.fixture is None:
        return Response(status_code=404)
    body = runtime.fixture.model_dump(mode="json", by_alias=True)
    body["emulatorVersion"] = _EMULATOR_VERSION
    return JSONResponse(body, headers={**_security_headers(), **_cors_headers(runtime)})


def _asset_response(runtime: _Runtime, requested: str, *, inject_bridge: bool) -> Response:
    if not requested or requested.endswith("/"):
        return Response(status_code=404)
    try:
        normalized = normalize_authored_path(f"src/{requested}").removeprefix("src/")
    except McpGrysterError:
        return Response(status_code=404)
    path = runtime.source_root.joinpath(*PurePosixPath(normalized).parts)
    try:
        if path.is_symlink() or not path.is_file():
            return Response(status_code=404)
        content = path.read_bytes()
    except OSError:
        return Response(status_code=404)
    media_type = _ALLOWED_MIME.get(path.suffix.lower())
    if media_type is None:
        return Response(status_code=415)
    headers = _security_headers()
    if inject_bridge and path.suffix.lower() == ".html":
        bridge_url = f"{runtime.origin}/t/{runtime.token}/grist-bridge.js"
        bridge = f'<script src="{bridge_url}"></script>'.encode()
        content = bridge + content
        headers.pop("X-Frame-Options")
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "script-src 'self'", f"script-src 'self' {runtime.origin}"
        ).replace(
            "connect-src 'self'", f"connect-src 'self' {runtime.origin}"
        ) + f"; frame-ancestors {runtime.origin}"
    return Response(content, media_type=None, headers={**headers, "Content-Type": media_type})


async def _status_response(request: Request, runtime: _Runtime) -> Response:
    if not _origin_allowed(request, runtime):
        return Response(status_code=403)
    if request.headers.get("x-mcp-gryster-status") != runtime.status_nonce:
        return Response(status_code=403)
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        return Response(status_code=415)
    if request.headers.get("content-length") not in {None, ""}:
        try:
            if int(request.headers["content-length"]) > 1024:
                return Response(status_code=413)
        except ValueError:
            return Response(status_code=400)
    try:
        body = await request.body()
        if len(body) > 1024:
            return Response(status_code=413)
        raw = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(status_code=400)
    status = _sanitize_status(raw)
    if status is None:
        return Response(status_code=400)
    if status.kind == "handshake" and not _handshake_allowed(runtime):
        return Response(status_code=409)
    if runtime.successful and status.kind not in {"unsupported", "uncaught"}:
        return Response(status_code=409)
    runtime.session._statuses.append(status)
    if status.kind in {"unsupported", "uncaught"}:
        runtime.failed = True
        runtime.successful = False
        await _cancel_smoke(runtime)
        runtime.evidence.discard(
            runtime.session.project_id,
            runtime.session.revision,
            runtime.session.fixture_id,
        )
    if status.kind == "handshake":
        runtime.smoke_task = asyncio.create_task(
            _complete_smoke_interval(runtime),
            name=f"preview-smoke-{runtime.session.session_id}",
        )
    return Response(status_code=204, headers=_cors_headers(runtime))

def _handshake_allowed(runtime: _Runtime) -> bool:
    if runtime.failed or runtime.successful or runtime.smoke_task is not None:
        return False
    statuses = runtime.session._statuses
    ready_indexes = [index for index, status in enumerate(statuses) if status.kind == "ready"]
    if len(ready_indexes) != 1:
        return False
    ready_index = ready_indexes[0]
    if any(status.kind != "callback" for status in statuses[:ready_index]):
        return False
    return [(status.kind, status.method) for status in statuses[ready_index:]] == [
        ("ready", None),
        ("event", "options"),
        ("event", "records"),
        ("event", "selectedRecord"),
    ]

async def _cancel_smoke(runtime: _Runtime) -> None:
    task = runtime.smoke_task
    runtime.smoke_task = None
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _complete_smoke_interval(runtime: _Runtime) -> None:
    await asyncio.sleep(runtime.smoke_interval_seconds)
    if runtime.failed:
        return
    runtime.successful = True
    runtime.evidence.record_success(
        runtime.session.project_id,
        runtime.session.revision,
        runtime.session.fixture_id,
    )



def _sanitize_status(raw: Any) -> PreviewStatus | None:
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    if kind in {"ready", "handshake"} and set(raw) == {"kind"}:
        return PreviewStatus(kind=kind)
    method = raw.get("method")
    if (
        kind in {"callback", "event"}
        and set(raw) == {"kind", "method"}
        and isinstance(method, str)
        and method
        in {
            "onOptions",
            "onRecords",
            "onRecord",
            "options",
            "records",
            "selectedRecord",
        }
    ):
        return PreviewStatus(kind=kind, method=method)
    if (
        kind == "unsupported"
        and set(raw) == {"kind", "method"}
        and isinstance(method, str)
        and _SAFE_METHOD.fullmatch(method)
    ):
        return PreviewStatus(
            kind="unsupported", method=method, error_class="GristPreviewUnsupportedError"
        )
    if kind == "uncaught":
        error_class = raw.get("errorClass")
        category = raw.get("category")
        location = raw.get("location")
        if (
            set(raw) == {"kind", "errorClass", "category", "location"}
            and isinstance(error_class, str)
            and error_class
            in {
                "AggregateError",
                "Error",
                "EvalError",
                "RangeError",
                "ReferenceError",
                "SyntaxError",
                "TypeError",
                "URIError",
            }
            and category in {"runtime", "promise"}
            and isinstance(location, str)
            and _SAFE_LOCATION.fullmatch(location)
        ):
            return PreviewStatus(
                kind="uncaught",
                error_class=error_class,
                category=category,
                location=location,
            )
    return None
