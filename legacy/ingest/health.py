"""Read-only audit of the retained collection. Writes reports, never font files."""
import array
import collections
import concurrent.futures
import json
import logging
from pathlib import Path
import struct
import sys
import threading
import time
import traceback

from fontTools.ttLib import TTFont

ROOT = Path(r"X:\Fonts")
RECORD = ROOT / "_字体整理记录"
OUT = RECORD / "字体健康检查_20261021"
LOCAL = threading.local()


class Capture(logging.Handler):
    def emit(self, record):
        messages = getattr(LOCAL, "messages", None)
        if messages is not None:
            text = record.getMessage()
            if text not in messages and len(messages) < 20:
                messages.append(text)


logger = logging.getLogger("fontTools")
logger.handlers.clear()
logger.addHandler(Capture())
logger.setLevel(logging.WARNING)
logger.propagate = False


def read_jsonl(path):
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def inspect(row):
    result = {"source": row["source"], "original_source": row["original_source"], "size": row["size"], "faces": []}
    path = ROOT / row["source"]
    try:
        stat = path.stat()
        if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
            result["changed_since_cleanup"] = True
        with path.open("rb") as handle:
            header = handle.read(12)
        count = struct.unpack(">I", header[8:12])[0] if header[:4] == b"ttcf" else 1
        if not 1 <= count <= 512:
            raise ValueError(f"Unexpected font face count: {count}")
        for index in range(count):
            LOCAL.messages = []
            face = {"index": index, "errors": [], "compatibility": []}
            font = None
            try:
                font = TTFont(path, fontNumber=index, lazy=True, recalcBBoxes=False, recalcTimestamp=False)
                tables = font.reader.tables
                face["outline"] = "glyf" if "glyf" in tables else "CFF2" if "CFF2" in tables else "CFF " if "CFF " in tables else "other"
                face["variable"] = "fvar" in tables
                for tag, entry in tables.items():
                    if entry.offset < 0 or entry.offset + entry.length > stat.st_size:
                        face["errors"].append(f"directory: {tag} outside file ({entry.offset}+{entry.length}>{stat.st_size})")
                mandatory = ["head", "maxp", "name", "cmap", "hhea", "hmtx"]
                if "glyf" in tables:
                    mandatory.append("loca")
                for tag in mandatory:
                    if tag not in tables:
                        face["errors"].append(f"missing: {tag}")
                for tag in ["head", "maxp", "hhea", "OS/2", "name", "post"]:
                    if tag not in tables:
                        continue
                    try:
                        table = font[tag]
                        if tag == "head":
                            face["units_per_em"] = table.unitsPerEm
                            if table.magicNumber != 0x5F0F3CF5:
                                face["errors"].append("head: invalid magicNumber")
                            if not 16 <= table.unitsPerEm <= 16384:
                                face["errors"].append(f"head: unitsPerEm out of range {table.unitsPerEm}")
                        elif tag == "maxp":
                            face["num_glyphs"] = table.numGlyphs
                        elif tag == "name":
                            names = collections.defaultdict(list)
                            for rec in table.names:
                                if rec.nameID in (1, 2, 4, 5, 6, 16, 17):
                                    try:
                                        value = rec.toUnicode().replace("\x00", "").strip()
                                        if value and value not in names[str(rec.nameID)]:
                                            names[str(rec.nameID)].append(value)
                                    except Exception as exc:
                                        face["compatibility"].append(f"name decode: {exc}")
                            face["names"] = dict(names)
                    except Exception as exc:
                        face["errors"].append(f"{tag}: {type(exc).__name__}: {exc}")
                # Use independent synthetic glyph names so malformed post names do
                # not cascade into false cmap/hmtx failures. No font is saved.
                num_glyphs = face.get("num_glyphs", 0)
                if num_glyphs:
                    font.setGlyphOrder([".notdef"] + [f"audit{i:05d}" for i in range(1, num_glyphs)])
                if "cmap" in tables:
                    try:
                        cmap = font["cmap"]
                        unicode_points = set()
                        all_formats = []
                        nonempty = 0
                        invalid_glyphs = set()
                        for sub in cmap.tables:
                            mapping = getattr(sub, "cmap", {})
                            nonempty += bool(mapping)
                            all_formats.append([sub.platformID, sub.platEncID, sub.format, len(mapping)])
                            if sub.isUnicode():
                                unicode_points.update(mapping)
                            for name in set(mapping.values()):
                                try:
                                    gid = font.getGlyphID(name)
                                    if gid >= num_glyphs:
                                        invalid_glyphs.add(gid)
                                except Exception:
                                    invalid_glyphs.add(str(name))
                        face["cmap_subtables"] = all_formats
                        face["unicode_count"] = len(unicode_points)
                        face["ascii_letters"] = sum(cp in unicode_points for cp in range(65, 91)) + sum(cp in unicode_points for cp in range(97, 123))
                        face["cjk_count"] = sum(0x3400 <= cp <= 0x9FFF or 0x20000 <= cp <= 0x323AF for cp in unicode_points)
                        if not unicode_points:
                            face["compatibility"].append("no Unicode cmap (legacy/symbol font possible)")
                        if not nonempty:
                            face["errors"].append("cmap: no character mappings")
                        if invalid_glyphs:
                            face["errors"].append(f"cmap: glyph IDs outside maxp ({sorted(invalid_glyphs, key=str)[:10]})")
                    except Exception as exc:
                        face["errors"].append(f"cmap: {type(exc).__name__}: {exc}")
                if "hmtx" in tables and "hhea" in font and num_glyphs:
                    try:
                        metrics_count = font["hhea"].numberOfHMetrics
                        if not 1 <= metrics_count <= num_glyphs:
                            face["errors"].append(f"hhea: numberOfHMetrics {metrics_count} for {num_glyphs} glyphs")
                        font["hmtx"]
                    except Exception as exc:
                        face["errors"].append(f"hmtx: {type(exc).__name__}: {exc}")
                if "glyf" in tables and "loca" in tables and "head" in font and num_glyphs:
                    try:
                        index_format = font["head"].indexToLocFormat
                        if index_format not in (0, 1):
                            raise ValueError(f"invalid indexToLocFormat {index_format}")
                        raw = font.reader["loca"]
                        width = 2 if index_format == 0 else 4
                        required = (num_glyphs + 1) * width
                        if len(raw) < required:
                            raise ValueError(f"loca truncated ({len(raw)} bytes, need {required})")
                        offsets = array.array("H" if width == 2 else "I")
                        offsets.frombytes(raw[:required])
                        if sys.byteorder == "little":
                            offsets.byteswap()
                        if width == 2:
                            offsets = [v * 2 for v in offsets]
                        if any(a > b for a, b in zip(offsets, offsets[1:])):
                            face["errors"].append("loca: glyph offsets not monotonic")
                        if max(offsets) > tables["glyf"].length:
                            face["errors"].append(f"loca: offset {max(offsets)} outside glyf length {tables['glyf'].length}")
                        face["nonempty_outlines"] = sum(a < b for a, b in zip(offsets, offsets[1:]))
                        if not face["nonempty_outlines"] and not any(t in tables for t in ["CBDT", "EBDT", "sbix", "SVG ", "COLR"]):
                            face["errors"].append("glyf: no nonempty outlines")
                    except Exception as exc:
                        face["errors"].append(f"loca: {type(exc).__name__}: {exc}")
            except Exception as exc:
                face["errors"].append(f"open: {type(exc).__name__}: {exc}")
            finally:
                if font is not None:
                    font.close()
                face["warnings"] = LOCAL.messages
                result["faces"].append(face)
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

