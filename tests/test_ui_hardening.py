"""UI 接线 + 代码级安全加固的机制测试（unittest，无网络一次性容器内运行）。

机制测试 ≠ 测量精度验收：只验证接线路由、契约保持与安全行为，
不验证任何测量数值准确性。
"""
import importlib
import io
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class SecurityHardening(unittest.TestCase):
    def test_cors_whitelist_entries(self):
        text = (ROOT / 'app.py').read_text(encoding='utf-8')
        # 公开版 CORS 由环境变量驱动：默认仅本机端口，部署注入本站域名。
        self.assertIn('CORS(app, origins=_CORS_ORIGINS)', text)
        self.assertIn("'http://localhost:8080'", text)
        self.assertIn("'http://127.0.0.1:8080'", text)
        self.assertIn('POSERUN_CORS_ORIGINS', text)
        # 公开代码不得硬编码内网 IP。
        self.assertNotIn('192.168.', text)

    def test_config_no_hardcoded_secret_random_fallback(self):
        sys.path.insert(0, str(ROOT))
        env = {k: v for k, v in os.environ.items() if k != 'SECRET_KEY'}
        with patch.dict(os.environ, env, clear=True):
            import config
            importlib.reload(config)
            k1 = config.Config.SECRET_KEY  # 立即取值：reload 复用同一模块对象
            importlib.reload(config)
            k2 = config.Config.SECRET_KEY
            self.assertNotEqual(k1, 'change-me-to-a-random-secret-key')
            self.assertNotEqual(k1, k2)
            self.assertEqual(len(k1), 64)
            int(k1, 16)  # hex
            self.assertTrue(config.get_config('testing').TESTING)
        with patch.dict(os.environ, {'SECRET_KEY': 'unit-test-explicit-key'}, clear=False):
            c3 = importlib.reload(config)
            self.assertEqual(c3.Config.SECRET_KEY, 'unit-test-explicit-key')

    def test_auth_fallback_warning_logged_without_password(self):
        sys.path.insert(0, str(ROOT))
        import auth
        logger = logging.getLogger('poserun.auth')
        env_no = {k: v for k, v in os.environ.items() if k != 'ADMIN_PASSWORD'}
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        try:
            with patch.dict(os.environ, env_no, clear=True):
                importlib.reload(auth)
        finally:
            logger.removeHandler(handler)
        out = stream.getvalue()
        self.assertIn('ADMIN_PASSWORD', out)
        self.assertNotIn(auth.DEFAULT_ADMIN_PASSWORD, out)
        # 设置了 ADMIN_PASSWORD 时不再告警
        stream2 = io.StringIO()
        handler2 = logging.StreamHandler(stream2)
        logger.addHandler(handler2)
        try:
            with patch.dict(os.environ, {'ADMIN_PASSWORD': 'unit_test_only_821'}, clear=False):
                importlib.reload(auth)
        finally:
            logger.removeHandler(handler2)
        self.assertEqual(stream2.getvalue(), '')

    def test_auth_seed_behavior_unchanged_with_fallback(self):
        sys.path.insert(0, str(ROOT))
        import auth
        env_no = {k: v for k, v in os.environ.items() if k != 'ADMIN_PASSWORD'}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, env_no, clear=True):
                importlib.reload(auth)
                with patch('auth.DEFAULT_ADMIN_USERNAME', 'dummy_admin_ui'), \
                     patch('auth.DEFAULT_ADMIN_PASSWORD', 'dummy_ui_only_357'):
                    mgr = auth.AuthManager(str(Path(tmp) / 'auth.db'))
                    user, err = mgr.authenticate('dummy_admin_ui', 'dummy_ui_only_357')
                    self.assertIsNone(err)
                    self.assertIsNotNone(user)
                    self.assertEqual(user['role'], 'admin')


class FrontendWiring(unittest.TestCase):
    """按钮→路由、基线调用、未知处理、备份可视化的静态接线证据。"""

    def read(self, name):
        return (ROOT / 'static' / name).read_text(encoding='utf-8')

    def test_results_export_buttons_wired_to_route(self):
        t = self.read('results.html')
        self.assertIn('id="exportDocxBtn"', t)
        self.assertIn('id="exportPdfBtn"', t)
        self.assertIn("'/api/export/report/' + encodeURIComponent(data.history_record) + '/' + fmt", t)
        self.assertIn('camp-two-page-v1', t)
        self.assertIn('data.history_record != null && data.report_file', t)

    def test_trend_baseline_and_unknown_guards(self):
        t = self.read('trend.html')
        self.assertIn("apiFetch('/api/trend/baseline'", t)
        self.assertIn('id="setBaselineBtn"', t)
        self.assertIn('record_ids: ids.map(Number)', t)
        # 未知分差不渲染绿色改善：known 守卫同时约束颜色与文案
        self.assertIn('Number.isFinite(t.first) && Number.isFinite(t.last) && Number.isFinite(t.delta)', t)
        self.assertIn('Number.isFinite(m.a) && Number.isFinite(m.b) && Number.isFinite(m.delta)', t)
        self.assertIn("!known ? 'text-muted'", t)
        # 测量口径/版本/覆盖率分母标注
        self.assertIn('id="trendMeta"', t)
        self.assertIn('measurement_revision', t)
        self.assertIn('含检测失败帧', t)
        # 图表横向溢出：局部滚动容器
        self.assertIn('chart-scroll', t)
        # AB 上下文：版本 + 两侧覆盖率 + 未知差异
        self.assertIn('recCoverageText', t)
        self.assertIn('AB_METRIC_KEYS', t)

    def test_users_backup_visualization(self):
        t = self.read('users.html')
        self.assertIn('id="backupWarn"', t)
        self.assertIn('24 * 3600 * 1000', t)
        self.assertIn('manual_review_no_automatic_deletion', t)
        self.assertIn('下次每日备份预估', t)
        self.assertIn('已超过 24 小时没有新的备份', t)
        self.assertNotIn('保留最近 7 份', t)

    def test_camera_secure_context_guidance(self):
        t = self.read('camera.html')
        self.assertIn('https://your-domain.example:99', t)
        self.assertIn('http://192.168.x.x:8080', t)
        self.assertIn('实时分析未上线', t)
        self.assertIn('isSecureContext', t)
        self.assertNotIn('realtime_analyzer', t)

    def test_footer_consumes_release_info(self):
        js = self.read('common.js')
        app = (ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertIn('__POSERUN_MEASUREMENT_REV__', js)
        self.assertIn("text.replace('__POSERUN_MEASUREMENT_REV__', CAPABILITIES['measurement_revision'])", app)

    def test_mobile_css_touch_targets(self):
        css = self.read('common.css')
        self.assertIn('.chart-scroll', css)
        self.assertIn('min-height: 44px', css)
        self.assertIn('font-size: 16px', css)


class ExportReportRoute(unittest.TestCase):
    """真实 Flask 路由机制测试：所有权、格式守卫、缺失保持 404（不伪造）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        work = Path(cls.tmp.name) / 'work'
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
            '__pycache__', 'data', 'uploads', 'reports', '.git', '*.pt', 'node_modules'))
        cls.work = work
        cls._old_cwd = os.getcwd()
        cls._old_sys_path = sys.path[:]
        # 全套件顺序污染修复：前序模块（test_regression 等）已把 app 缓存进
        # sys.modules，其 stores 绑定的是它们的临时目录；history/audit 用相对
        # 路径 data/*.db，连接时按当前 cwd 解析，chdir 后指向空新库
        # （sqlite3.OperationalError: no such table: history_records）。
        # 参照 test_regression 的隔离模式：显式临时环境变量 + 逐出缓存的 app
        # 后从临时副本重新导入，让 app 的真实初始化路径在本类临时目录重建全部表。
        # 不用 importlib.reload：reload 会按原模块 __file__（权威源码树）重新执行，
        # 全新导入则从临时副本加载，副作用全部限制在临时目录内。
        cls._saved_app = sys.modules.get('app')
        cls._saved_backup_hook = getattr(sys.modules.get('backup'), 'audit_hook', None)
        os.chdir(work)
        sys.path.insert(0, str(work))
        cls._env_patch = patch.dict(os.environ, {
            'AUTH_DB_PATH': str(work / 'data' / 'auth.db'),
            'ADMIN_USERNAME': 'admin',
            'ADMIN_PASSWORD': 'ui_admin_dummy_913',
            'POSERUN_DATA_DIR': str(work / 'data'),
            'POSERUN_BACKUP_DIR': str(work / 'reports'),
        })
        cls._env_patch.start()
        sys.modules.pop('app', None)
        import app as appmod  # noqa: E402  (重量级导入仅在容器内执行)
        cls.appmod = appmod
        cls.client = appmod.app.test_client()
        (work / 'reports').mkdir(exist_ok=True)
        (work / 'reports' / 'ui_hardening_dummy.pdf').write_bytes(b'%PDF-dummy-pdf')
        (work / 'reports' / 'ui_hardening_dummy.docx').write_bytes(b'PK-dummy-docx')
        (work / 'reports' / 'ui_hardening_pdf_only.pdf').write_bytes(b'%PDF-only')
        cls.client.post('/api/auth/register', json={'username': 'ui_owner', 'password': 'dummy_owner_613'})
        cls.client.post('/api/auth/register', json={'username': 'ui_other', 'password': 'dummy_other_614'})
        cls.owner = cls._login('ui_owner', 'dummy_owner_613')
        cls.other = cls._login('ui_other', 'dummy_other_614')
        owner_user = appmod.auth_manager.get_user_by_username('ui_owner')
        cls.rec_both = appmod.history_store.add_record(
            user=owner_user, report_file='ui_hardening_dummy.pdf', metrics={'cadence': 170})
        cls.rec_pdf_only = appmod.history_store.add_record(
            user=owner_user, report_file='ui_hardening_pdf_only.pdf', metrics={'cadence': 175})
        cls.rec_both_id = cls._record_id('ui_hardening_dummy.pdf')
        cls.rec_pdf_only_id = cls._record_id('ui_hardening_pdf_only.pdf')

    @classmethod
    def _login(cls, username, password):
        r = cls.client.post('/api/auth/login', json={'username': username, 'password': password})
        assert r.status_code == 200, r.data
        return {'Authorization': 'Bearer ' + r.json['token']}

    @classmethod
    def _record_id(cls, report_file):
        with cls.appmod.history_store._conn() as conn:
            row = conn.execute('SELECT id FROM history_records WHERE report_file=?',
                               (report_file,)).fetchone()
        return row['id']

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._old_cwd)
        sys.path[:] = cls._old_sys_path
        cls._env_patch.stop()
        # 恢复运行前的 app 模块缓存与 backup 审计钩子：后续测试模块
        # （如 test_worker_release）看到的仍是本类运行前的同一个 app 实例
        sys.modules.pop('app', None)
        if cls._saved_app is not None:
            sys.modules['app'] = cls._saved_app
        if cls._saved_backup_hook is not None and 'backup' in sys.modules:
            sys.modules['backup'].audit_hook = cls._saved_backup_hook
        cls.tmp.cleanup()

    def test_owner_downloads_both_formats(self):
        for fmt, magic in (('docx', b'PK-dummy-docx'), ('pdf', b'%PDF-dummy-pdf')):
            r = self.client.get(f'/api/export/report/{self.rec_both_id}/{fmt}', headers=self.owner)
            self.assertEqual(r.status_code, 200, r.data)
            self.assertEqual(r.data, magic)
            self.assertIn('attachment', r.headers.get('Content-Disposition', ''))

    def test_other_user_forbidden(self):
        r = self.client.get(f'/api/export/report/{self.rec_both_id}/docx', headers=self.other)
        self.assertEqual(r.status_code, 403)

    def test_unauthorized_401(self):
        r = self.client.get(f'/api/export/report/{self.rec_both_id}/docx')
        self.assertEqual(r.status_code, 401)

    def test_bad_format_400(self):
        r = self.client.get(f'/api/export/report/{self.rec_both_id}/exe', headers=self.owner)
        self.assertEqual(r.status_code, 400)

    def test_missing_record_404(self):
        r = self.client.get('/api/export/report/999999/docx', headers=self.owner)
        self.assertEqual(r.status_code, 404)

    def test_missing_sibling_format_stays_404(self):
        r = self.client.get(f'/api/export/report/{self.rec_pdf_only_id}/docx', headers=self.owner)
        self.assertEqual(r.status_code, 404)

    def test_baseline_route_wiring(self):
        r = self.client.post('/api/trend/baseline', headers=self.owner,
                             json={'record_ids': [self.rec_both_id, self.rec_pdf_only_id]})
        self.assertEqual(r.status_code, 200, r.data)
        b = r.json['baseline']
        self.assertEqual(b['baseline_id'], self.rec_both_id)
        self.assertEqual(len(b['comparisons']), 1)
        self.assertIn('cadence', b['comparisons'][0]['metrics'])

    def test_baseline_requires_two_records(self):
        r = self.client.post('/api/trend/baseline', headers=self.owner,
                             json={'record_ids': [self.rec_both_id]})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
