# Task 12 Report

## Delivered

- Expanded the Pygrister-first README with supported configuration, complete policy defaults, transport security boundaries, widget fixture/package protections, and tested-version limits.
- Added README contract and built-wheel distribution tests. The distribution test inspects metadata, console entrypoint, required static/registry/type data, exclusions, and performs an offline install into a clean environment from the built wheel.
- Explicitly included `registry.json`, `harness.html`, `grist-bridge.js`, and `py.typed` in the Hatch wheel configuration.
- Added unit, Docker integration, and distribution CI jobs. The distribution job starts with an empty cache, permits network only during locked dependency resolution, then runs offline `uvx --from <wheel>` CLI and FastMCP STDIO initialize/list-tools smoke checks with bounded timeouts.
- Added executable `scripts/stdio_smoke.py` using FastMCP 3 `Client` and `StdioTransport`.

## Verification

Per assignment instruction, no tests, builds, lint, mypy, installs, formatter, or uvx commands were run. `git diff --check` completed successfully.

## Controller Fix

- Normalized module import-to-constant spacing in both new tests for Ruff I001 compliance.
- Per controller instruction, no checks were run for this mechanical correction.

## Controller Distribution Fix

- Changed the clean-environment wheel install test to resolve dependencies normally instead of depending on incidental contents of the developer's global uv cache.
- Kept the CI distribution cache empty while allowing `uv sync` and wheel-based `uvx` invocations to populate and resolve it from scratch; removed offline flags from both bounded uvx smoke paths.
- Per controller instruction, no checks were run for this correction.

## Controller Fix Round 1

- Moved the distribution job's clean cache boundary to immediately before wheel-based uvx checks, leaving locked sync/build and smoke-helper dependencies on the normal cache.
- Recreates a distinct empty wheel uvx cache and exports it through `GITHUB_ENV`, so both direct uvx and the STDIO child inherit the same isolated cache.
- Corrected fixture documentation: column IDs are retained for mappings, matching labels and values are redacted, and sensitive identifiers must be omitted via the request allowlist.
- Per controller instruction, no checks were run.
