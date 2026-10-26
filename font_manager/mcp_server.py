from typing import Annotated

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from .library import Library

READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}


def create_mcp(library: Library):
    mcp = FastMCP(
        "Local Fonts Manager",
        instructions="NAS 字体库。先 search_fonts 获取 font_id，再查询详情、检测缺字、生成样张或整理选集。"
        "标签、收藏、选集和备注只修改管理器数据，不移动或删除字体。样张仅使用字体实际字形，缺字请用 check_characters 检查。",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        # The parent ASGI application enforces authentication and same-origin writes.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(annotations=READ)
    def library_status() -> dict[str, object]:
        """查看字体总数、厂商/语言/格式分类、标签、收藏和选集。"""
        return library.status()

    @mcp.tool(annotations=READ)
    def search_fonts(
        query: str = "",
        vendor: str = "",
        language: str = "",
        format: str = "",
        favorite: bool = False,
        collection: int | None = None,
        tag: str = "",
        limit: Annotated[int, Field(ge=1, le=100)] = 30,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, object]:
        """按名称、文件名或别名搜索；支持厂商、语言、格式、收藏、选集与标签过滤和分页。"""
        return library.search(query, vendor, language, format, favorite, collection, tag, limit, offset)

    @mcp.tool(annotations=READ)
    def font_details(font_id: str, face: int = 0) -> dict[str, object]:
        """读取字体内部名称、字重、版本、版权、字符数、可变轴及 TTC 字面数量。"""
        return library.details(font_id, face)

    @mcp.tool(annotations=READ)
    def check_characters(
        font_id: str, text: Annotated[str, Field(max_length=2000)], face: int = 0
    ) -> dict[str, object]:
        """检查文本是否缺字；依据字体 Unicode cmap，不使用系统字体补字。"""
        result = library.details(font_id, face, text)
        return {
            "font_id": font_id,
            "family": result["family"],
            "face": face,
            "missing_characters": result["missing_characters"],
            "supported": not result["missing_characters"],
            "character_count": result["character_count"],
        }

    @mcp.tool(annotations=READ)
    def preview_font(
        font_id: str,
        text: Annotated[str, Field(min_length=1, max_length=300)] = "山海之间，自有回响。",
        size: Annotated[int, Field(ge=12, le=120)] = 48,
        face: int = 0,
    ) -> Image:
        """生成真实字体 PNG 样张，直接返回 MCP 图片，供 Hermes 视觉预览。"""
        return Image(data=library.preview_image(font_id, text, size, face), format="png")

    @mcp.tool(annotations=WRITE)
    def organize_fonts(
        font_ids: list[str],
        favorite: bool | None = None,
        tags: list[str] | None = None,
        note: str | None = None,
        collection_id: int | None = None,
        remove_collection: bool = False,
    ) -> dict[str, object]:
        """批量收藏、替换标签/备注、加入或移出选集。省略字段保持原值；tags=[] 清空标签。最多 200 个。"""
        return library.annotate(font_ids, favorite, tags, note, collection_id, remove_collection)

    @mcp.tool(annotations=READ)
    def list_collections() -> list[dict]:
        """列出全部选集及其当前在库字体数量。"""
        return library.collections()

    @mcp.tool(annotations=WRITE)
    def create_collection(name: str) -> dict[str, object]:
        """创建一个字体选集，例如“字幕正文”或“标题候选”。"""
        return library.create_collection(name)

    @mcp.tool(annotations=WRITE)
    def remove_collection(collection_id: int) -> dict[str, object]:
        """删除选集和其成员关联；保留字体文件、标签、备注与收藏。"""
        return library.delete_collection(collection_id)

    @mcp.tool(annotations=READ)
    def inbox_status() -> dict[str, object]:
        """查看现有 NAS 自动入库服务的状态、最后更新时间及待入库项目。"""
        return library.inbox_status()

    @mcp.tool(annotations=WRITE)
    def refresh_index(source: str = "catalog") -> dict[str, object]:
        """后台刷新索引；catalog 使用现有目录（快），scan 完整扫描（慢）。用 library_status 查询进度。"""
        if source not in {"catalog", "scan"}:
            raise ValueError("source 只能是 catalog 或 scan")
        return library.start_refresh(source)

    return mcp
