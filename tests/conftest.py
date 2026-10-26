import json
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection, TTFont

from font_manager.config import Settings
from font_manager.library import Library


def make_font(path: Path, family="Test Sans", extra=""):
    chars = " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" + extra
    glyphs = [".notdef"] + [f"uni{ord(c):04X}" for c in chars]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({ord(c): f"uni{ord(c):04X}" for c in chars})
    outlines = {}
    for glyph in glyphs:
        pen = TTGlyphPen(None)
        if glyph != "uni0020":
            pen.moveTo((80, 0))
            pen.lineTo((80, 700))
            pen.lineTo((480, 700))
            pen.lineTo((480, 0))
            pen.closePath()
        outlines[glyph] = pen.glyph()
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics({g: (600, 0) for g in glyphs})
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


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "fonts"
    first = root / "完整包/测试厂商/西文/Test Sans/Test.ttf"
    second = root / "完整包/测试厂商/简体/测试中文/CJK.ttf"
    make_font(first)
    make_font(second, "测试中文", "山海之间")
    collection_path = root / "完整包/测试厂商/简体/合集/Test.ttc"
    collection_path.parent.mkdir(parents=True)
    collection = TTCollection()
    collection.fonts = [TTFont(first), TTFont(second)]
    collection.save(collection_path)
    collection.close()
    records = [
        {
            "p": path.relative_to(root).as_posix(),
            "a": "保留",
            "v": "测试厂商",
            "l": "西文" if path == first else "简体",
            "f": path.parent.name,
            "s": 4,
            "names": "别名",
        }
        for path in [first, second, collection_path]
    ]
    (root / "字体目录.html").write_text("<script>const data=" + json.dumps(records) + ";const $=1</script>")
    (root / "待入库").mkdir()
    service = Library(Settings(root=root, state=tmp_path / "state"))
    service.refresh()
    return service
