"""Build a reviewable plan; apply reversible, same-volume renames; verify identities.

No font data is rewritten and no file is unlinked. Selected decorative Latin
fonts are removed from the library into the sibling recovery folder on this NAS.
"""
from pathlib import Path
import argparse
import collections
import hashlib
import html
import json
import os
import re
import unicodedata
import concurrent.futures as futures
from functools import lru_cache

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
RECOVERY = ROOT.parent / '字体清理可恢复备份_20261021'
FONT_EXT = {'.otf', '.ttf', '.ttc', '.otc', '.woff', '.woff2', '.fon'}

def read_rows(name):
    if name == '原始清单.jsonl' and (OUT/'原始清单_大小写校正.jsonl').exists():
        name='原始清单_大小写校正.jsonl'
    return [json.loads(line) for line in (OUT / name).read_text(encoding='utf-8').splitlines() if line]

def dump(name, rows):
    with (OUT / name).open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')

def safe(value, limit=80):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip(' .') or '未命名'
    if re.fullmatch(r'CON|PRN|AUX|NUL|COM\d|LPT\d', value, re.I):
        value = '_' + value
    if len(value) > limit:
        value = value[:limit-9].rstrip() + '_' + hashlib.sha1(value.encode()).hexdigest()[:8]
    return value

def norm(value):
    return re.sub(r'[\s_\-（）()]+', '', unicodedata.normalize('NFKC', value).casefold())

def reference_index():
    stack = []
    exact = collections.defaultdict(list)
    normalized = collections.defaultdict(list)
    for line in (OUT / '参考站目录.txt').read_text(encoding='utf-8-sig').splitlines():
        match = re.search('[├└]───', line)
        if match:
            depth = match.start() // 4
            stack = stack[:depth] + [line[match.end():].strip()]
        elif stack and stack[0] == '完整包':
            filename = line.lstrip('│ ').strip()
            if Path(filename).suffix.lower() in FONT_EXT:
                record = stack[1:]
                exact[filename.casefold()].append(record)
                normalized[norm(filename)].append(record)
    return exact, normalized

EXACT, NORMALIZED = reference_index()

def refmatch(filename):
    hits = EXACT.get(filename.casefold(), [])
    if not hits:
        cleaned = re.sub(r'^\d{4,5}[ _]+', '', filename)
        cleaned = re.sub(r'(?:_\d+| \(\d+\))(?=\.[^.]+$)', '', cleaned)
        hits = NORMALIZED.get(norm(cleaned), [])
    # Only use a unique manufacturer/language assignment.
    keys = {tuple(h[:3] if h and h[0] == '其他' else h[:2]) for h in hits}
    return hits[0] if hits and len(keys) == 1 else None

VENDORS = {
    'BDFZ':'Founder Type（方正）','DYNA':'DynaFont（华康）','MRSW':'Morisawa（森泽）',
    'FWKS':'Fontworks','ARPH':'Arphic（文鼎）','HNYI':'Hanyi Fonts（汉仪）',
    'HANY':'Hanyi Fonts（汉仪）','HY':'Hanyi Fonts（汉仪）','ADBE':'Adobe',
    'MS':'Microsoft（微软）','MONO':'Monotype（蒙纳）','LINO':'Linotype',
    'SINO':'SinoType（华文）','TT':'TensenType（腾祥）','DS':'SCREEN（屏显字库）',
    'DNP':'DNP（大日本印刷）','TB':'TypeBank','JY':'Jiyukobo（字游工房）',
    'OCC':'Occupant Fonts','CDK':'Cadson Demak','DB':'DB Designs','KTD':'Katatrad Aksorn',
    'RST':'Rosetta','SAND':'Sandoll','YDI':'YoonDesign','RICO':'RICOH（理光）',
    'CANO':'Canon（佳能）','EPSN':'EPSON（爱普生）','URW':'URW',
    'BITS':'Bitstream','ITC':'ITC','FBI':'Font Bureau','MTY':'Motoya',
    'M+':'M PLUS','LXGW':'LXGW（霞鹜）','WQYF':'文泉驿','TSAN':'Tsanger（仓耳）',
    'CYEL':'长城字库','ZYEC':'中易字库','LETR':'Letraset','GOOG':'Google（谷歌）',
    'P3LV':'Pal3love（字体改版）','SSCF':'Summitsoft','CYAN':'Cyano Hao（字体改版）'
}
EXISTING = {
    'Morisawa':'Morisawa（森泽）','Arphic Types':'Arphic（文鼎）',
    '写研 Sha-Ken':'Sha-Ken（写研）','上海技研 SPTI':'SPTI（上海技研）',
    '昭和書体 SHOWASHOTAI':'Showa（昭和书体）','字游工房 JIYUKOBO':'Jiyukobo（字游工房）',
    'SCREEN GA':'SCREEN（屏显字库）','SANDOLL':'Sandoll'
}
BRANDS = [
    (r'阿里|Alibaba','Alibaba（阿里巴巴）'),(r'寒蝉|寒蟬|Chill','寒蝉字型'),
    (r'上首|ShangShou','上首造字'),(r'字由','字由'),(r'汉鼎|漢鼎','汉鼎字库'),
    (r'汉真|漢真','汉真字库'),(r'字心坊','字心坊'),(r'华光|華光','华光字库'),
    (r'三极|三極','三极字库'),(r'义启|義啟','义启字库'),(r'汉仪|漢儀','Hanyi Fonts（汉仪）'),
    (r'更纱|更紗|sarasa','Sarasa（更纱黑体）'),
    (r'大赦','大赦（字体改版）'), (r'夢回瓊華|梦回琼华','梦回琼华（字体改版）'),
    (r'霞鹜|霞鶩|LXGW','LXGW（霞鹜）'), (r'思源|Source.?Han','Adobe'),
    (r'\bNoto','Google（谷歌）'), (r'仓耳|倉耳|tsanger','Tsanger（仓耳）'),
    (r'造字工房|makefont|^MF(?-i:[A-Z])','MakeFont（造字工房）'),
    (r'方正|^FZ(?-i:[A-Z])|Founder','Founder Type（方正）'),
    (r'汉仪|漢儀|^HY(?-i:[A-Z])|Hanyi','Hanyi Fonts（汉仪）'),
    (r'华康|華康|DynaFont|DynaComware|^DF(?-i:[A-ZＧＰ])|^ＤＦ','DynaFont（华康）'),
    (r'文鼎|^AR(?-i:[A-Z])|Arphic','Arphic（文鼎）'),
    (r'腾祥|騰祥|tensentype','TensenType（腾祥）'),
    (r'华文|華文|SinoType','SinoType（华文）'), (r'蒙纳|蒙納','Monotype（蒙纳）'),
    (r'字魂','字魂'), (r'汉呈|漢呈','汉呈字库'), (r'汉标|漢標','汉标字库'),
    (r'汉真|漢真','汉真字库'),(r'锐字|銳字','锐字字库'), (r'文泉驿|文泉驛','文泉驿'),
    (r'长城|長城','长城字库'), (r'叶根友|葉根友','叶根友'),
    (r'迷你','迷你字库'),(r'超研泽|超研澤','超研泽'),(r'金梅','金梅字库'),
    (r'新蒂|Senty','Senty（新蒂）'),(r'本墨','本墨字库'), (r'851','851（手写字体）'),
    (r'小塚|KozGo|KozMin|^Adobe','Adobe'),
    (r'白舟|Hakusyu|Hakushu','Hakusyu（白舟书体）'),
    (r'^FOT-|^FOT[ -]|Fontworks','Fontworks'),
    (r'^A-OTF|^AP-OTF|^G-OTF|^U-OTF|Morisawa','Morisawa（森泽）'),
    (r'游ゴシック|游明朝|YuGothic|YuMincho|Jiyukobo','Jiyukobo（字游工房）'),
    (r'Hiragino|冬青|ヒラギノ','SCREEN（屏显字库）'),
    (r'IPAex|IPAGothic|IPAMincho','IPA'), (r'^Mplus|^M PLUS|^M\+','M PLUS')
]

def names(row, ids=(1,4,6,16)):
    return [n for f in row['faces'] for key in ids for n in f['names'].get(str(key), [])]

def vendor(row, ref):
    parts = Path(row['source']).parts
    filename = parts[-1]
    if len(parts) > 1:
        return EXISTING.get(parts[0], parts[0]), '原目录'
    # Explicit author/derivative names take priority over inherited font metadata.
    for pattern, label in BRANDS:
        if re.search(pattern, filename, re.I):
            return label, '文件名标识'
    if ref and ref[0] != '慎用':
        if ref[0] != '其他':
            return ref[0], '参考目录匹配'
        if len(ref) > 2:
            return ref[2], '参考目录匹配'
    all_names = ' | '.join(names(row))
    for pattern, label in BRANDS:
        if re.search(pattern, all_names, re.I):
            return label, '字体内部名称'
    vendors = {f.get('vendor','').replace('\x00','').strip().upper() for f in row['faces']}
    if len(vendors) == 1 and next(iter(vendors)) in VENDORS:
        return VENDORS[next(iter(vendors))], 'OS2 厂商标识'
    maker = ' | '.join(names(row,(8,)))
    known = [(r'Microsoft','Microsoft（微软）'),(r'Adobe','Adobe'),(r'Monotype','Monotype（蒙纳）'),
             (r'Linotype','Linotype'),(r'Occupant','Occupant Fonts'),(r'Summitsoft','Summitsoft'),
             (r'Pal3love','Pal3love（字体改版）'),(r'梦回琼华|夢回瓊華','梦回琼华（字体改版）'),
             (r'Superior Type','Superior Type'),(r'SinoType','SinoType（华文）'),
             (r'justfont','justfont'),(r'NIS Corporation','NIS Font'),(r'Positype','Positype'),
             (r'Ascender','Ascender'),(r'QiSiZhonghe|奇思众和','Aa 字库'),
             (r'HanMeiHutong|翰美互通','字体管家'),(r'WenYue|文悦','文悦字库'),
             (r'Toppan|凸版','Toppan（凸版印刷）'),(r'YEFONT|也字工厂','也字工厂'),
             (r'SolidZORO','SolidZORO'),(r'文道|wendao','文道字库'),
             (r'Nowar','Nowar（字体改版）'),(r'Rui Xian|锐线','锐线字库'),
             (r'Lexend','Lexend'),(r'Sounso','上首造字')]
    for pattern,label in known:
        if re.search(pattern,maker,re.I): return label,'厂商名称字段'
    return '其他（厂商待确认）', '待确认'

LANGS = {'简体','繁体','简繁','日文','韩文','英文','其他','CJK','地区版','符号','图案','假名','输简得繁'}
LANG_ALIAS = {'英文':'西文','其他':'其他文字','图案':'符号与图案','符号':'符号与图案',
              'CJK':'CJK 多语言','地区版':'CJK 地区版'}
def language(row, ref, maker):
    parts = Path(row['source']).parts
    paths = {'Japanese':'日文','Korean':'韩文','Latin':'西文','Simplified Chinese':'简体',
             'Traditional Chinese':'繁体','Others':'其他文字'}
    for part in parts[:-1]:
        if part in paths: return paths[part], '原目录'
    text = unicodedata.normalize('NFKC', ' | '.join([parts[-1]]+names(row)))
    # The source repository language labels are a useful exact-name reference.
    if ref:
        for part in ref[1:]:
            if part in LANGS: return LANG_ALIAS.get(part,part), '参考目录'
    if re.search(r'Source.?Han|思源|\bNoto.*CJK', text, re.I):
        if re.search(r'(?:Sans|Serif)(?:CN|TW|HK|JP|KR)(?:\b|-)',text): return 'CJK 地区版','名称'
        return 'CJK 多语言','名称'
    if re.search(r'GB18030|GBK|简繁|簡繁|简体中文|簡體中文', text, re.I): return '简繁','名称标称'
    if re.search(r'繁体|繁體|BIG.?5|(?:-|_)B5\b|\b(?:TC|HK|TW|HKSCS)\b',text,re.I): return '繁体','名称'
    if re.search(r'简体|簡體|GB2312|(?:-|_)GB\b|\b(?:SC|CN)\b',text,re.I): return '简体','名称'
    if re.search(r'\b(?:JP|JPN)\b|仮名|かな|カナ|Pr[56]N|Pro[56]N',text,re.I): return '日文','名称'
    cps = 0
    for f in row['faces']: cps |= f.get('codepages',0)
    cjk = [k for k,bit in [('日文',17),('简体',18),('繁体',20),('韩文',19),('韩文',21)] if cps & (1<<bit)]
    cjk = set(cjk)
    if cjk == {'简体','繁体'}: return '简繁','字符集标记'
    if len(cjk) == 1: return next(iter(cjk)), '字符集标记'
    if cjk:
        # Makers frequently include every CJK code-page bit; localized family names help.
        if maker in {'Morisawa（森泽）','Fontworks','Jiyukobo（字游工房）','TypeBank','Showa（昭和书体）','Sha-Ken（写研）','DNP（大日本印刷）'}:
            return '日文','厂商及字符集'
        if maker in {'Founder Type（方正）','Hanyi Fonts（汉仪）','SinoType（华文）','TensenType（腾祥）','Tsanger（仓耳）','MakeFont（造字工房）'}:
            return '中文（待分简繁）','厂商及字符集'
        return 'CJK 多语言','字符集标记'
    if cps & (1<<16): return '泰文','字符集标记'
    if cps & ((1<<5)|(1<<6)): return '希伯来文与阿拉伯文','字符集标记'
    if any('\u4e00' <= ch <= '\u9fff' for ch in ' '.join(names(row,(1,16)))):
        return '中文或日文（待确认）','名称待确认'
    if cps & 1: return '西文','字符集标记'
    return '语言待确认','缺少标记'

STYLE = re.compile(r'(?:[ _-]+(?:Extra|Ultra|Semi|Demi)?[ _-]*(?:Bold|Light|Heavy|Medium|Regular|Roman|Normal|Italic|Oblique|Thin|Black|Book)|[ _-]*[Ww]\d+|[ _-]+[ELMRBDHU])$', re.I)
def family(row):
    values = []
    for face in row['faces']:
        candidates = face['names'].get('16') or face['names'].get('1') or []
        clean = [n for n in candidates if not any(c in n for c in '�±¥æ')]
        cjk = [n for n in clean if any('\u3040' <= c <= '\u9fff' for c in n)]
        value = (cjk[-1] if cjk else clean[0] if clean else Path(row['source']).stem)
        last = None
        while value and value != last:
            last = value
            value = STYLE.sub('',value).strip()
        values.append(value or candidates[0] if candidates else value)
    unique = list(dict.fromkeys(values))
    if not unique: return safe(Path(row['source']).stem)
    if len(unique)>1: return safe(Path(row['source']).stem + '（字体合集）')
    return safe(unique[0])
