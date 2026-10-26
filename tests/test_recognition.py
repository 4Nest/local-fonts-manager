import io
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from PIL import Image, ImageOps

from font_manager import recognition
from font_manager.config import Settings
from font_manager.library import Library
from font_manager.recognition import (
    JOB_TTL,
    OCRUnavailable,
    RecognitionBusy,
    RecognitionManager,
    decode_image,
    ocr_image,
)
from font_manager.web import create_app


def make_shape_font(path, family, variant):
    characters = " AB"
    glyphs = [".notdef"] + [f"uni{ord(c):04X}" for c in characters]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({ord(c): f"uni{ord(c):04X}" for c in characters})
    outlines = {}
    for index, glyph in enumerate(glyphs):
        pen = TTGlyphPen(None)
        character = characters[index - 1] if index else " "
        if character != " ":
            if (variant + (character == "B")) % 2:
                pen.moveTo((60, 0))
                pen.lineTo((60, 700))
                pen.lineTo((520, 700))
                pen.lineTo((520, 0))
            else:
                pen.moveTo((50, 0))
                pen.lineTo((290, 700))
                pen.lineTo((540, 0))
            pen.closePath()
        outlines[glyph] = pen.glyph()
    builder.setupGlyf(outlines)
    width = 600 + variant * 100
    builder.setupHorizontalMetrics({glyph: (width, 0) for glyph in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {
            "familyName": family,
            "styleName": "Regular",
            "fullName": family + " Regular",
            "psName": family.replace(" ", "") + "-Regular",
            "version": "Version 1.0",
        }
    )
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    path.parent.mkdir(parents=True, exist_ok=True)
    builder.save(path)


def shape_library(tmp_path):
    root = tmp_path / "shapes"
    first = root / "完整包/测试/西文/Shape Zero/zero.ttf"
    second = root / "完整包/测试/西文/Shape One/one.ttf"
    make_shape_font(first, "Shape Zero", 0)
    make_shape_font(second, "Shape One", 1)
    service = Library(Settings(root=root, state=tmp_path / "shape-state"))
    service.refresh("scan")
    return service, first


def rendered_png(manager, path, text, inverted=False, scale=1):
    required = {c for c in text if not c.isspace()}
    mask, _ = manager._face_mask(path, 0, text, required, 96)
    image = ImageOps.expand(ImageOps.invert(mask), border=12, fill=255)
    if scale != 1:
        image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
    if inverted:
        image = ImageOps.invert(image)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def plain_png():
    output = io.BytesIO()
    Image.new("RGB", (100, 40), "white").save(output, format="PNG")
    return output.getvalue()


def wait_job(manager, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError("recognition job did not finish")


@pytest.mark.parametrize(("inverted", "scale"), [(False, 1), (True, 2)])
def test_matching_ranks_source_font_first_for_scaled_and_inverted_images(
    tmp_path, inverted, scale
):
    library, source = shape_library(tmp_path)
    manager = RecognitionManager(library)
    try:
        data = rendered_png(manager, source, "AB", inverted=inverted, scale=scale)
        created = manager.create(data, "AB")
        job = wait_job(manager, created["id"])
        assert job["status"] == "completed", job["error"]
        assert len(job["results"]) == 2
        assert job["results"][0]["family"] == "Shape Zero"
        assert job["results"][0]["score"] > 90
        assert job["results"][0]["score"] > job["results"][1]["score"]
    finally:
        manager.close()


def test_matching_checks_every_ttc_face_and_skips_missing_coverage(library):
    manager = RecognitionManager(library)
    source = library.path(library.search("测试中文")["items"][0]["id"])
    collection = library.search(format="TTC")["items"][0]
    try:
        created = manager.create(rendered_png(manager, source, "山海"), "山海")
        job = wait_job(manager, created["id"])
        assert job["status"] == "completed", job["error"]
        assert job["matched"] == 2
        assert job["skipped_missing"] == 2
        faces = {(row["id"], row["face"]) for row in job["results"]}
        assert (collection["id"], 1) in faces
        assert (collection["id"], 0) not in faces
    finally:
        manager.close()


def test_parallel_and_single_worker_keep_the_same_cross_batch_winner(tmp_path, monkeypatch):
    library, source = shape_library(tmp_path)
    monkeypatch.setattr(recognition, "COARSE_CHUNK_SIZE", 1)
    monkeypatch.setattr(recognition, "REFINE_LIMIT", 1)
    data = None

    def match(workers):
        manager = RecognitionManager(library, workers=workers)
        try:
            nonlocal data
            data = data or rendered_png(manager, source, "AB")
            created = manager.create(data, "AB")
            return wait_job(manager, created["id"])
        finally:
            manager.close()

    single = match(1)
    parallel = match(2)
    assert single["status"] == parallel["status"] == "completed"
    assert single["processed"] == parallel["processed"] == 1  # Completed refine phase.
    assert single["matched"] == parallel["matched"] == 2
    assert len(parallel["results"]) == 1
    assert parallel["results"] == single["results"]
    assert parallel["results"][0]["family"] == "Shape Zero"


def test_cancellation_with_pending_process_batches(tmp_path, monkeypatch):
    library, source = shape_library(tmp_path)
    monkeypatch.setattr(recognition, "COARSE_CHUNK_SIZE", 1)
    manager = RecognitionManager(library, workers=2)
    data = rendered_png(manager, source, "AB")
    started = threading.Event()
    release = threading.Event()
    real_wait = recognition.wait

    def blocked_wait(*args, **kwargs):
        started.set()
        release.wait(2)
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(recognition, "wait", blocked_wait)
    try:
        created = manager.create(data, "AB")
        assert started.wait(1)
        manager.cancel(created["id"])
        release.set()
        assert wait_job(manager, created["id"])["status"] == "cancelled"
    finally:
        release.set()
        manager.close()


def test_job_cancellation_concurrency_and_ttl(library, monkeypatch):
    manager = RecognitionManager(library)
    source = library.path(library.search("测试中文")["items"][0]["id"])
    data = rendered_png(manager, source, "山海")
    started = threading.Event()
    release = threading.Event()
    original = library.recognition_candidates

    def blocked_candidates():
        started.set()
        release.wait(2)
        return original()

    monkeypatch.setattr(library, "recognition_candidates", blocked_candidates)
    try:
        created = manager.create(data, "山海")
        assert started.wait(1)
        with pytest.raises(RecognitionBusy):
            manager.create(data, "山海")
        manager.cancel(created["id"])
        release.set()
        assert wait_job(manager, created["id"])["status"] == "cancelled"
        manager.jobs[created["id"]].updated = time.time() - JOB_TTL - 1
        with pytest.raises(KeyError):
            manager.get(created["id"])
    finally:
        release.set()
        manager.close()


def test_ocr_parses_lines_and_uses_restricted_cli(monkeypatch):
    monkeypatch.setattr(recognition, "available_languages", lambda: ["eng"])
    monkeypatch.setattr(recognition.shutil, "which", lambda _: "/usr/bin/tesseract")
    calls = []
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t91.5\tHello\n"
        "5\t1\t1\t1\t1\t2\t12\t0\t10\t10\t88.5\tworld\n"
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=tsv.encode(), stderr=b"")

    monkeypatch.setattr(recognition.subprocess, "run", fake_run)
    result = ocr_image(plain_png(), "eng")
    assert result == {"text": "Hello world", "confidence": 90.0, "language": "eng"}
    command, kwargs = calls[0]
    assert command[:3] == ["/usr/bin/tesseract", "stdin", "stdout"]
    assert command[command.index("-l") + 1] == "eng"
    assert kwargs["env"]["OMP_THREAD_LIMIT"] == "1"
    assert kwargs["timeout"] == 45
    assert kwargs["input"].startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="语言包"):
        ocr_image(plain_png(), "chi_sim")


def test_ocr_timeout_and_unavailable(monkeypatch):
    monkeypatch.setattr(recognition, "available_languages", lambda: ["eng"])
    monkeypatch.setattr(recognition.shutil, "which", lambda _: "/usr/bin/tesseract")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("tesseract", 45)

    monkeypatch.setattr(recognition.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="超时"):
        ocr_image(plain_png(), "eng")
    monkeypatch.setattr(recognition, "available_languages", list)
    with pytest.raises(OCRUnavailable):
        ocr_image(plain_png(), "eng")


def test_image_validation_and_ocr_multipart_route(library, monkeypatch):
    with pytest.raises(ValueError, match="损坏"):
        decode_image(b"broken")
    monkeypatch.setattr(recognition, "IMAGE_PIXEL_LIMIT", 50)
    with pytest.raises(ValueError, match="像素"):
        decode_image(plain_png())
    monkeypatch.setattr(recognition, "IMAGE_PIXEL_LIMIT", 24_000_000)
    def fake_ocr(self, data, language):
        decode_image(data)
        return {"text": f"AB {language}", "confidence": 99, "language": language}

    monkeypatch.setattr(RecognitionManager, "ocr", fake_ocr)
    with TestClient(create_app(library.settings)) as client:
        response = client.post(
            "/api/recognition/ocr",
            files={"file": ("text.png", plain_png(), "image/png")},
            data={"language": "eng"},
        )
        assert response.status_code == 200
        assert response.json()["text"] == "AB eng"
        assert client.post(
            "/api/recognition/ocr", files={"file": ("broken.png", b"bad", "image/png")}
        ).status_code == 400
