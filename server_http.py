"""Open Brain remote MCP — minimal HTTP surface (capture + search) for claude.ai."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as brain

from mcp.server.fastmcp import FastMCP

SECRET = os.environ["OPEN_BRAIN_REMOTE_SECRET"]

mcp = FastMCP("open-brain", host="127.0.0.1", port=8090, stateless_http=True)
mcp.settings.streamable_http_path = f"/{SECRET}/mcp"

mcp.tool()(brain.capture_thought)
mcp.tool()(brain.search_thoughts)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
