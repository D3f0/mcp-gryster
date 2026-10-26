#!/usr/bin/env python3
"""Initialize a built mcp-gryster wheel over STDIO and list its tools."""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def smoke(wheel: Path, timeout: float) -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-gryster-smoke-") as workspace:
        environment = dict(os.environ)
        environment["MCP_GRYSTER_WORKSPACE"] = workspace
        transport = StdioTransport(
            command="uvx",
            args=["--from", str(wheel.resolve()), "mcp-gryster"],
            env=environment,
            keep_alive=False,
        )
        async with asyncio.timeout(timeout):
            async with Client(transport, init_timeout=timeout, timeout=timeout) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools}
                if "grist.call" not in names or "widget.package" not in names:
                    raise RuntimeError("installed server did not expose the expected tool surface")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    arguments = parser.parse_args()
    if not arguments.wheel.is_file():
        parser.error(f"wheel does not exist: {arguments.wheel}")
    asyncio.run(smoke(arguments.wheel, arguments.timeout))


if __name__ == "__main__":
    main()
