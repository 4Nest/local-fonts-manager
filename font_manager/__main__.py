import argparse
import os


def main():
    parser = argparse.ArgumentParser(description="NAS 字体管理器")
    parser.add_argument("mode", nargs="?", choices=["web", "mcp", "index"], default="web")
    parser.add_argument("--root", help="字体根目录，包含完整包、待入库等目录")
    parser.add_argument("--state", help="本地状态目录，请勿放在 SMB 上")
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="WebUI / MCP HTTP 端口，默认 8765")
    args = parser.parse_args()
    for key in ("root", "state", "host", "port"):
        if (value := getattr(args, key)) is not None:
            os.environ[f"FONT_MANAGER_{key.upper()}"] = str(value)
    from .config import Settings
    from .library import Library

    settings = Settings()
    if args.mode == "index":
        import json

        print(json.dumps(Library(settings).refresh(), ensure_ascii=False, indent=2))
    elif args.mode == "mcp":
        from .mcp_server import create_mcp

        library = Library(settings)
        if library.status()["total"] == 0:
            library.refresh()
        create_mcp(library).run(transport="stdio")
    else:
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
