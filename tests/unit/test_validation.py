from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.models import FileUpdate, WidgetColumn, WidgetMetadata
from mcp_gryster.widget.projects import ProjectStore
from mcp_gryster.widget.validation import (
    ALLOWED_GRIST_COLUMN_TYPES,
    PreviewEvidenceStore,
    ValidationIssue,
    WidgetValidator,
)


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path / "workspace")


def _metadata(**updates: Any) -> WidgetMetadata:
    values: dict[str, Any] = {
        "project_id": "safe-widget",
        "widget_id": "com.example.safe-widget",
        "name": "Safe Widget",
        "entrypoint": "index.html",
        "access_level": "read table",
        "columns": [WidgetColumn(name="name", type="Text")],
    }
    values.update(updates)
    return WidgetMetadata.model_validate(values)


def _valid_html(script: str = "app.js") -> bytes:
    return (
        "<!doctype html><html><body>"
        '<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>'
        f'<script src="{script}"></script>'
        "</body></html>"
    ).encode()


def _valid_javascript() -> bytes:
    return b"grist.ready({requiredAccess: 'read table', columns: [{name: 'name', type: 'Text'}]});\n"


def _project(store: ProjectStore, **metadata_updates: Any) -> str:
    created = store.create(_metadata(**metadata_updates))
    return store.apply_files(
        "safe-widget",
        created.revision,
        {
            "src/index.html": _valid_html(),
            "src/app.js": _valid_javascript(),
        },
        [],
    ).revision


def _codes(report: Any) -> list[str]:
    return [issue.code for issue in report.issues]


def _replace_widget_json(store: ProjectStore, payload: dict[str, Any]) -> None:
    current = store.read("safe-widget")
    store.apply_files(
        "safe-widget",
        current.revision,
        [FileUpdate(path="widget.json", content=json.dumps(payload).encode())],
        [],
    )


def _fixture(_revision: str, **updates: Any) -> bytes:
    value: dict[str, Any] = {
        "schemaVersion": 1,
        "fixtureId": "preview",
        "source": {
            "docId": "sensitive-doc",
            "tableId": "SensitiveTable",
            "capturedAt": "2026-10-06T12:00:00Z",
        },
        "columns": [{"id": "SecretName", "label": "Sensitive Label", "type": "Text"}],
        "records": [{"id": 7, "fields": {"SecretName": "authored secret value"}}],
        "selectedRecordId": 7,
        "mappings": {"name": "SecretName"},
        "options": {},
        "accessLevel": "read table",
    }
    value.update(updates)
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _add_fixture(store: ProjectStore, fixture: bytes) -> str:
    current = store.read("safe-widget")
    return store.apply_files(
        "safe-widget", current.revision, {"fixtures/preview.json": fixture}, []
    ).revision


def test_valid_static_widget_reports_only_external_sdk_warning(store: ProjectStore) -> None:
    revision = _project(store)
    evidence = PreviewEvidenceStore()
    evidence.record_success("safe-widget", revision, None)

    report = WidgetValidator(store, evidence).validate("safe-widget")

    assert report.revision == revision
    assert _codes(report) == ["EXTERNAL_ORIGIN"]
    assert report.valid is True
    assert report.packageable is True


def test_packageable_requires_successful_preview_for_exact_revision(store: ProjectStore) -> None:
    _project(store)
    evidence = PreviewEvidenceStore()
    evidence.record_success("safe-widget", "0" * 64, None)

    report = WidgetValidator(store, evidence).validate("safe-widget")

    assert report.valid is True
    assert report.packageable is False

def test_discarded_preview_evidence_revokes_packageability(store: ProjectStore) -> None:
    revision = _project(store)
    evidence = PreviewEvidenceStore()
    evidence.record_success("safe-widget", revision, None)
    validator = WidgetValidator(store, evidence)
    assert validator.validate("safe-widget").packageable is True

    evidence.discard("safe-widget", revision, None)

    assert validator.validate("safe-widget").packageable is False


def test_invalid_metadata_has_one_stable_code_without_authored_evidence(store: ProjectStore) -> None:
    _project(store)
    _replace_widget_json(
        store,
        {
            "schemaVersion": 1,
            "widgetId": "invalid widget id",
            "name": "Never include this authored name",
            "description": "",
            "entrypoint": "index.html",
            "accessLevel": "owner",
            "columns": [],
        },
    )

    report = WidgetValidator(store).validate("safe-widget")

    assert _codes(report) == ["METADATA_INVALID"]
    serialized_evidence = json.dumps([issue.evidence for issue in report.issues])
    assert "invalid widget id" not in serialized_evidence
    assert "Never include this authored name" not in serialized_evidence
    assert "owner" not in serialized_evidence


def test_missing_entrypoint_and_local_asset_have_exact_codes(store: ProjectStore) -> None:
    revision = _project(store)
    missing_entrypoint = store.apply_files(
        "safe-widget", revision, {}, ["src/index.html"]
    )
    assert _codes(WidgetValidator(store).validate("safe-widget")) == ["ENTRYPOINT_MISSING"]

    restored = store.apply_files(
        "safe-widget",
        missing_entrypoint.revision,
        {"src/index.html": _valid_html("missing.js")},
        [],
    )
    assert restored.revision
    assert _codes(WidgetValidator(store).validate("safe-widget")) == [
        "EXTERNAL_ORIGIN",
        "ASSET_MISSING",
        "GRIST_READY_MISSING",
    ]


def test_external_origin_and_full_access_are_warnings(store: ProjectStore) -> None:
    revision = _project(store, access_level="full")
    store.apply_files(
        "safe-widget",
        revision,
        {
            "src/app.js": b"grist.ready({requiredAccess: 'full', columns: [{name: 'name', type: 'Text'}]});\n"
        },
        [],
    )

    report = WidgetValidator(store).validate("safe-widget")

    assert [(issue.code, issue.severity) for issue in report.issues] == [
        ("FULL_ACCESS", "warning"),
        ("EXTERNAL_ORIGIN", "warning"),
    ]
    assert report.valid is True


def test_project_store_rejects_traversal_and_validator_rejects_revision_symlink(
    store: ProjectStore, tmp_path: Path
) -> None:
    with pytest.raises(McpGrysterError) as traversal:
        store.read("../escape")
    assert traversal.value.code == "INVALID_ARGUMENT"

    if not hasattr(os, "symlink"):
        return
    revision = _project(store)
    entrypoint = store.project_path("safe-widget") / "revisions" / revision / "src/index.html"
    entrypoint.unlink()
    entrypoint.symlink_to(tmp_path / "outside")
    with pytest.raises(McpGrysterError) as symlink:
        WidgetValidator(store).validate("safe-widget")
    assert symlink.value.code == "PATH_NOT_ALLOWED"


def test_plugin_must_precede_grist_initialization(store: ProjectStore) -> None:
    revision = _project(store)
    html = (
        b"<script>grist.ready({requiredAccess: 'read table', columns: "
        b"[{name: 'name', type: 'Text'}]});</script>"
        b'<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>'
    )
    store.apply_files("safe-widget", revision, {"src/index.html": html}, [])

    report = WidgetValidator(store).validate("safe-widget")

    assert _codes(report) == ["EXTERNAL_ORIGIN", "INLINE_SCRIPT", "PLUGIN_ORDER"]


def test_missing_ready_and_literal_access_mismatch_have_stable_codes(store: ProjectStore) -> None:
    revision = _project(store)
    without_ready = store.apply_files(
        "safe-widget", revision, {"src/app.js": b"grist.onRecord(() => {});\n"}, []
    )
    assert _codes(WidgetValidator(store).validate("safe-widget")) == [
        "EXTERNAL_ORIGIN",
        "GRIST_READY_MISSING",
    ]

    store.apply_files(
        "safe-widget",
        without_ready.revision,
        {
            "src/app.js": b"grist.ready({requiredAccess: 'none', columns: [{name: 'name', type: 'Text'}]});\n"
        },
        [],
    )
    assert _codes(WidgetValidator(store).validate("safe-widget")) == [
        "EXTERNAL_ORIGIN",
        "READY_ACCESS_MISMATCH",
    ]


def test_dynamic_ready_config_is_not_treated_as_literal_semantics(store: ProjectStore) -> None:
    revision = _project(store)
    store.apply_files(
        "safe-widget",
        revision,
        {"src/app.js": b"const config = buildConfig(); grist.ready(config);\n"},
        [],
    )

    report = WidgetValidator(store).validate("safe-widget")

    assert _codes(report) == ["EXTERNAL_ORIGIN", "DYNAMIC_PLUGIN_CONFIG"]


def test_declared_columns_and_unsupported_grist_members_are_checked_from_ast(
    store: ProjectStore,
) -> None:
    revision = _project(store)
    store.apply_files(
        "safe-widget",
        revision,
        {
            "src/app.js": (
                b"grist.ready({requiredAccess: 'read table', columns: []});\n"
                b"grist.notARealApi();\n"
            )
        },
        [],
    )

    report = WidgetValidator(store).validate("safe-widget")

    assert _codes(report) == [
        "EXTERNAL_ORIGIN",
        "DECLARED_COLUMN_MISSING",
        "UNSUPPORTED_GRIST_MEMBER",
    ]


def test_malformed_javascript_is_deterministic_across_parser_versions(store: ProjectStore) -> None:
    revision = _project(store)
    store.apply_files("safe-widget", revision, {"src/app.js": b"grist.ready({ columns: [ });"}, [])

    report = WidgetValidator(store).validate("safe-widget")

    assert _codes(report) == ["EXTERNAL_ORIGIN", "JAVASCRIPT_SYNTAX"]
    issue = report.issues[-1]
    assert issue == ValidationIssue(
        code="JAVASCRIPT_SYNTAX",
        severity="error",
        message="JavaScript contains syntax errors",
        evidence=("javascript",),
    )


def test_fixture_checks_required_mapping_type_selection_multiplicity_and_access(
    store: ProjectStore,
) -> None:
    revision = _project(store)
    fixture = _fixture(
        revision,
        columns=[{"id": "SecretName", "label": "Sensitive Label", "type": "ChoiceList"}],
        selectedRecordId=999,
        mappings={},
        accessLevel="none",
    )
    _add_fixture(store, fixture)

    report = WidgetValidator(store).validate("safe-widget", "preview")

    assert _codes(report) == [
        "EXTERNAL_ORIGIN",
        "FIXTURE_REQUIRED_COLUMN",
        "UNUSED_FIXTURE_COLUMN",
        "FIXTURE_SELECTED_RECORD",
        "FIXTURE_ACCESS_MISMATCH",
    ]

    current = store.read("safe-widget")
    fixture_with_mapping = _fixture(
        revision,
        columns=[{"id": "SecretName", "label": "Sensitive Label", "type": "ChoiceList"}],
        mappings={"name": ["SecretName"]},
    )
    store.apply_files(
        "safe-widget",
        current.revision,
        {"fixtures/preview.json": fixture_with_mapping},
        [],
    )
    mapped_report = WidgetValidator(store).validate("safe-widget", "preview")
    assert "FIXTURE_MAPPING_MULTIPLICITY" in _codes(mapped_report)
    assert "FIXTURE_TYPE_MISMATCH" in _codes(mapped_report)


def test_fixture_rejects_unknown_column_types_centrally_and_never_echoes_values(
    store: ProjectStore,
) -> None:
    revision = _project(store)
    assert "Text" in ALLOWED_GRIST_COLUMN_TYPES
    assert "AuthoredSecretType" not in ALLOWED_GRIST_COLUMN_TYPES
    _add_fixture(
        store,
        _fixture(
            revision,
            columns=[
                {"id": "SecretName", "label": "Sensitive Label", "type": "AuthoredSecretType"}
            ],
        ),
    )

    report = WidgetValidator(store).validate("safe-widget", "preview")

    assert "FIXTURE_TYPE_UNSUPPORTED" in _codes(report)
    evidence = json.dumps([issue.evidence for issue in report.issues])
    assert "AuthoredSecretType" not in evidence
    assert "SecretName" not in evidence
    assert "Sensitive Label" not in evidence
    assert "authored secret value" not in evidence


def test_fixture_preview_evidence_is_scoped_to_fixture_and_revision(store: ProjectStore) -> None:
    revision = _project(store)
    with_fixture = _add_fixture(store, _fixture(revision))
    evidence = PreviewEvidenceStore()
    evidence.record_success("safe-widget", with_fixture, "different")

    unmatched = WidgetValidator(store, evidence).validate("safe-widget", "preview")
    assert unmatched.valid is True
    assert unmatched.packageable is False

    evidence.record_success("safe-widget", with_fixture, "preview")
    matched = WidgetValidator(store, evidence).validate("safe-widget", "preview")
    assert matched.packageable is True


def test_persisted_fixture_omits_request_only_expected_revision(store: ProjectStore) -> None:
    revision = _project(store)
    persisted = _fixture(revision)
    assert b"expectedRevision" not in persisted
    _add_fixture(store, persisted)
    assert "FIXTURE_INVALID" not in _codes(WidgetValidator(store).validate("safe-widget", "preview"))


def test_zero_argument_ready_defaults_access_to_none_and_is_not_dynamic(store: ProjectStore) -> None:
    revision = _project(store, access_level="none", columns=[])
    store.apply_files("safe-widget", revision, {"src/app.js": b"grist.ready();\n"}, [])
    assert _codes(WidgetValidator(store).validate("safe-widget")) == ["EXTERNAL_ORIGIN"]


def test_omitted_ready_access_defaults_to_none_for_comparison(store: ProjectStore) -> None:
    revision = _project(store)
    store.apply_files(
        "safe-widget", revision,
        {"src/app.js": b"grist.ready({columns: [{name: 'name', type: 'Text'}]});\n"}, [],
    )
    assert _codes(WidgetValidator(store).validate("safe-widget")) == [
        "EXTERNAL_ORIGIN", "READY_ACCESS_MISMATCH",
    ]


@pytest.mark.parametrize("column_type", ["Ref:People", "RefList:People", "DateTime:UTC"])
def test_fixture_allows_parameterized_grist_column_types(
    store: ProjectStore, column_type: str
) -> None:
    revision = _project(store, columns=[WidgetColumn(name="name", type="Any")])
    _add_fixture(store, _fixture(revision, columns=[
        {"id": "SecretName", "label": "Sensitive Label", "type": column_type}
    ]))
    report = WidgetValidator(store).validate("safe-widget", "preview")
    assert "FIXTURE_TYPE_UNSUPPORTED" not in _codes(report)
    assert "FIXTURE_TYPE_MISMATCH" not in _codes(report)


def test_mapping_multiplicity_uses_mapping_shape(store: ProjectStore) -> None:
    revision = _project(store)
    _add_fixture(store, _fixture(revision, mappings={"name": ["SecretName"]}))
    assert "FIXTURE_MAPPING_MULTIPLICITY" in _codes(
        WidgetValidator(store).validate("safe-widget", "preview")
    )


def test_allow_multiple_accepts_list_mapping(store: ProjectStore) -> None:
    revision = _project(store, columns=[
        WidgetColumn(name="name", type="Text", allowMultiple=True)
    ])
    _add_fixture(store, _fixture(revision, mappings={"name": ["SecretName"]}))
    assert "FIXTURE_MAPPING_MULTIPLICITY" not in _codes(
        WidgetValidator(store).validate("safe-widget", "preview")
    )


@pytest.mark.parametrize("declared_type", ["Any", "Text,Numeric"])
def test_declared_type_wildcard_and_alternatives_accept_compatible_fixture(
    store: ProjectStore, declared_type: str
) -> None:
    revision = _project(store, columns=[WidgetColumn(name="name", type=declared_type)])
    _add_fixture(store, _fixture(revision))
    assert "FIXTURE_TYPE_MISMATCH" not in _codes(
        WidgetValidator(store).validate("safe-widget", "preview")
    )


def test_ready_member_passed_as_value_does_not_count_as_initialization(store: ProjectStore) -> None:
    revision = _project(store)
    store.apply_files("safe-widget", revision, {"src/app.js": b"register(grist.ready);\n"}, [])
    assert "GRIST_READY_MISSING" in _codes(WidgetValidator(store).validate("safe-widget"))


def test_inline_script_emits_stable_warning(store: ProjectStore) -> None:
    revision = _project(store)
    inline = (
        b'<script src="https://docs.getgrist.com/grist-plugin-api.js"></script>'
        b"<script>grist.ready({requiredAccess: 'read table', columns: "
        b"[{name: 'name', type: 'Text'}]});</script>"
    )
    store.apply_files("safe-widget", revision, {"src/index.html": inline}, [])
    assert "INLINE_SCRIPT" in _codes(WidgetValidator(store).validate("safe-widget"))


def test_unused_fixture_column_emits_value_free_warning(store: ProjectStore) -> None:
    revision = _project(store)
    fixture = json.loads(_fixture(revision))
    fixture["columns"].append({"id": "AuthoredUnused", "label": "Hidden", "type": "Text"})
    fixture["records"][0]["fields"]["AuthoredUnused"] = "secret"
    _add_fixture(store, (json.dumps(fixture) + "\n").encode())
    report = WidgetValidator(store).validate("safe-widget", "preview")
    issue = next(issue for issue in report.issues if issue.code == "UNUSED_FIXTURE_COLUMN")
    assert issue.severity == "warning"
    assert "AuthoredUnused" not in json.dumps(issue.evidence)


@pytest.mark.parametrize(
    "declared,ready",
    [
        (WidgetColumn(name="name", type="Text"), "{name: 'name', type: 'Numeric'}"),
        (WidgetColumn(name="name", type="Text", optional=True), "{name: 'name', type: 'Text'}"),
        (WidgetColumn(name="name", type="Text", allowMultiple=True), "{name: 'name', type: 'Text'}"),
    ],
)
def test_ready_column_contract_must_match_metadata(
    store: ProjectStore, declared: WidgetColumn, ready: str
) -> None:
    revision = _project(store, columns=[declared])
    javascript = (
        "grist.ready({requiredAccess: 'read table', columns: [" + ready + "]});\n"
    ).encode()
    store.apply_files("safe-widget", revision, {"src/app.js": javascript}, [])
    assert "READY_COLUMN_MISMATCH" in _codes(WidgetValidator(store).validate("safe-widget"))


@pytest.mark.parametrize(
    "declared_type,ready_type",
    [("Any", "Numeric"), ("Text,Numeric", "Text")],
)
def test_ready_column_type_wildcard_and_alternatives_are_compatible(
    store: ProjectStore, declared_type: str, ready_type: str
) -> None:
    revision = _project(store, columns=[WidgetColumn(name="name", type=declared_type)])
    javascript = (
        "grist.ready({requiredAccess: 'read table', columns: "
        f"[{{name: 'name', type: '{ready_type}'}}]}});\n"
    ).encode()
    store.apply_files("safe-widget", revision, {"src/app.js": javascript}, [])
    assert "READY_COLUMN_MISMATCH" not in _codes(WidgetValidator(store).validate("safe-widget"))


def test_ready_extra_column_name_is_contract_mismatch(store: ProjectStore) -> None:
    revision = _project(store)
    javascript = (
        b"grist.ready({requiredAccess: 'read table', columns: ["
        b"{name: 'name', type: 'Text'}, {name: 'extra', type: 'Text'}]});\n"
    )
    store.apply_files("safe-widget", revision, {"src/app.js": javascript}, [])
    assert "READY_COLUMN_MISMATCH" in _codes(WidgetValidator(store).validate("safe-widget"))


def test_absent_ready_column_does_not_duplicate_contract_mismatch(store: ProjectStore) -> None:
    revision = _project(store)
    store.apply_files(
        "safe-widget", revision,
        {"src/app.js": b"grist.ready({requiredAccess: 'read table', columns: []});\n"}, [],
    )
    codes = _codes(WidgetValidator(store).validate("safe-widget"))
    assert "DECLARED_COLUMN_MISSING" in codes
    assert "READY_COLUMN_MISMATCH" not in codes


def test_invalid_literal_ready_column_emits_value_free_stable_issue(store: ProjectStore) -> None:
    revision = _project(store)
    javascript = (
        b"grist.ready({requiredAccess: 'read table', columns: ["
        b"{name: '', type: 'Text'}, {name: 'name', type: 'Text'}]});\n"
    )
    store.apply_files("safe-widget", revision, {"src/app.js": javascript}, [])

    report = WidgetValidator(store).validate("safe-widget")

    issue = next(issue for issue in report.issues if issue.code == "READY_COLUMN_INVALID")
    assert issue.severity == "error"
    assert issue.evidence == ("column", "0")
    assert "READY_COLUMN_MISMATCH" not in _codes(report)
