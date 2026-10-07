# Task 10 Report

## Status

Implemented deterministic widget packaging and the complete widget-authoring MCP surface.

## Delivered

- Added `WidgetPackager` and `PackageResult` for exact-revision, packageable widget builds.
- Packages contain only materialized `src/` files, with sorted ZIP paths, fixed timestamps, normalized file modes, deterministic compression, canonical manifests, and SHA-256 metadata.
- Manifest URLs preserve `WidgetMetadata.entrypoint`, including nested entrypoints, under the supplied public base URL and project ID.
- Added typed widget project, fixture, validation, preview, and packaging tools.
- Added read-only project, authored-file, validation, distribution-artifact, and Grist method-registry resources with path containment and fixture exclusion.
- Composed fixture management, validation, preview, packaging, tools, and resources in `create_app`; the lifespan continues to own `PreviewManager.stop_all()`.
- Exported packaging and registration APIs from `mcp_gryster.widget`.
- Extended `Artifact` with the SHA-256 produced by `ArtifactStore`; packaging consumes the store-computed digest instead of recomputing it. Added direct artifact digest coverage.
- Added focused packaging, MCP flow/resource/error, app-composition, and artifact digest tests.
- Updated the existing README widget section with the MCP surface and packaging contract.

## Verification

Per assignment, no tests, build, lint, mypy, install, or formatter commands were run.

Observed smoke checks:

- Created a FastMCP app and listed its registered surface: 31 tools and 4 resource templates, including `widget.package`, `widget.projects.apply`, and `widget://projects/{project_id}`.
- Exercised project creation and repeated stale package calls, observing stable masked MCP errors without filesystem details.
- Exercised an actual nested-entrypoint package twice, observing byte-identical ZIP and manifest output, a `nested/main.html`-based public URL, and a ZIP containing only the authored source file.
- Exercised `ArtifactStore.put_bytes` and observed the expected SHA-256 for known bytes.
- `git diff --check` completed cleanly.

## Concerns

- The prescribed focused and project-wide test, Ruff, and mypy commands remain for the controller, as explicitly required by this assignment.
- Distribution resources intentionally expose only single artifact basenames beneath `<workspace>/artifacts/widget`; symlinked roots and files are rejected.

## Controller Fix Round

- Updated the widget MCP test helper to consume typed FastMCP structured content directly, matching the established public MCP response shape instead of expecting an artificial `result` wrapper.
- Per controller instruction, no checks were executed in this fix round.

## Controller Fix Round 2

- Kept the fake preview handshake behaviorally faithful by recording evidence through the real manager's evidence store; corrected the flow fixture to include the required Grist plugin before initialization.
- Changed authored-file resources from binary blobs to UTF-8 `text/plain` content and added a MIME assertion; invalid UTF-8 is rejected with a stable validation error.
- Per controller instruction, no checks were executed in this fix round.

## Controller Fix Round 3

- Updated denied-resource assertions to require FastMCP 3's protocol-visible `McpError` rather than a generic exception, covering both unmatched traversal URIs and handler-level containment failures.
- Disabled the FastMCP server logger only within the expected-denial test so handled security errors do not emit noisy tracebacks.
- Per controller instruction, no checks were executed in this fix round.

## Controller Fix Round 4

- Added a protocol-denial helper that accepts only FastMCP 3's two safe outcomes: `McpError` or an empty content list; any returned text/blob content fails the test.
- Added direct containment assertions proving every decoded malicious authored path raises `PATH_NOT_ALLOWED` in the production reader.
- Scoped expected FastMCP logger suppression to `monkeypatch.context()` blocks around denied reads only.
- Per controller instruction, no checks were executed in this fix round.

## Controller Fix Round 5

- Accounted for FastMCP/AnyUrl normalization of an encoded dot segment before dispatch: the protocol assertion now proves it resolves only to the safe canonical `widget.json` resource and exposes no fixture content.
- Retained the direct production-reader assertion that the original decoded malicious authored path is denied with `PATH_NOT_ALLOWED`.
- Per controller instruction, no checks were executed in this fix round.

## Style Cleanup

- Corrected all six listed Ruff findings: packaging import order and invalid-metadata exception typing, test import grouping, local artifact imports, and widget-tool third-party/project import separation.
- Per assignment, no tests, lint, mypy, build, or formatter commands were run in this cleanup round.

## Controller Fix Round 6

- Constructed `GristFixtureRequest` through `model_validate` so strict typing accepts field-name input while Pydantic continues to handle aliases.
- Annotated public-base URL parsing with the exact `SplitResult` return type.
- Controller reported the focused tests and Ruff passing before this fix; per instruction, no checks were executed in this fix round.

## Packaging Security Fix Round

- Made manifest artifact names content-addressed so different public base URLs cannot overwrite one another for the same project revision.
- Held the cross-process project revision lock through both package artifact commits, preventing a revision update from racing between the ZIP and manifest writes.
- Replaced filesystem-bearing package results with frozen, path-free artifact references and removed duplicate top-level digest fields.
- Added descriptor-anchored `ArtifactStore.read_bytes`; widget distribution resources now use that reader instead of checking and reopening paths.
- Kept the no-`dir_fd` compatibility path private to `ArtifactStore`, with explicit containment and symlink-component rejection tests.
- Added focused coverage for content-keyed manifests, lock scope, path-free results, descriptor-safe reads, portable fallback containment, and resource delegation.
- Per assignment, no checks were executed in this fix round.
