"""Dummy-account ownership contracts; execute through verify_file_access_nas.py only.

No original videos or fresh model inference. Real Flask authentication, routing,
SQLite ownership, file reads, ZIP creation and deletion; explicit spies prove
that rejected requests never enter video decoding or pose inference.
"""
import io
import os
from pathlib import Path
import sqlite3
import unittest
import uuid
import zipfile
from unittest.mock import patch


class FileAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('POSERUN_ISOLATED_ACCESS_TEST') != '1':
            raise unittest.SkipTest('Run calibration/verify_file_access_nas.py in its OS sandbox')
        import app
        cls.m = app
        app.limiter.enabled = False
        app.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

    def setUp(self):
        self.client = self.m.app.test_client()
        self.tag = uuid.uuid4().hex[:12]
        self.users, self.headers = {}, {}
        for label in ('a', 'b'):
            r = self.client.post('/api/auth/register', json={'username':label+self.tag,
                'password':'TEST-only-Password-123!', 'name':'同名测试账号'})
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            d = r.get_json(); self.users[label] = d['user']
            login = self.client.post('/api/auth/login', json={'username':label+self.tag,
                'password':'TEST-only-Password-123!'})
            self.assertEqual(login.status_code,200,login.get_data(as_text=True))
            self.headers[label] = {'Authorization':'Bearer '+login.get_json()['token']}
        d = self.client.post('/api/auth/login', json={'username':os.environ['ADMIN_USERNAME'],
            'password':os.environ['ADMIN_PASSWORD']}).get_json()
        self.headers['admin'] = {'Authorization':'Bearer '+d['token']}
        self.files, self.records = {}, {}
        for label in ('a', 'b'):
            r = self.client.post('/api/upload', headers=self.headers[label],
                data={'video':(io.BytesIO(('TEST VIDEO '+label).encode()),label+'.mp4')})
            self.assertEqual(r.status_code,200,r.get_data(as_text=True))
            video = r.get_json()['filename']
            report, frames = label+self.tag+'.pdf', label+self.tag
            Path('reports',report).write_bytes(('TEST REPORT '+label).encode())
            Path(self.m.KEYFRAMES_FOLDER,frames).mkdir(parents=True)
            Path(self.m.KEYFRAMES_FOLDER,frames,'frame.png').write_bytes(('TEST FRAME '+label).encode())
            self.files[label] = {'video':video,'report':report,'frames':frames}
            self.records[label] = self.m.history_store.add_record(self.users[label],
                video_file=video, report_file=report, keyframes_dir=frames,
                metrics={'measurement_status':'insufficient_evidence'})

    def request(self, method, route, actor='a', **kwargs):
        return self.client.open(route,method=method,headers=self.headers.get(actor,{}),**kwargs)

    def test_report_owner_and_admin_allowed_foreign_and_anonymous_denied(self):
        route='/api/report/'+self.files['a']['report']
        for who,status in (('a',200),('admin',200),('b',404),('anonymous',401)):
            with self.subTest(who=who):
                r=self.request('GET',route,who);self.assertEqual(r.status_code,status)
                if status==200:self.assertEqual(r.data,b'TEST REPORT a')
                else:self.assertNotIn(b'TEST REPORT a',r.data)
                r.close()

    def test_keyframe_and_zip_owner_boundaries(self):
        for tail in ('/frame.png','/download'):
            for who,status in (('a',200),('admin',200),('b',404),('anonymous',401)):
                with self.subTest(tail=tail,who=who):
                    r=self.request('GET','/api/keyframes/'+self.files['a']['frames']+tail,who)
                    self.assertEqual(r.status_code,status)
                    if status==200 and tail=='/download':
                        with zipfile.ZipFile(io.BytesIO(r.data)) as z:
                            self.assertEqual(z.namelist(),['frame.png'])
                            self.assertEqual(z.read('frame.png'),b'TEST FRAME a')
                    r.close()

    def test_foreign_video_cannot_decode_analyze_batch_or_delete(self):
        video=self.files['a']['video'];path=Path('uploads',video);before=path.read_bytes()
        with patch.object(self.m,'PoseAnalyzer') as model, patch.object(self.m,'_video_quality') as quality:
            for route in ('/api/video/quality','/api/analyze','/api/analyze/advanced','/api/batch'):
                with self.subTest(route=route):
                    body={'video_files':[self.files['b']['video'],video]} if route.endswith('batch') else {'video_file':video}
                    r=self.request('POST',route,'b',json=body);self.assertEqual(r.status_code,404)
            self.assertEqual(self.request('POST','/api/delete/'+video,'b').status_code,404)
            model.assert_not_called();quality.assert_not_called()
        self.assertEqual(path.read_bytes(),before)
        self.assertTrue(self.m.history_store.can_access_asset('video',video,self.users['a']))

    def test_owned_quality_and_delete_work_and_ownership_survives_store_reload(self):
        video=self.files['a']['video']
        fresh=type(self.m.history_store)(self.m.history_store.db_path)
        self.assertTrue(fresh.can_access_asset('video',video,self.users['a']))
        self.assertFalse(fresh.can_access_asset('video',video,self.users['b']))
        with patch.object(self.m,'_video_quality',return_value={'fixture_only':True}) as quality:
            r=self.request('POST','/api/video/quality',json={'video_file':video})
            self.assertEqual(r.status_code,200);self.assertTrue(r.get_json()['fixture_only']);quality.assert_called_once()
        self.assertEqual(self.request('POST','/api/delete/'+video).status_code,200)
        self.assertFalse(Path('uploads',video).exists())
        self.assertEqual(self.request('POST','/api/video/quality',json={'video_file':video}).status_code,404)

    def test_same_display_name_and_modified_display_name_never_grant_history(self):
        rid=self.records['a']['id']
        r=self.request('GET','/api/history','b').get_json()
        self.assertEqual([v['id'] for v in r['records']],[self.records['b']['id']])
        self.assertEqual(r['total_records'],1)
        for name in ('临时修改姓名',self.users['a']['name']):
            changed=self.request('PATCH','/api/auth/me','b',json={'name':name})
            self.assertEqual(changed.status_code,200)
            readback=self.request('GET','/api/auth/me','b').get_json()['user']
            self.assertEqual(readback['name'],name)
            self.assertEqual(readback['id'],self.users['b']['id'])
            visible=self.request('GET','/api/history','b').get_json()['records']
            self.assertNotIn(rid,[row['id'] for row in visible])
        self.assertFalse(self.m.history_store._owns_record(self.records['a'],self.users['b'],False))
        self.assertEqual(self.request('DELETE','/api/history/delete/'+str(rid),'b').status_code,403)
        self.assertIsNotNone(self.m.history_store.get_record(rid))
        self.assertEqual(self.request('GET','/api/export/record/'+str(rid),'b').status_code,403)
        self.assertIsNone(self.m.history_store.compare([rid],user=self.users['b']))

    def test_unknown_or_ambiguous_asset_ownership_is_closed(self):
        unknown='orphan'+self.tag+'.mp4';Path('uploads',unknown).write_bytes(b'TEST ORPHAN')
        for kind,name in (('video',unknown),('report','missing.pdf'),('keyframes','missing')):
            self.assertFalse(self.m.history_store.can_access_asset(kind,name,self.users['a']))
        self.m.history_store.add_record(None,report_file=self.files['a']['report'])
        r=self.request('GET','/api/report/'+self.files['a']['report']);self.assertEqual(r.status_code,404)
        r=self.request('GET','/api/report/'+self.files['a']['report'],'admin');self.assertEqual(r.status_code,200);r.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.m.history_store.register_upload(self.files['a']['video'],self.users['b'])
        self.assertTrue(self.m.history_store.can_access_asset('video',self.files['a']['video'],self.users['a']))

    def test_traversal_and_file_or_directory_symlinks_are_rejected(self):
        a,b=self.files['a']['frames'],self.files['b']['frames']
        folder=Path(self.m.KEYFRAMES_FOLDER,a)
        (folder/'foreign.png').symlink_to(Path('..',b,'frame.png'))
        (folder/'foreign-dir').symlink_to(Path('..',b),target_is_directory=True)
        for suffix in ('../'+b+'/frame.png','foreign.png','foreign-dir/frame.png','/frame.png'):
            with self.subTest(suffix=suffix):
                r=self.request('GET','/api/keyframes/'+a+'/'+suffix)
                self.assertEqual(r.status_code,404)
        r=self.request('GET','/api/keyframes/'+a+'/download')
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:self.assertEqual(z.namelist(),['frame.png'])
        r.close()
        video=self.files['a']['video'];Path('uploads',video).unlink()
        Path('uploads',video).symlink_to(self.files['b']['video'])
        with patch.object(self.m,'_video_quality') as decode:
            self.assertEqual(self.request('POST','/api/video/quality',json={'video_file':video}).status_code,404)
            self.assertEqual(self.request('POST','/api/delete/'+video).status_code,404)
            decode.assert_not_called()
        self.assertTrue(Path('uploads',self.files['b']['video']).exists())

    def test_failed_owner_registration_cleans_new_upload(self):
        before=set(Path('uploads').iterdir())
        with patch.object(self.m.history_store,'register_upload',side_effect=RuntimeError('TEST registry failure')):
            r=self.request('POST','/api/upload',data={'video':(io.BytesIO(b'TEST cleanup'),'cleanup.mp4')})
            self.assertEqual(r.status_code,500)
        self.assertEqual(before,set(Path('uploads').iterdir()))

    def test_quality_invalid_json_types_return_400_not_500(self):
        for body in (None,[],True,{'video_file':True},{'video_file':7}):
            with self.subTest(body=body):
                self.assertEqual(self.request('POST','/api/video/quality',json=body).status_code,400)


if __name__=='__main__':
    unittest.main()
