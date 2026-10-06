# Task 6 implementation report

## Status

Implemented Task 6: a security-focused, immutable revision store for widget projects with atomic state-pointer publication, canonical authored paths, deterministic revisions, optimistic concurrency, bounded writes, safe recovery, and controlled materialization.

## Files

- `src/mcp_gryster/widget/__init__.py`: exports the widget project interface.
- `src/mcp_gryster/widget/models.py`: frozen widget metadata, authored update, and project snapshot models with canonical JSON serialization.
- `src/mcp_gryster/widget/paths.py`: strict project-ID, revision-ID, and authored POSIX path validation.
- `src/mcp_gryster/widget/projects.py`: immutable revision trees, hard-link reuse for unchanged regular files, durable staging, atomic `current.json`, validated reads, startup staging cleanup, and workspace-contained materialization.
- `tests/unit/test_paths.py`: hostile path, project-ID, and symlinked-workspace coverage.
- `tests/unit/test_projects.py`: creation/read/list, deterministic digest, batch update/delete, conflicts, limits, hard links, failure atomicity, recovery, pointer validation, and materialization security coverage.

## Security and atomicity

- Authored paths accept only `widget.json`, files below `src/`, and one-component JSON fixture names below `fixtures/`; absolute, Windows, non-canonical, traversal, control-character, reserved, and symlink paths are rejected.
- `current.json` is required to be a no-follow regular file containing exactly one lowercase SHA-256 revision field before any revision path is derived.
- Revision reads traverse directory descriptors and use no-follow opens, rejecting links and non-regular authored entries.
- Limits are evaluated against the complete resulting tree before staging starts.
- Unchanged prior-revision files are hard-linked only after validating the source as a regular file; unsupported or failed hard links fall back to durable copies.
- New revisions are complete and fsynced before rename; the fsynced temporary pointer is atomically replaced only afterward. Failures preserve the old visible pointer.
- Startup removes unreachable `.staging-*` trees without following symlinks.
- Materialization accepts only unused destinations under the canonical workspace and outside internal project state. The source revision is validated and read without following symlinks before destination creation, and destination ancestors are revalidated before publication.

## TDD / verification status

Tests were authored before implementation. Per the task contract, no tests, builds, lint, mypy, dependency installs, or formatters were executed by this worker. The controller must run:

```sh
uv run pytest tests/unit/test_paths.py tests/unit/test_projects.py -v
uv run ruff check src/mcp_gryster/widget tests/unit/test_paths.py tests/unit/test_projects.py
uv run mypy src/mcp_gryster/widget
```

## Concerns

- Store mutation is serialized within one `ProjectStore` process by an `RLock`; cross-process compare-and-swap semantics are not provided. Each publication remains filesystem-atomic, but multiple independently constructed writer processes must be externally serialized.
- Materialization deliberately requires a new destination below the configured workspace and rejects paths inside `.mcp-gryster/projects`; callers that need another destination must first validate it into an owned workspace path.
- No Grist version pin was introduced; Task 6 is independent of the Grist 1.7.19 compatibility target.
