import asyncio
import hmac
import json
import logging
import shutil
import uuid
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .library import Library
from .mcp_server import create_mcp
from .recognition import (
    IMAGE_FILE_LIMIT,
    IMAGE_REQUEST_LIMIT,
    OCRUnavailable,
    RecognitionBusy,
    RecognitionManager,
)

STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)
UPLOAD_FILE_LIMIT = 100 * 1024 * 1024
UPLOAD_REQUEST_LIMIT = 101 * 1024 * 1024


@asynccontextmanager
async def bounded_upload(
    request: Request,
    lock: asyncio.Lock,
    *,
    request_limit=None,
    file_limit=None,
    allowed_fields=frozenset(),
    item_description="字体文件",
    request_limit_message="上传请求最大 101 MiB，单个字体最大 100 MiB",
    file_limit_message="单个字体最大 100 MiB；更大的文件请通过 SMB 投递",
):
    """Bound multipart parsing before it can spool files into the container's /tmp."""
    if request_limit is None:
        request_limit = UPLOAD_REQUEST_LIMIT
    if file_limit is None:
        file_limit = UPLOAD_FILE_LIMIT
    lengths = request.headers.getlist("content-length")
    if lengths:
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise HTTPException(400, "上传请求的 Content-Length 无效")
        if int(lengths[0]) > request_limit:
            raise HTTPException(413, request_limit_message)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, f"请使用 multipart/form-data 上传{item_description}")
    if lock.locked():
        raise HTTPException(429, "另一个文件正在上传，请稍后重试", headers={"Retry-After": "2"})

    # Lock.acquire() does not yield when unlocked, so another request cannot slip
    # between this check and acquisition. Keep the slot until temporary files close.
    async with lock:
        received = 0

        async def receive():
            nonlocal received
            message = await request.receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > request_limit:
                    raise HTTPException(413, request_limit_message)
            return message

        bounded_request = Request(request.scope, receive)
        async with aclosing(bounded_request.stream()) as stream:
            parser = MultiPartParser(
                request.headers, stream, max_files=1, max_fields=len(allowed_fields)
            )
            try:
                try:
                    form = await parser.parse()
                except MultiPartException as exc:
                    raise HTTPException(
                        400, f"上传格式不正确；每次只能提交一个 file {item_description}及支持的字段"
                    ) from exc
                file = form.get("file")
                if not isinstance(file, UploadFile) or set(form) - {"file", *allowed_fields}:
                    raise HTTPException(400, f"请提交一个名为 file 的{item_description}字段")
                if file.size is not None and file.size > file_limit:
                    raise HTTPException(413, file_limit_message)
                yield form
            finally:
                # The pinned Starlette parser tracks every temporary file here,
                # including unfinished parts absent from FormData. Close directly
                # without a cancellation point before releasing the upload slot.
                for temporary in parser._files_to_close_on_error:
                    temporary.close()


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    font_ids: list[str] = Field(min_length=1, max_length=200)
    favorite: bool | None = None
    tags: list[str] | None = None
    note: str | None = Field(default=None, max_length=4000)
    collection_id: int | None = None
    remove_collection: bool = False


class Collection(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class Refresh(BaseModel):
    source: str = "catalog"


class APIToken(BaseModel):
    token: str = Field(min_length=16, max_length=256)


def create_app(settings=None):
    settings = settings or Settings()
    library = Library(settings)
    recognition = RecognitionManager(library)
    mcp = create_mcp(library)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        if library.status()["total"] == 0:
            library.start_refresh()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            recognition.close()

    app = FastAPI(title="Local Fonts Manager", lifespan=lifespan)
    app.state.library = library
    app.state.recognition = recognition
    upload_lock = asyncio.Lock()
    recognition_upload_lock = asyncio.Lock()
    if settings.host in {"127.0.0.1", "localhost", "::1"}:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]
        )

    @app.middleware("http")
    async def access(request: Request, call_next):
        path = request.url.path
        # The WebUI opens directly; external MCP clients authenticate with the API token.
        # Browser writes must come from this origin, including token changes.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "不允许跨站写入"}, status_code=403)
        if path == "/mcp" or path.startswith("/mcp/"):
            bearer = request.headers.get("authorization", "")
            supplied = bearer[7:] if bearer.startswith("Bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), library.api_token().encode()):
                return JSONResponse(
                    {"detail": "API Token 无效，请到设置页面复制当前 Token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)

    @app.exception_handler(RecognitionBusy)
    async def recognition_busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "2"})

    @app.exception_handler(OCRUnavailable)
    async def ocr_unavailable(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(OSError)
    async def filesystem_error(request, exc):
        logger.warning("Font file unavailable: %s", exc)
        return JSONResponse({"detail": "字体文件不可用，请检查 NAS 连接或刷新索引"}, status_code=503)

    @app.get("/api/auth")
    def auth_info():
        return {"required": False, "mcp_token_required": True}

    @app.get("/api/settings")
    def get_settings():
        return {"api_token": library.api_token(), "root": str(library.root)}

    @app.put("/api/settings/api-token")
    def save_api_token(body: APIToken):
        return library.update_api_token(body.token)

    @app.post("/api/settings/api-token/reset")
    def reset_api_token():
        return library.update_api_token()

    @app.get("/api/status")
    def status():
        return library.status()

    @app.get("/api/recognition/languages")
    def recognition_languages():
        return recognition.languages()

    @app.post("/api/recognition/ocr")
    async def recognition_ocr(request: Request):
        async with bounded_upload(
            request,
            recognition_upload_lock,
            request_limit=IMAGE_REQUEST_LIMIT,
            file_limit=IMAGE_FILE_LIMIT,
            allowed_fields={"language"},
            item_description="识别图片",
            request_limit_message="识别请求最大 12.25 MiB，请先裁剪或压缩图片",
            file_limit_message="识别图片最大 12 MiB，请先裁剪或压缩",
        ) as form:
            data = await form["file"].read(IMAGE_FILE_LIMIT + 1)
            if len(data) > IMAGE_FILE_LIMIT:
                raise HTTPException(413, "识别图片最大 12 MiB，请先裁剪或压缩")
            language = form.get("language", "eng")
            if not isinstance(language, str):
                raise HTTPException(400, "OCR 语言参数无效")
            return await asyncio.to_thread(recognition.ocr, data, language)

    @app.post("/api/recognition/jobs", status_code=202)
    async def create_recognition_job(request: Request):
        async with bounded_upload(
            request,
            recognition_upload_lock,
            request_limit=IMAGE_REQUEST_LIMIT,
            file_limit=IMAGE_FILE_LIMIT,
            allowed_fields={"text"},
            item_description="识别图片",
            request_limit_message="识别请求最大 12.25 MiB，请先裁剪或压缩图片",
            file_limit_message="识别图片最大 12 MiB，请先裁剪或压缩",
        ) as form:
            text = form.get("text")
            if not isinstance(text, str):
                raise HTTPException(400, "请填写需要匹配的文字")
            data = await form["file"].read(IMAGE_FILE_LIMIT + 1)
            if len(data) > IMAGE_FILE_LIMIT:
                raise HTTPException(413, "识别图片最大 12 MiB，请先裁剪或压缩")
            return await asyncio.to_thread(recognition.create, data, text)

    @app.get("/api/recognition/jobs/{job_id}")
    def recognition_job(job_id: str):
        return recognition.get(job_id)

    @app.delete("/api/recognition/jobs/{job_id}")
    def cancel_recognition_job(job_id: str):
        return recognition.cancel(job_id)

    @app.get("/api/fonts")
    def fonts(
        q: str = "",
        vendor: str = "",
        language: str = "",
        format: str = "",
        favorite: bool = False,
        collection: int | None = None,
        tag: str = "",
        limit: int = Query(36, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        return library.search(q, vendor, language, format, favorite, collection, tag, limit, offset)

    @app.get("/api/fonts/{font_id}")
    def details(font_id: str, face: int = Query(0, ge=0), text: str = Query("", max_length=2000)):
        try:
            return library.details(font_id, face, text)
        except (KeyError, ValueError, OSError):
            raise
        except Exception as exc:
            raise HTTPException(422, "无法解析该字体：" + str(exc)[:160]) from exc

    @app.delete("/api/fonts/{font_id}")
    def delete_font(font_id: str):
        return library.delete_font(font_id)

    @app.get("/api/fonts/{font_id}/preview.woff2")
    def preview(font_id: str, text: str = Query(min_length=1, max_length=2000), face: int = Query(0, ge=0)):
        try:
            path = library.preview_font(font_id, text, face)
            missing = path.with_suffix(".json").read_text(encoding="utf-8")
            return FileResponse(
                path,
                media_type="font/woff2",
                headers={"Cache-Control": "private,max-age=86400", "X-Missing-Characters": missing},
            )
        except (KeyError, ValueError, OSError):
            raise
        except Exception as exc:
            raise HTTPException(422, "字体预览不可用：" + str(exc)[:160]) from exc

    @app.get("/api/fonts/{font_id}/preview.png")
    def image(
        font_id: str,
        text: str = Query("山海之间，自有回响。", min_length=1, max_length=300),
        size: int = Query(48, ge=12, le=120),
        face: int = Query(0, ge=0),
    ):
        return Response(library.preview_image(font_id, text, size, face), media_type="image/png")

    @app.get("/api/fonts/{font_id}/download")
    def download(font_id: str):
        path = library.path(font_id)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.post("/api/annotations")
    def annotate(body: Annotation):
        return library.annotate(**body.model_dump())

    @app.get("/api/collections")
    def collections():
        return library.collections()

    @app.post("/api/collections", status_code=201)
    def create_collection(body: Collection):
        return library.create_collection(body.name)

    @app.delete("/api/collections/{collection_id}")
    def delete_collection(collection_id: int):
        return library.delete_collection(collection_id)

    @app.post("/api/refresh", status_code=202)
    def refresh(body: Refresh):
        if body.source not in {"catalog", "scan"}:
            raise ValueError("source 只能是 catalog 或 scan")
        return library.start_refresh(body.source)

    @app.get("/api/inbox")
    def inbox():
        return library.inbox_status()

    @app.post("/api/inbox/upload", status_code=201)
    async def upload(request: Request):
        async with bounded_upload(request, upload_lock) as form:
            return await store_upload(form["file"])

    async def store_upload(file: UploadFile):
        filename = file.filename or ""
        if (
            Path(filename).name != filename
            or "\\" in filename
            or Path(filename).suffix.lower() not in {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2"}
        ):
            raise ValueError("请上传 TTF、OTF、TTC、OTC、WOFF 或 WOFF2 字体文件")
        if not library.root.is_dir():
            raise ValueError("字体共享未连接")
        batch = "web_" + uuid.uuid4().hex
        staging = library.checked("_管理器上传暂存/" + batch)
        target = library.checked("待入库/" + batch)
        staging.mkdir(parents=True)
        size = 0
        try:
            with (staging / filename).open("xb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > UPLOAD_FILE_LIMIT:
                        raise HTTPException(413, "单个字体最大 100 MiB；更大的文件请通过 SMB 投递")
                    await asyncio.to_thread(stream.write, chunk)

            def validate():
                with library.open_font(staging / filename)[0] as font:
                    _ = font["name"]

            try:
                await asyncio.to_thread(validate)
            except Exception as exc:
                raise ValueError("文件不是可识别的字体：" + str(exc)[:120]) from exc
            target.parent.mkdir(exist_ok=True)
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return {
            "queued": filename,
            "bytes": size,
            "batch": batch,
            "message": "已投递到待入库，等待 NAS 自动入库服务处理",
        }

    @app.get("/api/export")
    def export():
        with library.db() as db:
            result = {
                "version": 1,
                "root": str(settings.root),
                "collections": library.collections(),
                "annotations": [
                    dict(r)
                    for r in db.execute(
                        "SELECT a.*,f.path FROM annotations a LEFT JOIN fonts f ON f.id=a.font_id"
                    )
                ],
                "members": [dict(r) for r in db.execute("SELECT * FROM members")],
            }
        return Response(
            json.dumps(result, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="font-organization.json"'},
        )

    @app.get("/")
    @app.get("/settings")
    def home():
        return FileResponse(STATIC / "index.html")

    app.mount("/assets", StaticFiles(directory=STATIC), name="static")
    app.mount("/", mcp_app)
    return app
