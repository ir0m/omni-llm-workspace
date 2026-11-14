"""A small MCP server over stdio for the tests (the official SDK's server)."""
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("demo")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def add(a: int, b: int) -> int:
    """Add two numbers"""
    return a + b


@server.tool()
def secret_hint() -> str:
    """Says whether the token from the settings screen arrived (never its value)"""
    return "token:" + ("set" if os.environ.get("DEMO_TOKEN") == "s3cret" else "missing")


if __name__ == "__main__":
    server.run()
