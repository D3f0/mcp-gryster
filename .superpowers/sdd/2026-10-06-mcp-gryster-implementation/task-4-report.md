# Task 4 implementation report

## Status

Implemented the isolated, policy-checked Pygrister adapter and controlled dispatch seam.

## Files

- `src/mcp_gryster/grist/adapter.py`: authorization-first dispatch, strict signature binding and annotation validation, isolated per-call clients, thread offloading, bounded pagination, safe response normalization, artifact persistence, and metadata-only auditing.
- `src/mcp_gryster/grist/models.py`: recursive JSON value type and frozen `GristCallResult` carrying HTTP status plus normalized data.
- `src/mcp_gryster/grist/__init__.py`: public adapter/result exports.
- `tests/unit/test_adapter.py`: fake-API behavioral coverage for authorization and validation ordering, lookup containment, per-call clients and config, concurrency, status/results, masked failures, file/binary artifacts, and pagination limits.

## Design details

- Registry lookup and policy authorization happen before signature inspection, argument binding, factory creation, or instance attribute lookup.
- Registered Python signatures are bound with `inspect.Signature.bind`; values are recursively checked against resolved annotations without coercion. Workspace file names become contained `Path` objects.
- Each invocation receives a fresh `GristApi(config=...)` with only documented settings, forced raise-on-error, and safe mode derived from whether policy permits any writes. Global Pygrister configuration is untouched.
- Blocking construction, method execution, paginator iteration, file reads, and artifact writes occur in `asyncio.to_thread`.
- Successful `(status_code, response)` values retain the status code. JSON-compatible data is recursively normalized; non-finite floats and unsupported objects are rejected rather than stringified.
- Non-2xx responses retain only their numeric status in safe error details; response bodies and raised exception text are not exposed.
- Binary and output-file data is copied into `ArtifactStore`; only safe artifact metadata is returned. Temporary workspace output is removed.
- The `finally` audit path always records tool, domain/effect when known, success/error/denied status, duration, and optional correlation ID. It never records arguments or response data.

## TDD evidence

Observed focused test collection fail before implementation because `mcp_gryster.grist.adapter` did not exist. After implementation and fixes, the focused adapter suite passed.

## Verification

Executed scoped verification only, as required by the assignment:

```sh
uv run pytest tests/unit/test_adapter.py -q
# 9 passed in 0.25s

git diff --check
# exit 0
```

Controller commands intentionally not executed:

```sh
uv run pytest tests/unit/test_adapter.py -v
uv run ruff check src/mcp_gryster/grist/adapter.py tests/unit/test_adapter.py
uv run mypy src/mcp_gryster/grist/adapter.py
```

Controller follow-up: the pristine focused suite passed 9/9. Ruff then reported RUF012 for mutable fake API class state; `configs` and `getattr_names` were annotated as `ClassVar`. Per controller instruction, no checks were rerun after this mechanical typing fix.

Controller follow-up: Mypy reported the untyped third-party Pygrister import and branch-inferred paginator data type. The import now has a narrow `import-untyped` suppression, and the normalization variable is explicitly `Any`. Per controller instruction, no checks were rerun after these typing-only fixes.

## Concerns

- Pagination limits are adapter constructor controls (`max_pages=100`, `max_items=10000`) because the existing immutable `Settings` interface does not define dedicated Grist pagination fields. Callers may set stricter configured values when constructing the adapter.
- Pygrister file operations with an omitted optional output path are assigned a contained unique workspace path so their output cannot fall into the process working directory.
