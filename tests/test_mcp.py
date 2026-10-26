import asyncio
import os
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from font_manager.config import Settings
from font_manager.web import create_app


def test_stdio_mcp_end_to_end(library):
    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "font_manager", "mcp"],
            env={
                **os.environ,
                "FONT_MANAGER_ROOT": str(library.root),
                "FONT_MANAGER_STATE": str(library.settings.state),
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 11
            results = await session.call_tool("search_fonts", {"query": "Test Sans"})
            assert not results.isError
            font = results.structuredContent["items"][0]
            coverage = await session.call_tool("check_characters", {"font_id": font["id"], "text": "A山"})
            assert coverage.structuredContent["missing_characters"] == ["山"]
            picture = await session.call_tool("preview_font", {"font_id": font["id"], "text": "ABC"})
            assert not picture.isError
            assert picture.content[0].type == "image"
            collection = await session.call_tool("create_collection", {"name": "Hermes选集"})
            organized = await session.call_tool(
                "organize_fonts",
                {
                    "font_ids": [font["id"]],
                    "collection_id": collection.structuredContent["id"],
                    "favorite": True,
                },
            )
            assert not organized.isError

    asyncio.run(exercise())
    assert library.search(favorite=True)["total"] == 1
    assert library.collections()[0]["name"] == "Hermes选集"


def test_authenticated_streamable_http_mcp(library):
    async def exercise():
        settings = Settings(root=library.root, state=library.settings.state, token="remote-test-token")
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": f"Bearer {library.api_token()}"},
            ) as client,
            streamable_http_client("http://testserver/mcp", http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("search_fonts", {"language": "西文"})
            assert not result.isError and result.structuredContent["total"] == 1
            font = result.structuredContent["items"][0]
            result = await session.call_tool(
                "organize_fonts", {"font_ids": [font["id"]], "tags": ["远程Hermes"]}
            )
            assert not result.isError
            picture = await session.call_tool("preview_font", {"font_id": font["id"], "text": "ABC"})
            assert picture.content[0].type == "image"

    asyncio.run(exercise())
    assert library.search(tag="远程Hermes")["total"] == 1
