from __future__ import annotations

import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from zipfile import ZipFile

import pytest

from mcp_gryster.artifacts import Artifact, ArtifactStore
from mcp_gryster.config import Settings
from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.fixtures import FixtureManager
from mcp_gryster.widget.models import WidgetAuthor, WidgetColumn, WidgetMetadata
from mcp_gryster.widget.packaging import WidgetPackager
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.validation import PreviewEvidenceStore, WidgetValidator


class _UnusedAdapter:
    async def call(self, method: str, arguments: dict[str, object]) -> object:
        raise AssertionError(f"unexpected Grist call: {method} {arguments}")


def _components(
    tmp_path: Path,
) -> tuple[ProjectStore, PreviewEvidenceStore, ArtifactStore, WidgetPackager]:
    settings = Settings(workspace_root=tmp_path / "workspace")
    store = ProjectStore(settings)
    evidence = PreviewEvidenceStore()
    fixtures = FixtureManager(store, _UnusedAdapter(), settings)
    artifacts = ArtifactStore(settings.workspace_root / "artifacts")
    packager = WidgetPackager(
        store,
        fixtures,
        WidgetValidator(store, evidence),
        artifacts,
    )
    return store, evidence, artifacts, packager


def _project(store: ProjectStore) -> str:
    created = store.create(
        WidgetMetadata(
            project_id="safe-widget",
            widget_id="com.example.safe",
            name="Safe Widget",
            description="A deterministic widget",
            entrypoint="pages/main.html",
            access_level="read table",
            columns=(WidgetColumn(name="name", type="Text", optional=True),),
            authors=(WidgetAuthor(name="Ada", email="ada@example.test"),),
        )
    )
    return store.apply_files(
        "safe-widget",
        created.revision,
        {
            "src/pages/main.html": (
                b"<!doctype html><script src='https://docs.getgrist.com/grist-plugin-api.js'></script>"
                b"<script src='app.js'></script>"
            ),
            "src/pages/app.js": (
                b"grist.ready({requiredAccess: 'read table', "
                b"columns: [{name: 'name', type: 'Text', optional: true}]});\n"
            ),
            "fixtures/private.json": b'{"secret":"must-not-ship"}\n',
        },
        [],
    ).revision


def test_package_requires_exact_current_packageable_revision(tmp_path: Path) -> None:
    store, evidence, _, packager = _components(tmp_path)
    revision = _project(store)

    with pytest.raises(McpGrysterError) as not_packageable:
        packager.package("safe-widget", "https://widgets.example", expected_revision=revision)
    assert not_packageable.value.code == "PACKAGE_FAILED"

    evidence.record_success("safe-widget", revision, None)
    current = store.apply_files(
        "safe-widget", revision, {"src/extra.css": b"body{}\n"}, []
    )
    with pytest.raises(McpGrysterError) as stale:
        packager.package("safe-widget", "https://widgets.example", expected_revision=revision)
    assert stale.value.code == "REVISION_CONFLICT"
    assert stale.value.details == {"current_revision": current.revision}


def test_package_uses_fixture_source_materialization_and_only_src_enters_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, evidence, artifacts, packager = _components(tmp_path)
    revision = _project(store)
    evidence.record_success("safe-widget", revision, None)
    called: list[tuple[str, str]] = []
    original = packager._fixtures.materialize_sources

    def observed(project_id: str, requested_revision: str, destination: str | Path) -> None:
        called.append((project_id, requested_revision))
        original(project_id, requested_revision, destination)

    monkeypatch.setattr(packager._fixtures, "materialize_sources", observed)
    result = packager.package(
        "safe-widget", "https://widgets.example/base/", expected_revision=revision
    )

    assert called == [("safe-widget", revision)]
    archive_bytes = artifacts.read_bytes(result.artifact.kind, result.artifact.name)
    with ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert archive.namelist() == ["pages/app.js", "pages/main.html"]
        assert all(
            not name.startswith(("fixtures/", ".mcp-gryster/", "preview/"))
            for name in archive.namelist()
        )
    assert b"must-not-ship" not in archive_bytes


def test_manifest_is_canonical_and_package_is_byte_deterministic(tmp_path: Path) -> None:
    store, evidence, artifacts, packager = _components(tmp_path)
    revision = _project(store)
    evidence.record_success("safe-widget", revision, None)

    first = packager.package(
        "safe-widget", "https://widgets.example/base/", expected_revision=revision
    )
    first_zip = artifacts.read_bytes(first.artifact.kind, first.artifact.name)
    first_manifest = artifacts.read_bytes(first.manifest.kind, first.manifest.name)
    second = packager.package(
        "safe-widget", "https://widgets.example/base/", expected_revision=revision
    )

    expected_manifest = {
        "accessLevel": "read table",
        "authors": [{"email": "ada@example.test", "name": "Ada"}],
        "columns": [
            {"allowMultiple": False, "name": "name", "optional": True, "type": "Text"}
        ],
        "description": "A deterministic widget",
        "name": "Safe Widget",
        "published": True,
        "url": "https://widgets.example/base/safe-widget/pages/main.html",
        "widgetId": "com.example.safe",
    }
    assert first_manifest == (json.dumps(expected_manifest, indent=2, sort_keys=True) + "\n").encode()
    assert artifacts.read_bytes(second.artifact.kind, second.artifact.name) == first_zip
    assert artifacts.read_bytes(second.manifest.kind, second.manifest.name) == first_manifest
    assert first.artifact.sha256 == hashlib.sha256(first_zip).hexdigest()
    assert first.manifest.sha256 == hashlib.sha256(first_manifest).hexdigest()
    assert first.artifact.name == f"safe-widget-{revision}.zip"
    assert first.manifest.name == f"safe-widget-{revision}-{first.manifest.sha256}.json"
    assert "path" not in first.artifact.model_dump()
    assert "path" not in first.manifest.model_dump()

    with ZipFile(io.BytesIO(first_zip)) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr >> 16 == 0o100644


def test_package_rejects_revision_change_before_artifact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, evidence, _, packager = _components(tmp_path)
    revision = _project(store)
    evidence.record_success("safe-widget", revision, None)
    original = packager._fixtures.materialize_sources

    def mutate_after_materialization(
        project_id: str, requested_revision: str, destination: str | Path
    ) -> None:
        original(project_id, requested_revision, destination)
        store.apply_files(project_id, requested_revision, {"src/late.txt": b"late\n"}, [])

    monkeypatch.setattr(packager._fixtures, "materialize_sources", mutate_after_materialization)

    with pytest.raises(McpGrysterError) as caught:
        packager.package("safe-widget", "https://widgets.example", expected_revision=revision)
    assert caught.value.code == "REVISION_CONFLICT"
    assert not (store.workspace / "artifacts" / "widget").exists()


def test_manifest_artifact_identity_includes_manifest_content(
    tmp_path: Path,
) -> None:
    store, evidence, artifacts, packager = _components(tmp_path)
    revision = _project(store)
    evidence.record_success("safe-widget", revision, None)

    first = packager.package(
        "safe-widget", "https://one.example/widgets", expected_revision=revision
    )
    second = packager.package(
        "safe-widget", "https://two.example/widgets", expected_revision=revision
    )

    assert first.artifact.name == second.artifact.name
    assert first.manifest.name != second.manifest.name
    assert first.manifest.sha256 != second.manifest.sha256
    assert artifacts.read_bytes(first.manifest.kind, first.manifest.name) != artifacts.read_bytes(
        second.manifest.kind, second.manifest.name
    )


def test_package_holds_revision_lock_through_both_artifact_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, evidence, artifacts, packager = _components(tmp_path)
    competing_store = ProjectStore(store.workspace)
    revision = _project(store)
    evidence.record_success("safe-widget", revision, None)
    first_commit_started = Event()
    continue_commits = Event()
    update_started = Event()
    update_completed = Event()
    commits: list[tuple[str, str]] = []
    original_put_bytes = artifacts.put_bytes

    def observe_commit(kind: str, name: str, data: bytes) -> Artifact:
        commits.append((kind, name))
        if len(commits) == 1:
            first_commit_started.set()
            assert continue_commits.wait(timeout=5)
        return original_put_bytes(kind, name, data)

    def update_project() -> str:
        update_started.set()
        updated = competing_store.apply_files(
            "safe-widget", revision, {"src/late.txt": b"late\n"}, []
        )
        update_completed.set()
        return updated.revision

    monkeypatch.setattr(artifacts, "put_bytes", observe_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        package_future = pool.submit(
            packager.package,
            "safe-widget",
            "https://widgets.example",
            expected_revision=revision,
        )
        assert first_commit_started.wait(timeout=5)
        update_future = pool.submit(update_project)
        update_started.wait(timeout=5)
        update_was_blocked = not update_completed.wait(timeout=0.1)
        continue_commits.set()
        result = package_future.result(timeout=5)
        updated_revision = update_future.result(timeout=5)
    assert update_was_blocked

    assert updated_revision != revision
    assert commits == [
        ("widget", result.artifact.name),
        ("widget", result.manifest.name),
    ]
