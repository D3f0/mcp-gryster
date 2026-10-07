# mcp-gryster

`mcp-gryster` is a FastMCP 3 server based on Pygrister. It exposes policy-controlled Grist operations from `pygrister.api.GristApi` to MCP clients.

## Grist tools

The server presents common Grist operations as typed MCP tools and provides a controlled `grist.call` route for other supported public API methods. Both routes use the same method registry and per-domain policy checks.

`mcp-gryster` calls `GristApi` directly and does not execute Gry.

## Quick start

Configure an MCP client to launch the default STDIO transport with `uvx`:

```json
{
  "mcpServers": {
    "grist": {
      "command": "uvx",
      "args": ["mcp-gryster"],
      "env": {
        "GRIST_API_KEY": "...",
        "GRIST_TEAM_SITE": "docs"
      }
    }
  }
}
```

Run `uvx mcp-gryster --help` to see transport and workspace options.

## Security defaults

Grist operations are checked against explicit per-domain `none`, `read`, or `write` permissions. Sensitive identity, service-account, and administration domains are disabled by default. File access is confined to the configured workspace. Streamable HTTP currently binds only to loopback addresses; configured bearer tokens do not permit non-loopback exposure until FastMCP authentication is enforced end to end.

## Extra: custom-widget authoring

As an additional capability, MCP clients can author plain HTML, CSS, and JavaScript custom widgets in managed workspace projects. The server supports controlled project mutation, Grist-backed preview fixtures, validation, local preview, and deployable packaging; it does not invoke an LLM itself.

Fixture previews run on ephemeral IPv4 loopback ports behind per-session capability URLs. They expose only the selected immutable project revision, use a sanitized in-memory Grist bridge, and record successful preview evidence without retaining fixture or source bodies. Live previews serve authored static files unchanged.

The MCP surface exposes task-oriented `widget.projects.*`, `widget.fixtures.*`,
`widget.validate`, `widget.preview.*`, and `widget.package` tools. Read-only
`widget://` resources expose project metadata and authored files, validation
reports, generated distribution artifacts, and the checked-in Grist method
registry. Packages contain only `src/` files and produce a canonical manifest
whose public URL uses the project's configured entrypoint.
