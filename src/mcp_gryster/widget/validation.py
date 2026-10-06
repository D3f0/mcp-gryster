"""Static validation for immutable widget project revisions."""

from __future__ import annotations

import json
import secrets
import shutil
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Literal, Protocol
from urllib.parse import urlsplit

import tree_sitter_javascript
from pydantic import BaseModel, ConfigDict, ValidationError
from tree_sitter import Language, Node, Parser

from mcp_gryster.errors import McpGrysterError
from mcp_gryster.widget.models import PreviewFixture, WidgetMetadata
from mcp_gryster.widget.paths import normalize_authored_path
from mcp_gryster.widget.projects import ProjectStore

ALLOWED_GRIST_COLUMN_TYPES = frozenset(
    {
        "Any", "Attachments", "Bool", "Choice", "ChoiceList", "Date", "DateTime",
        "Int", "Numeric", "Ref", "RefList", "Text",
    }
)
_ALLOWED_GRIST_MEMBERS = frozenset(
    {
        "docApi", "fetchSelectedRecord", "fetchSelectedTable", "getTableId",
        "mapColumnNames", "onNewRecord", "onOptions", "onRecord", "onRecords",
        "ready", "selectedTable", "setCursorPos", "setOption", "setOptions", "widgetApi",
    }
)
_ACCESS_RANK = {"none": 0, "read table": 1, "full": 2}
_JAVASCRIPT_LANGUAGE = Language(tree_sitter_javascript.language())


class ValidationIssue(BaseModel):
    """One stable, value-free validation finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    severity: Literal["error", "warning"]
    message: str
    evidence: tuple[str, ...] = ()


class ValidationReport(BaseModel):
    """Static findings and package eligibility for one immutable revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    project_id: str
    revision: str
    fixture_id: str | None
    issues: tuple[ValidationIssue, ...]
    valid: bool
    packageable: bool


class PreviewEvidence(Protocol):
    """Lookup contract for successful fixture-preview evidence."""

    def has_success(self, project_id: str, revision: str, fixture_id: str | None) -> bool:
        """Return whether this exact revision and fixture completed preview."""
        ...


class PreviewEvidenceStore:
    """In-memory successful preview evidence without authored data."""

    def __init__(self) -> None:
        self._successes: set[tuple[str, str, str | None]] = set()

    def record_success(self, project_id: str, revision: str, fixture_id: str | None) -> None:
        self._successes.add((project_id, revision, fixture_id))

    def has_success(self, project_id: str, revision: str, fixture_id: str | None) -> bool:
        return (project_id, revision, fixture_id) in self._successes


class _MissingPreviewEvidence:
    def has_success(self, project_id: str, revision: str, fixture_id: str | None) -> bool:
        return False


class _Script:
    def __init__(self, source: str | None) -> None:
        self.source = source
        self.content: bytes | None = None


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.assets: list[str] = []
        self.scripts: list[_Script] = []
        self._inline: _Script | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() == "script":
            source = values.get("src")
            script = _Script(source)
            self.scripts.append(script)
            if source is None:
                self._inline, self._parts = script, []
            else:
                self.assets.append(source)
            return
        asset = values.get("href" if tag.lower() == "link" else "src")
        if asset is not None:
            self.assets.append(asset)

    def handle_data(self, data: str) -> None:
        if self._inline is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inline is not None:
            self._inline.content = "".join(self._parts).encode()
            self._inline, self._parts = None, []


class _JavaScriptFacts:
    def __init__(self) -> None:
        self.ready_configs: list[tuple[Node | None, bytes]] = []
        self.grist_members: list[str] = []
        self.first_grist_script: int | None = None
        self.has_syntax_error = False


class WidgetValidator:
    """Validate metadata, assets, JavaScript ASTs, and optional fixtures."""

    def __init__(self, store: ProjectStore, preview_evidence: PreviewEvidence | None = None) -> None:
        self._store = store
        self._preview_evidence = preview_evidence or _MissingPreviewEvidence()

    def validate(self, project_id: str, fixture_id: str | None = None) -> ValidationReport:
        """Validate only the current immutable revision without executing JavaScript."""
        snapshot = self._store.read(project_id)
        destination = self._store.workspace / f".validation-{secrets.token_hex(16)}"
        self._store.materialize(project_id, snapshot.revision, destination)
        try:
            files = {
                path: destination.joinpath(*path.split("/")).read_bytes()
                for path in snapshot.files
            }
            issues = self._validate_files(files, fixture_id)
        finally:
            shutil.rmtree(destination, ignore_errors=True)
        valid = all(issue.severity != "error" for issue in issues)
        return ValidationReport(
            project_id=project_id,
            revision=snapshot.revision,
            fixture_id=fixture_id,
            issues=tuple(issues),
            valid=valid,
            packageable=valid and self._preview_evidence.has_success(
                project_id, snapshot.revision, fixture_id
            ),
        )

    def _validate_files(
        self, files: Mapping[str, bytes], fixture_id: str | None
    ) -> list[ValidationIssue]:
        metadata = _metadata(files.get("widget.json"))
        if metadata is None:
            return [_issue("METADATA_INVALID", "error", "Widget metadata is invalid", "metadata")]
        issues: list[ValidationIssue] = []
        if metadata.access_level == "full":
            issues.append(_issue("FULL_ACCESS", "warning", "Widget requests full access", "access"))
        entrypoint = f"src/{metadata.entrypoint}"
        html = files.get(entrypoint)
        if html is None:
            issues.append(
                _issue("ENTRYPOINT_MISSING", "error", "Widget entrypoint is missing", "entrypoint")
            )
            return issues
        document = _DocumentParser()
        try:
            document.feed(html.decode())
            document.close()
        except (UnicodeDecodeError, ValueError):
            issues.append(_issue("ENTRYPOINT_INVALID", "error", "Widget entrypoint is invalid", "entrypoint"))
            return issues
        base = PurePosixPath(entrypoint).parent
        if any(_is_external(asset) for asset in document.assets):
            issues.append(_issue("EXTERNAL_ORIGIN", "warning", "Widget references an external origin", "asset"))
        if any(
            (path := _local_asset_path(base, asset)) is None or path not in files
            for asset in document.assets
            if not _is_external(asset)
        ):
            issues.append(_issue("ASSET_MISSING", "error", "A local widget asset is missing", "asset"))
        javascript, plugin_scripts = _javascript_sources(document, files, base)
        facts = _analyze_javascript(javascript)
        if facts.has_syntax_error:
            issues.append(_issue("JAVASCRIPT_SYNTAX", "error", "JavaScript contains syntax errors", "javascript"))
        else:
            issues.extend(_javascript_issues(metadata, facts, plugin_scripts))
        if fixture_id is not None:
            issues.extend(_fixture_issues(files, metadata, fixture_id))
        return issues


def _metadata(raw: bytes | None) -> WidgetMetadata | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
        expected = {
            "accessLevel", "authors", "columns", "description", "entrypoint", "name",
            "schemaVersion", "widgetId",
        }
        if not isinstance(parsed, dict) or parsed.get("schemaVersion") != 1 or not set(parsed) <= expected:
            return None
        widget_id = parsed.get("widgetId")
        if not isinstance(widget_id, str) or len(widget_id) > 255:
            return None
        parts = widget_id.split(".")
        if any(not part or any(not (char.isalnum() or char in "_-") for char in part) for part in parts):
            return None
        return WidgetMetadata.model_validate(
            {
                "project_id": "validation-project", "widget_id": widget_id,
                "name": parsed.get("name"), "description": parsed.get("description", ""),
                "entrypoint": parsed.get("entrypoint"), "access_level": parsed.get("accessLevel"),
                "columns": parsed.get("columns", []), "authors": parsed.get("authors", []),
            }
        )
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValidationError, McpGrysterError):
        return None


def _javascript_sources(
    document: _DocumentParser, files: Mapping[str, bytes], base: PurePosixPath
) -> tuple[list[tuple[int, bytes]], list[int]]:
    sources: list[tuple[int, bytes]] = []
    plugins: list[int] = []
    for index, script in enumerate(document.scripts):
        if script.source is None:
            if script.content is not None:
                sources.append((index, script.content))
            continue
        if _is_grist_plugin(script.source):
            plugins.append(index)
        if not _is_external(script.source):
            path = _local_asset_path(base, script.source)
            if path is not None and path in files:
                sources.append((index, files[path]))
    return sources, plugins


def _javascript_issues(
    metadata: WidgetMetadata, facts: _JavaScriptFacts, plugin_scripts: list[int]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if facts.first_grist_script is not None and (
        not plugin_scripts or facts.first_grist_script < min(plugin_scripts)
    ):
        issues.append(_issue("PLUGIN_ORDER", "error", "Grist plugin must load before initialization", "script"))
    if not facts.ready_configs:
        issues.append(_issue("GRIST_READY_MISSING", "error", "Widget does not initialize Grist", "javascript"))
    else:
        node, source = facts.ready_configs[0]
        config = _literal_ready_config(node, source)
        if config is None:
            issues.append(_issue("DYNAMIC_PLUGIN_CONFIG", "error", "Grist config is not statically verifiable", "javascript"))
        else:
            access, columns = config
            if access is not None and access != metadata.access_level:
                issues.append(_issue("READY_ACCESS_MISMATCH", "error", "Grist access differs from metadata", "access"))
            names = {name for name, _ in columns}
            if any(column.name not in names for column in metadata.columns):
                issues.append(_issue("DECLARED_COLUMN_MISSING", "error", "Metadata column is absent from initialization", "column"))
    if any(member not in _ALLOWED_GRIST_MEMBERS for member in facts.grist_members):
        issues.append(_issue("UNSUPPORTED_GRIST_MEMBER", "warning", "JavaScript uses an unsupported Grist member", "javascript"))
    return issues


def _fixture_issues(
    files: Mapping[str, bytes], metadata: WidgetMetadata, fixture_id: str
) -> list[ValidationIssue]:
    try:
        path = normalize_authored_path(f"fixtures/{fixture_id}.json")
    except McpGrysterError:
        return [_issue("FIXTURE_INVALID", "error", "Fixture identifier is invalid", "fixture")]
    raw = files.get(path)
    if raw is None:
        return [_issue("FIXTURE_MISSING", "error", "Fixture is missing", "fixture")]
    try:
        fixture = PreviewFixture.model_validate_json(raw)
    except (ValidationError, ValueError):
        return [_issue("FIXTURE_INVALID", "error", "Fixture is invalid", "fixture")]
    issues: list[ValidationIssue] = []
    fixture_columns = {column.id: column for column in fixture.columns}
    if any(column.type not in ALLOWED_GRIST_COLUMN_TYPES for column in fixture.columns):
        issues.append(_issue("FIXTURE_TYPE_UNSUPPORTED", "error", "Fixture has an unsupported column type", "column"))
    for declaration in metadata.columns:
        mapped = fixture.mappings.get(declaration.name)
        if mapped is None:
            if not declaration.optional:
                issues.append(_issue("FIXTURE_REQUIRED_COLUMN", "error", "Fixture lacks a required mapping", "mapping"))
            continue
        column = fixture_columns.get(mapped)
        if column is None:
            issues.append(_issue("FIXTURE_REQUIRED_COLUMN", "error", "Fixture mapping lacks a column", "mapping"))
            continue
        if _base_type(column.type) != _base_type(declaration.type):
            issues.append(_issue("FIXTURE_TYPE_MISMATCH", "error", "Fixture column type is incompatible", "column"))
        if not declaration.allow_multiple and column.type in {"ChoiceList", "RefList"}:
            issues.append(_issue("FIXTURE_MAPPING_MULTIPLICITY", "error", "Fixture mapping multiplicity is incompatible", "mapping"))
    if fixture.selected_record_id is not None and all(
        record.id != fixture.selected_record_id for record in fixture.records
    ):
        issues.append(_issue("FIXTURE_SELECTED_RECORD", "error", "Fixture selected record is absent", "selection"))
    if _ACCESS_RANK[fixture.access_level] < _ACCESS_RANK[metadata.access_level]:
        issues.append(_issue("FIXTURE_ACCESS_MISMATCH", "error", "Fixture access is insufficient", "access"))
    return issues


def _issue(code: str, severity: Literal["error", "warning"], message: str, *evidence: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity=severity, message=message, evidence=evidence)


def _is_external(reference: str) -> bool:
    return urlsplit(reference).scheme.lower() in {"http", "https"} or reference.startswith("//")


def _is_grist_plugin(reference: str) -> bool:
    return _is_external(reference) and urlsplit(reference).path.endswith("/grist-plugin-api.js")


def _local_asset_path(base: PurePosixPath, reference: str) -> str | None:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return None
    try:
        return normalize_authored_path(str(base / parsed.path))
    except McpGrysterError:
        return None


def _analyze_javascript(sources: Iterable[tuple[int, bytes]]) -> _JavaScriptFacts:
    parser = Parser(_JAVASCRIPT_LANGUAGE)
    facts = _JavaScriptFacts()
    for script_index, source in sources:
        root = parser.parse(source).root_node
        facts.has_syntax_error |= root.has_error
        for node in _walk(root):
            if node.type != "member_expression":
                continue
            object_node = node.child_by_field_name("object")
            property_node = node.child_by_field_name("property")
            if object_node is None or property_node is None or _node_text(source, object_node) != "grist":
                continue
            member = _node_text(source, property_node)
            facts.grist_members.append(member)
            if facts.first_grist_script is None:
                facts.first_grist_script = script_index
            if member == "ready" and node.parent is not None and node.parent.type == "call_expression":
                arguments = node.parent.child_by_field_name("arguments")
                named = arguments.named_children if arguments is not None else []
                facts.ready_configs.append((named[0] if len(named) == 1 else None, source))
    return facts


def _walk(root: Node) -> Iterable[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode(errors="replace")


def _literal_ready_config(
    config: Node | None, source: bytes
) -> tuple[str | None, tuple[tuple[str, str], ...]] | None:
    if config is None or config.type != "object":
        return None
    values = _object_values(config, source)
    if values is None:
        return None
    access: str | None = None
    if "requiredAccess" in values:
        access = _literal_string(values["requiredAccess"], source)
        if access is None:
            return None
    columns: list[tuple[str, str]] = []
    columns_node = values.get("columns")
    if columns_node is not None:
        if columns_node.type != "array":
            return None
        for item in columns_node.named_children:
            item_values = _object_values(item, source)
            if item_values is None or "name" not in item_values or "type" not in item_values:
                return None
            name = _literal_string(item_values["name"], source)
            column_type = _literal_string(item_values["type"], source)
            if name is None or column_type is None:
                return None
            columns.append((name, column_type))
    return access, tuple(columns)


def _object_values(node: Node, source: bytes) -> dict[str, Node] | None:
    if node.type != "object":
        return None
    values: dict[str, Node] = {}
    for pair in node.named_children:
        if pair.type != "pair":
            return None
        key = pair.child_by_field_name("key")
        value = pair.child_by_field_name("value")
        if key is None or value is None:
            return None
        key_text = _literal_string(key, source) if key.type == "string" else _node_text(source, key)
        if key_text is None:
            return None
        values[key_text] = value
    return values


def _literal_string(node: Node, source: bytes) -> str | None:
    if node.type != "string":
        return None
    text = _node_text(source, node)
    if len(text) < 2 or text[0] not in {'"', "'"} or text[-1] != text[0] or "\\" in text[1:-1]:
        return None
    return text[1:-1]


def _base_type(column_type: str) -> str:
    return column_type.split(":", 1)[0]
