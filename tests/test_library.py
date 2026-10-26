import io
import json

import pytest
from fontTools.ttLib import TTFont
from PIL import Image

from font_manager.library import Library


def test_refresh_preserves_organization_and_failed_refresh_keeps_index(library):
    font = library.search("Test Sans")["items"][0]
    collection = library.create_collection("字幕候选")
    library.annotate(
        [font["id"]], favorite=True, tags=["字幕", "待看"], note="测试备注", collection_id=collection["id"]
    )
    library.refresh()
    reloaded = Library(library.settings)
    assert reloaded.search(favorite=True)["total"] == 1
    assert reloaded.search(tag="字幕", collection=collection["id"])["total"] == 1
    assert reloaded.get(font["id"])["note"] == "测试备注"
    (library.root / "字体目录.html").write_text("incomplete write")
    with pytest.raises(ValueError, match="格式无法识别"):
        library.refresh()
    assert library.search()["total"] == 3
    assert library.get(font["id"])["tags"] == ["字幕", "待看"]


def test_batch_is_atomic_and_collection_removal_does_not_delete_font(library):
    font = library.search("Test Sans")["items"][0]
    with pytest.raises(KeyError):
        library.annotate([font["id"], "missing-id"], favorite=True)
    assert not library.get(font["id"])["favorite"]
    collection = library.create_collection("字幕")
    library.annotate([font["id"]], collection_id=collection["id"], tags=["Tag"])
    path = library.path(font["id"])
    library.delete_collection(collection["id"])
    assert path.exists()
    assert library.get(font["id"])["tags"] == ["Tag"]
    assert library.get(font["id"])["collections"] == []


def test_search_covers_unicode_names_and_filters_and_paginates(library):
    assert library.search("test sans", language="西文")["total"] == 1
    assert library.search("别名", format="TTC")["total"] == 1
    first = library.search(limit=2)
    second = library.search(limit=2, offset=2)
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}


def test_path_guard_rejects_traversal_and_symlink_escape(library, tmp_path):
    for value in ("../secret.ttf", "/etc/passwd", "完整包/../../secret.ttf", "完整包\\..\\secret.ttf"):
        with pytest.raises(ValueError):
            library.checked(value)
    outside = tmp_path / "outside.ttf"
    outside.write_bytes(b"outside")
    link = library.root / "完整包/link.ttf"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        library.checked("完整包/link.ttf")


def test_ttc_face_selection_coverage_and_subset_are_real(library):
    font = library.search(format="TTC")["items"][0]
    latin = library.details(font["id"], face=0, text="A山海")
    chinese = library.details(font["id"], face=1, text="A山海😀")
    assert latin["face_count"] == 2
    assert latin["missing_characters"] == ["山", "海"]
    assert chinese["missing_characters"] == ["😀"]
    preview = library.preview_font(font["id"], "A山海😀", face=1)
    with TTFont(preview) as result:
        assert {ord("A"), ord("山"), ord("海")} <= set(result.getBestCmap())
        assert ord("B") not in result.getBestCmap()
    assert json.loads(preview.with_suffix(".json").read_text()) == ["😀"]
    image = Image.open(io.BytesIO(library.preview_image(font["id"], "A山海", face=1)))
    assert image.format == "PNG" and image.width == 1200
    assert image.getextrema()[0][0] < 255  # Actual drawn glyphs, not a blank image.
    with pytest.raises(ValueError, match="不支持当前文本"):
        library.preview_image(font["id"], "山海", face=0)
    with pytest.raises(ValueError, match="face"):
        library.details(font["id"], face=2)


def test_full_scan_excludes_backups_and_reconciles_missing_files(library):
    removed = library.search("Test Sans")["items"][0]
    library.annotate([removed["id"]], favorite=True)
    library.path(removed["id"]).unlink()
    (library.root / "_入库重复备份").mkdir()
    (library.root / "_入库重复备份/ignored.ttf").write_bytes(b"not a font")
    assert library.refresh("scan")["indexed"] == 2
    assert library.search()["total"] == 2
    assert library.search(favorite=True)["total"] == 0


def test_delete_font_removes_file_organization_and_catalog_entry(library):
    font = library.search("Test Sans")["items"][0]
    collection = library.create_collection("待删除")
    library.annotate([font["id"]], favorite=True, tags=["删除"], collection_id=collection["id"])
    path = library.path(font["id"])

    result = library.delete_font(font["id"])

    assert result["warning"] is None
    assert not path.exists()
    assert library.search()["total"] == 2
    with pytest.raises(KeyError):
        library.get(font["id"])
    with library.db() as db:
        assert not db.execute("SELECT 1 FROM annotations WHERE font_id=?", (font["id"],)).fetchone()
        assert not db.execute("SELECT 1 FROM members WHERE font_id=?", (font["id"],)).fetchone()
    page = (library.root / "字体目录.html").read_text()
    entries = json.loads(page.split("const data=", 1)[1].split(";const $=", 1)[0])
    deleted = next(row for row in entries if row["p"] == font["path"])
    assert deleted["a"] == "已删除" and deleted["deleted_at"] > 0

    library.refresh()
    assert library.search()["total"] == 2
    with pytest.raises(KeyError):
        library.delete_font(font["id"])


def test_delete_font_rejects_unmanaged_paths(library):
    path = library.root / "outside.ttf"
    path.write_bytes(b"not empty")
    row = library.index_row("outside.ttf", "Outside", "Vendor", "西文", 9, "")
    with library.db() as db:
        db.execute("INSERT INTO fonts VALUES(?,?,?,?,?,?,?,?,?,?)", row)

    with pytest.raises(ValueError, match="完整包"):
        library.delete_font(row[0])
    assert path.read_bytes() == b"not empty"
