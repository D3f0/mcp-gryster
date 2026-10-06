from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import requests
from pydantic import ValidationError

from mcp_gryster.artifacts import ArtifactStore
from mcp_gryster.audit import AuditEvent, JsonLineAuditSink
from mcp_gryster.errors import McpGrysterError, public_error


def test_public_error_hides_remote_exception_input(
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = str(Path.home())
    record_body = '{"records":[{"fields":{"email":"private@example.test"}}]}'
    exception = requests.RequestException(
        f"Authorization: Bearer secret-token body={record_body} path={home}/private.txt"
    )

    with caplog.at_level(logging.DEBUG, logger="mcp_gryster.errors"):
        error = public_error(exception)

    public_json = error.model_dump_json()
    assert error.code == "GRIST_REQUEST_FAILED"
    assert error.correlation_id
    for secret in ("secret-token", record_body, home):
        assert secret not in public_json
        assert secret not in caplog.text


def test_public_error_preserves_known_safe_local_error() -> None:
    original = McpGrysterError("PATH_NOT_ALLOWED", "Artifact path is not allowed")
    assert public_error(original) is original


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (ValueError("private input"), "INVALID_ARGUMENT"),
        (NotImplementedError("private input"), "METHOD_NOT_SUPPORTED"),
        (PermissionError("private input"), "PERMISSION_DENIED"),
        (FileNotFoundError("private input"), "PROJECT_NOT_FOUND"),
    ],
)
def test_public_error_maps_local_failures_without_input(
    exception: Exception, code: str
) -> None:
    error = public_error(exception)
    assert error.code == code
    assert "private input" not in error.model_dump_json()


def test_mcp_error_is_frozen_and_rejects_unstable_codes() -> None:
    with pytest.raises(ValidationError):
        McpGrysterError("UNKNOWN", "No")  # type: ignore[arg-type]

    error = McpGrysterError("LIMIT_EXCEEDED", "Limit exceeded")
    with pytest.raises(ValidationError):
        error.message = "changed"  # type: ignore[misc]


def test_audit_sink_writes_only_metadata(tmp_path: Path) -> None:
    output = tmp_path / "audit.jsonl"
    sink = JsonLineAuditSink(output)
    event = AuditEvent(
        tool="records_add",
        domain="records",
        effect="write",
        status="success",
        duration_ms=12.5,
        correlation_id="correlation-test",
    )

    sink.emit(event)

    payload = json.loads(output.read_text())
    assert payload["tool"] == "records_add"
    assert payload["domain"] == "records"
    assert payload["effect"] == "write"
    assert payload["status"] == "success"
    assert payload["duration_ms"] == 12.5
    assert "arguments" not in payload
    assert "response" not in payload
    assert "body" not in payload


def test_artifact_store_writes_bytes_atomically(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    artifact = store.put_bytes("preview", "index.html", b"safe")

    assert artifact.kind == "preview"
    assert artifact.name == "index.html"
    assert artifact.size_bytes == 4
    assert artifact.path.read_bytes() == b"safe"
    assert artifact.path.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("name", ["../x", "/tmp/x"])
def test_artifact_store_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    store = ArtifactStore(tmp_path)
    with pytest.raises(McpGrysterError) as caught:
        store.put_bytes("preview", name, b"unsafe")
    assert caught.value.code == "PATH_NOT_ALLOWED"


def test_artifact_store_rejects_symlink_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "preview").symlink_to(outside, target_is_directory=True)

    store = ArtifactStore(root)
    with pytest.raises(McpGrysterError) as caught:
        store.put_bytes("preview", "index.html", b"unsafe")

    assert caught.value.code == "PATH_NOT_ALLOWED"
    assert not (outside / "index.html").exists()
