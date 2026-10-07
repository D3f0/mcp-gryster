# Task 11 Report

## Status

Implemented the Docker-backed integration suite against the digest-pinned Grist 1.7.19 image.

## Delivered

- Added `integration/compose.yml` with the exact pinned image, test login, one fixed single organization, loopback-only random host port, isolated named volume, and a `/status` healthcheck using the image-provided `curl` binary.
- Added `integration/bootstrap.py`; it waits for `/status`, establishes a cookie session through the root and test-login endpoints, rotates the API key through `/api/profile/apikey`, then uses the source project's pinned Pygrister client to create one workspace, one document, a `People` table, and deterministic records.
- Bootstrap emits credentials and IDs only to a mode-0600 file and sanitizes exception output. The API key is never passed in process arguments.
- Added session-scoped Compose lifecycle fixtures with unique project names and ports, local Docker-unavailable skip / CI failure behavior, unconditional volume-and-orphan teardown, and sanitized status plus final-200-line diagnostics on startup/test/teardown failures.
- Added the requested `grist_base_url`, `grist_api_key`, `grist_ids`, and `integration_mcp_client` fixtures, plus read-policy settings/client fixtures.
- Added public FastMCP coverage for typed organization, workspace, document, table, column, and record reads; a less-common generic `see_team` read; policy-denied writes before adapter execution; write add/read/delete restoration; explicitly selected and redacted fixture capture; fixture exclusion from public project resources; and fresh app/client restart behavior against the same Grist.
- Marked every integration test both directly and at module level with `pytest.mark.integration`.
- Documented the integration command in `README.md`.

## Verification

Per assignment, no Docker containers, pytest suite, build, lint, mypy, or formatter commands were run.

Observed static checks:

- `uv run python -m py_compile` completed successfully for the bootstrap, integration fixtures, and all integration test modules.
- `GRIST_PORT=18484 docker compose ... config --quiet` accepted the Compose configuration.
- `git diff --check` completed cleanly.
- The configured healthcheck uses `curl`; the pinned Grist 1.7.19 Dockerfile installs `curl` specifically for Docker healthchecks.

## Concerns

- Runtime behavior against the pulled Grist image remains for the controller's prescribed `uv run pytest -m integration -v` run.
- The integration suite intentionally enables Grist's unauthenticated test-login endpoint only inside the ephemeral, loopback-bound test container.
