# Task 7 Report

## Status

Complete.

## Commits

- `f8ac903` — `feat: create safe widget preview fixtures`
- `e11035b` — `fix: sanitize client fixture records`
- `902332c` — `fix: keep preview fixture schema canonical`

## Implementation

- Added frozen Pydantic models for client fixtures, Grist fixture requests, fixture columns/records/sources, and value-free summaries.
- Added `FixtureManager.create` and async `FixtureManager.create_from_grist`.
- Both creation paths require an expected project revision and commit only through `ProjectStore.apply_files`; summaries return the resulting revision and fixture digest.
- Grist capture uses only `GristAdapter.call` routes for tables, columns, and records. It accepts `GristCallResult` and exact `{status_code, data}` normalized wrappers, while rejecting malformed or unexpected table, column, record, and wrapper structures.
- Enforced non-empty unique column allowlists, requested-field-only serialization, row/byte/depth bounds before commit, attachment metadata opt-in with ID-only storage, configured name/value redaction, and authentication-secret rejection in metadata.
- Added a source materialization path selector demonstrating how Task 10 can omit `fixtures/` without changing package materialization now.

## Verification Run

- `uv run pytest tests/unit/test_fixtures.py -v` — 15 passed.
- `uv run ruff check src/mcp_gryster/widget tests/unit/test_fixtures.py` — passed.
- `uv run mypy src/mcp_gryster/widget` — passed, 5 source files.
- Runtime smoke: captured a Grist-derived fixture through a fake adapter, read its committed JSON from the new revision, confirmed only the selected field was present, and observed `fixture=preview rows=1 revision_changed=True`.

## Verification Not Run

- Project-wide `uv run pytest -m 'not integration'` was not run per delegated-task instructions; the main agent owns project-wide validation.
- No formatter, build, install, or packaging command was run.

## Concerns

- Packaging exclusion itself remains intentionally deferred to Task 10. Task 7 provides only `FixtureManager.source_materialization_paths` and its unit test as the source-only selector/interface.

## Review Round 1

- Added trusted `ProjectStore.materialize(..., include_paths=...)` selection with normalized, unique, revision-present path validation. Default materialization remains all files for preview compatibility.
- Added `FixtureManager.materialize_sources`, which invokes the store with only `widget.json` and `src/` paths; its test verifies the destination contains no fixtures.
- Replaced serialized metadata regex scanning with recursive key/value validation for authorization, API-key, token, and Bearer secrets.
- Rejected recursive NaN and infinity values before commit and disabled non-standard JSON floats during serialization.
- Applied column-name redaction to both IDs and labels, replacing sensitive labels and their values with `[REDACTED]`.
- Focused verification: `uv run pytest tests/unit/test_fixtures.py -v` — 27 passed.
