import contextlib
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from font_ingest import Importer, StableInbox, fingerprint, identity, main, read_lease, single_writer, no_replace_rename


def make_font(path, family='Inbox Test', revision=1):
    builder=FontBuilder(1000,isTTF=True)
    order=['.notdef','space','A','B','C','x','y','z','zero']
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({32:'space',65:'A',66:'B',67:'C',120:'x',121:'y',122:'z',48:'zero'})
    glyphs={}
    for name in order:
        pen=TTGlyphPen(None)
        if name!='space':
            pen.moveTo((50,0));pen.lineTo((500+revision,0));pen.lineTo((250,700));pen.closePath()
        glyphs[name]=pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name:(600,0) for name in order})
    builder.setupHorizontalHeader(ascent=800,descent=-200)
    builder.setupNameTable({'familyName':family,'styleName':'Regular','uniqueFontIdentifier':family+str(revision),
        'fullName':family+' Regular','psName':family.replace(' ','')+'-Regular','version':'Version 1.'+str(revision)})
    builder.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=200,achVendID='TEST')
    builder.setupPost();builder.setupMaxp()
    path.parent.mkdir(parents=True,exist_ok=True)
    builder.save(path)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='font-ingest-test-')
        self.root=Path(self.tmp.name)/'Fonts';self.root.mkdir()
        self.cfg={'root':str(self.root),'inbox':'待入库','library':'完整包','duplicates':'_入库重复备份',
                  'review':'_入库待检查','attachments':'附件与原有脚本/自动入库附件','stable_seconds':60,'poll_seconds':1,'retry_seconds':1,
                  'smb_attributes':hasattr(os,'getxattr')}
        self.worker=Importer(self.cfg,Path(self.tmp.name)/'state')

    def tearDown(self):
        self.worker.close();self.tmp.cleanup()

    def process(self,path,batch='test'):
        return self.worker.process_file(self.worker.relative(path),identity(path),batch)

    def test_stability_resets_on_size_change(self):
        s=StableInbox(60);first={'pack':[('待入库/pack/a.ttf',(10,1,1))]}
        self.assertFalse(s.ready(first,0));self.assertFalse(s.ready(first,59))
        self.assertEqual(len(s.ready(first,60)),1)
        second={'pack':[('待入库/pack/a.ttf',(11,2,1))]}
        self.assertFalse(s.ready(second,61));self.assertFalse(s.ready(second,120));self.assertEqual(len(s.ready(second,121)),1)
        self.assertEqual(s.ready({},200),[])

    def test_new_duplicate_variants_and_non_overwrite(self):
        first=self.worker.inbox/'untrusted brand folder'/'same.ttf';make_font(first)
        original=first.read_bytes();a=self.process(first)
        self.assertEqual(a['action'],'入库');self.assertNotIn('untrusted brand folder',a['target'])
        self.assertEqual((self.root/a['target']).read_bytes(),original)
        duplicate=self.worker.inbox/'again.ttf';duplicate.write_bytes(original);b=self.process(duplicate)
        self.assertEqual(b['action'],'重复备份');self.assertEqual(b['retained'],a['target'])
        self.assertEqual((self.root/b['target']).read_bytes(),original)
        other=self.worker.inbox/'same.ttf';make_font(other,revision=2);c=self.process(other)
        self.assertEqual(c['action'],'入库');self.assertNotEqual(a['target'],c['target'])
        self.assertIn('__来源_',c['target']);self.assertTrue(c['same_name_versions'])
        self.assertEqual((self.root/a['target']).read_bytes(),original)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM files').fetchone()[0],2)

    def test_existing_library_index_and_stale_sample(self):
        existing=self.worker.library/'Known'/'西文'/'Test'/'a.ttf';make_font(existing)
        original=existing.read_bytes();self.worker.refresh()
        x=self.worker.inbox/'x.ttf';x.write_bytes(original)
        r=self.process(x);self.assertEqual(r['action'],'重复备份')
        # Same-size contents change with original timestamp: full rehash must
        # defeat a cached hash/sample even if filesystem metadata was restored.
        stat=existing.stat();altered=bytearray(original);altered[-1]^=1;existing.write_bytes(altered)
        os.utime(existing,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        y=self.worker.inbox/'y.ttf';y.write_bytes(original)
        r=self.process(y);self.assertEqual(r['action'],'入库')

    def test_corrupt_font_and_attachments_preserved(self):
        bad=self.worker.inbox/'bad.ttf';bad.write_bytes(b'not a font')
        r=self.process(bad);self.assertEqual(r['action'],'待检查');self.assertEqual((self.root/r['target']).read_bytes(),b'not a font')
        attachment=self.worker.inbox/'license.txt';attachment.write_text('keep license',encoding='utf-8')
        r=self.process(attachment);self.assertEqual(r['action'],'附件');self.assertEqual((self.root/r['target']).read_text('utf-8'),'keep license')

    def test_copy_writer_is_never_processed(self):
        path=self.worker.inbox/'busy.ttf';make_font(path)
        if os.name!='nt':
            with path.open('r+b'):
                with self.assertRaises(OSError):self.process(path)
                self.assertTrue(path.exists())
            self.assertEqual(self.process(path)['action'],'入库')
            return
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype=wintypes.HANDLE;kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.CreateFileW(str(path),0x40000000,0x1|0x2|0x4,None,3,0x80,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(OSError):self.process(path)
            self.assertTrue(path.exists());self.assertEqual(self.worker.db.execute('SELECT count(*) FROM operations').fetchone()[0],0)
        finally:kernel.CloseHandle(handle)
        self.assertEqual(self.process(path)['action'],'入库')

    def test_interrupted_rename_recovery(self):
        path=self.worker.inbox/'recover.ttf';make_font(path)
        finish=self.worker.finish
        def crash(*args):raise RuntimeError('simulated crash after rename')
        self.worker.finish=crash
        with self.assertRaises(RuntimeError):self.process(path)
        self.assertFalse(path.exists());self.assertEqual(self.worker.db.execute("SELECT count(*) FROM operations WHERE state='pending'").fetchone()[0],1)
        self.worker.finish=finish;self.worker.recover()
        self.assertEqual(self.worker.db.execute("SELECT count(*) FROM operations WHERE state='done'").fetchone()[0],1)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM files').fetchone()[0],1)

    def test_single_writer_and_path_escape(self):
        with single_writer(self.worker.app/'worker.lock'):
            # POSIX record locks are per process, so verify exclusion in a
            # separate process on both platforms.
            code='from pathlib import Path; from font_ingest import single_writer;\nwith single_writer(Path('+repr(str(self.worker.app/'worker.lock'))+')): pass'
            child=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).parent,capture_output=True,text=True)
            self.assertNotEqual(child.returncode,0);self.assertIn('RuntimeError',child.stderr)
        with self.assertRaises(ValueError):self.worker.checked(self.root.parent/'outside.ttf')
        with self.assertRaises(ValueError):self.worker.checked(self.root/'..'/'outside.ttf')

    def test_atomic_rename_does_not_overwrite(self):
        a=self.worker.inbox/'a';b=self.worker.inbox/'b'
        a.write_bytes(b'input');b.write_bytes(b'keep')
        with self.assertRaises(OSError):no_replace_rename(a,b)
        self.assertEqual(a.read_bytes(),b'input');self.assertEqual(b.read_bytes(),b'keep')

    def test_case_only_destination_keeps_distinct_contents(self):
        first=self.worker.inbox/'SAME.TTF';make_font(first)
        a=self.process(first)
        second=self.worker.inbox/'same.ttf';make_font(second,revision=2)
        b=self.process(second)
        self.assertIn('__来源_',b['target'])
        self.assertNotEqual(a['target'].casefold(),b['target'].casefold())
        existing=self.worker.library/'Vendor'/'West';existing.mkdir(parents=True)
        path=self.worker.unique(self.worker.library/'vendor'/'west'/'font.ttf','a'*64)
        self.assertEqual(path.parent,existing)

    @unittest.skipIf(os.name=='nt','Linux symlink protection')
    def test_linux_symlink_rejected(self):
        target=self.worker.inbox/'real';target.mkdir()
        (target/'font.ttf').write_bytes(b'data')
        linked=self.worker.inbox/'link';linked.symlink_to(target,target_is_directory=True)
        with self.assertRaises(ValueError):self.worker.checked(linked/'font.ttf')

    def test_batch_report_partial_copy_and_restart(self):
        (self.root/'字体目录.html').write_text('<h1>字体目录</h1><p>old</p><input id="q"><script>const data=[];const $=x=>x;</script>',encoding='utf-8')
        pack=self.worker.inbox/'batch';make_font(pack/'font.ttf')
        temporary=pack/'copy.part';temporary.write_bytes(b'copy in progress')
        self.assertEqual(self.worker.snapshot()[0],{})
        temporary.rename(pack/'readme.txt')
        snapshots,errors=self.worker.snapshot();self.assertFalse(errors)
        self.worker.batch(list(snapshots.items()))
        self.assertFalse(pack.exists());self.assertTrue((self.root/'自动入库记录.html').exists())
        self.assertTrue(list(self.worker.reports.glob('*.md')))
        page=(self.root/'字体目录.html').read_text('utf-8')
        self.assertIn('Inbox Test',page)
        self.worker.publish_catalog()
        import re
        data=json.loads(re.search(r'const data=(.*?);const \$=',(self.root/'字体目录.html').read_text('utf-8'),re.S).group(1))
        self.assertEqual(len(data),1)
        self.worker.refresh();self.assertEqual(self.worker.db.execute('SELECT count(*) FROM files').fetchone()[0],1)

    def test_configured_state_directory_is_used_directly(self):
        config=Path(self.tmp.name)/'config.json'
        config.write_text(json.dumps({'root':str(self.root)}),encoding='utf-8')
        state=Path(self.tmp.name)/'existing-state';state.mkdir()
        (state/'status.json').write_text('{"phase":"等待投递"}',encoding='utf-8')
        output=io.StringIO()
        with mock.patch.dict(os.environ,{'FONT_INGEST_STATE':str(state)}), \
             mock.patch.object(sys,'argv',['font_ingest.py','status','--config',str(config)]), \
             contextlib.redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue()),{'phase':'等待投递'})
        self.assertFalse((state/'FontIngest').exists())

    def test_deleted_catalog_path_stays_deleted_until_file_returns(self):
        import re
        catalog=self.root/'字体目录.html'
        catalog.write_text('<h1>字体目录</h1><p>old</p><input id="q"><script>const data=[];const $=x=>x;</script>',encoding='utf-8')
        target=self.worker.library/'Vendor'/'West'/'Family'/'deleted.ttf';make_font(target)
        record={'id':'delete-test','batch':'test','time':'2026-01-01 00:00:00','source':'待入库/deleted.ttf',
                'size':target.stat().st_size,'sha':'x','sample':'','ps_names':[],'warnings':[],
                'action':'入库','target':self.worker.relative(target),'vendor':'Vendor','language':'West',
                'family':'Family','reason':'test'}
        self.worker.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',
            (record['id'],record['batch'],record['source'],record['target'],record['action'],record['sha'],
             record['size'],json.dumps(record),'done'))
        self.worker.db.commit()
        self.worker.publish_catalog()
        page=catalog.read_text('utf-8')
        match=re.search(r'const data=(.*?);const \$=',page,re.S)
        entries=json.loads(match.group(1));entries[0]['a']='已删除';entries[0]['deleted_at']=1
        data=json.dumps(entries,ensure_ascii=False)
        catalog.write_text(page[:match.start(1)]+data+page[match.end(1):],encoding='utf-8')
        target.unlink()

        self.worker.publish_catalog()
        page=catalog.read_text('utf-8')
        entries=json.loads(re.search(r'const data=(.*?);const \$=',page,re.S).group(1))
        self.assertEqual(len(entries),1);self.assertEqual(entries[0]['a'],'已删除')

        make_font(target,revision=2)
        self.worker.publish_catalog()
        page=catalog.read_text('utf-8')
        entries=json.loads(re.search(r'const data=(.*?);const \$=',page,re.S).group(1))
        self.assertEqual(entries[0]['a'],'保留');self.assertNotIn('deleted_at',entries[0])


if __name__=='__main__':unittest.main(verbosity=2)
