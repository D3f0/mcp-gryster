from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from mcp_gryster.artifacts import ArtifactStore
from mcp_gryster.audit import AuditEvent, AuditSink
from mcp_gryster.config import DomainAccess, DomainPolicy, Settings
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.grist.adapter import GristAdapter
from mcp_gryster.grist.models import MethodSpec, ParameterSpec
from mcp_gryster.grist.policy import Policy
from mcp_gryster.grist.registry import MethodRegistry


class RecordingAudit(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


class FakeApi:
    created = 0
    configs: ClassVar[list[dict[str, str]]] = []
    getattr_names: ClassVar[list[str]] = []
    constructor_delay = 0.0

    def __init__(self, config: dict[str, str]) -> None:
        time.sleep(type(self).constructor_delay)
        type(self).created += 1
        type(self).configs.append(config)

    def __getattribute__(self, name: str) -> Any:
        if name not in {"getattr_names", "created", "configs", "__class__"}:
            type(self).getattr_names.append(name)
        return super().__getattribute__(name)

    def fetch(self, count: int, enabled: bool = False) -> tuple[int, object]:
        return 200, {"count": count, "enabled": enabled}

    def fail_status(self) -> tuple[int, object]:
        return 503, {"private": "response"}

    def explode(self) -> tuple[int, object]:
        raise RuntimeError("private argument and response")

    def write_file(self, filename: Path) -> tuple[int, object]:
        filename.write_bytes(b"download")
        return 200, None

    def bytes_result(self) -> tuple[int, object]:
        return 201, b"binary"

    def pages(self, chunk: int = 2) -> object:
        def iterator() -> object:
            while True:
                yield 200, list(range(chunk))

        return iterator()

    def slow(self, delay: float) -> tuple[int, object]:
        time.sleep(delay)
        return 200, "done"


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeApi.created = 0
    FakeApi.configs = []
    FakeApi.getattr_names = []
    FakeApi.constructor_delay = 0.0


def spec(
    name: str,
    *,
    effect: str = "read",
    parameters: dict[str, ParameterSpec] | None = None,
    result_kind: str = "json",
) -> MethodSpec:
    return MethodSpec.model_validate(
        {
            "name": name,
            "domain": "records",
            "effect": effect,
            "parameters": parameters or {},
            "result_kind": result_kind,
        }
    )


def adapter(
    tmp_path: Path,
    method: MethodSpec,
    *,
    access: DomainAccess = DomainAccess.READ,
    max_pages: int = 10,
    max_items: int = 100,
    max_file_bytes: int = 5 * 1024 * 1024,
    max_aggregate_bytes: int = 25 * 1024 * 1024,
) -> tuple[GristAdapter, RecordingAudit]:
    registry = MethodRegistry.model_construct(pygrister_version="0.10.0", methods=(method,))
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        policy=DomainPolicy(records=access),
        grist_config={"GRIST_API_KEY": "secret", "UNSAFE_EXTRA": "no"},
        max_file_bytes=max_file_bytes,
        max_aggregate_bytes=max_aggregate_bytes,
    )
    settings.workspace_root.mkdir(exist_ok=True)
    audit = RecordingAudit()
    return (
        GristAdapter(
            registry,
            Policy(settings.policy),
            ArtifactStore(tmp_path / "artifacts"),
            settings,
            audit,
            api_factory=FakeApi,
            max_pages=max_pages,
            max_items=max_items,
        ),
        audit,
    )


@pytest.mark.asyncio
async def test_authorizes_and_binds_before_creating_client(tmp_path: Path) -> None:
    denied, denied_audit = adapter(tmp_path, spec("fetch", effect="write"))
    with pytest.raises(McpGrysterError) as caught:
        await denied.call("fetch", {"count": 1})
    assert caught.value.code == "PERMISSION_DENIED"
    assert FakeApi.created == 0
    assert denied_audit.events[-1].status == "denied"

    allowed, audit = adapter(
        tmp_path,
        spec(
            "fetch",
            parameters={
                "count": ParameterSpec(required=True),
                "enabled": ParameterSpec(required=False, default=False),
            },
        ),
    )
    with pytest.raises(McpGrysterError) as caught:
        await allowed.call("fetch", {"wrong": 1})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert FakeApi.created == 0
    assert audit.events[-1].status == "error"


@pytest.mark.asyncio
async def test_unknown_private_and_wrong_json_types_never_reach_getattr(tmp_path: Path) -> None:
    checked, _ = adapter(
        tmp_path,
        spec(
            "fetch",
            parameters={
                "count": ParameterSpec(required=True),
                "enabled": ParameterSpec(required=False, default=False),
            },
        ),
    )
    for method in ("unknown", "_private"):
        with pytest.raises(McpGrysterError) as caught:
            await checked.call(method, {})
        assert caught.value.code == "METHOD_NOT_SUPPORTED"
    with pytest.raises(McpGrysterError) as caught:
        await checked.call("fetch", {"count": "1"})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert FakeApi.created == 0
    assert FakeApi.getattr_names == []


@pytest.mark.asyncio
async def test_fresh_client_config_and_structured_result(tmp_path: Path) -> None:
    checked, audit = adapter(
        tmp_path,
        spec(
            "fetch",
            parameters={
                "count": ParameterSpec(required=True),
                "enabled": ParameterSpec(required=False, default=False),
            },
        ),
    )
    first = await checked.call("fetch", {"count": 3, "enabled": True})
    second = await checked.call("fetch", {"count": 4})

    assert first.status_code == 200
    assert first.data == {"count": 3, "enabled": True}
    assert second.data == {"count": 4, "enabled": False}
    assert FakeApi.created == 2
    assert FakeApi.configs == [
        {"GRIST_API_KEY": "secret", "GRIST_RAISE_ERROR": "Y", "GRIST_SAFEMODE": "Y"}
    ] * 2
    assert all(event.status == "success" and event.duration_ms >= 0 for event in audit.events)
    assert "arguments" not in audit.events[0].model_dump()


@pytest.mark.asyncio
async def test_blocking_call_runs_in_thread(tmp_path: Path) -> None:
    checked, _ = adapter(
        tmp_path,
        spec("slow", parameters={"delay": ParameterSpec(required=True)}),
    )
    marker = False

    async def mark() -> None:
        nonlocal marker
        await asyncio.sleep(0.01)
        marker = True

    call_task = asyncio.create_task(checked.call("slow", {"delay": 0.05}))
    await mark()
    assert marker and not call_task.done()
    assert (await call_task).data == "done"


@pytest.mark.asyncio
async def test_client_construction_runs_in_thread(tmp_path: Path) -> None:
    checked, _ = adapter(tmp_path, spec("fail_status"))
    FakeApi.constructor_delay = 0.05
    marker = False

    async def mark() -> None:
        nonlocal marker
        await asyncio.sleep(0.01)
        marker = True

    call_task = asyncio.create_task(checked.call("fail_status", {}))
    await mark()
    assert marker and not call_task.done()
    with pytest.raises(McpGrysterError):
        await call_task


@pytest.mark.asyncio
async def test_failures_are_masked_and_audited(tmp_path: Path) -> None:
    for name in ("fail_status", "explode"):
        checked, audit = adapter(tmp_path, spec(name))
        with pytest.raises(McpGrysterError) as caught:
            await checked.call(name, {})
        assert caught.value.code == "GRIST_REQUEST_FAILED"
        assert "private" not in caught.value.model_dump_json()
        assert audit.events[-1].status == "error"
        assert audit.events[-1].duration_ms >= 0


@pytest.mark.asyncio
async def test_output_file_is_workspace_relative_and_persisted(tmp_path: Path) -> None:
    checked, _ = adapter(
        tmp_path,
        spec(
            "write_file",
            parameters={"filename": ParameterSpec(required=True)},
            result_kind="file",
        ),
    )
    result = await checked.call("write_file", {"filename": "exports/result.bin"})

    assert result.status_code == 200
    assert result.data == {
        "artifact": {
            "kind": "grist",
            "name": "result.bin",
            "size_bytes": 8,
        }
    }
    assert (tmp_path / "artifacts" / "grist" / "result.bin").read_bytes() == b"download"
    assert not (tmp_path / "workspace" / "exports" / "result.bin").exists()


@pytest.mark.asyncio
async def test_output_file_never_clobbers_caller_workspace_path(tmp_path: Path) -> None:
    destination = tmp_path / "workspace" / "exports" / "result.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"keep")
    checked, _ = adapter(
        tmp_path,
        spec(
            "write_file",
            parameters={"filename": ParameterSpec(required=True)},
            result_kind="file",
        ),
    )

    result = await checked.call("write_file", {"filename": "exports/result.bin"})

    assert destination.read_bytes() == b"keep"
    assert result.data["artifact"]["name"] == "result.bin"
    assert (tmp_path / "artifacts" / "grist" / "result.bin").read_bytes() == b"download"


@pytest.mark.asyncio
async def test_oversize_file_is_rejected_without_artifact(tmp_path: Path) -> None:
    checked, _ = adapter(
        tmp_path,
        spec(
            "write_file",
            parameters={"filename": ParameterSpec(required=True)},
            result_kind="file",
        ),
        max_file_bytes=4,
    )

    with pytest.raises(McpGrysterError) as caught:
        await checked.call("write_file", {"filename": "result.bin"})

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert not (tmp_path / "artifacts" / "grist").exists()


@pytest.mark.asyncio
async def test_oversize_binary_is_rejected_without_artifact(tmp_path: Path) -> None:
    checked, _ = adapter(
        tmp_path,
        spec("bytes_result", result_kind="binary"),
        max_aggregate_bytes=5,
    )

    with pytest.raises(McpGrysterError) as caught:
        await checked.call("bytes_result", {})

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert not (tmp_path / "artifacts" / "grist").exists()


@pytest.mark.asyncio
async def test_binary_result_is_persisted_without_exposing_bytes(tmp_path: Path) -> None:
    checked, _ = adapter(tmp_path, spec("bytes_result", result_kind="binary"))

    result = await checked.call("bytes_result", {})

    assert result.status_code == 201
    artifact = result.data["artifact"]
    assert artifact["kind"] == "grist"
    assert artifact["size_bytes"] == 6
    assert (tmp_path / "artifacts" / "grist" / artifact["name"]).read_bytes() == b"binary"


@pytest.mark.asyncio
async def test_file_path_escape_is_rejected_before_client_creation(tmp_path: Path) -> None:
    checked, _ = adapter(
        tmp_path,
        spec(
            "write_file",
            parameters={"filename": ParameterSpec(required=True)},
            result_kind="file",
        ),
    )
    with pytest.raises(McpGrysterError) as caught:
        await checked.call("write_file", {"filename": "../outside.bin"})
    assert caught.value.code == "PATH_NOT_ALLOWED"
    assert FakeApi.created == 0


@pytest.mark.asyncio
async def test_pagination_respects_page_and_item_limits(tmp_path: Path) -> None:
    checked, _ = adapter(
        tmp_path,
        spec(
            "pages",
            parameters={"chunk": ParameterSpec(required=False, default=2)},
            result_kind="paginated",
        ),
        max_pages=2,
        max_items=3,
    )
    result = await checked.call("pages", {"chunk": 2})

    assert result.status_code == 200
    assert result.data == [0, 1, 0]
