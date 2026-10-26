"""验证 HTTP MCP：FONT_MANAGER_API_TOKEN=... uv run python scripts/check_mcp.py http://NAS:8765/mcp"""

import asyncio
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"
    token = os.getenv("FONT_MANAGER_API_TOKEN", os.getenv("FONT_MANAGER_TOKEN", ""))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx.AsyncClient(headers=headers, timeout=60) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        status = await session.call_tool("library_status")
        if status.isError:
            raise RuntimeError(str(status.content))
        value = status.structuredContent
        print(f"MCP 连接成功，{len(tools.tools)} 个工具，{value['total']:,} 个字体。")


if __name__ == "__main__":
    asyncio.run(main())
