# Task 2 implementation report

## Status

Implemented Task 2: immutable explicit-mapping configuration, safe public errors, metadata-only JSONL auditing, and traversal/symlink-resistant atomic artifact storage.

## Files

- `src/mcp_gryster/config.py`: frozen settings and domain policy models; complete twelve-domain JSON policy parsing; safe policy and numeric defaults; positive port/timeout/limit validation; explicit Grist overrides, redaction patterns, and bearer authentication; non-loopback HTTP guard; one-time resolved workspace creation; no process-environment access or mutation.
- `src/mcp_gryster/errors.py`: stable public error code model, known local and dependency failure mapping, random correlation IDs, input-independent public messages, and redacted server-side diagnostics.
- `src/mcp_gryster/audit.py`: frozen metadata-only event and durable, synchronized JSON Lines sink.
- `src/mcp_gryster/artifacts.py`: canonical containment checks, rejection of traversal/absolute/symlink paths, `O_NOFOLLOW` temporary creation where supported, file and directory `fsync`, and atomic replacement.
- `tests/unit/test_config.py`: configuration defaults, complete policy handling, remote HTTP authentication, positive numeric validation, frozen models, workspace creation, and explicit environment isolation.
- `tests/unit/test_errors_audit.py`: exception input secrecy, stable mappings/codes, metadata-only audit records, atomic artifacts, and hostile path rejection.

## TDD / expected RED and GREEN reasoning

- Tests were written before production modules. Before implementation they are expected to fail during collection because `config`, `errors`, `audit`, and `artifacts` do not exist.
- The implementation supplies every imported interface and the behavior asserted by those tests, so controller execution is expected to turn those failures GREEN.
- Per the delegation contract, no tests, builds, linters, formatters, dependency installs, or other verification commands were run in this worktree.

## Self-review

- `Settings.load` copies and reads only its supplied mapping. It neither reads nor mutates `os.environ`.
- Default access is deny-all except records read; supplied policies must contain exactly the twelve required keys and valid enum values.
- Port, preview timeout, file limit, aggregate limit, and fixture row limit have explicit safe defaults and require positive values; port is additionally capped at 65535.
- Streamable HTTP may bind externally only with configured authentication.
- Public messages are constants rather than exception-derived strings. Raw exception text appears only in debug logging after authorization, secret, JSON body, and absolute-path redaction.
- Audit models structurally cannot accept arguments or response bodies because extra fields are forbidden.
- Artifact writes validate every path component, refuse symlink parents, open an unpredictable exclusive temporary file with `O_NOFOLLOW` when available, flush it, replace atomically, and sync the parent directory.

## Controller verification commands

```sh
uv run pytest tests/unit/test_config.py tests/unit/test_errors_audit.py -v
uv run ruff check src/mcp_gryster/config.py src/mcp_gryster/errors.py src/mcp_gryster/audit.py src/mcp_gryster/artifacts.py
uv run mypy src/mcp_gryster/config.py src/mcp_gryster/errors.py src/mcp_gryster/audit.py src/mcp_gryster/artifacts.py
```

## Concerns

- Authentication is represented as a configured bearer token (`MCP_GRYSTER_HTTP_AUTH_TOKEN`, with `MCP_GRYSTER_HTTP_BEARER_TOKEN` accepted as an alternate input); the later HTTP composition task must enforce it on incoming requests.
- Filesystem symlink safety is checked immediately before replacement and uses `O_NOFOLLOW` for temporary creation; fully race-free directory traversal would require platform-specific directory-descriptor APIs throughout.

## Controller collection-failure fix

- Root cause: Python 3.14 rejects the incompatible C-level instance layouts from direct `BaseModel` and `Exception` multiple inheritance before test collection can begin.
- Regression coverage was added first for constructing, raising, catching, inspecting, and JSON-serializing `McpGrysterError`.
- `McpGrysterError` now inherits only from `Exception` and composes a frozen private Pydantic model. Its constructor and public structured fields remain unchanged, as do `model_dump` and `model_dump_json` serialization.
- Structured field assignment delegates to the frozen model and therefore continues to raise `ValidationError`; invalid stable codes remain Pydantic validation failures.
- No checks were executed per controller instruction. Expected verification remains:

```sh
uv run pytest tests/unit/test_config.py tests/unit/test_errors_audit.py -v
uv run ruff check src/mcp_gryster/config.py src/mcp_gryster/errors.py src/mcp_gryster/audit.py src/mcp_gryster/artifacts.py
uv run mypy src/mcp_gryster/config.py src/mcp_gryster/errors.py src/mcp_gryster/audit.py src/mcp_gryster/artifacts.py
```

## Controller clean-output fix

- Preserved the external policy JSON key `schema` using the internal `schema_access` field with a Pydantic alias, eliminating the Pydantic `BaseModel.schema` shadowing warning.
- Updated exact policy-key derivation to use serialized aliases, so the accepted twelve-key contract remains unchanged.
- Combined the nested external-HTTP/authentication guard to satisfy Ruff `SIM102` without changing behavior.
- Declared `ErrorCode` with Python 3.12 type-alias syntax to satisfy Ruff `UP040`.
- No checks were executed per controller instruction. The expected pytest, Ruff, and mypy commands are listed above.

## Controller strict-typing fix

- Declared the composed `_data` slot as `_ErrorData`, making the internal delegation target visible to strict mypy.
- Added explicit typed casts at the Pydantic delegation boundary because the installed Pydantic typing exposes these generated attributes and serializer returns as `Any` under strict checking.
- Public property, serialization, and throwable exception behavior is unchanged.
- No checks were executed per controller instruction. The expected pytest, Ruff, and mypy commands remain listed above.

## Controller redundant-cast fix

- Removed the six casts that strict mypy identified as redundant after `_data` received its explicit `_ErrorData` annotation, and removed the unused `cast` import.
- Throwable and structured public error behavior is unchanged.
- No checks were executed per controller instruction. The expected pytest, Ruff, and mypy commands remain listed above.

## Security review fixes

- `test_public_error_redacts_quoted_headers_and_response_repr` covers quoted `Authorization` dictionary reprs and structured `response` bodies; sanitization now removes both before debug logging.
- `test_requests_value_error_subclass_is_remote_failure` covers Requests exceptions that also inherit `ValueError`; dependency-origin classification now precedes generic local failure mapping.
- `test_artifact_store_anchors_replace_when_parent_is_substituted` simulates replacing the validated parent path with a symlink during the write. Directory traversal, temporary creation, replacement, cleanup, and directory `fsync` now remain anchored to no-follow directory descriptors, so bytes cannot escape through pathname substitution.
- No checks were executed per controller instruction. Expected verification:

```sh
uv run pytest tests/unit/test_config.py tests/unit/test_errors_audit.py -v
uv run ruff check src/mcp_gryster/config.py src/mcp_gryster/errors.py src/mcp_gryster/audit.py src/mcp_gryster/artifacts.py
uv run mypy src/mcp_gryster/config.py src/mcp_gryster/errors.py src/mcp_gryster/audit.py src/mcp_gryster/artifacts.py
```

## Portable artifact fallback fix

- `test_artifact_store_fallback_writes_contained_artifact` forces the no-`dir_fd` branch and covers a successful contained durable write.
- `test_artifact_store_fallback_rejects_unsafe_paths` forces that branch and covers traversal, absolute path, and symlink-parent rejection.
- Artifact storage now detects the complete directory-descriptor capability set before using descriptor-relative operations. Unsupported platforms use canonical containment plus repeated symlink checks, exclusive temporary creation, file `fsync`, atomic `os.replace`, and best-effort directory `fsync` where the platform allows opening directories.
- No checks were executed per controller instruction. Expected verification remains the pytest, Ruff, and mypy commands listed above.
