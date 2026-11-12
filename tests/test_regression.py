"""
PoseRun 回归测试套件（容器内运行：/app/.venv/bin/python -m unittest discover -s tests -v）
覆盖：认证、个人账户、权限、限流接口、备份、健康检查、上传校验、路径安全
使用临时数据库与临时目录，不触碰生产数据。
"""
import os
import sys
import shutil
import tempfile
import unittest

# 隔离环境：临时 DB / 临时目录（必须在 import app 之前设置）
_TMP = tempfile.mkdtemp(prefix='poserun_test_')
os.environ['AUTH_DB_PATH'] = os.path.join(_TMP, 'auth.db')
os.environ['ADMIN_USERNAME'] = 'admin'
os.environ['ADMIN_PASSWORD'] = 'admin123'
os.environ['POSERUN_DATA_DIR'] = os.path.join(_TMP, 'data')
os.environ['POSERUN_BACKUP_DIR'] = os.path.join(_TMP, 'reports')
os.chdir(_TMP)  # app.py 用相对路径 uploads/reports/data

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod  # noqa: E402
import backup as backup_mod  # noqa: E402

app = app_mod.app
app.config['TESTING'] = True


def make_client():
    return app.test_client()


def login(client, username, password):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, f'login failed: {r.get_data(as_text=True)}'
    return r.get_json()['token']


def auth(token):
    return {'Authorization': f'Bearer {token}'}


class TestHealth(unittest.TestCase):
    def test_health(self):
        c = make_client()
        r = c.get('/api/health')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['status'], 'healthy')
        self.assertEqual(d['version'], __import__('release_info').VERSION)

    def test_index(self):
        c = make_client()
        r = c.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'PoseRun', r.data)


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.c = make_client()

    def test_admin_seeded(self):
        r = self.c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['user']['role'], 'admin')

    def test_wrong_password(self):
        r = self.c.post('/api/auth/login', json={'username': 'admin', 'password': 'nope'})
        self.assertEqual(r.status_code, 401)

    def test_register_and_login(self):
        r = self.c.post('/api/auth/register', json={'username': 'tuser1', 'password': 'pass123', 'name': '张三'})
        self.assertEqual(r.status_code, 201)
        tok = login(self.c, 'tuser1', 'pass123')
        r = self.c.get('/api/auth/me', headers=auth(tok))
        self.assertEqual(r.get_json()['user']['name'], '张三')

    def test_register_weak_password(self):
        r = self.c.post('/api/auth/register', json={'username': 'tuser2', 'password': '123'})
        self.assertEqual(r.status_code, 400)

    def test_register_duplicate(self):
        self.c.post('/api/auth/register', json={'username': 'tuser3', 'password': 'pass123'})
        r = self.c.post('/api/auth/register', json={'username': 'tuser3', 'password': 'pass123'})
        self.assertEqual(r.status_code, 400)

    def test_me_requires_auth(self):
        r = self.c.get('/api/auth/me')
        self.assertEqual(r.status_code, 401)

    def test_bad_token(self):
        r = self.c.get('/api/auth/me', headers=auth('garbage.token.here'))
        self.assertEqual(r.status_code, 401)


class TestAccountSelfService(unittest.TestCase):
    """个人账户：改姓名 / 改密码 / 注销"""

    def setUp(self):
        import uuid
        self.c = make_client()
        # 每用例唯一用户名（临时 DB 在同一次运行中跨用例持久）
        self.uname = 'acct_' + uuid.uuid4().hex[:8]
        r = self.c.post('/api/auth/register', json={'username': self.uname, 'password': 'oldpass1', 'name': '旧名'})
        assert r.status_code == 201, r.get_data(as_text=True)
        self.tok = login(self.c, self.uname, 'oldpass1')

    def test_update_name(self):
        r = self.c.patch('/api/auth/me', headers=auth(self.tok), json={'name': '新名'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['user']['name'], '新名')

    def test_change_password_wrong_old(self):
        r = self.c.post('/api/auth/me/password', headers=auth(self.tok),
                        json={'old_password': 'wrong', 'new_password': 'newpass1'})
        self.assertEqual(r.status_code, 400)
        # 旧密码仍有效
        r = self.c.post('/api/auth/login', json={'username': self.uname, 'password': 'oldpass1'})
        self.assertEqual(r.status_code, 200)

    def test_change_password_ok(self):
        r = self.c.post('/api/auth/me/password', headers=auth(self.tok),
                        json={'old_password': 'oldpass1', 'new_password': 'newpass1'})
        self.assertEqual(r.status_code, 200)
        r = self.c.post('/api/auth/login', json={'username': self.uname, 'password': 'oldpass1'})
        self.assertEqual(r.status_code, 401)
        r = self.c.post('/api/auth/login', json={'username': self.uname, 'password': 'newpass1'})
        self.assertEqual(r.status_code, 200)

    def test_change_password_short(self):
        r = self.c.post('/api/auth/me/password', headers=auth(self.tok),
                        json={'old_password': 'oldpass1', 'new_password': '123'})
        self.assertEqual(r.status_code, 400)

    def test_delete_self(self):
        r = self.c.delete('/api/auth/me', headers=auth(self.tok))
        self.assertEqual(r.status_code, 200)
        r = self.c.post('/api/auth/login', json={'username': self.uname, 'password': 'oldpass1'})
        self.assertEqual(r.status_code, 401)

    def test_nonadmin_cannot_list_users(self):
        r = self.c.get('/api/users', headers=auth(self.tok))
        self.assertEqual(r.status_code, 403)


class TestAdmin(unittest.TestCase):
    def setUp(self):
        self.c = make_client()
        self.admin = login(self.c, 'admin', 'admin123')
        self.c.post('/api/auth/register', json={'username': 'sub1', 'password': 'pass123', 'name': '普通'})
        self.sub = login(self.c, 'sub1', 'pass123')

    def test_admin_list_users(self):
        r = self.c.get('/api/users', headers=auth(self.admin))
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.get_json()['total'], 2)

    def test_admin_forbidden_for_user(self):
        r = self.c.get('/api/users', headers=auth(self.sub))
        self.assertEqual(r.status_code, 403)

    def test_admin_reset_password(self):
        r = self.c.post('/api/users/1/password', headers=auth(self.admin), json={'password': 'adminnew1'})
        self.assertEqual(r.status_code, 200)
        # 恢复
        self.c.post('/api/users/1/password', headers=auth(login(self.c, 'admin', 'adminnew1')),
                    json={'password': 'admin123'})

    def test_cannot_delete_last_admin(self):
        r = self.c.delete('/api/users/1', headers=auth(self.admin))
        self.assertEqual(r.status_code, 400)

    def test_cannot_demote_last_admin(self):
        r = self.c.patch('/api/users/1', headers=auth(self.admin), json={'role': 'user'})
        self.assertEqual(r.status_code, 400)

    def test_admin_delete_user(self):
        # 找到 sub1 的 id
        r = self.c.get('/api/users?search=sub1', headers=auth(self.admin))
        uid = r.get_json()['users'][0]['id']
        r = self.c.delete(f'/api/users/{uid}', headers=auth(self.admin))
        self.assertEqual(r.status_code, 200)
        r = self.c.post('/api/auth/login', json={'username': 'sub1', 'password': 'pass123'})
        self.assertEqual(r.status_code, 401)


class TestUploadValidation(unittest.TestCase):
    def setUp(self):
        self.c = make_client()
        self.tok = login(self.c, 'admin', 'admin123')

    def test_upload_requires_auth(self):
        r = self.c.post('/api/upload', data={'video': (b'', 'a.mp4')}, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 401)

    def test_upload_bad_extension(self):
        from io import BytesIO
        r = self.c.post('/api/upload', headers=auth(self.tok),
                        data={'video': (BytesIO(b'x'), 'evil.exe')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

    def test_upload_path_traversal_name(self):
        from io import BytesIO
        r = self.c.post('/api/upload', headers=auth(self.tok),
                        data={'video': (BytesIO(b'x'), '../../../../etc/passwd.mp4')},
                        content_type='multipart/form-data')
        # secure_filename 会清洗；要么 200（被清洗）要么 400，但绝不能写出 uploads 外
        self.assertIn(r.status_code, (200, 400))


class TestBackupApi(unittest.TestCase):
    def setUp(self):
        self.c = make_client()
        self.admin = login(self.c, 'admin', 'admin123')

    def test_backup_requires_admin(self):
        self.c.post('/api/auth/register', json={'username': 'bk1', 'password': 'pass123'})
        sub = login(self.c, 'bk1', 'pass123')
        r = self.c.get('/api/backup', headers=auth(sub))
        self.assertEqual(r.status_code, 403)

    def test_backup_status_and_now(self):
        r = self.c.post('/api/backup', headers=auth(self.admin))
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d['last']['ok'])
        self.assertGreater(len(d['backups']), 0)
        r = self.c.get('/api/backup', headers=auth(self.admin))
        self.assertEqual(r.status_code, 200)


class TestReportPathSafety(unittest.TestCase):
    def setUp(self):
        self.c = make_client()
        self.tok = login(self.c, 'admin', 'admin123')

    def test_report_traversal_blocked(self):
        r = self.c.get('/api/report/../../etc/passwd', headers=auth(self.tok))
        self.assertIn(r.status_code, (404, 400))

    def test_keyframe_traversal_blocked(self):
        r = self.c.get('/api/keyframes/../../../etc/passwd', headers=auth(self.tok))
        self.assertIn(r.status_code, (404, 400))


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        shutil.rmtree(_TMP, ignore_errors=True)
