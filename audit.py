"""
PoseRun Analyzer 操作日志（审计）模块
- SQLite 审计库（data/audit.db，与 auth.db / history.db 同卷持久化）
- 记录管理员关注的操作：登录/登出/注册、上传、分析、报告下载、删除、人员管理、备份、异常
- 自动清理：超过 MAX_EVENTS 条时删除最旧记录

用法:
    audit = AuditStore(os.path.join('data', 'audit.db'))
    audit.log('login', user={'id':1,'username':'admin'})
    rows = audit.list(limit=100, action='upload')
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

MAX_EVENTS = 2000
TRIM_BATCH = 200


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class AuditStore:
    """操作日志存储（零第三方依赖，线程安全）"""

    def __init__(self, db_path):
        self.db_path = db_path
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init_db(self):
        with self._lock, self._conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    user_id INTEGER,
                    username TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    ip TEXT NOT NULL DEFAULT ''
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(username)')

    # ---------- 写入 ----------

    def log(self, action, user=None, target='', detail='', ip=''):
        """
        写入一条审计记录。失败不影响主流程（静默降级为 print）。
        user: 登录用户 dict（含 id/username/name），None 时匿名。
        """
        user = user or {}
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    'INSERT INTO audit_log (ts, user_id, username, user_name, action, target, detail, ip) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (
                        _now(),
                        user.get('id'),
                        str(user.get('username') or ''),
                        str(user.get('name') or ''),
                        str(action or ''),
                        str(target or '')[:300],
                        str(detail or '')[:1000],
                        str(ip or '')[:64],
                    )
                )
                # 控制体积：超限时分批删最旧
                total = conn.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]
                if total > MAX_EVENTS:
                    conn.execute(
                        'DELETE FROM audit_log WHERE id IN '
                        '(SELECT id FROM audit_log ORDER BY id ASC LIMIT ?)',
                        (TRIM_BATCH,)
                    )
        except Exception as e:
            print(f'[audit] 写入失败: {e}')

    # ---------- 查询 ----------

    def list(self, limit=100, offset=0, action=None, username=None):
        sql = 'SELECT * FROM audit_log'
        clauses, args = [], []
        if action:
            clauses.append('action=?')
            args.append(action)
        if username:
            clauses.append('(username LIKE ? OR user_name LIKE ?)')
            args.extend([f'%{username}%', f'%{username}%'])
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY id DESC LIMIT ? OFFSET ?'
        args.extend([int(limit), int(offset)])
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def count(self, action=None, username=None):
        sql = 'SELECT COUNT(*) FROM audit_log'
        clauses, args = [], []
        if action:
            clauses.append('action=?')
            args.append(action)
        if username:
            clauses.append('(username LIKE ? OR user_name LIKE ?)')
            args.extend([f'%{username}%', f'%{username}%'])
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        with self._lock, self._conn() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def actions(self):
        """出现过的操作类型（按最近使用排序）"""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                'SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action ORDER BY n DESC'
            ).fetchall()
        return [dict(r) for r in rows]
