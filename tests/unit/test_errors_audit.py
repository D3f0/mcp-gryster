from __future__ import annotations

import hashlib
import json
import logging
import os
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


def test_public_error_redacts_quoted_headers_and_response_repr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "secret-token"
    private_body = "private@example.test"
    headers = {"Authorization": f"Bearer {secret}"}
    response = {"records": [{"email": private_body}]}
    exception = requests.RequestException(f"headers={headers!r}, response={response!r}")

    with caplog.at_level(logging.DEBUG, logger="mcp_gryster.errors"):
        error = public_error(exception)

    assert error.code == "GRIST_REQUEST_FAILED"
    assert secret not in caplog.text
    assert private_body not in caplog.text


def test_requests_value_error_subclass_is_remote_failure() -> None:
    error = public_error(requests.exceptions.MissingSchema("private URL input"))

    assert error.code == "GRIST_REQUEST_FAILED"
    assert "private URL input" not in error.model_dump_json()

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


def test_mcp_error_can_be_raised_caught_and_serialized() -> None:
    error = McpGrysterError(
        "REVISION_CONFLICT",
        "Project revision does not match",
        {"expected_revision": "abc"},
    )

    with pytest.raises(McpGrysterError) as caught:
        raise error

    assert caught.value is error
    assert caught.value.code == "REVISION_CONFLICT"
    assert caught.value.message == "Project revision does not match"
    assert caught.value.details == {"expected_revision": "abc"}
    assert json.loads(caught.value.model_dump_json()) == {
        "code": "REVISION_CONFLICT",
        "message": "Project revision does not match",
        "details": {"expected_revision": "abc"},
        "correlation_id": None,
    }


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
    assert artifact.sha256 == hashlib.sha256(b"safe").hexdigest()
    assert artifact.path.resolve().is_relative_to(tmp_path.resolve())


def test_artifact_store_reads_regular_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.put_bytes("preview", "index.html", b"safe")

    assert store.read_bytes("preview", "index.html") == b"safe"


def test_artifact_store_read_rejects_symlink_components(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.bin").write_bytes(b"private")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "preview").mkdir()
    (root / "preview" / "linked.bin").symlink_to(outside / "private.bin")
    store = ArtifactStore(root)

    for kind, name in (("linked", "private.bin"), ("preview", "linked.bin")):
        with pytest.raises(McpGrysterError) as caught:
            store.read_bytes(kind, name)
        assert caught.value.code == "PATH_NOT_ALLOWED"


def test_artifact_store_read_fallback_stays_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_gryster import artifacts

    monkeypatch.setattr(artifacts, "_supports_dir_fd", lambda: False)
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.bin").write_bytes(b"private")
    (root / "preview").mkdir()
    (root / "preview" / "safe.bin").write_bytes(b"safe")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    store = ArtifactStore(root)

    assert store.read_bytes("preview", "safe.bin") == b"safe"
    with pytest.raises(McpGrysterError) as caught:
        store.read_bytes("linked", "private.bin")
    assert caught.value.code == "PATH_NOT_ALLOWED"


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


def test_artifact_store_anchors_replace_when_parent_is_substituted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = ArtifactStore(root)
    original_replace = os.replace

    def substitute_parent(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        preview = root / "preview"
        displaced = root / "displaced"
        preview.rename(displaced)
        preview.symlink_to(outside, target_is_directory=True)
        original_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", substitute_parent)

    with pytest.raises(McpGrysterError) as caught:
        store.put_bytes("preview", "index.html", b"safe")

    assert caught.value.code == "PATH_NOT_ALLOWED"
    assert not (outside / "index.html").exists()
    assert (root / "displaced" / "index.html").read_bytes() == b"safe"


def test_artifact_store_fallback_writes_contained_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_gryster import artifacts

    monkeypatch.setattr(artifacts, "_supports_dir_fd", lambda: False)
    store = ArtifactStore(tmp_path)

    artifact = store.put_bytes("preview", "index.html", b"portable")

    assert artifact.path.read_bytes() == b"portable"


def test_artifact_store_fallback_rejects_unsafe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_gryster import artifacts

    monkeypatch.setattr(artifacts, "_supports_dir_fd", lambda: False)
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    store = ArtifactStore(root)

    for kind, name in (("preview", "../x"), ("preview", "/tmp/x"), ("linked", "x")):
        with pytest.raises(McpGrysterError) as caught:
            store.put_bytes(kind, name, b"unsafe")
        assert caught.value.code == "PATH_NOT_ALLOWED"

    assert not (outside / "x").exists()
