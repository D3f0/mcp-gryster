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
