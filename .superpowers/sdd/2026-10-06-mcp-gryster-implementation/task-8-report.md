# Task 8 Report

## Status

Implemented the widget validation pipeline against immutable `ProjectStore` revisions.

## Delivered

- Added `WidgetValidator.validate(project_id, fixture_id=None) -> ValidationReport`.
- Added stable, value-free validation issues for metadata, entrypoints/assets, access, JavaScript semantics, and fixtures.
- Parses HTML with the standard library while retaining script order.
- Parses JavaScript exclusively with pinned `tree-sitter` and `tree-sitter-javascript`; it never executes authored JavaScript.
- Added exact revision/fixture preview evidence matching. Missing evidence keeps a valid widget non-packageable.
- Centralized allowed Grist fixture column types in `ALLOWED_GRIST_COLUMN_TYPES`.
- Added basic widget fixture files and deterministic AST tests that assert stable issue codes/evidence rather than parser-specific positions.
- Validation reads only the immutable revision materialized by `ProjectStore`; traversal and symlink protections remain enforced by that boundary.

## Verification

Executed successfully:

- `uv run pytest tests/unit/test_validation.py -v` — 14 passed.
- `uv lock --check` — resolved successfully.
- `uv run ruff check src/mcp_gryster/widget/validation.py tests/unit/test_validation.py` — all checks passed.
- `uv run mypy src/mcp_gryster/widget/validation.py` — no issues.
- `git diff --check` — clean.

## Unexecuted Verification

- Project-wide test, lint, type-check, build, and formatter commands were intentionally not run; the controller owns project-wide validation.

## Concerns

- Preview evidence is intentionally in-memory and minimal for Task 9 injection; it records only project ID, revision, fixture ID, and success.
- Static analysis deliberately reports dynamic `grist.ready` configuration as `DYNAMIC_PLUGIN_CONFIG` rather than attempting evaluation.

## Review Round 1

- Added a persisted fixture schema without request-only `expectedRevision` and support for scalar/list mappings.
- Corrected zero-argument/default-access `grist.ready` semantics and direct-call AST recognition.
- Added parameterized Grist type support, `Any`/alternative contract matching, and full ready column contract comparison.
- Added value-free `INLINE_SCRIPT` and `UNUSED_FIXTURE_COLUMN` findings.
- Mapping multiplicity now follows mapping value shape rather than Grist column type.
- Added focused regression coverage for every review finding before implementation.
- Per controller instruction, no checks were executed in this review round.

## Controller Fix Round

- Suppressed redundant `READY_COLUMN_MISMATCH` when a declared column is already reported missing.
- Canonicalized tuple-backed mapping values to JSON lists before recursive fixture validation.
- Corrected attachment-only test setup to use an empty mapping while preserving strict mapping enforcement.
- Added focused regression coverage before implementation.
- Per controller instruction, no checks were executed in this fix round.
