"""Shared service for WebUI and MCP. Font files stay on the NAS; SQLite stays local."""

import hashlib
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from fontTools import subset
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

from .config import Settings

EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".fon"}
logger = logging.getLogger(__name__)
logging.getLogger("fontTools.subset").setLevel(logging.WARNING)


class Library:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.root
        self.cache = settings.state / "previews"
        self.cache.mkdir(exist_ok=True)
        self.db_path = settings.state / "library.sqlite3"
        self.refresh_lock = threading.Lock()
        self.catalog_lock = threading.Lock()
        self.render_lock = threading.Semaphore(2)
        self.indexing = False
        self.index_error = None
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fonts (
                    id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, family TEXT NOT NULL,
                    filename TEXT NOT NULL, vendor TEXT NOT NULL, language TEXT NOT NULL,
                    format TEXT NOT NULL, size INTEGER NOT NULL, names TEXT NOT NULL, search TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS fonts_vendor ON fonts(vendor);
                CREATE INDEX IF NOT EXISTS fonts_language ON fonts(language);
                CREATE TABLE IF NOT EXISTS annotations (
                    font_id TEXT PRIMARY KEY, favorite INTEGER NOT NULL DEFAULT 0,
                    tags TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '', updated REAL
                );
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                    font_id TEXT NOT NULL, PRIMARY KEY(collection_id, font_id)
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            # Seed once; later edits and resets persist independently of container env.
            db.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES('api_token',?)",
                (settings.token or secrets.token_urlsafe(32),),
            )

    def api_token(self):
        with self.db() as db:
            return db.execute("SELECT value FROM settings WHERE key='api_token'").fetchone()[0]

    def update_api_token(self, token=None):
        token = secrets.token_urlsafe(32) if token is None else token.strip()
        if not 16 <= len(token) <= 256 or any(not "!" <= c <= "~" for c in token):
            raise ValueError("API Token 须为 16–256 位 ASCII 字符，不能包含空格")
        with self.db() as db:
            db.execute("UPDATE settings SET value=? WHERE key='api_token'", (token,))
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES('api_token_updated_at',?)", (str(time.time()),)
            )
        return {"api_token": token}

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def checked(self, relative: str) -> Path:
        rel = PurePosixPath(relative)
        if rel.is_absolute() or ".." in rel.parts or "\\" in relative or not rel.parts:
            raise ValueError("不允许访问字体根目录以外的路径")
        path = self.root.joinpath(*rel.parts)
        path.resolve().relative_to(self.root.resolve())
        return path

    def refresh(self, source="catalog"):
        """Atomically replace the index only after a complete successful inventory."""
        if source not in {"catalog", "scan"}:
            raise ValueError("source 只能是 catalog 或 scan")
        if not self.refresh_lock.acquire(blocking=False):
            return {"indexing": True}
        self.indexing = True
        self.index_error = None
        try:
            if not self.root.is_dir():
                raise ValueError(f"字体共享未连接：{self.root}")
            catalog = self.root / "字体目录.html"
            if source == "catalog" and catalog.exists():
                page = catalog.read_text(encoding="utf-8-sig")
                marker = "const data="
                start = page.find(marker)
                if start == -1:
                    raise ValueError("字体目录格式无法识别，请使用完整扫描")
                raw, _ = json.JSONDecoder().raw_decode(page[start + len(marker) :].lstrip())
                entries = []
                for row in raw:
                    if row.get("a") != "保留" or not row.get("p", "").startswith("完整包/"):
                        continue
                    rel = PurePosixPath(row["p"])
                    if ".." in rel.parts or "\\" in str(rel):
                        continue
                    path = self.root.joinpath(*rel.parts)
                    if path.is_symlink() or not path.is_file():
                        continue
                    entries.append(
                        self.index_row(
                            str(rel),
                            row.get("f", rel.stem),
                            row.get("v", "未分类"),
                            row.get("l", "待确认"),
                            int(row.get("s", 0)) * 1024,
                            row.get("names", ""),
                        )
                    )
                origin = "NAS 字体目录"
            else:
                base = self.root / "完整包"
                if not base.is_dir():
                    raise ValueError("未找到完整包目录")
                with self.db() as db:
                    known_names = dict(db.execute("SELECT path,names FROM fonts").fetchall())
                entries = []
                errors = []
                for parent, dirs, files in os.walk(base, onerror=lambda e: errors.append(str(e))):
                    dirs[:] = [d for d in dirs if not (Path(parent) / d).is_symlink()]
                    for filename in files:
                        path = Path(parent) / filename
                        if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                            continue
                        rel = path.relative_to(self.root)
                        parts = rel.parts
                        try:
                            size = path.stat().st_size
                        except OSError as exc:
                            errors.append(str(exc))
                            continue
                        entries.append(
                            self.index_row(
                                rel.as_posix(),
                                parts[-2] if len(parts) > 4 else path.stem,
                                parts[1] if len(parts) > 2 else "未分类",
                                parts[2] if len(parts) > 3 else "待确认",
                                size,
                                known_names.get(rel.as_posix(), path.stem),
                            )
                        )
                if errors:
                    raise ValueError(
                        f"扫描未完成，保留原索引；{len(errors)} 个目录或文件无法读取：{errors[0]}"
                    )
                origin = "完整目录扫描"
            if not entries:
                raise ValueError("未找到字体记录，保留原索引")
            # Keep annotations and memberships independently, including temporarily unavailable fonts.
            with self.db() as db:
                db.execute("DELETE FROM fonts")
                db.executemany("INSERT OR REPLACE INTO fonts VALUES(?,?,?,?,?,?,?,?,?,?)", entries)
                db.executemany(
                    "INSERT OR REPLACE INTO settings VALUES(?,?)",
                    [("indexed_at", str(time.time())), ("index_source", origin)],
                )
            return {"indexed": len(entries), "source": origin}
        except Exception as exc:
            self.index_error = str(exc)
            raise
        finally:
            self.indexing = False
            self.refresh_lock.release()

    @staticmethod
    def index_row(path, family, vendor, language, size, names):
        filename = PurePosixPath(path).name
        font_id = hashlib.sha256(path.encode()).hexdigest()[:24]
        search = f"{path} {family} {vendor} {language} {names}".casefold()
        return (
            font_id,
            path,
            family,
            filename,
            vendor,
            language,
            PurePosixPath(path).suffix[1:].upper(),
            size,
            names,
            search,
        )

    def start_refresh(self, source="catalog"):
        def run():
            try:
                self.refresh(source)
            except Exception:
                logger.exception("Index refresh failed; previous index retained")

        threading.Thread(target=run, daemon=True).start()
        return {"indexing": True}

    @staticmethod
    def serialize(row):
        result = dict(row)
        result.pop("search", None)
        result["favorite"] = bool(result.get("favorite"))
        result["tags"] = json.loads(result.get("tags") or "[]")
        result["note"] = result.get("note") or ""
        return result

    def search(
        self,
        query="",
        vendor="",
        language="",
        format="",
        favorite=False,
        collection=None,
        tag="",
        limit=36,
        offset=0,
    ):
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit 范围为 1–100，offset 不能小于 0")
        clauses, values = [], []
        for part in query.casefold().split():
            clauses.append("instr(f.search, ?) > 0")
            values.append(part)
        for key, value in (("vendor", vendor), ("language", language), ("format", format)):
            if value:
                clauses.append(f"f.{key} = ?")
                values.append(value)
        if favorite:
            clauses.append("a.favorite = 1")
        if collection is not None:
            clauses.append("f.id IN (SELECT font_id FROM members WHERE collection_id = ?)")
            values.append(collection)
        if tag:
            clauses.append("EXISTS (SELECT 1 FROM json_each(a.tags) WHERE value = ?)")
            values.append(tag)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        base = " FROM fonts f LEFT JOIN annotations a ON a.font_id=f.id" + where
        with self.db() as db:
            total = db.execute("SELECT count(*)" + base, values).fetchone()[0]
            result = db.execute(
                "SELECT f.*, a.favorite, a.tags, a.note"
                + base
                + " ORDER BY f.family COLLATE NOCASE,f.filename,f.id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {
            "total": total,
            "items": [self.serialize(r) for r in result],
            "limit": limit,
            "offset": offset,
        }

    def get(self, font_id):
        with self.db() as db:
            row = db.execute(
                """SELECT f.*,a.favorite,a.tags,a.note FROM fonts f
                LEFT JOIN annotations a ON a.font_id=f.id WHERE f.id=?""",
                (font_id,),
            ).fetchone()
            if row is None:
                raise KeyError("字体不存在，请刷新索引")
            result = self.serialize(row)
            result["collections"] = [
                dict(r)
                for r in db.execute(
                    """SELECT c.id,c.name FROM collections c
                JOIN members m ON m.collection_id=c.id WHERE m.font_id=?""",
                    (font_id,),
                )
            ]
        return result

    def path(self, font_id):
        row = self.get(font_id)
        path = self.checked(row["path"])
        if not path.is_file():
            raise FileNotFoundError("字体文件暂时不可用，请检查共享连接或刷新索引")
        return path

    def annotate(
        self, font_ids, favorite=None, tags=None, note=None, collection_id=None, remove_collection=False
    ):
        if not font_ids or len(font_ids) > 200:
            raise ValueError("每次请选择 1–200 个字体")
        if tags is not None:
            tags = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
            if len(tags) > 30 or any(len(t) > 60 for t in tags):
                raise ValueError("最多 30 个标签，每个不超过 60 字")
        if note is not None and len(note) > 4000:
            raise ValueError("备注不能超过 4000 字")
        with self.db() as db:
            if (
                collection_id is not None
                and not db.execute("SELECT 1 FROM collections WHERE id=?", (collection_id,)).fetchone()
            ):
                raise KeyError("选集不存在")
            for font_id in set(font_ids):
                if not db.execute("SELECT 1 FROM fonts WHERE id=?", (font_id,)).fetchone():
                    raise KeyError("字体不存在：" + font_id)
                db.execute("INSERT OR IGNORE INTO annotations(font_id) VALUES(?)", (font_id,))
                for column, value in (
                    ("favorite", int(favorite) if favorite is not None else None),
                    ("tags", json.dumps(tags, ensure_ascii=False) if tags is not None else None),
                    ("note", note),
                ):
                    if value is not None:
                        db.execute(
                            f"UPDATE annotations SET {column}=?,updated=? WHERE font_id=?",
                            (value, time.time(), font_id),
                        )
                if collection_id is not None:
                    if remove_collection:
                        db.execute(
                            "DELETE FROM members WHERE collection_id=? AND font_id=?",
                            (collection_id, font_id),
                        )
                    else:
                        db.execute("INSERT OR IGNORE INTO members VALUES(?,?)", (collection_id, font_id))
        return {"updated": len(set(font_ids))}

    def collections(self):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute("""SELECT c.id,c.name,count(f.id) count FROM collections c
                LEFT JOIN members m ON m.collection_id=c.id LEFT JOIN fonts f ON f.id=m.font_id
                GROUP BY c.id ORDER BY c.id""")
            ]

    def create_collection(self, name):
        name = name.strip()
        if not name or len(name) > 60:
            raise ValueError("选集名称须为 1–60 字")
        try:
            with self.db() as db:
                cur = db.execute("INSERT INTO collections(name) VALUES(?)", (name,))
                return {"id": cur.lastrowid, "name": name, "count": 0}
        except sqlite3.IntegrityError:
            raise ValueError("已经存在同名选集") from None

    def delete_collection(self, collection_id):
        with self.db() as db:
            if not db.execute("DELETE FROM collections WHERE id=?", (collection_id,)).rowcount:
                raise KeyError("选集不存在")
        return {"deleted": collection_id}

    def _mark_catalog_deleted(self, relative):
        catalog = self.root / "字体目录.html"
        if not catalog.exists():
            return None
        with self.catalog_lock:
            page = catalog.read_text(encoding="utf-8-sig")
            match = re.search(r"const data=(.*?);const \$=", page, re.DOTALL)
            if not match:
                raise ValueError("字体目录格式无法识别")
            entries = json.loads(match.group(1))
            found = False
            for entry in entries:
                if entry.get("p", "").casefold() == relative.casefold():
                    entry["a"] = "已删除"
                    entry["deleted_at"] = time.time()
                    found = True
            if found:
                data = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).replace(
                    "<", "\\u003c"
                )
                page = page[: match.start(1)] + data + page[match.end(1) :]
                temporary = catalog.with_suffix(".delete.tmp")
                try:
                    temporary.write_text(page, encoding="utf-8")
                    os.replace(temporary, catalog)
                finally:
                    temporary.unlink(missing_ok=True)
            return found

    def delete_font(self, font_id):
        row = self.get(font_id)
        relative = PurePosixPath(row["path"])
        if len(relative.parts) < 2 or relative.parts[0] != "完整包":
            raise ValueError("只能删除完整包中已索引的字体文件")
        path = self.path(font_id)
        if path.is_symlink():
            raise ValueError("不支持删除符号链接字体")
        path.unlink()
        with self.db() as db:
            db.execute("DELETE FROM members WHERE font_id=?", (font_id,))
            db.execute("DELETE FROM annotations WHERE font_id=?", (font_id,))
            db.execute("DELETE FROM fonts WHERE id=?", (font_id,))
        result = {"deleted": font_id, "path": row["path"], "warning": None}
        try:
            updated = self._mark_catalog_deleted(row["path"])
            if updated is False:
                result["warning"] = "字体已删除，但字体目录中未找到对应记录"
        except (OSError, ValueError) as exc:
            logger.warning("Font deleted but catalog update failed: %s", exc)
            result["warning"] = "字体已删除；目录标记更新失败，快速刷新仍会忽略缺失文件"
        return result

    def recognition_candidates(self):
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute("SELECT id,path,family,filename,format FROM fonts ORDER BY id")
            ]

    def status(self):
        with self.db() as db:
            total = db.execute("SELECT count(*) FROM fonts").fetchone()[0]
            favorites = db.execute(
                "SELECT count(*) FROM annotations a JOIN fonts f ON f.id=a.font_id WHERE favorite=1"
            ).fetchone()[0]
            settings = dict(db.execute("SELECT key,value FROM settings").fetchall())
            facets = {
                key: [
                    dict(r)
                    for r in db.execute(
                        f"SELECT {key} name,count(*) count FROM fonts GROUP BY {key} ORDER BY count DESC"
                    )
                ]
                for key in ("vendor", "language", "format")
            }
            tags = [
                dict(r)
                for r in db.execute("""SELECT j.value name,count(*) count
                FROM annotations a JOIN fonts f ON f.id=a.font_id, json_each(a.tags) j
                GROUP BY j.value ORDER BY count DESC""")
            ]
        return {
            "root": str(self.root),
            "connected": self.root.is_dir(),
            "total": total,
            "favorites": favorites,
            "facets": facets,
            "tags": tags,
            "collections": self.collections(),
            "indexed_at": float(settings.get("indexed_at", 0)),
            "index_source": settings.get("index_source"),
            "indexing": self.indexing,
            "index_error": self.index_error,
        }

    def inbox_status(self):
        path = self.root / "_自动入库/运行状态.json"
        try:
            worker = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            worker = {"phase": "无法读取入库状态", "updated_at": None}
        inbox = self.root / "待入库"
        try:
            pending = [
                {"name": p.name, "directory": p.is_dir()}
                for p in inbox.iterdir()
                if not p.name.startswith(".")
            ][:100]
        except OSError:
            pending = []
        return {"worker": worker, "pending": pending, "inbox": str(inbox)}

    @staticmethod
    def open_font(path, face=0):
        with path.open("rb") as stream:
            header = stream.read(12)
        count = struct.unpack(">I", header[8:12])[0] if header[:4] == b"ttcf" else 1
        if not 0 <= face < count:
            raise ValueError(f"face 超出范围，该文件有 {count} 个字面")
        return TTFont(
            path,
            fontNumber=face if count > 1 or header[:4] == b"ttcf" else -1,
            lazy=True,
            recalcBBoxes=False,
            recalcTimestamp=False,
        ), count

    def details(self, font_id, face=0, text=""):
        if len(text) > 2000:
            raise ValueError("检测文本最多 2000 字")
        result = self.get(font_id)
        path = self.path(font_id)
        with self.open_font(path, face)[0] as font:
            names = font["name"]
            cmap = font.getBestCmap() or {}
            os2 = font.get("OS/2")
            result.update(
                full_name=names.getDebugName(4),
                family_name=names.getDebugName(16) or names.getDebugName(1),
                style=names.getDebugName(17) or names.getDebugName(2),
                postscript=names.getDebugName(6),
                version=names.getDebugName(5),
                copyright=names.getDebugName(0),
                license=names.getDebugName(13),
                designer=names.getDebugName(9),
                manufacturer=names.getDebugName(8),
                glyph_count=font["maxp"].numGlyphs,
                character_count=len(cmap),
                weight=getattr(os2, "usWeightClass", None),
                variable="fvar" in font,
                axes=[
                    {"tag": a.axisTag, "min": a.minValue, "default": a.defaultValue, "max": a.maxValue}
                    for a in font["fvar"].axes
                ]
                if "fvar" in font
                else [],
                face=face,
                size_bytes=path.stat().st_size,
                missing_characters=list(
                    dict.fromkeys(c for c in text if not c.isspace() and ord(c) not in cmap)
                ),
            )
        with path.open("rb") as stream:
            header = stream.read(12)
        result["face_count"] = struct.unpack(">I", header[8:12])[0] if header[:4] == b"ttcf" else 1
        return result

    def preview_font(self, font_id, text, face=0):
        if not text or len(text) > 2000:
            raise ValueError("试字文本须为 1–2000 字")
        path = self.path(font_id)
        stat = path.stat()
        key = hashlib.sha256(
            f"{font_id}:{stat.st_size}:{stat.st_mtime_ns}:{face}:{''.join(sorted(set(text)))}".encode()
        ).hexdigest()
        cached = self.cache / (key + ".woff2")
        if cached.exists() and cached.with_suffix(".json").exists():
            return cached
        with self.render_lock:
            if cached.exists() and cached.with_suffix(".json").exists():
                return cached
            with self.open_font(path, face)[0] as font:
                cmap = font.getBestCmap() or {}
                missing = list(dict.fromkeys(c for c in text if not c.isspace() and ord(c) not in cmap))
                options = subset.Options()
                options.layout_features = ["*"]
                options.ignore_missing_glyphs = True
                sub = subset.Subsetter(options=options)
                sub.populate(text=text)
                sub.subset(font)
                font.flavor = "woff2"
                output = io.BytesIO()
                font.save(output)
                cached.with_suffix(".json").write_text(json.dumps(missing), encoding="utf-8")
                temp = cached.with_suffix(f".{threading.get_ident()}.tmp")
                temp.write_bytes(output.getvalue())
                temp.replace(cached)
        return cached

    def preview_image(self, font_id, text="山海之间，自有回响。", size=48, face=0):
        if not text or len(text) > 300 or not 12 <= size <= 120:
            raise ValueError("样张须为 1–300 字，字号 12–120")
        # Use the same extracted face/subset as the browser, including TTC and WOFF.
        preview = self.preview_font(font_id, text, face)
        missing = set(json.loads(preview.with_suffix(".json").read_text(encoding="utf-8")))
        if not any(not c.isspace() and c not in missing for c in text):
            raise ValueError("该字面不支持当前文本，请更换试字或使用 check_characters 查看缺字")
        with TTFont(preview) as font:
            font.flavor = None
            raw = io.BytesIO()
            font.save(raw)
        raw.seek(0)
        display = ImageFont.truetype(raw, size=size)
        lines = []
        for paragraph in text.split("\n"):
            line = ""
            for character in paragraph:
                if display.getlength(line + character) > 1120 and line:
                    lines.append(line)
                    line = ""
                line += character
            lines.append(line)
        line_height = round(size * 1.6)
        image = Image.new("RGB", (1200, max(180, line_height * len(lines) + 80)), "#ffffff")
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(lines):
            y = 40 + line_height * index
            draw.text((40, y), line, font=display, fill="#172034")
            for position, character in enumerate(line):
                if character in missing:
                    x = 40 + display.getlength(line[:position])
                    draw.rectangle(
                        (x + 2, y + size * 0.25, x + size * 0.5, y + size * 0.9),
                        outline="#9da9bd",
                        width=max(1, size // 30),
                    )
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
