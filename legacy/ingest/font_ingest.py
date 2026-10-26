"""Font ingest for fnOS/Linux or Windows. Exact deduplication and durable moves.

Single writer lock lives on the share; SQLite lives on the worker host, not on SMB.
Input files are never deleted. A read lease prevents processing an open writer.
"""
import argparse
import collections
import contextlib
import ctypes
from ctypes import wintypes
import datetime as dt
import errno
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import signal
import stat as stat_module
import struct
import sqlite3
import sys
import time
import unicodedata
import uuid

import classification
import health
import metadata
from fontTools.ttLib import TTFont
from PIL import ImageFont

APP = Path(__file__).resolve().parent
EXTENSIONS = {'.ttf', '.otf', '.ttc', '.otc', '.woff', '.woff2', '.fon'}
SUPPORTED = {'.ttf', '.otf', '.ttc', '.otc'}
PARTIAL = {'.part', '.partial', '.tmp', '.crdownload', '.download'}
REPARSE = 0x400
LEASE_SIGNALS = 0
STOP_REQUESTED = False


def lease_signal(signum, frame):
    global LEASE_SIGNALS
    LEASE_SIGNALS += 1


def stop_signal(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


class Lease:
    def __init__(self, fd=None):
        self.fd = fd
        self.signals = LEASE_SIGNALS

    def verify(self):
        if self.fd is not None:
            import fcntl
            if LEASE_SIGNALS != self.signals or fcntl.fcntl(self.fd, fcntl.F_GETLEASE) != fcntl.F_RDLCK:
                raise OSError('有其他程序请求写入该文件，等复制稳定后再试')


def no_replace_rename(source, target):
    if os.name == 'nt':
        source.rename(target)
    else:
        libc=ctypes.CDLL(None,use_errno=True)
        rename=libc.renameat2
        rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        rename.restype=ctypes.c_int
        if rename(-100,os.fsencode(source),-100,os.fsencode(target),1)!=0:
            error=ctypes.get_errno()
            raise OSError(error,os.strerror(error),str(target))


def is_link(path):
    value=path.lstat()
    return stat_module.S_ISLNK(value.st_mode) or bool(getattr(value,'st_file_attributes',0)&REPARSE)


def timestamp():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')


def rows(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def ensure_smb_attributes(path):
    """Supply Samba DOS metadata for files created outside SMB on this fnOS.

    Preserve existing attributes. Format follows Samba xattr_DosInfo5:
    https://github.com/samba-team/samba/blob/master/librpc/idl/xattr.idl
    """
    if os.name == 'nt':
        return
    try:
        os.getxattr(path, 'user.DOSATTRIB', follow_symlinks=False)
        return
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise
    value = path.stat()
    created = value.st_ctime_ns // 100 + 116444736000000000
    attributes = 0x10 if stat_module.S_ISDIR(value.st_mode) else 0x20
    data = struct.pack('<HHIIIQ', 0, 5, 5, 0x11, attributes, created)
    try:
        os.setxattr(path, 'user.DOSATTRIB', data, flags=os.XATTR_CREATE, follow_symlinks=False)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise


def atomic_json(path, value, smb_attributes=False):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex[:8] + '.tmp')
    with temp.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    if smb_attributes:
        ensure_smb_attributes(temp)
    os.replace(temp, path)


def identity(path):
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ino)


def fingerprint(path, full=True):
    before = identity(path)
    digest = hashlib.sha256()
    sample = hashlib.sha256()
    with path.open('rb') as stream:
        if full:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        for offset in sorted({0, max(0, before[0] // 2 - 32768), max(0, before[0] - 65536)}):
            stream.seek(offset)
            sample.update(stream.read(65536))
    if identity(path) != before:
        raise OSError('文件在读取过程中变化：' + str(path))
    return before, digest.hexdigest() if full else '', sample.hexdigest()


@contextlib.contextmanager
def read_lease(path):
    """Allow reads/renames, deny all concurrent writers, including existing ones."""
    if os.name != 'nt':
        import fcntl
        signal.signal(signal.SIGIO,lease_signal)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        acquired=False
        try:
            fcntl.fcntl(fd,fcntl.F_SETLEASE,fcntl.F_RDLCK)
            acquired=True
            lease=Lease(fd)
            yield lease
        finally:
            if acquired:fcntl.fcntl(fd,fcntl.F_SETLEASE,fcntl.F_UNLCK)
            os.close(fd)
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield Lease()
    finally:
        close(handle)


@contextlib.contextmanager
def single_writer(path):
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            lock=lambda:msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            unlock=lambda:msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock=lambda:fcntl.lockf(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB,1,0)
            unlock=lambda:fcntl.lockf(stream.fileno(),fcntl.LOCK_UN,1,0)
        try:
            lock()
        except OSError:
            raise RuntimeError('已经有自动入库程序在运行。') from None
        try:
            yield
        finally:
            stream.seek(0)
            unlock()


class StableInbox:
    def __init__(self, seconds):
        self.seconds = seconds
        self.observed = {}

    def ready(self, snapshots, now):
        self.observed = {k:v for k,v in self.observed.items() if k in snapshots}
        ready = []
        for key, files in snapshots.items():
            signature = tuple(sorted((r, *stat) for r, stat in files))
            previous = self.observed.get(key)
            if previous is None or previous[0] != signature:
                self.observed[key] = (signature, now)
            elif now - previous[1] >= self.seconds:
                ready.append((key, files))
        return ready


class Importer:
    def __init__(self, config, state):
        self.config = config
        self.root = Path(config['root']).resolve(strict=True)
        self.app = self.root / '_自动入库'
        self.ensure_directory(self.app)
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.inbox = self.root / config['inbox']
        self.library = self.root / config['library']
        for key in ['inbox', 'library', 'duplicates', 'review', 'attachments']:
            relative = Path(config[key])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Invalid configured directory: ' + key)
            directory = self.root / relative
            self.checked(directory)
            self.ensure_directory(directory)
        self.reports = self.app / '入库记录'
        self.ensure_directory(self.reports)
        for report in self.reports.iterdir():
            if report.is_file():
                self.visible(self.checked(report))
        self.db = sqlite3.connect(self.state / 'index.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS files(
            path TEXT PRIMARY KEY COLLATE NOCASE,size INTEGER,mtime INTEGER,inode TEXT,
            sample TEXT,sha TEXT,names TEXT DEFAULT '[]');
          CREATE INDEX IF NOT EXISTS file_size ON files(size);
          CREATE TABLE IF NOT EXISTS operations(
            id TEXT PRIMARY KEY,batch TEXT,source TEXT,target TEXT,action TEXT,sha TEXT,
            size INTEGER,record TEXT,state TEXT);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        ''')
        self.db.commit()
        metadata.ROOT = self.root
        health.ROOT = self.root
        self.stable = StableInbox(config['stable_seconds'])
        self.stop_file = self.state / 'stop.request'
        self.start_time = timestamp()
        self.last_batch = None
        self.errors = []
        self.last_library_refresh = 0

    def close(self):
        self.db.close()

    def checked(self, path):
        """Validate absolute containment and reject links/junctions before mutation."""
        path = Path(path)
        path.relative_to(self.root)
        path.resolve(strict=False).relative_to(self.root)
        current = path
        while current != self.root:
            try:
                if is_link(current):
                    raise ValueError('不处理符号链接或目录联接：' + str(current))
            except FileNotFoundError:
                pass
            current = current.parent
        return path

    def relative(self, path):
        return path.relative_to(self.root).as_posix()

    def visible(self, path):
        if self.config.get('smb_attributes', False):
            ensure_smb_attributes(path)

    def ensure_directory(self, directory):
        self.checked(directory).mkdir(parents=True, exist_ok=True)
        current = directory
        while current != self.root:
            self.visible(current)
            current = current.parent

    def display_path(self, relative):
        base = self.config.get('share_path')
        return base.rstrip('\\/') + '\\' + relative.replace('/', '\\') if base else str(self.root / relative)

    def status(self, phase, **extra):
        value = {'phase':phase, 'updated_at':timestamp(), 'started_at':self.start_time,
                 'pid':os.getpid(), 'computer':os.environ.get('COMPUTERNAME', os.environ.get('HOSTNAME','')),
                 'inbox':self.display_path(self.config['inbox']), 'host_root':self.config.get('host_path',str(self.root)),
                 'library_files':self.db.execute('SELECT count(*) FROM files').fetchone()[0],
                 'last_batch':self.last_batch, **extra}
        atomic_json(self.state / 'status.json', value)
        try:
            atomic_json(self.app / '运行状态.json', value, self.config.get('smb_attributes', False))
        except OSError:
            pass

    def walk(self, directory):
        found = []
        seen = set()
        for parent, directories, files in os.walk(directory, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
            for name in list(directories):
                p = Path(parent) / name
                if is_link(p):
                    raise ValueError('目录包含链接，留在原位：' + str(p))
            for name in files:
                p = Path(parent) / name
                if is_link(p):
                    raise ValueError('文件是链接，留在原位：' + str(p))
                rel = self.relative(p)
                if rel.casefold() in seen:
                    raise ValueError('存在仅大小写不同的路径，留在原位：' + rel)
                seen.add(rel.casefold())
                found.append((rel, identity(p)))
        return found

    def refresh(self):
        self.status('正在更新字体库索引')
        files = [(p,s) for p,s in self.walk(self.library) if Path(p).suffix.lower() in EXTENSIONS]
        old = {r['path'].casefold():dict(r) for r in self.db.execute('SELECT * FROM files')}
        fresh = []
        for path, stat in files:
            previous = old.get(path.casefold(), {})
            same = (previous.get('size'), previous.get('mtime'), previous.get('inode')) == (stat[0], stat[1], str(stat[2]))
            fresh.append((path,stat[0],stat[1],str(stat[2]),previous.get('sample','') if same else '',
                          previous.get('sha','') if same else '',previous.get('names','[]') if same else '[]'))
        with self.db:
            self.db.execute('DELETE FROM files')
            self.db.executemany('INSERT INTO files VALUES(?,?,?,?,?,?,?)', fresh)
        if not self.db.execute("SELECT 1 FROM settings WHERE key='initial_names'").fetchone():
            audit = self.root / '_字体整理记录/字体健康检查_20261021/结构扫描.jsonl'
            plan = self.root / '_字体整理记录/去重与西文精简_20261021/执行计划.jsonl'
            signatures = {r['source'].casefold():(r['size'],r['mtime_ns'],str(r['file_id'])) for r in rows(plan) if r['action']=='keep'}
            current = {r['path'].casefold():r for r in self.db.execute('SELECT * FROM files')}
            with self.db:
                for r in rows(audit):
                    item = current.get(r['source'].casefold())
                    if item and (item['size'],item['mtime'],item['inode']) == signatures.get(r['source'].casefold()):
                        names = sorted({n.casefold() for f in r['faces'] for n in f.get('names',{}).get('6',[])})
                        self.db.execute('UPDATE files SET names=? WHERE path=?',(json.dumps(names,ensure_ascii=False),item['path']))
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('initial_names','1')")
        self.last_library_refresh = time.monotonic()

    def snapshot(self):
        result = {}
        failures = []
        for entry in sorted(self.inbox.iterdir(), key=lambda p:p.name.casefold()):
            try:
                self.checked(entry)
                files = self.walk(entry) if entry.is_dir() else [(self.relative(entry),identity(entry))]
                if files and not any(Path(p).suffix.lower() in PARTIAL for p,_ in files):
                    result[entry.name] = files
            except (OSError, ValueError) as exc:
                failures.append(str(exc))
        return result, failures

    def duplicate(self, size, sha, sample):
        for candidate in list(self.db.execute('SELECT * FROM files WHERE size=?', (size,))):
            path = self.root / candidate['path']
            try:
                self.checked(path)
                with read_lease(path) as lease:
                    current = identity(path)
                    cached = (candidate['size'],candidate['mtime'],candidate['inode']) == (current[0],current[1],str(current[2]))
                    if current[0] != size:
                        continue
                    sample_value = candidate['sample'] if cached else ''
                    if not sample_value:
                        _,_,sample_value = fingerprint(path, full=False)
                        with self.db:
                            self.db.execute('UPDATE files SET size=?,mtime=?,inode=?,sample=?,sha=? WHERE path=?',
                                (current[0],current[1],str(current[2]),sample_value,'',candidate['path']))
                    if sample_value != sample:
                        continue
                    # Re-read the full retained file even if its previous hash is
                    # cached. A stale hash must never justify removing an input.
                    _, retained_sha, _ = fingerprint(path)
                    lease.verify()
                    with self.db:
                        self.db.execute('UPDATE files SET sha=? WHERE path=?',(retained_sha,candidate['path']))
                    if retained_sha == sha:
                        return candidate['path']
            except FileNotFoundError:
                with self.db:
                    self.db.execute('DELETE FROM files WHERE path=?',(candidate['path'],))
            # Other I/O errors fail this input safely; do not silently miss an
            # unreadable possible duplicate or write into an unavailable share.
        return None

    def classify(self, path, info):
        # Never interpret an arbitrary dropped folder name as a manufacturer.
        row = {**info,'source':path.name}
        reference = classification.refmatch(path.name)
        vendor,basis = classification.vendor(row,reference)
        if reference and reference[0] == '慎用':
            vendor = '改版字体（参考包标注慎用）'
        language,lbasis = classification.language(row,reference,vendor)
        family = classification.family(row)
        # Correct misleading OS/2 flags for fonts labelled Western/unknown.
        if language in {'西文','语言待确认','其他文字','中文或日文（待确认）','中文（待分简繁）','希伯来文与阿拉伯文'}:
            scripts = collections.Counter()
            for index in range(len(info['faces'])):
                with TTFont(path,fontNumber=index,lazy=True) as font:
                    for cp in set(font.getBestCmap() or {}):
                        if 0x3400<=cp<=0x9fff or 0x20000<=cp<=0x323af:scripts['CJK']+=1
                        elif 0x3040<=cp<=0x30ff:scripts['KANA']+=1
                        elif 0xac00<=cp<=0xd7af:scripts['KOREAN']+=1
                        elif 0x0e00<=cp<=0x0e7f:scripts['THAI']+=1
                        elif 0x0590<=cp<=0x05ff:scripts['HEBREW']+=1
                        elif 0x0600<=cp<=0x06ff:scripts['ARABIC']+=1
                        elif 0x0900<=cp<=0x097f:scripts['DEVANAGARI']+=1
                        elif 65<=cp<=90 or 97<=cp<=122:scripts['LATIN']+=1
            if scripts['CJK']>50:
                language = 'CJK 多语言' if scripts['CJK']>15000 and scripts['KANA']>50 else '日文' if scripts['KANA']>50 else '中文（待分简繁）'
            elif scripts['KOREAN']>50:language='韩文'
            elif scripts['KANA']>20:language='日文假名'
            else:
                for script,label in [('THAI','泰文'),('ARABIC','阿拉伯文'),('HEBREW','希伯来文'),('DEVANAGARI','天城文')]:
                    if scripts[script]>20:
                        language=label;break
                else:
                    if scripts['LATIN']>=26:language='西文'
            lbasis += '；实际字符映射校正'
        return {'vendor':vendor,'language':language,'family':family,'basis':basis+'；'+lbasis}

    def unique(self, path, sha):
        path = self.checked(path)
        # Reuse case-equivalent directories and avoid case-only filenames,
        # because the same Linux collection is also accessed through SMB.
        parent = self.root
        for component in path.parent.relative_to(self.root).parts:
            matches = [p for p in parent.iterdir() if p.name.casefold() == component.casefold()] if parent.exists() else []
            if len(matches) > 1:
                raise ValueError('目标目录存在大小写冲突：' + str(parent / component))
            parent = self.checked(matches[0] if matches else parent / component)
        path = parent / path.name
        stem = classification.safe(path.stem, 100)
        path = path.with_name(stem + path.suffix)
        occupied = {p.name.casefold() for p in parent.iterdir()} if parent.exists() else set()
        suffix = 0
        proposed = path
        while proposed.name.casefold() in occupied:
            suffix += 1
            proposed = path.with_name(stem+'__来源_'+sha[:12]+('' if suffix==1 else '_'+str(suffix))+path.suffix)
        self.checked(proposed)
        return proposed

    def journal(self, record):
        path = self.reports / (record['batch']+'.jsonl')
        with path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(record,ensure_ascii=False)+'\n')
            stream.flush();os.fsync(stream.fileno())
        self.visible(path)

    def finish(self, record, destination):
        self.visible(destination)
        if record['action']=='入库':
            stat = identity(destination)
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)',
                    (record['target'],stat[0],stat[1],str(stat[2]),record['sample'],record['sha256'],json.dumps(record['ps_names'],ensure_ascii=False)))
        with self.db:
            self.db.execute("UPDATE operations SET state='done' WHERE id=?",(record['id'],))
        self.journal({**record,'event':'completed','completed_at':timestamp()})

    def recover(self):
        for operation in list(self.db.execute("SELECT * FROM operations WHERE state='pending'")):
            record = json.loads(operation['record'])
            source = self.checked(self.root / record['source'])
            target = self.checked(self.root / record['target'])
            if not source.exists() and target.is_file():
                with read_lease(target):
                    if fingerprint(target)[1] != record['sha256']:
                        raise RuntimeError('中断恢复校验不一致：'+str(target))
                    self.finish(record,target)
            elif source.is_file() and not target.exists():
                with self.db:
                    self.db.execute("UPDATE operations SET state='cancelled' WHERE id=?",(operation['id'],))
                self.journal({**record,'event':'cancelled_before_move'})
            else:
                raise RuntimeError('中断恢复发现路径冲突，需要检查：'+str(source))

    def process_file(self, relative, expected, batch):
        source = self.checked(self.root / relative)
        source.relative_to(self.inbox)
        if identity(source) != tuple(expected):
            raise OSError('文件仍在变化，稍后重试：'+relative)
        with read_lease(source) as lease:
            stat,sha,sample = fingerprint(source)
            if stat != tuple(expected):
                raise OSError('复制尚未稳定：'+relative)
            lease.verify()
            record = {'id':uuid.uuid4().hex,'batch':batch,'time':timestamp(),'source':relative,
                      'size':stat[0],'sha256':sha,'sample':sample,'ps_names':[],'warnings':[]}
            suffix = source.suffix.lower()
            relative_input = source.relative_to(self.inbox)
            if suffix not in EXTENSIONS:
                record.update(action='附件',reason='非字体文件随批次保留')
                target = self.root/self.config['attachments']/batch/relative_input
            else:
                retained = self.duplicate(stat[0],sha,sample)
                if retained:
                    record.update(action='重复备份',retained=retained,reason='大小及完整 SHA-256 与库内文件一致')
                    target = self.root/self.config['duplicates']/batch/relative_input
                else:
                    info = metadata.inventory(source)
                    issues = [info['error']] if info.get('error') else []
                    if suffix not in SUPPORTED:
                        issues.append('该格式暂不自动入库，请人工检查或转换')
                    scan = None
                    if not issues:
                        scan = health.inspect({'source':relative,'original_source':source.name,'size':stat[0],'mtime_ns':stat[1]})
                        if scan.get('error'):issues.append(scan['error'])
                        for face in scan['faces']:
                            issues.extend(face['errors'])
                            record['warnings'].extend(face['compatibility'])
                            record['ps_names'].extend(n.casefold() for n in face.get('names',{}).get('6',[]))
                            try:
                                font = ImageFont.truetype(str(source),32,index=face['index'])
                                font.getmask('ABCxyz0123 中文字体测试漢字繁體 日本語あいうカキク')
                            except Exception as exc:
                                issues.append('试绘失败：'+str(exc))
                            if any('no Unicode cmap' in text for text in face['compatibility']):
                                issues.append('缺少 Unicode 字符映射，需要人工确认用途')
                    record['ps_names'] = sorted(set(record['ps_names']))
                    if issues:
                        record.update(action='待检查',reason='；'.join(dict.fromkeys(issues)))
                        target = self.root/self.config['review']/batch/relative_input
                    else:
                        classification_result = self.classify(source,info)
                        record.update(classification_result)
                        matches=[]
                        ps=set(record['ps_names'])
                        if ps:
                            for item in self.db.execute("SELECT path,names FROM files WHERE names!='[]'"):
                                if ps.intersection(json.loads(item['names'])):
                                    matches.append(item['path'])
                                    if len(matches)>=10:break
                        if matches:
                            record['warnings'].append('存在相同内部名称的其他版本；内容不同，均保留')
                            record['same_name_versions']=matches
                        record.update(action='入库',reason='新内容；关键结构检查和样本试绘通过')
                        target = self.library/classification.safe(record['vendor'])/classification.safe(record['language'])/classification.safe(record['family'])/source.name
            target = self.unique(target,sha)
            self.ensure_directory(target.parent)
            self.checked(target)
            if identity(source)!=stat:
                raise OSError('文件在处理过程中变化：'+relative)
            record['target']=self.relative(target)
            with self.db:
                self.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',
                    (record['id'],batch,relative,record['target'],record['action'],sha,stat[0],json.dumps(record,ensure_ascii=False),'pending'))
            self.journal({**record,'event':'planned'})
            # Windows rename is atomic on this share and fails if target exists.
            # The input stays protected from writers until the rename completes.
            lease.verify()
            no_replace_rename(source,target)
            if identity(target)!=stat:
                raise RuntimeError('移动后的文件标识发生变化，需要中断恢复：'+str(target))
            self.finish(record,target)
            return record

    def publish(self, batch):
        records = [json.loads(r['record']) for r in self.db.execute("SELECT record FROM operations WHERE batch=? AND state='done'",(batch,))]
        counts = dict(collections.Counter(r['action'] for r in records))
        lines=['# 自动入库记录','', '批次：'+batch,'', '统计：'+json.dumps(counts,ensure_ascii=False),'',
               '| 文件 | 结果 | 当前位置 | 依据 |','|---|---|---|---|']
        for r in records:
            from urllib.parse import quote
            lines.append('| '+r['source'].replace('|','\\|')+' | '+r['action']+' | ['+Path(r['target']).name.replace('|','\\|')+'](../../'+quote(r['target'],safe='/')+') | '+r['reason'].replace('|','\\|')+' |')
        report = self.reports/(batch+'.md')
        report.write_text('\n'.join(lines)+'\n',encoding='utf-8')
        self.visible(report)
        self.last_batch={'batch':batch,'counts':counts,'report':self.display_path(self.relative(self.reports/(batch+'.md')))}
        self.publish_catalog()

    def publish_catalog(self):
        body=[]
        recent=list(self.db.execute("SELECT record FROM operations WHERE state='done' ORDER BY rowid DESC LIMIT 2000"))
        for item in recent:
            r=json.loads(item['record'])
            from urllib.parse import quote
            href=quote(r['target'],safe='/')
            body.append('<tr><td>'+html.escape(r['time'])+'</td><td>'+html.escape(r['action'])+'</td><td><a href="'+href+'">'+html.escape(Path(r['target']).name)+'</a></td><td>'+html.escape(r['reason'])+'</td></tr>')
        page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>自动入库记录</title><style>body{font:16px system-ui;max-width:1400px;margin:40px auto;padding:0 24px;background:#f7f8fa;color:#202532}table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}a{color:#1754ad}input{padding:10px;width:360px;margin:16px 0}</style><h1>自动入库记录</h1><p>将字体文件夹放入「待入库」，复制稳定 60 秒后自动处理。此页列出最近 2,000 个文件；全部记录见 _自动入库/入库记录。</p><p><a href="字体目录.html">原字体目录</a> · <a href="_自动入库/运行状态.json">运行状态</a> · <a href="_自动入库/使用说明.md">使用说明</a></p><input id="q" placeholder="搜索文件、状态、原因"><table><thead><tr><th>时间</th><th>结果</th><th>文件</th><th>说明</th></tr></thead><tbody>'+''.join(body)+'</tbody></table><script>document.querySelector("#q").oninput=e=>{const q=e.target.value.toLowerCase();document.querySelectorAll("tbody tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))}</script></html>'
        target=self.root/'自动入库记录.html'
        temp=target.with_suffix('.tmp')
        temp.write_text(page,encoding='utf-8')
        self.visible(temp)
        os.replace(temp,target)
        self.update_main_catalog()

    def update_main_catalog(self):
        if not self.config.get('update_main_catalog',True):return
        target=self.root/'字体目录.html'
        if not target.exists():return
        page=target.read_text('utf-8')
        match=re.search(r'const data=(.*?);const \$=',page,re.S)
        if not match:
            logging.warning('Main catalog format unknown; separate import catalog updated.')
            return
        entries=json.loads(match.group(1))
        existing={e['p'].casefold():e for e in entries}
        available={}
        for item in self.db.execute("SELECT record FROM operations WHERE state='done' AND action='入库' ORDER BY rowid"):
            r=json.loads(item['record'])
            if (self.root/r['target']).is_file():
                available[r['target'].casefold()]=r
        for key,entry in existing.items():
            if entry.get('a')=='已删除' and key in available:
                r=available.pop(key)
                entry.pop('deleted_at',None)
                entry.update({'v':r['vendor'],'l':r['language'],'f':r['family'],
                    'n':Path(r['target']).name,'a':'保留','p':r['target'],'o':r['source'],
                    's':round(r['size']/1024),'names':'；'.join(r['ps_names']),
                    'r':r['reason'],'batch':r['batch']})
        for key,r in available.items():
            if key not in existing:
                entries.append({'v':r['vendor'],'l':r['language'],'f':r['family'],
                    'n':Path(r['target']).name,'a':'保留','p':r['target'],'o':r['source'],
                    's':round(r['size']/1024),'names':'；'.join(r['ps_names']),
                    'r':r['reason'],'batch':r['batch']})
        backup=self.app/'字体目录_自动入库启用前.html'
        if not backup.exists():
            with backup.open('x',encoding='utf-8') as stream:stream.write(page)
        self.visible(backup)
        data=json.dumps(entries,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')
        page=page[:match.start(1)]+data+page[match.end(1):]
        count=self.db.execute('SELECT count(*) FROM files').fetchone()[0]
        page=re.sub(r'<h1>字体目录</h1><p>.*?</p>',
            '<h1>字体目录</h1><p>按厂商 → 语言／字符集 → 字族整理。搜索支持文件名、原路径、字体内部名称。当前库内 '+str(count)+' 个字体；历史移出记录一并保留。</p>',page,count=1,flags=re.S)
        if 'id="auto-ingest-link"' not in page:
            page=page.replace('<input id="q"','<p id="auto-ingest-link">新增字体放入 <b>待入库</b>。<a href="自动入库记录.html">查看自动入库记录</a> · <a href="_自动入库/使用说明.md">使用说明</a></p><input id="q"',1)
        temp=target.with_suffix('.auto.tmp')
        temp.write_text(page,encoding='utf-8')
        self.visible(temp)
        os.replace(temp,target)

    def batch(self, ready):
        batch=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
        self.refresh()
        errors=[]
        try:
            for key,files in ready:
                for relative,expected in files:
                    if STOP_REQUESTED or self.stop_file.exists():break
                    self.status('正在入库',batch=batch,current=relative)
                    try:
                        self.process_file(relative,expected,batch)
                    except (OSError,ValueError) as exc:
                        errors.append({'source':relative,'error':str(exc)})
                        self.journal({'event':'deferred','batch':batch,'source':relative,'time':timestamp(),'error':str(exc)})
                    # RuntimeError / interrupted move stops processing until
                    # recover() has reconciled the pending journal operation.
                self.stable.observed.pop(key,None)
                entry=self.inbox/key
                if entry.is_dir():
                    for parent,dirs,files in os.walk(entry,topdown=False):
                        directory=self.checked(Path(parent))
                        try:directory.rmdir()
                        except OSError:pass  # Never remove a nonempty folder.
        finally:
            self.publish(batch)
        self.status('等待投递' if not errors else '部分文件等待重试',deferred=errors)
        return batch

    def run(self, once=False):
        if self.stop_file.exists():self.stop_file.unlink()
        with single_writer(self.app/'worker.lock'):
            self.recover()
            self.refresh()
            self.publish_catalog()
            self.status('等待投递',stable_seconds=self.config['stable_seconds'])
            while not self.stop_file.exists() and not STOP_REQUESTED:
                try:
                    # Recover any rename interrupted by a transient SMB failure.
                    self.recover()
                    snapshots,failures=self.snapshot()
                    ready=self.stable.ready(snapshots,time.monotonic())
                    if ready:self.batch(ready)
                    elif snapshots:self.status('等待复制稳定',pending_batches=list(snapshots),warnings=failures)
                    else:self.status('等待投递',warnings=failures)
                    if once and not snapshots:break
                    time.sleep(self.config['poll_seconds'])
                except Exception as exc:
                    logging.exception('Batch deferred')
                    self.status('连接或处理异常，稍后重试',error=str(exc))
                    if once:raise
                    time.sleep(self.config['retry_seconds'])
            self.status('已停止')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['watch','once','index','status','stop'])
    parser.add_argument('--config',type=Path,default=APP/'config.json')
    parser.add_argument('--state',type=Path)
    args=parser.parse_args()
    config=json.loads(args.config.read_text('utf-8-sig'))
    configured_state=os.environ.get('FONT_INGEST_STATE')
    state=args.state or (Path(configured_state) if configured_state else Path(os.environ.get('LOCALAPPDATA','/state'))/'FontIngest'/hashlib.sha256(config['root'].casefold().encode()).hexdigest()[:12])
    state.mkdir(parents=True,exist_ok=True)
    if args.command=='stop':
        (state/'stop.request').write_text(timestamp(),encoding='utf-8')
        return
    if args.command=='status':
        print((state/'status.json').read_text('utf-8') if (state/'status.json').exists() else '尚未启动')
        return
    logging.basicConfig(filename=state/'worker.log',level=logging.INFO,encoding='utf-8',format='%(asctime)s %(levelname)s %(message)s')
    if os.name!='nt':signal.signal(signal.SIGTERM,stop_signal)
    worker=Importer(config,state)
    try:
        if args.command=='index':
            with single_writer(worker.app/'worker.lock'):
                worker.recover();worker.refresh();worker.status('索引就绪')
        else:worker.run(once=args.command=='once')
    finally:worker.close()


if __name__=='__main__':
    main()
