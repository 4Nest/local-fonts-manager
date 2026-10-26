import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette import formparsers

from font_manager import web
from font_manager.config import Settings
from font_manager.library import Library
from font_manager.web import create_app


def mcp_initialize(client, token):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "token-test", "version": "1"},
            },
        },
    )


def test_web_opens_directly_and_mcp_requires_token(library):
    with TestClient(create_app(library.settings)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/settings").status_code == 200
        assert client.get("/api/status").json()["total"] == 3
        assert client.get("/api/auth").json() == {"required": False, "mcp_token_required": True}
        assert client.post("/mcp", json={}).status_code == 401
        assert client.get("/mcp/").status_code == 401
        assert mcp_initialize(client, "wrong").status_code == 401
        current = client.get("/api/settings")
        assert current.headers["Cache-Control"] == "no-store"
        assert current.json()["root"] == str(library.root)
        assert mcp_initialize(client, current.json()["api_token"]).status_code == 200
        assert (
            client.post(
                "/api/collections", json={"name": "bad"}, headers={"Origin": "http://attacker.invalid"}
            ).status_code
            == 403
        )
        assert client.get("/api/status", headers={"Host": "attacker.invalid"}).status_code == 400


def test_token_save_reset_revokes_old_tokens_and_persists(library):
    with TestClient(create_app(library.settings)) as client:
        old = client.get("/api/settings").json()["api_token"]
        custom = "my-hermes-custom-token"
        saved = client.put("/api/settings/api-token", json={"token": custom})
        assert saved.status_code == 200 and saved.json()["api_token"] == custom
        assert mcp_initialize(client, old).status_code == 401
        assert mcp_initialize(client, custom).status_code == 200
        reset = client.post("/api/settings/api-token/reset")
        assert reset.status_code == 200
        current = reset.json()["api_token"]
        assert len(current) >= 32 and current not in {old, custom}
        assert mcp_initialize(client, custom).status_code == 401
        assert mcp_initialize(client, current).status_code == 200

    # A stale environment seed must not overwrite the saved token after restart.
    settings = Settings(root=library.root, state=library.settings.state, token="old-environment-seed")
    assert Library(settings).api_token() == current
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/settings").json()["api_token"] == current
        assert mcp_initialize(client, current).status_code == 200


def test_invalid_and_cross_origin_token_changes_keep_current_value(library):
    current = library.api_token()
    with TestClient(create_app(library.settings)) as client:
        for token in ["short", "x" * 257, "x" * 16 + " 中", "x" * 16 + " y", "x" * 16 + "\x00"]:
            assert client.put("/api/settings/api-token", json={"token": token}).status_code in {400, 422}
            assert library.api_token() == current
        headers = {"Origin": "http://attacker.invalid"}
        assert (
            client.put(
                "/api/settings/api-token", json={"token": "attacker-custom-token"}, headers=headers
            ).status_code
            == 403
        )
        assert client.post("/api/settings/api-token/reset", headers=headers).status_code == 403
        assert library.api_token() == current


def test_delete_font_api_enforces_origin_and_removes_file(library):
    font = library.search("Test Sans")["items"][0]
    path = library.path(font["id"])
    with TestClient(create_app(library.settings)) as client:
        forbidden = client.delete(
            f"/api/fonts/{font['id']}", headers={"Origin": "http://attacker.invalid"}
        )
        assert forbidden.status_code == 403 and path.exists()
        response = client.delete(f"/api/fonts/{font['id']}")
        assert response.status_code == 200
        assert response.json()["deleted"] == font["id"]
        assert not path.exists()
        assert client.get(f"/api/fonts/{font['id']}").status_code == 404
        assert client.delete(f"/api/fonts/{font['id']}").status_code == 404


def test_upload_stages_and_validates_without_overwriting(library):
    font = library.search("Test Sans")["items"][0]
    data = library.path(font["id"]).read_bytes()
    with TestClient(create_app(library.settings)) as client:
        assert client.post("/api/inbox/upload", files={"file": ("../escape.ttf", data)}).status_code == 400
        assert client.post("/api/inbox/upload", files={"file": ("bad.ttf", b"broken")}).status_code == 400
        assert list((library.root / "待入库").iterdir()) == []
        first = client.post("/api/inbox/upload", files={"file": ("same.ttf", data)})
        second = client.post("/api/inbox/upload", files={"file": ("same.ttf", data)})
        assert first.status_code == second.status_code == 201
        assert first.json()["batch"] != second.json()["batch"]
        assert len(list((library.root / "待入库").glob("*/same.ttf"))) == 2
        assert not list((library.root / "_管理器上传暂存").iterdir())
        assert library.path(font["id"]).read_bytes() == data


def track_upload_spools(monkeypatch):
    opened = []
    original = formparsers.SpooledTemporaryFile

    def create(*args, **kwargs):
        kwargs["max_size"] = 32
        file = original(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", create)
    return opened


def multipart_prefix():
    return (
        b"--font-boundary\r\n"
        b'Content-Disposition: form-data; name="file"; filename="font.ttf"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )


def test_upload_rejects_declared_size_before_receiving(library, monkeypatch):
    opened = track_upload_spools(monkeypatch)

    async def exercise():
        async def body():
            raise AssertionError("Oversized request must be rejected before reading its body")
            yield b""  # pragma: no cover

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(library.settings)), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/api/inbox/upload",
                headers={
                    "Content-Type": "multipart/form-data; boundary=font-boundary",
                    "Content-Length": str(web.UPLOAD_REQUEST_LIMIT + 1),
                },
                content=body(),
            )
            assert response.status_code == 413
            assert "101 MiB" in response.json()["detail"]

    asyncio.run(exercise())
    assert opened == []


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_upload_bounds_stream_without_trusting_length(library, monkeypatch, declared_length):
    monkeypatch.setattr(web, "UPLOAD_REQUEST_LIMIT", 1024)
    opened = track_upload_spools(monkeypatch)

    async def exercise():
        async def body():
            yield multipart_prefix()
            yield b"a" * 128
            yield b"b" * 1024
            raise AssertionError("Body reading must stop at the limit")

        headers = {"Content-Type": "multipart/form-data; boundary=font-boundary"}
        if declared_length is not None:
            headers["Content-Length"] = declared_length
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(library.settings)), base_url="http://testserver"
        ) as client:
            response = await client.post("/api/inbox/upload", headers=headers, content=body())
            assert response.status_code == 413

    asyncio.run(exercise())
    assert len(opened) == 1 and opened[0].closed
    assert not list((library.root / "待入库").iterdir())


@pytest.mark.parametrize(
    "parts",
    [
        [("file", ("a.ttf", b"a" * 128)), ("file", ("b.ttf", b"b" * 128))],
        [("file", ("a.ttf", b"a" * 128)), ("note", (None, "extra field"))],
        [("wrong", ("a.ttf", b"a" * 128))],
    ],
)
def test_upload_rejects_extra_parts_and_closes_files(library, monkeypatch, parts):
    opened = track_upload_spools(monkeypatch)
    with TestClient(create_app(library.settings)) as client:
        response = client.post("/api/inbox/upload", files=parts)
        assert response.status_code == 400
        assert "file" in response.json()["detail"]
    assert len(opened) == 1 and opened[0].closed
    assert not list((library.root / "待入库").iterdir())


def test_upload_keeps_file_limit_and_accepts_exact_boundary(library, monkeypatch):
    data = library.path(library.search("Test Sans")["items"][0]["id"]).read_bytes()
    monkeypatch.setattr(web, "UPLOAD_FILE_LIMIT", len(data))
    opened = track_upload_spools(monkeypatch)
    with TestClient(create_app(library.settings)) as client:
        assert client.post("/api/inbox/upload", files={"file": ("exact.ttf", data)}).status_code == 201
        response = client.post("/api/inbox/upload", files={"file": ("large.ttf", data + b"x")})
        assert response.status_code == 413
        assert "100 MiB" in response.json()["detail"]
    assert len(opened) == 2 and all(file.closed for file in opened)
    assert len(list((library.root / "待入库").glob("*/exact.ttf"))) == 1
    assert not list((library.root / "待入库").glob("*/large.ttf"))


def test_upload_closes_truncated_multipart_file(library, monkeypatch):
    opened = track_upload_spools(monkeypatch)
    with TestClient(create_app(library.settings)) as client:
        response = client.post(
            "/api/inbox/upload",
            headers={"Content-Type": "multipart/form-data; boundary=font-boundary"},
            content=multipart_prefix() + b"unfinished-font" * 100,
        )
        assert response.status_code == 400
    assert len(opened) == 1 and opened[0].closed
    assert not list((library.root / "待入库").iterdir())


def test_upload_rejects_concurrent_body_and_releases_on_cancellation(library, monkeypatch):
    opened = track_upload_spools(monkeypatch)
    data = library.path(library.search("Test Sans")["items"][0]["id"]).read_bytes()

    async def exercise():
        receiving = asyncio.Event()
        stalled = asyncio.Event()

        async def first_body():
            yield multipart_prefix()
            yield b"a" * 128
            receiving.set()
            await stalled.wait()

        async def second_body():
            raise AssertionError("A concurrent upload must not read or spool its body")
            yield b""  # pragma: no cover

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(library.settings)), base_url="http://testserver"
        ) as client:
            headers = {"Content-Type": "multipart/form-data; boundary=font-boundary"}
            first = asyncio.create_task(
                client.post("/api/inbox/upload", headers=headers, content=first_body())
            )
            try:
                await asyncio.wait_for(receiving.wait(), timeout=5)
                response = await client.post("/api/inbox/upload", headers=headers, content=second_body())
                assert response.status_code == 429
                assert response.headers["Retry-After"] == "2"
                assert len(opened) == 1
            finally:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            assert opened[0].closed
            response = await client.post("/api/inbox/upload", files={"file": ("after-cancel.ttf", data)})
            assert response.status_code == 201

    asyncio.run(exercise())
    assert len(opened) == 2 and all(file.closed for file in opened)


def test_http_preview_headers_and_export(library):
    font = library.search("Test Sans")["items"][0]
    with TestClient(create_app(library.settings)) as client:
        response = client.get(f"/api/fonts/{font['id']}/preview.woff2", params={"text": "A山"})
        assert response.status_code == 200
        assert response.content[:4] == b"wOF2"
        assert response.headers["x-missing-characters"] == '["\\u5c71"]'
        assert client.get(f"/api/fonts/{font['id']}", params={"text": "山"}).json()["missing_characters"] == [
            "山"
        ]
        assert (
            client.post("/api/annotations", json={"font_ids": [font["id"]], "tags": ["字幕"]}).status_code
            == 200
        )
        assert client.get("/api/fonts", params={"tag": "字幕"}).json()["total"] == 1
        backup = client.get("/api/export").json()
        assert backup["annotations"][0]["path"] == font["path"]
        assert client.get("/api/fonts", params={"limit": 201}).status_code == 422
        assert client.post("/api/refresh", json={"source": "invalid"}).status_code == 400
