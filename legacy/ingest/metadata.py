"""Read-only font inventory. Run with Python + fontTools; does not load glyph outlines."""
import concurrent.futures as futures
import collections
import json
import logging
import os
from pathlib import Path
import struct
import time

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
logging.getLogger('fontTools').setLevel(logging.ERROR)
EXTS = {'.ttf', '.otf', '.ttc', '.otc', '.woff', '.woff2', '.fon'}

def inventory(path):
    stat = path.stat()
    row = dict(source=path.relative_to(ROOT).as_posix(), size=stat.st_size,
               mtime_ns=stat.st_mtime_ns, file_id=stat.st_ino, device=stat.st_dev,
               attributes=getattr(stat, 'st_file_attributes', 0), faces=[])
    if path.suffix.lower() not in EXTS:
        row['nonfont'] = True
        return row
    try:
        with path.open('rb') as stream:
            header = stream.read(12)
        is_collection = header[:4] == b'ttcf'
        count = struct.unpack('>I', header[8:12])[0] if is_collection else 1
        if not 1 <= count <= 1000:
            raise ValueError(f'Invalid collection size: {count}')
        for index in range(count):
            with path.open('rb') as raw, TTFont(raw, fontNumber=index if is_collection else -1, lazy=True,
                        recalcBBoxes=False, recalcTimestamp=False) as font:
                names = collections.defaultdict(list)
                lang_ids = set()
                for rec in font['name'].names:
                    if rec.nameID not in {0, 1, 2, 4, 6, 8, 9, 16, 17}:
                        continue
                    try:
                        val = rec.toUnicode().replace('\x00', '').strip()
                    except Exception:
                        continue
                    if not val:
                        continue
                    if rec.nameID in {0, 8, 9}:
                        val = val[:600]
                    if val not in names[str(rec.nameID)]:
                        names[str(rec.nameID)].append(val)
                    if rec.platformID == 3:
                        lang_ids.add(rec.langID)
                face = dict(names=dict(names), name_languages=sorted(lang_ids))
                if 'OS/2' in font:
                    os2 = font['OS/2']
                    vendor = os2.achVendID
                    if isinstance(vendor, bytes):
                        vendor = vendor.decode('ascii', errors='replace')
                    face.update(vendor=vendor.strip(), weight=os2.usWeightClass,
                                codepages=getattr(os2, 'ulCodePageRange1', 0),
                                unicode_ranges=[getattr(os2, f'ulUnicodeRange{i}', 0) for i in range(1, 5)])
                row['faces'].append(face)
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
    return row
