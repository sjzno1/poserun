"""Real Flask auth routes, public budget API and full dummy restore seam.
Tests must run on NAS in the supplied no-network constrained container.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from flask import Flask
from auth import AuthManager
from compute_budget import Budget, BudgetExceeded, Limits, unique_batch


class SessionRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch('auth.DEFAULT_ADMIN_USERNAME', 'dummy_admin'), patch('auth.DEFAULT_ADMIN_PASSWORD', 'dummy_admin_only_982'):
            self.auth = AuthManager(str(Path(self.tmp.name, 'auth.db')))
        self.app = Flask(__name__)
        self.auth.attach(self.app)
        self.client = self.app.test_client()
        self.client.post('/api/auth/register', json={'username':'dummy_runner','password':'dummy_only_672'})

    def login(self, user='dummy_runner', password='dummy_only_672'):
        r=self.client.post('/api/auth/login',json={'username':user,'password':password})
        self.assertEqual(r.status_code,200)
        return {'Authorization':'Bearer '+r.json['token']}

    def test_logout_revokes_all_devices_old_tokens_401_new_login_200(self):
        a,b=self.login(),self.login()
        self.assertNotEqual(a,b)
        self.assertEqual(self.client.post('/api/auth/logout',headers=a).status_code,200)
        for h in (a,b): self.assertEqual(self.client.get('/api/auth/me',headers=h).status_code,401)
        self.assertEqual(self.client.get('/api/auth/me',headers=self.login()).status_code,200)

    def test_change_password_old_tokens_401_new_login_200(self):
        a,b=self.login(),self.login()
        self.assertEqual(self.client.post('/api/auth/me/password',headers=a,json={'old_password':'dummy_only_672','new_password':'dummy_new_723'}).status_code,200)
        for h in (a,b): self.assertEqual(self.client.get('/api/auth/me',headers=h).status_code,401)
        self.assertEqual(self.client.get('/api/auth/me',headers=self.login(password='dummy_new_723')).status_code,200)

    def test_admin_reset_disable_delete_and_role_controls(self):
        admin=self.login('dummy_admin','dummy_admin_only_982')
        user=self.login()
        uid=self.client.get('/api/auth/me',headers=user).json['user']['id']
        self.assertEqual(self.client.get('/api/users',headers=user).status_code,403)
        self.assertEqual(self.client.post(f'/api/users/{uid}/password',headers=admin,json={'password':'dummy_reset_468'}).status_code,200)
        self.assertEqual(self.client.get('/api/auth/me',headers=user).status_code,401)
        user=self.login(password='dummy_reset_468')
        self.assertEqual(self.client.patch(f'/api/users/{uid}',headers=admin,json={'role':'admin'}).status_code,200)
        self.assertEqual(self.client.get('/api/users',headers=user).status_code,200)
        self.assertEqual(self.client.patch(f'/api/users/{uid}',headers=admin,json={'role':'user','active':False}).status_code,200)
        self.assertEqual(self.client.get('/api/auth/me',headers=user).status_code,401)
        self.assertEqual(self.client.patch(f'/api/users/{uid}',headers=admin,json={'active':True}).status_code,200)
        user=self.login(password='dummy_reset_468')
        self.assertEqual(self.client.get('/api/users',headers=user).status_code,403)
        self.assertEqual(self.client.delete(f'/api/users/{uid}',headers=admin).status_code,200)
        self.assertEqual(self.client.get('/api/auth/me',headers=user).status_code,401)

    def test_legacy_epochless_signed_token_rejected(self):
        import base64,hmac
        h=self.login()['Authorization'][7:]
        payload=json.loads(base64.urlsafe_b64decode(h.split('.')[0]+'=='))
        del payload['sv']
        body=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        legacy=body+'.'+hmac.new(self.auth._secret,body.encode(),hashlib.sha256).hexdigest()
        self.assertEqual(self.client.get('/api/auth/me',headers={'Authorization':'Bearer '+legacy}).status_code,401)


class BudgetContract(unittest.TestCase):
    def test_batch_deduplicates_in_order_and_bounds_raw_count(self):
        self.assertEqual(unique_batch(['a','a','b']),['a','b'])
        with self.assertRaises(BudgetExceeded):unique_batch(['a']*5)
        with self.assertRaises(BudgetExceeded):unique_batch([])

    def test_resolution_frames_duration_and_total(self):
        limits=Limits(max_frames=2,max_total_frames=3,max_seconds=1,max_total_seconds=1.5)
        with self.assertRaises(BudgetExceeded):Budget(limits).frame(0,(1081,1920),30)
        with self.assertRaises(BudgetExceeded):Budget(limits).frame(2,(64,64),30)
        with self.assertRaises(BudgetExceeded):Budget(limits).frame(1,(64,64),1)
        b=Budget(limits)
        for _ in range(3):b.frame(0,(64,64),2)
        with self.assertRaises(BudgetExceeded):b.frame(0,(64,64),2)

    def test_unknown_fps_not_fabricated_and_deadline(self):
        now=[0.0]
        b=Budget(Limits(wall_seconds=2),clock=lambda:now[0])
        b.frame(0,(64,64),None)
        self.assertEqual(b.seconds,0)
        now[0]=2
        with self.assertRaises(BudgetExceeded):b.check()


class RestoreDrill(unittest.TestCase):
    def test_sqlite_wal_nested_frames_original_reports_manifest_restore(self):
        import backup
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            for name in ('data/keyframes/session/nested','uploads','reports','archives','restored'):
                (p/name).mkdir(parents=True)
            blobs={'data/.auth_secret':b'dummy-secret-only','data/keyframes/session/nested/frame.jpg':b'dummy-frame-not-a-photo','uploads/runner.mp4':b'dummy-video-not-inference','reports/runner.pdf':b'dummy-report-not-PDF-validation'}
            for name,data in blobs.items():(p/name).write_bytes(data)
            conn=sqlite3.connect(p/'data/auth.db')
            self.addCleanup(conn.close)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE dummy_account(id INTEGER, video TEXT, report TEXT, frame TEXT)')
            conn.execute('INSERT INTO dummy_account VALUES(7,?,?,?)',('runner.mp4','runner.pdf','session/nested/frame.jpg'))
            conn.commit()
            with patch.dict(os.environ,{'POSERUN_CONSISTENCY_LOCK':str(p/'gate.lock')}), patch.multiple(backup,DATA_DIR=str(p/'data'),UPLOADS_DIR=str(p/'uploads'),REPORTS_DIR=str(p/'reports'),BACKUP_DIR=str(p/'archives')):
                ok,out=backup.run_backup()
                self.assertTrue(ok,out)
            with tarfile.open(out) as t:
                manifest=json.load(t.extractfile('manifest.json'))
                for entry in manifest['files']:
                    data=t.extractfile(entry['path']).read()
                    self.assertEqual(hashlib.sha256(data).hexdigest(),entry['sha256'])
                    dest=p/'restored'/entry['path']
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    dest.write_bytes(data)
                self.assertNotIn('data/auth.db-wal',t.getnames())
            for name,data in blobs.items():self.assertEqual((p/'restored'/name).read_bytes(),data)
            with sqlite3.connect(p/'restored/data/auth.db') as restored:
                self.assertEqual(restored.execute('PRAGMA integrity_check').fetchall(),[('ok',)])
                self.assertEqual(restored.execute('SELECT * FROM dummy_account').fetchall(),[(7,'runner.mp4','runner.pdf','session/nested/frame.jpg')])


if __name__=='__main__':unittest.main()
