from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mcp_gryster.config import Settings
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.models import FileUpdate, WidgetMetadata
from mcp_gryster.widget.projects import ProjectStore


@pytest.fixture
def metadata() -> WidgetMetadata:
    return WidgetMetadata(
        project_id="example-widget",
        widget_id="com.example.widget",
        name="Example Widget",
        description="Displays a record",
        entrypoint="index.html",
        access_level="read table",
        columns=[],
    )


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(
        tmp_path / "workspace",
        max_file_bytes=1_024,
        max_files=10,
        max_aggregate_bytes=4_096,
    )


def test_create_read_and_list_have_deterministic_revision(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    created = store.create(metadata)
    read = store.read(metadata.project_id)

    assert created == read
    assert created.project_id == "example-widget"
    assert created.files == ("src/index.html", "widget.json")
    assert store.list() == (created,)
    material = {
        "src/index.html": b"<!doctype html>\n<html><body></body></html>\n",
        "widget.json": metadata.to_widget_json(),
    }
    digest = hashlib.sha256()
    for path, content in sorted(material.items()):
        encoded = path.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    assert created.revision == digest.hexdigest()


def test_create_uses_configured_nested_entrypoint(store: ProjectStore) -> None:
    metadata = WidgetMetadata(
        project_id="nested-widget",
        widget_id="com.example.nested",
        name="Nested Widget",
        entrypoint="pages/main.html",
    )

    created = store.create(metadata)

    assert created.files == ("src/pages/main.html", "widget.json")


def test_store_accepts_settings_limits(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        max_file_bytes=128,
        max_aggregate_bytes=256,
    )
    settings.workspace_root.mkdir()
    store = ProjectStore(settings)

    assert store.workspace == settings.workspace_root


def test_batch_update_delete_and_identical_content_are_atomic(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    initial = store.create(metadata)
    updated = store.apply_files(
        metadata.project_id,
        initial.revision,
        [
            FileUpdate(path="src/app.js", content=b"grist.ready();\n"),
            FileUpdate(path="src/index.html", content=b"updated\n"),
        ],
        [],
    )
    deleted = store.apply_files(
        metadata.project_id,
        updated.revision,
        [],
        ["src/app.js"],
    )
    identical = store.apply_files(
        metadata.project_id,
        deleted.revision,
        [FileUpdate(path="src/index.html", content=b"updated\n")],
        [],
    )

    assert updated.revision != initial.revision
    assert deleted.files == ("src/index.html", "widget.json")
    assert identical.revision == deleted.revision

def test_unchanged_files_are_hard_linked_into_new_revision(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    initial = store.create(metadata)
    updated = store.apply_files(
        metadata.project_id,
        initial.revision,
        [FileUpdate(path="src/new.js", content=b"new\n")],
        [],
    )
    revisions = store.project_path(metadata.project_id) / "revisions"

    old_stat = (revisions / initial.revision / "widget.json").stat()
    new_stat = (revisions / updated.revision / "widget.json").stat()
    assert (old_stat.st_dev, old_stat.st_ino) == (new_stat.st_dev, new_stat.st_ino)


def test_stale_and_overlapping_updates_do_not_mutate(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    initial = store.create(metadata)
    current = store.apply_files(
        metadata.project_id,
        initial.revision,
        [FileUpdate(path="src/index.html", content=b"current\n")],
        [],
    )

    with pytest.raises(McpGrysterError) as stale:
        store.apply_files(
            metadata.project_id,
            initial.revision,
            [FileUpdate(path="src/index.html", content=b"stale\n")],
            [],
        )
    assert stale.value.code == "REVISION_CONFLICT"
    assert stale.value.details == {"current_revision": current.revision}

    with pytest.raises(McpGrysterError) as overlap:
        store.apply_files(
            metadata.project_id,
            current.revision,
            [FileUpdate(path="src/index.html", content=b"both\n")],
            ["src/index.html"],
        )
    assert overlap.value.code == "INVALID_ARGUMENT"
    assert store.read(metadata.project_id) == current


def test_concurrent_store_instances_accept_expected_revision_once(
    tmp_path: Path, metadata: WidgetMetadata
) -> None:
    workspace = tmp_path / "workspace"
    first = ProjectStore(workspace)
    second = ProjectStore(workspace)
    initial = first.create(metadata)

    def update(store: ProjectStore, content: bytes) -> str:
        try:
            return store.apply_files(
                metadata.project_id,
                initial.revision,
                [FileUpdate(path="src/index.html", content=content)],
                [],
            ).revision
        except McpGrysterError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda args: update(*args),
                ((first, b"first\n"), (second, b"second\n")),
            )
        )

    assert results.count("REVISION_CONFLICT") == 1
    assert first.read(metadata.project_id).revision in results


def test_stale_dead_owner_lock_is_recovered(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    initial = store.create(metadata)
    lock = store.project_path(metadata.project_id) / ".write.lock"
    lock.write_text(json.dumps({"pid": -1, "timestamp": 0}), encoding="utf-8")

    updated = store.apply_files(
        metadata.project_id,
        initial.revision,
        [FileUpdate(path="src/index.html", content=b"recovered\n")],
        [],
    )

    assert updated.revision != initial.revision
    assert not lock.exists()


def test_limits_are_checked_before_any_revision_write(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    initial = store.create(metadata)
    revisions = store.project_path(metadata.project_id) / "revisions"
    before = set(revisions.iterdir())

    with pytest.raises(McpGrysterError) as caught:
        store.apply_files(
            metadata.project_id,
            initial.revision,
            [FileUpdate(path="src/large.js", content=b"x" * 1_025)],
            [],
        )

    assert caught.value.code == "LIMIT_EXCEEDED"
    assert set(revisions.iterdir()) == before
    assert store.read(metadata.project_id) == initial


def test_file_count_and_aggregate_limits_precede_writes(
    tmp_path: Path, metadata: WidgetMetadata
) -> None:
    store = ProjectStore(
        tmp_path / "workspace",
        max_file_bytes=1_024,
        max_files=2,
        max_aggregate_bytes=1_024,
    )
    initial = store.create(metadata)
    revisions = store.project_path(metadata.project_id) / "revisions"
    before = set(revisions.iterdir())

    for updates in (
        [FileUpdate(path="src/extra.js", content=b"x")],
        [FileUpdate(path="src/index.html", content=b"x" * 1_000)],
    ):
        with pytest.raises(McpGrysterError) as caught:
            store.apply_files(metadata.project_id, initial.revision, updates, [])
        assert caught.value.code == "LIMIT_EXCEEDED"
        assert set(revisions.iterdir()) == before


def test_failure_while_staging_leaves_previous_revision_visible(
    store: ProjectStore, metadata: WidgetMetadata, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = store.create(metadata)

    def fail_write(path: Path, content: bytes) -> None:
        raise OSError("staging failed")

    monkeypatch.setattr("mcp_gryster.widget.projects._write_new_file", fail_write)
    with pytest.raises(OSError, match="staging failed"):
        store.apply_files(
            metadata.project_id,
            initial.revision,
            [FileUpdate(path="src/new.js", content=b"new\n")],
            [],
        )

    assert store.read(metadata.project_id) == initial


def test_injected_failure_leaves_previous_revision_visible(
    store: ProjectStore, metadata: WidgetMetadata, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = store.create(metadata)

    def fail_before_pointer(project_path: Path, revision: str) -> None:
        raise OSError("injected")

    monkeypatch.setattr(store, "_replace_pointer", fail_before_pointer)
    with pytest.raises(OSError, match="injected"):
        store.apply_files(
            metadata.project_id,
            initial.revision,
            [FileUpdate(path="src/index.html", content=b"uncommitted\n")],
            [],
        )

    assert store.read(metadata.project_id) == initial


def test_startup_removes_incomplete_unreachable_staging(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    current = store.create(metadata)
    project = store.project_path(metadata.project_id)
    stale = project / "revisions" / ".staging-interrupted"
    stale.mkdir()
    (stale / "partial").write_bytes(b"partial")

    recovered = ProjectStore(store.workspace)

    assert not stale.exists()
    assert recovered.read(metadata.project_id) == current


def test_materialize_copies_revision_without_following_source_symlinks(
    store: ProjectStore, metadata: WidgetMetadata, tmp_path: Path
) -> None:
    current = store.create(metadata)
    destination = store.workspace / "materialized"
    store.materialize(metadata.project_id, current.revision, destination)

    assert (destination / "widget.json").read_bytes() == metadata.to_widget_json()
    assert (destination / "src" / "index.html").is_file()

    if not hasattr(os, "symlink"):
        return
    revision = store.project_path(metadata.project_id) / "revisions" / current.revision
    (revision / "src" / "escape.js").symlink_to(tmp_path / "outside")
    with pytest.raises(McpGrysterError) as caught:
        store.materialize(
            metadata.project_id,
            current.revision,
            store.workspace / "unsafe-copy",
        )
    assert caught.value.code == "PATH_NOT_ALLOWED"
    assert not (store.workspace / "unsafe-copy").exists()


def test_materialize_rejects_stale_revision(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    initial = store.create(metadata)
    current = store.apply_files(
        metadata.project_id,
        initial.revision,
        [FileUpdate(path="src/index.html", content=b"current\n")],
        [],
    )

    with pytest.raises(McpGrysterError) as caught:
        store.materialize(
            metadata.project_id,
            initial.revision,
            store.workspace / "stale",
        )

    assert caught.value.code == "REVISION_CONFLICT"
    assert caught.value.details == {"current_revision": current.revision}


def test_materialize_requires_trusted_workspace_destination(
    store: ProjectStore, metadata: WidgetMetadata, tmp_path: Path
) -> None:
    current = store.create(metadata)
    with pytest.raises(McpGrysterError) as caught:
        store.materialize(metadata.project_id, current.revision, tmp_path / "outside")
    assert caught.value.code == "PATH_NOT_ALLOWED"


def test_materialize_rejects_parent_traversal_escape(
    store: ProjectStore, metadata: WidgetMetadata, tmp_path: Path
) -> None:
    current = store.create(metadata)
    escaped = tmp_path / "outside"

    with pytest.raises(McpGrysterError) as caught:
        store.materialize(
            metadata.project_id,
            current.revision,
            store.workspace / ".." / "outside",
        )

    assert caught.value.code == "PATH_NOT_ALLOWED"
    assert not escaped.exists()


def test_invalid_pointer_content_is_rejected_before_revision_read(
    store: ProjectStore, metadata: WidgetMetadata
) -> None:
    store.create(metadata)
    pointer = store.project_path(metadata.project_id) / "current.json"
    pointer.write_text(json.dumps({"revision": "../outside"}), encoding="utf-8")

    with pytest.raises(McpGrysterError) as caught:
        store.read(metadata.project_id)

    assert caught.value.code == "VALIDATION_FAILED"
