"""Command-line entry point for mcp-gryster."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Build the mcp-gryster command-line parser."""
    parser = argparse.ArgumentParser(
        prog="mcp-gryster",
        description="Expose policy-controlled Pygrister operations through FastMCP.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="MCP transport to use (default: stdio)",
    )
    parser.add_argument("--host", help="HTTP bind host")
    parser.add_argument("--port", type=int, help="HTTP bind port")
    parser.add_argument("--workspace", help="workspace root for generated artifacts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Load configuration and run the MCP server until normal shutdown."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        from mcp_gryster.app import create_app
        from mcp_gryster.config import Settings
    except ModuleNotFoundError as exc:
        if exc.name in {"mcp_gryster.app", "mcp_gryster.config"}:
            parser.error(
                "server components are not available yet; install a complete mcp-gryster release"
            )
        raise

    environment = dict(os.environ)
    environment["MCP_GRYSTER_TRANSPORT"] = args.transport
    if args.host is not None:
        environment["MCP_GRYSTER_HOST"] = args.host
    if args.port is not None:
        environment["MCP_GRYSTER_PORT"] = str(args.port)
    if args.workspace is not None:
        environment["MCP_GRYSTER_WORKSPACE"] = args.workspace

    settings = Settings.load(environment)
    app = create_app(settings)
    if args.transport == "stdio":
        app.run(transport="stdio")
    else:
        app.run(transport="http", host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
