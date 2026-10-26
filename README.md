# mcp-gryster

`mcp-gryster` is a FastMCP 3 server based on Pygrister. It exposes policy-controlled Grist operations from `pygrister.api.GristApi` to MCP clients.

## Grist tools

The server targets **Pygrister 0.10.0** and calls `GristApi` directly; it does not execute Gry. Common operations are first-class, typed MCP tools such as `grist.records.list`. The controlled `grist.call` tool covers other public methods marked supported in the bundled registry. Both routes use that registry and the same per-domain policy checks.

For Grist SaaS, set `GRIST_API_KEY` and `GRIST_TEAM_SITE`; optional defaults include `GRIST_WORKSPACE_ID` and `GRIST_DOC_ID`. For self-managed Grist, also configure `GRIST_SELF_MANAGED=true` and the relevant `GRIST_SELF_MANAGED_HOME`, `GRIST_SELF_MANAGED_SINGLE_ORG`, `GRIST_SERVER_PROTOCOL`, `GRIST_API_SERVER`, or `GRIST_API_ROOT` values. These Pygrister settings may instead be supplied as a JSON object through `MCP_GRYSTER_GRIST_CONFIG`.

## Quick start

Python 3.12 or newer is required. [Download the wheel from GitHub Pages](https://d3f0.github.io/mcp-gryster/). The Pages workflow rebuilds it on pushes to the default branch (`feat/mcp-gryster`), after verifying clean installation and STDIO tool discovery; it can also be run manually.

Run the published wheel directly:

```bash
uvx --from https://d3f0.github.io/mcp-gryster/mcp_gryster-0.1.0-py3-none-any.whl mcp-gryster --help
```

Configure an MCP client to launch the default STDIO transport with `uvx`:

```json
{
  "mcpServers": {
    "grist": {
      "command": "uvx",
      "args": [
        "--from",
        "https://d3f0.github.io/mcp-gryster/mcp_gryster-0.1.0-py3-none-any.whl",
        "mcp-gryster"
      ],
      "env": {
        "GRIST_API_KEY": "...",
        "GRIST_TEAM_SITE": "docs"
      }
    }
  }
}
```

The `--help` output lists transport and workspace options. The versioned wheel URL changes when the package version changes; the Pages download page links the current build.

Streamable HTTP is available with `--transport streamable-http --host 127.0.0.1 --port 8000`. It is restricted to loopback (`127.0.0.1`, `::1`, or `localhost`): configured bearer-token settings do not make public binding safe because authentication is not yet enforced end to end.

## Security defaults

`MCP_GRYSTER_POLICY` is a JSON object whose values are `none`, `read`, or `write`. Every domain is explicit; omitted domains retain these defaults:

| Domain | Default |
| --- | --- |
| `organizations` | `read` |
| `workspaces` | `read` |
| `documents` | `read` |
| `schema` | `read` |
| `records` | `read` |
| `access` | `none` |
| `attachments` | `none` |
| `webhooks` | `none` |
| `sql` | `none` |
| `identity` | `none` |
| `service_accounts` | `none` |
| `administration` | `none` |

Example: `MCP_GRYSTER_POLICY='{"records":"write","schema":"read"}'`. Policy is an access ceiling, not a substitute for Grist authorization. Workspace file access is confined to `MCP_GRYSTER_WORKSPACE` (default `.mcp-gryster`). Credentials must remain in environment configuration and must not be placed in authored files or fixtures.

The bundled method registry is generated for Pygrister 0.10.0. Unsupported or unregistered methods are unavailable; upgrades require reviewing and regenerating the registry. The Docker integration is tested against Grist 1.7.19, not a promise of compatibility with every Grist release.

## Extra: custom-widget authoring

As an additional workflow—not the product's primary identity—MCP clients can create a project, edit plain HTML/CSS/JavaScript under `src/`, capture a bounded preview fixture, validate and preview an immutable revision, and package it. The server does not invoke an LLM itself.

Fixtures capture only explicitly requested columns and rows. Column IDs remain unchanged so widget mappings continue to work; labels and values matching redaction rules are replaced. If an identifier is itself sensitive, omit that column with the request allowlist. Attachment metadata requires explicit opt-in. Limits control row count and payload size. Private `fixtures/` data and credentials are excluded from generated widget packages, which contain authored `src/` files only. Fixture previews use ephemeral IPv4 loopback ports and capability URLs; live previews serve authored files unchanged.

The MCP surface includes `widget.projects.apply_files`, `widget.fixtures.create_from_grist`, `widget.validate`, `widget.preview.*`, and `widget.package`. Read-only resources expose projects and their authored files, validation reports, project-scoped generated artifacts under `widget://projects/{project_id}/dist/{name}`, and the checked-in registry at `grist://method-registry`.

## Integration tests

The integration suite starts the digest-pinned Grist 1.7.19 image in an isolated Docker Compose project, provisions disposable test data, and removes containers and volumes afterward:

```bash
uv run pytest -m integration -v
```

Docker must be available. Local runs skip when it is unavailable; with `CI=true`, unavailability fails the suite. Never point this destructive test workflow at production data.
