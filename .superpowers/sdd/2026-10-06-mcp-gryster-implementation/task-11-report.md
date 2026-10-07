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

## Controller Collection Fix

- Added package markers to `tests/unit` and `tests/integration` so pytest collection keeps duplicate test-module basenames in distinct import namespaces.
- Per controller instruction, no checks were run for this fix.

## Login Readiness Follow-up

- Changed both the Compose healthcheck and bootstrap health probe to `/status?ready=1`; Grist 1.7.19 serves plain `/status` before `FlexServer.setReady(true)`, while its request gate returns HTTP 503 until that point.
- Added one bounded login-readiness deadline across root session establishment, `/test/login`, and API-key rotation. The same cookie-enabled opener is retained throughout; only transient HTTP 429/502/503/504 and transport failures are retried, while permanent 4xx errors fail immediately.
- Set the public `APP_HOME_URL` to the host-reachable loopback URL and `APP_HOME_INTERNAL_URL` to the container-local URL, with same-origin serving enabled. This keeps login redirects and cookies on the caller's origin without asking the container to reach the host-published port.
- Added focused unit coverage for the readiness query, transient status handling, cookie retention, and permanent-client-error behavior.
- Verification: `uv run pytest tests/unit/test_integration_bootstrap.py -q` passed (3 tests). Per assignment, Docker and project-wide checks were not run.

## Static Analysis Follow-up

- Replaced the bootstrap's broad exception handler with the concrete network, filesystem, payload, runtime, Requests, and Pygrister exception families expected from its operations.
- Added structural validation for organization and table API payloads before indexing, resolving strict typing without suppressions.
- Per controller instruction, no checks were run for this follow-up.

## Quick Setup Follow-up

- Added `GRIST_IN_SERVICE: "true"` to the integration Compose environment so Grist starts in open single-user service mode instead of requiring the Quick setup flow at `/boot`.
- No focused unit expectation required updating; the existing bootstrap tests cover HTTP behavior rather than Compose configuration.
- Per assignment, Docker and tests were not run.

## API Key Response Follow-up

- Updated API-key rotation parsing for Grist 1.7.19's actual response: a bare 40-character hexadecimal key sent as `text/html`, rather than a JSON document.
- Retained compatibility with quoted JSON string responses and reject empty, malformed, or non-key payloads without including their contents in errors.
- Added regression coverage for both bare-text and JSON-string response forms.
- Per controller instruction, no checks were run for this follow-up.

## Organization Selection Follow-up

- Changed bootstrap organization discovery to select the unique organization whose domain or name matches configured `GRIST_TEAM_SITE`, rather than requiring the API's complete organization list to contain one entry.
- Added regression coverage showing the personal `docs-5` site is ignored in favor of the `integration` team site.
- Matching failures remain generic and do not include the organization payload.
- Per controller instruction, no checks were run for this follow-up.
