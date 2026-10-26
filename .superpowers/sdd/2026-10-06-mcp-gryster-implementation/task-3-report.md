# Task 3 implementation report

## Status

Implemented the checked-in Pygrister 0.10.0 method registry and per-domain authorization policy.

## Files

- `src/mcp_gryster/grist/models.py`: frozen Pydantic method and parameter models with constrained domains, effects, result kinds, public names, dispatch support, and typed-tool exposure.
- `src/mcp_gryster/grist/registry.py`: immutable registry loading, unsupported-operation-safe dispatch lookup, audit introspection, exact pinned-version/signature validation, and full remote API drift reporting.
- `src/mcp_gryster/grist/registry.json`: 137 remote operations from the installed pinned Pygrister 0.10.0 `GristApi`, including exact Python parameter names/defaults, domains, effects, result kinds, typed-tool flags, and explicit unsupported status for `delete_myself`.
- `src/mcp_gryster/grist/policy.py`: `none`/`read`/`write` authorization truth table using Task 2's external `schema` alias through its internal `schema_access` field.
- `src/mcp_gryster/grist/__init__.py`: public registry and policy exports.
- `tests/unit/test_registry.py`: representative classifications, complete pinned API coverage, helper exclusion, unsupported introspection/dispatch, exact signatures/defaults, validation, SCIM read semantics, installed-version rejection, and API drift behavior.
- `tests/unit/test_policy.py`: complete authorization truth table, schema alias, and SQL read-only invariant.

## Design details

- `NON_REMOTE_METHODS` explicitly lists constructor-adjacent configuration, session, and inspection helpers. Private methods are excluded by name.
- `MethodRegistry.get()` is dispatch-oriented and rejects missing or unsupported methods with stable `METHOD_NOT_SUPPORTED` errors. `MethodRegistry.inspect()` returns unsupported entries for audit and tests.
- Registry model construction validates the artifact against both installed Pygrister 0.10.0 and the pinned `GristApi` signatures. `assert_compatible()` additionally reports supported methods that disappeared or changed and newly added public remote methods; removal of an unsupported stub is allowed.
- HTTP POST does not automatically imply a write effect: SCIM searches and parameterized SQL are classified read-only based on operation semantics.
- File-writing downloads are `file`; paginator wrappers are `paginated`; ordinary decoded responses are `json`. No pinned 0.10.0 operation directly returns binary bytes.
- Every SQL entry is model-validated as read-only.

## TDD evidence

- Initial focused collection failed because `mcp_gryster.grist` did not exist.
- After implementation, focused tests passed.
- Additional installed-version and unsupported-stub compatibility tests were observed failing before their fixes, then passed afterward.

## Verification

Executed successfully:

```sh
uv run pytest tests/unit/test_registry.py tests/unit/test_policy.py -q
# 22 passed in 0.19s

uv run ruff check src/mcp_gryster/grist tests/unit/test_registry.py tests/unit/test_policy.py
# All checks passed!

uv run mypy src/mcp_gryster/grist
# Success: no issues found in 4 source files

uv run python -c 'from mcp_gryster.grist.registry import load_registry; from pygrister.api import GristApi; load_registry().assert_compatible(GristApi)'
# exit 0, no output
```

## Concerns

- Domain/effect/result classifications are a maintained capability boundary. Any intentional Pygrister upgrade must regenerate and review the checked-in artifact rather than accepting API additions automatically.
- `typed_tool` marks the initial high-value typed surface; all other supported entries remain available to the later controlled generic call route.
