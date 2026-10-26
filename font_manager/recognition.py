"""Local OCR and font matching. Images never leave the fonts-manager process."""

import csv
import io
import logging
import os
import shutil
import struct
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageStat

logger = logging.getLogger(__name__)
IMAGE_FILE_LIMIT = 12 * 1024 * 1024
IMAGE_REQUEST_LIMIT = IMAGE_FILE_LIMIT + 256 * 1024
IMAGE_PIXEL_LIMIT = 24_000_000
TEXT_LIMIT = 100
RESULT_LIMIT = 12
REFINE_LIMIT = 96
JOB_TTL = 30 * 60
JOB_TIMEOUT = 15 * 60
OCR_LANGUAGES = {
    "eng": "English",
    "chi_sim": "简体中文",
    "chi_tra": "繁體中文",
    "jpn": "日本語",
}


class RecognitionBusy(RuntimeError):
    pass


class OCRUnavailable(RuntimeError):
    pass


class RecognitionCancelled(RuntimeError):
    pass


def decode_image(data):
    if not data:
        raise ValueError("图片内容为空")
    if len(data) > IMAGE_FILE_LIMIT:
        raise ValueError("识别图片最大 12 MiB，请先裁剪或压缩")
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if opened.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("请使用 PNG、JPEG 或 WebP 图片")
            if opened.width * opened.height > IMAGE_PIXEL_LIMIT:
                raise ValueError("图片像素不能超过 2400 万，请先裁剪文字区域")
            opened.load()
            image = ImageOps.exif_transpose(opened)
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                image = Image.new("RGBA", rgba.size, "white")
                image.alpha_composite(rgba)
            return image.convert("RGB")
    except ValueError:
        raise
    except (Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ValueError("图片损坏或格式无法识别") from exc


def _otsu_threshold(image):
    histogram = image.histogram()
    total = sum(histogram)
    weighted_total = sum(value * count for value, count in enumerate(histogram))
    background = 0
    background_weight = 0.0
    best = 127
    best_variance = -1.0
    for value, count in enumerate(histogram):
        background += count
        if not background:
            continue
        foreground = total - background
        if not foreground:
            break
        background_weight += value * count
        background_mean = background_weight / background
        foreground_mean = (weighted_total - background_weight) / foreground
        variance = background * foreground * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            best = value
    return best


def source_mask(image):
    gray = ImageOps.autocontrast(ImageOps.grayscale(image))
    width, height = gray.size
    sample_width = max(1, min(16, width // 8))
    sample_height = max(1, min(16, height // 8))
    corners = [
        gray.crop((0, 0, sample_width, sample_height)),
        gray.crop((width - sample_width, 0, width, sample_height)),
        gray.crop((0, height - sample_height, sample_width, height)),
        gray.crop((width - sample_width, height - sample_height, width, height)),
    ]
    background = sum(ImageStat.Stat(corner).mean[0] for corner in corners) / 4
    if background < 128:
        gray = ImageOps.invert(gray)
    threshold = _otsu_threshold(gray)
    mask = gray.point(lambda value: 255 if value <= threshold else 0)
    bounds = mask.getbbox()
    if not bounds:
        raise ValueError("未检测到可匹配的文字区域，请裁剪后重试")
    mask = mask.crop(bounds)
    if mask.width < 2 or mask.height < 4:
        raise ValueError("文字区域太小，请提供更清晰的图片")
    return mask


def available_languages():
    executable = shutil.which("tesseract")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    installed = {line.strip() for line in result.stdout.splitlines()}
    return [code for code in OCR_LANGUAGES if code in installed]


def _join_ocr_words(words, language):
    if not any(code in {"chi_sim", "chi_tra", "jpn"} for code in language.split("+")):
        return " ".join(words)
    result = ""
    for word in words:
        if result and result[-1].isascii() and word[0].isascii():
            result += " "
        result += word
    return result


def ocr_image(data, language):
    image = decode_image(data)
    installed = available_languages()
    if not installed:
        raise OCRUnavailable("未安装 Tesseract OCR 或所需语言包；仍可手动输入文字进行匹配")
    requested = language.split("+") if language else ["eng"]
    if not requested or len(requested) > 2 or any(code not in OCR_LANGUAGES for code in requested):
        raise ValueError("请选择一种语言，或一种中日文语言与 English 组合")
    if len(set(requested)) != len(requested) or (len(requested) == 2 and "eng" not in requested):
        raise ValueError("请选择一种语言，或一种中日文语言与 English 组合")
    missing = [code for code in requested if code not in installed]
    if missing:
        raise ValueError("未安装 OCR 语言包：" + "、".join(missing))
    language = "+".join(requested)
    output = io.BytesIO()
    image.save(output, format="PNG")
    command = [
        shutil.which("tesseract") or "tesseract",
        "stdin",
        "stdout",
        "-l",
        language,
        "--psm",
        "6",
        "tsv",
    ]
    try:
        result = subprocess.run(
            command,
            input=output.getvalue(),
            capture_output=True,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            timeout=45,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OCRUnavailable("未安装 Tesseract OCR；仍可手动输入文字进行匹配") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("OCR 超时，请裁剪更小的文字区域后重试") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        logger.warning("Tesseract OCR failed: %s", message[-1] if message else result.returncode)
        raise ValueError("OCR 无法处理该图片，请手动输入文字")
    rows = csv.DictReader(io.StringIO(result.stdout.decode("utf-8", "replace")), delimiter="\t")
    lines = {}
    confidences = []
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        key = tuple(row.get(key, "0") for key in ("page_num", "block_num", "par_num", "line_num"))
        lines.setdefault(key, []).append(text)
        try:
            confidence = float(row.get("conf", "-1"))
            if confidence >= 0:
                confidences.append(confidence)
        except ValueError:
            pass
    text = "\n".join(_join_ocr_words(lines[key], language) for key in sorted(lines)).strip()
    if len(text) > TEXT_LIMIT:
        raise ValueError(f"OCR 文本超过 {TEXT_LIMIT} 字，请裁剪为更小的同字体区域")
    return {
        "text": text,
        "confidence": round(sum(confidences) / len(confidences), 1) if confidences else 0,
        "language": language,
    }


def _tight_text_mask(font, text, size):
    probe = Image.new("L", (1, 1))
    draw = ImageDraw.Draw(probe)
    spacing = round(size * 0.35)
    bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="left")
    width = max(1, bounds[2] - bounds[0] + 4)
    height = max(1, bounds[3] - bounds[1] + 4)
    image = Image.new("L", (width, height))
    draw = ImageDraw.Draw(image)
    draw.multiline_text(
        (2 - bounds[0], 2 - bounds[1]),
        text,
        font=font,
        fill=255,
        spacing=spacing,
        align="left",
    )
    bounds = image.getbbox()
    if not bounds:
        raise ValueError("字体没有生成可见字形")
    return image.crop(bounds)


def _normalized(mask, height):
    width = max(1, round(mask.width * height / mask.height))
    resized = mask.resize((width, height), Image.Resampling.LANCZOS)
    return resized.point(lambda value: 255 if value > 127 else 0)


def _dice_at(source, candidate, dx=0, dy=0):
    width = max(source.width, candidate.width) + 16
    height = max(source.height, candidate.height) + 16
    left = Image.new("L", (width, height))
    right = Image.new("L", (width, height))
    left.paste(source, ((width - source.width) // 2, (height - source.height) // 2))
    right.paste(candidate, ((width - candidate.width) // 2 + dx, (height - candidate.height) // 2 + dy))
    source_area = source.histogram()[255]
    candidate_area = candidate.histogram()[255]
    if not source_area or not candidate_area:
        return 0.0
    intersection = ImageChops.darker(left, right).histogram()[255]
    return 2 * intersection / (source_area + candidate_area)


def similarity(source, candidate, height=64, refine=False):
    source = _normalized(source, height)
    candidate = _normalized(candidate, height)
    offsets = range(-4, 5, 2) if refine else (0,)
    return max(_dice_at(source, candidate, dx, dy) for dx in offsets for dy in offsets)


@dataclass
class RecognitionJob:
    id: str
    text: str
    image: Image.Image
    created: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    updated: float = field(default_factory=time.time)
    status: str = "queued"
    phase: str = "等待"
    processed: int = 0
    total: int = 0
    matched: int = 0
    skipped_missing: int = 0
    skipped_errors: int = 0
    results: list = field(default_factory=list)
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def snapshot(self):
        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "processed": self.processed,
            "total": self.total,
            "matched": self.matched,
            "skipped_missing": self.skipped_missing,
            "skipped_errors": self.skipped_errors,
            "results": self.results,
            "error": self.error,
        }


class RecognitionManager:
    def __init__(self, library):
        self.library = library
        self.jobs = {}
        self.lock = threading.Lock()
        self.ocr_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="font-recognition")

    def languages(self):
        installed = available_languages()
        return {
            "available": bool(installed),
            "languages": [{"code": code, "label": OCR_LANGUAGES[code]} for code in installed],
        }

    def ocr(self, data, language):
        if not self.ocr_lock.acquire(blocking=False):
            raise RecognitionBusy("另一个图片正在 OCR，请稍后重试")
        try:
            return ocr_image(data, language)
        finally:
            self.ocr_lock.release()

    def _discard_expired(self):
        cutoff = time.time() - JOB_TTL
        expired = [
            job_id
            for job_id, job in self.jobs.items()
            if job.updated < cutoff and job.status not in {"queued", "running"}
        ]
        for job_id in expired:
            del self.jobs[job_id]

    def create(self, data, text):
        text = text.strip()
        if not text or len(text) > TEXT_LIMIT:
            raise ValueError(f"匹配文本须为 1–{TEXT_LIMIT} 字")
        mask = source_mask(decode_image(data))
        with self.lock:
            self._discard_expired()
            if any(job.status in {"queued", "running"} for job in self.jobs.values()):
                raise RecognitionBusy("已有一个全库识别任务，请等待完成或先取消")
            job = RecognitionJob(uuid.uuid4().hex, text, mask)
            self.jobs[job.id] = job
            self.executor.submit(self._run, job.id)
            return job.snapshot()

    def get(self, job_id):
        with self.lock:
            self._discard_expired()
            if job_id not in self.jobs:
                raise KeyError("识别任务不存在或已过期")
            return self.jobs[job_id].snapshot()

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("识别任务不存在或已过期")
            job = self.jobs[job_id]
            job.cancel.set()
            if job.status == "queued":
                job.status = "cancelled"
                job.phase = "已取消"
                job.updated = time.time()
            return job.snapshot()

    def close(self):
        with self.lock:
            for job in self.jobs.values():
                job.cancel.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _checkpoint(self, job):
        if job.cancel.is_set():
            raise RecognitionCancelled
        if time.monotonic() - job.started_monotonic > JOB_TIMEOUT:
            raise ValueError("全库识别超时，请减少文字或稍后重试")

    def _face_mask(self, path, face, text, required, size):
        with self.library.open_font(path, face)[0] as font:
            cmap = font.getBestCmap() or {}
            if any(ord(character) not in cmap for character in required):
                return None, None
            names = font["name"]
            style = names.getDebugName(17) or names.getDebugName(2) or ""
            family = names.getDebugName(16) or names.getDebugName(1) or ""
            raw = None
            if path.suffix.lower() in {".woff", ".woff2"}:
                font.flavor = None
                raw = io.BytesIO()
                font.save(raw)
                raw.seek(0)
                display = ImageFont.truetype(raw, size=size)
            else:
                display = None
        if display is None:
            display = ImageFont.truetype(str(path), size=size, index=face)
        return _tight_text_mask(display, text, size), {"family": family, "style": style}

    @staticmethod
    def _face_count(path):
        with path.open("rb") as stream:
            header = stream.read(12)
        if header[:4] == b"ttcf" and len(header) == 12:
            return min(struct.unpack(">I", header[8:12])[0], 1000)
        return 1

    def _run(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if job.status == "cancelled":
                return
            job.status = "running"
            job.phase = "初筛"
            job.updated = time.time()
        try:
            candidates = self.library.recognition_candidates()
            with self.lock:
                job.total = len(candidates)
            required = {character for character in job.text if not character.isspace()}
            if not required:
                raise ValueError("匹配文本至少需要一个非空白字符")
            coarse = []
            for candidate in candidates:
                self._checkpoint(job)
                try:
                    try:
                        path = self.library.checked(candidate["path"])
                        face_count = self._face_count(path)
                    except Exception:  # noqa: BLE001 - a bad face must not stop the library scan
                        with self.lock:
                            job.skipped_errors += 1
                        continue
                    for face in range(face_count):
                        self._checkpoint(job)
                        try:
                            mask, names = self._face_mask(path, face, job.text, required, 96)
                        except Exception as exc:  # noqa: BLE001 - skip broken third-party font data
                            logger.debug("Skipping unreadable font %s face %s: %s", candidate["path"], face, exc)
                            with self.lock:
                                job.skipped_errors += 1
                            continue
                        if mask is None:
                            with self.lock:
                                job.skipped_missing += 1
                            continue
                        score = similarity(job.image, mask)
                        coarse.append((score, candidate, face, names))
                        with self.lock:
                            job.matched += 1
                finally:
                    with self.lock:
                        job.processed += 1
                        job.updated = time.time()
            coarse.sort(key=lambda item: item[0], reverse=True)
            with self.lock:
                job.phase = "精排"
                job.processed = 0
                job.total = min(REFINE_LIMIT, len(coarse))
            refined = []
            for score, candidate, face, names in coarse[:REFINE_LIMIT]:
                self._checkpoint(job)
                try:
                    path = self.library.checked(candidate["path"])
                    mask, _ = self._face_mask(path, face, job.text, required, 192)
                    score = similarity(job.image, mask, height=160, refine=True)
                    refined.append(
                        {
                            "id": candidate["id"],
                            "family": names["family"] or candidate["family"],
                            "style": names["style"],
                            "filename": candidate["filename"],
                            "format": candidate["format"],
                            "face": face,
                            "score": round(score * 100, 1),
                        }
                    )
                except Exception:
                    logger.debug("Unable to refine font %s face %s", candidate["path"], face, exc_info=True)
                    with self.lock:
                        job.skipped_errors += 1
                finally:
                    with self.lock:
                        job.processed += 1
            refined.sort(key=lambda item: item["score"], reverse=True)
            with self.lock:
                job.results = refined[:RESULT_LIMIT]
                job.status = "completed"
                job.phase = "完成"
                job.updated = time.time()
        except RecognitionCancelled:
            with self.lock:
                job.status = "cancelled"
                job.phase = "已取消"
                job.updated = time.time()
        except Exception as exc:  # noqa: BLE001 - background failures must reach the polling client
            logger.warning("Font recognition job failed: %s", exc)
            with self.lock:
                job.status = "failed"
                job.phase = "失败"
                job.error = str(exc)[:240] if isinstance(exc, ValueError) else "识别过程中发生错误"
                job.updated = time.time()
        finally:
            job.image = None
