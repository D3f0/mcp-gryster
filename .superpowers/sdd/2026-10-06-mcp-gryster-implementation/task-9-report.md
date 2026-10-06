# Task 9 Report

## Status

Implemented the capability-scoped loopback preview server and in-browser Grist fixture emulator.

## Delivered

- Added `PreviewManager.start`, `stop`, and `stop_all` with isolated Uvicorn servers bound to ephemeral IPv4 loopback ports.
- Added high-entropy capability URLs, exact Host/Origin enforcement, no directory listing, strict static MIME allowlisting, traversal and cross-project denial, no-store/nosniff responses, and same-origin framing.
- Fixture preview uses a static harness and injects the packaged bridge only into an in-memory copy of authored HTML; live preview serves authored static files unchanged and exposes no emulator routes.
- Added a fake `grist` bridge with callback registration, deterministic options/records/selected-record dispatch, column mapping, access-controlled in-memory table reads/writes, and named unsupported-operation errors.
- Added serialized, schema-constrained, value-free statuses and evidence recording only after a valid successful handshake. Unsupported calls and uncaught failures permanently disqualify a session from evidence.
- Integrated `PreviewManager` and the Task 8 `PreviewEvidenceStore` into the FastMCP lifespan, with `stop_all()` guaranteed on shutdown.
- Exported preview APIs from `mcp_gryster.widget`.
- Added explicit pinned Starlette/Uvicorn runtime dependencies and regenerated `uv.lock` without installing packages.
- Added explicit Hatch wheel configuration and force-included the harness and bridge static resources.
- Added server, browser, bridge, evidence, live-mode, and lifespan tests.
- Documented loopback preview security behavior in the existing README.

## Verification

Executed successfully:

- `.venv/bin/python .preview-smoke.py` — started an actual live preview, fetched the authored entrypoint over loopback with `nosniff`, stopped it, and observed `200 … nosniff True`; the throwaway script/workspace were removed.
- `python3 -m py_compile src/mcp_gryster/widget/preview.py src/mcp_gryster/app.py tests/unit/test_preview.py tests/unit/test_app.py`
- `node --check src/mcp_gryster/widget/static/grist-bridge.js`
- `uv lock` — resolved 100 packages and refreshed direct dependency metadata without package installation.
- `uv lock --check` — resolved successfully.
- `git diff --check` — clean.

## Unexecuted Verification

Per assignment, the controller owns these commands and browser installation; they were not run:

- `uv run playwright install chromium`
- `uv run pytest tests/unit/test_preview.py -v`
- `uv run pytest -m 'not integration'`
- `uv run ruff check src/mcp_gryster/widget tests/unit/test_preview.py`
- `uv run mypy src/mcp_gryster/widget`
- build/formatter commands

## Concerns

- Chromium browser behavior remains unexecuted until the controller installs/runs Playwright. The tests deliberately accept the browser-reported runtime column while requiring the sanitized location to identify `app.js` line 1.
- Preview status and evidence are process-local by design, matching the Task 8 injected evidence store.

## Controller Fix Round

- Normalized urllib response header names in the test helper while retaining explicit `nosniff`, frame policy, and MIME assertions.
- Replaced CSP-blocked Playwright string evaluation with frame locator assertions; the CSP remains strict and does not permit `unsafe-eval`.
- Browser tests now stop preview servers in `finally` blocks.
- Preview shutdown now requests graceful Uvicorn exit, awaits it with a bounded timeout, escalates to `force_exit`, and cancels only as a final fallback.
- Per controller instruction, no checks were executed in this fix round.

## Controller Ruff Fix

- Sorted `StopPreviewResult` before `StoredPreviewFixture` in `widget.__all__` to satisfy RUF022.
- Controller reported 5/5 focused preview tests and 187/187 unit tests passing with pristine output before this exact mechanical reorder.
- Per controller instruction, no checks were executed for this mechanical fix.

## Review Fix Round 1

- `onRecord` now receives cloned raw `{id, fields}` records; `mapColumnNames` is the explicit mapping operation.
- Added exact evidence revocation, including a packageability regression test and late unsupported-status browser coverage.
- Isolated authored widgets on `localhost` from the `127.0.0.1` reporter origin, with strict Host/Origin allowlists, constrained CORS, per-session status nonces, and captured native fetch.
- Added authored-code forgery coverage proving a direct fake handshake cannot create preview evidence.
- Normalized arbitrary browser `Error.name` values to a fixed allowlist before reporting.
- CSP remains strict without `unsafe-eval`; cross-origin bridge, connect, and frame permissions are session-origin-specific.
- Per controller instruction, no checks were executed in this review fix round.

## Review Fix Round 2

- Made the credential-bearing bridge bootstrap one-shot per preview session; the first parser-blocking request atomically consumes the grant and all later bridge requests return HTTP 410 with no script.
- Added a browser regression that replaces `fetch`, appends the same bridge URL, verifies the duplicate is rejected and the nonce is never observed, then proves forged status does not prevent the genuine bridge handshake.
- Reloading a fixture preview requires starting a new preview session so it receives a fresh one-shot bootstrap grant.
- Per controller instruction, no checks were executed in this review fix round.

## Review Fix Round 3

- Removed regex-based head injection. The bridge script is now byte-prefixed immediately after a leading doctype (or before all authored bytes without one), guaranteeing parser-blocking bridge execution before any authored markup or script.
- Extended the one-shot browser regression with an authored inline script before the explicit head that wraps `fetch`; the test proves authored code executes later, cannot observe the status nonce, cannot reload the bridge, and cannot forge evidence.
- Per controller instruction, no checks were executed in this review fix round.
