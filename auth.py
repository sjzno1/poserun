"""
PoseRun Analyzer 认证与人员管理模块
- SQLite 用户库（data/auth.db）
- 注册 / 登录 / 登出 / 当前用户
- 管理员人员管理：列表、搜索、改角色/姓名/状态、重置密码、删除
- 零第三方依赖：PBKDF2-HMAC-SHA256 密码哈希 + HMAC-SHA256 签名令牌

用法:
    auth = create_auth_manager()
    auth.attach(flask_app)     # 注册蓝图 + 请求拦截
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime

from flask import Blueprint, g, jsonify, request

# 令牌有效期：7天
TOKEN_TTL = 7 * 24 * 3600
PBKDF2_ITERATIONS = 200_000

DEFAULT_ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
DEFAULT_ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class AuthManager:
    """用户认证与人员管理"""

    def __init__(self, db_path):
        self.db_path = db_path
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._secret = self._load_or_create_secret(db_dir)
        self._init_db()
        self._seed_admin()
        # 审计钩子：由 app.py 注入（self.audit(action, target=, detail=, user=)）
        self.audit = None

    def _audit(self, action, user=None, target='', detail=''):
        """写审计日志（钩子未注入或失败时静默跳过）"""
        fn = self.audit
        if not fn:
            return
        try:
            fn(action, target=target, detail=detail, user=user)
        except Exception:
            pass

    # ---------- 基础设施 ----------

    def _load_or_create_secret(self, db_dir):
        """持久化签名密钥（重启后旧令牌仍有效）"""
        env_secret = os.environ.get('AUTH_SECRET')
        if env_secret:
            return env_secret.encode()
        secret_file = os.path.join(db_dir, '.auth_secret')
        if os.path.exists(secret_file):
            with open(secret_file, 'r', encoding='utf-8') as f:
                s = f.read().strip()
                if s:
                    return s.encode()
        new_secret = secrets.token_hex(32)
        with open(secret_file, 'w', encoding='utf-8') as f:
            f.write(new_secret)
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return new_secret.encode()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def _init_db(self):
        with self._lock, self._conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_login_at TEXT
                )
            ''')
            columns = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
            if 'session_version' not in columns:
                conn.execute('ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')

    def _seed_admin(self):
        """首次启动创建默认管理员（admin/admin123，可经环境变量覆盖）"""
        with self._lock, self._conn() as conn:
            row = conn.execute('SELECT id FROM users LIMIT 1').fetchone()
            if row is None:
                ph, salt = self._hash_password(DEFAULT_ADMIN_PASSWORD)
                conn.execute(
                    'INSERT INTO users (username, password_hash, salt, name, role, active, created_at) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (DEFAULT_ADMIN_USERNAME, ph, salt, '系统管理员', 'admin', 1, _now())
                )
                print(f"[auth] 已创建默认管理员账号: {DEFAULT_ADMIN_USERNAME}（请登录后尽快修改密码）")

    # ---------- 密码与令牌 ----------

    def _hash_password(self, password):
        salt = secrets.token_hex(16)
        ph = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()
        return ph, salt

    def _verify_password(self, password, salt, expected_hash):
        # 必须用存储的 salt 重新计算（_hash_password 每次会生成新盐）
        ph = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()
        return hmac.compare_digest(ph, expected_hash)

    def create_token(self, user):
        payload = {
            'uid': user['id'],
            'sv': user['session_version'],
            'jti': secrets.token_hex(16),
            'username': user['username'],
            'role': user['role'],
            'exp': int(time.time()) + TOKEN_TTL,
        }
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(',', ':')).encode()
        ).decode().rstrip('=')
        sig = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def verify_token(self, token):
        """返回用户行（dict）或 None"""
        if not token or '.' not in token:
            return None
        body, _, sig = token.partition('.')
        expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            pad = '=' * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(body + pad))
        except Exception:
            return None
        if (not isinstance(payload, dict)
                or type(payload.get('exp')) is not int
                or type(payload.get('sv')) is not int
                or type(payload.get('uid')) is not int
                or payload['exp'] <= time.time()):
            return None
        with self._lock, self._conn() as conn:
            row = conn.execute(
                'SELECT * FROM users WHERE id=? AND active=1', (payload.get('uid'),)
            ).fetchone()
        return dict(row) if row and row['session_version'] == payload['sv'] else None

    def revoke_sessions(self, uid):
        """Logout is explicitly all-device logout, atomically incremented."""
        with self._lock, self._conn() as conn:
            conn.execute('UPDATE users SET session_version=session_version+1 WHERE id=?', (uid,))

    # ---------- 用户 CRUD ----------

    def get_user_by_username(self, username):
        with self._lock, self._conn() as conn:
            row = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, uid):
        with self._lock, self._conn() as conn:
            row = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        return dict(row) if row else None

    def register_user(self, username, password, name=''):
        """注册新用户。返回 (user_dict, None) 或 (None, error_msg)"""
        username = (username or '').strip()
        if not username:
            return None, '请输入用户名'
        if not (3 <= len(username) <= 32):
            return None, '用户名长度需在 3-32 个字符之间'
        if not username.isidentifier() and not all(
            c.isalnum() or c in '_-' for c in username
        ):
            return None, '用户名只能包含字母、数字、下划线或连字符'
        if len(password or '') < 6:
            return None, '密码长度至少 6 位'
        if self.get_user_by_username(username):
            return None, '用户名已存在'
        ph, salt = self._hash_password(password)
        with self._lock, self._conn() as conn:
            try:
                cur = conn.execute(
                    'INSERT INTO users (username, password_hash, salt, name, role, active, created_at) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (username, ph, salt, (name or '').strip()[:20], 'user', 1, _now())
                )
            except sqlite3.IntegrityError:
                return None, '用户名已存在'
            uid = cur.lastrowid
        return self.get_user_by_id(uid), None

    def authenticate(self, username, password):
        """登录验证。返回 (user_dict, None) 或 (None, error_msg)"""
        user = self.get_user_by_username((username or '').strip())
        if not user:
            return None, '用户名或密码错误'
        if not user['active']:
            return None, '该账号已被禁用，请联系管理员'
        if not self._verify_password(password or '', user['salt'], user['password_hash']):
            return None, '用户名或密码错误'
        with self._lock, self._conn() as conn:
            conn.execute('UPDATE users SET last_login_at=? WHERE id=?', (_now(), user['id']))
        user['last_login_at'] = _now()
        return user, None

    def list_users(self, search=''):
        sql = 'SELECT id, username, name, role, active, created_at, last_login_at FROM users'
        args = []
        if search:
            sql += ' WHERE username LIKE ? OR name LIKE ?'
            args = [f'%{search}%', f'%{search}%']
        sql += ' ORDER BY id ASC'
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def count_admins(self):
        with self._lock, self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
            ).fetchone()[0]

    def update_user(self, uid, fields):
        """更新 name / role / active。返回 (user_dict, None) 或 (None, error_msg)"""
        user = self.get_user_by_id(uid)
        if not user:
            return None, '用户不存在'
        updates, args = [], []
        if 'name' in fields:
            updates.append('name=?')
            args.append(str(fields['name'] or '').strip()[:20])
        if 'role' in fields:
            new_role = fields['role']
            if new_role not in ('admin', 'user'):
                return None, '无效的角色'
            # 不能取消最后一个管理员的 admin 身份
            if user['role'] == 'admin' and new_role != 'admin' and self.count_admins() <= 1:
                return None, '不能取消最后一个管理员的管理权限'
            updates.append('role=?')
            args.append(new_role)
        if 'active' in fields:
            new_active = 1 if fields['active'] else 0
            if user['role'] == 'admin' and not new_active and self.count_admins() <= 1:
                return None, '不能禁用最后一个管理员'
            updates.append('active=?')
            args.append(new_active)
        if not updates:
            return user, None
        args.append(uid)
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", args)
        return self.get_user_by_id(uid), None

    def reset_password(self, uid, new_password):
        user = self.get_user_by_id(uid)
        if not user:
            return None, '用户不存在'
        if len(new_password or '') < 6:
            return None, '密码长度至少 6 位'
        ph, salt = self._hash_password(new_password)
        with self._lock, self._conn() as conn:
            conn.execute(
                'UPDATE users SET password_hash=?, salt=?, session_version=session_version+1 WHERE id=?', (ph, salt, uid)
            )
        return self.get_user_by_id(uid), None

    def change_password(self, uid, old_password, new_password):
        """本人改密：校验旧密码后设置新密码"""
        user = self.get_user_by_id(uid)
        if not user:
            return None, '用户不存在'
        if not self._verify_password(old_password or '', user['salt'], user['password_hash']):
            return None, '当前密码不正确'
        if len(new_password or '') < 6:
            return None, '新密码长度至少 6 位'
        ph, salt = self._hash_password(new_password)
        with self._lock, self._conn() as conn:
            conn.execute(
                'UPDATE users SET password_hash=?, salt=?, session_version=session_version+1 WHERE id=?', (ph, salt, uid)
            )
        return self.get_user_by_id(uid), None

    def delete_user(self, uid):
        user = self.get_user_by_id(uid)
        if not user:
            return '用户不存在'
        if user['role'] == 'admin' and self.count_admins() <= 1:
            return '不能删除最后一个管理员'
        with self._lock, self._conn() as conn:
            conn.execute('DELETE FROM users WHERE id=?', (uid,))
        return None

    @staticmethod
    def public_view(user):
        """去掉敏感字段的对外视图"""
        if not user:
            return None
        return {
            'id': user['id'],
            'username': user['username'],
            'name': user['name'],
            'role': user['role'],
            'active': bool(user['active']),
            'created_at': user['created_at'],
            'last_login_at': user.get('last_login_at'),
        }

    # ---------- Flask 集成 ----------

    def attach(self, app):
        """注册蓝图与请求拦截到 Flask app"""
        app.extensions['poserun_auth'] = self
        bp = Blueprint('auth', __name__)
        self._register_routes(bp)
        app.register_blueprint(bp)
        app.before_request(self._before_request)

    # 无需登录即可访问的路径前缀
    PUBLIC_PATHS = ('/api/health', '/api/auth/login', '/api/auth/register', '/api/templates')

    def _before_request(self):
        path = request.path
        # 静态资源与首页放行
        if not path.startswith('/api/'):
            return None
        if any(path == p or path.startswith(p + '/') for p in self.PUBLIC_PATHS):
            return None
        # 解析令牌
        header = request.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else header
        user = self.verify_token(token)
        if not user:
            return jsonify({'error': '未登录或登录已过期', 'code': 'unauthorized'}), 401
        g.current_user = user
        # 人员管理接口仅管理员
        if path.startswith('/api/users') and user['role'] != 'admin':
            return jsonify({'error': '需要管理员权限', 'code': 'forbidden'}), 403
        return None

    def _register_routes(self, bp):

        @bp.route('/api/auth/register', methods=['POST'])
        def register():
            data = request.get_json(silent=True) or {}
            user, err = self.register_user(
                data.get('username', ''), data.get('password', ''), data.get('name', '')
            )
            if err:
                self._audit('register_failed', user=None,
                            target=str(data.get('username') or ''), detail=err)
                return jsonify({'error': err}), 400
            self._audit('register', user=user, target=user['username'],
                        detail=f'注册新用户（{user.get("name") or "未填姓名"}）')
            return jsonify({'success': True, 'user': self.public_view(user)}), 201

        @bp.route('/api/auth/login', methods=['POST'])
        def login():
            data = request.get_json(silent=True) or {}
            user, err = self.authenticate(data.get('username', ''), data.get('password', ''))
            if err:
                self._audit('login_failed', user=None,
                            target=str(data.get('username') or ''), detail=err)
                return jsonify({'error': err}), 401
            token = self.create_token(user)
            self._audit('login', user=user, target=user['username'],
                        detail=f'登录成功（{user.get("role")}）')
            return jsonify({'success': True, 'token': token, 'user': self.public_view(user)})

        @bp.route('/api/auth/me', methods=['GET'])
        def me():
            return jsonify({'success': True, 'user': self.public_view(g.current_user)})

        @bp.route('/api/auth/logout', methods=['POST'])
        def logout():
            # 明确语义：退出所有设备；旧令牌不因浏览器仍保留而继续有效。
            self.revoke_sessions(g.current_user['id'])
            self._audit('logout', user=g.current_user, target=g.current_user['username'],
                        detail='退出所有设备，会话已撤销')
            return jsonify({'success': True})

        # ---------- 个人账户（本人操作） ----------

        @bp.route('/api/auth/me', methods=['PATCH'])
        def update_me():
            data = request.get_json(silent=True) or {}
            if 'name' not in data:
                return jsonify({'error': '无可更新的字段'}), 400
            user, err = self.update_user(g.current_user['id'], {'name': data.get('name')})
            if err:
                return jsonify({'error': err}), 400
            self._audit('profile_update', user=user, target=user['username'],
                        detail=f'修改姓名为「{data.get("name")}」')
            return jsonify({'success': True, 'user': self.public_view(user)})

        @bp.route('/api/auth/me/password', methods=['POST'])
        def change_my_password():
            data = request.get_json(silent=True) or {}
            user, err = self.change_password(
                g.current_user['id'], data.get('old_password', ''), data.get('new_password', '')
            )
            if err:
                return jsonify({'error': err}), 400
            self._audit('password_change', user=user, target=user['username'],
                        detail='本人修改密码')
            return jsonify({'success': True, 'message': '密码已修改，下次登录请使用新密码'})

        @bp.route('/api/auth/me', methods=['DELETE'])
        def delete_me():
            err = self.delete_user(g.current_user['id'])
            if err:
                return jsonify({'error': err}), 400
            self._audit('account_delete', user=g.current_user,
                        target=g.current_user['username'], detail='注销本人账号')
            return jsonify({'success': True, 'message': '账号已注销'})

        # ---------- 人员管理（管理员，拦截器已校验角色） ----------

        @bp.route('/api/users', methods=['GET'])
        def list_users():
            search = (request.args.get('search') or '').strip()
            users = self.list_users(search)
            return jsonify({
                'success': True,
                'total': len(users),
                'users': [self.public_view(u) for u in users],
            })

        @bp.route('/api/users/<int:uid>', methods=['PATCH'])
        def update_user(uid):
            data = request.get_json(silent=True) or {}
            if uid == g.current_user['id'] and data.get('role') not in (None, g.current_user['role']):
                return jsonify({'error': '不能修改自己的角色'}), 400
            user, err = self.update_user(uid, data)
            if err:
                return jsonify({'error': err}), 400
            changes = []
            if 'role' in data:
                changes.append(f'角色→{data["role"]}')
            if 'active' in data:
                changes.append(f'状态→{"启用" if data["active"] else "禁用"}')
            if 'name' in data:
                changes.append(f'姓名→{data["name"]}')
            self._audit('user_update', user=g.current_user, target=user['username'],
                        detail='；'.join(changes) or '更新用户资料')
            return jsonify({'success': True, 'user': self.public_view(user)})

        @bp.route('/api/users/<int:uid>/password', methods=['POST'])
        def reset_password(uid):
            data = request.get_json(silent=True) or {}
            user, err = self.reset_password(uid, data.get('password', ''))
            if err:
                return jsonify({'error': err}), 400
            self._audit('user_password_reset', user=g.current_user, target=user['username'],
                        detail='管理员重置密码')
            return jsonify({'success': True, 'message': '密码已重置'})

        @bp.route('/api/users/<int:uid>', methods=['DELETE'])
        def delete_user(uid):
            if uid == g.current_user['id']:
                return jsonify({'error': '不能删除当前登录账号'}), 400
            err = self.delete_user(uid)
            if err:
                return jsonify({'error': err}), 400
            self._audit('user_delete', user=g.current_user, target=str(uid),
                        detail='管理员删除用户')
            return jsonify({'success': True, 'message': '用户已删除'})


def create_auth_manager(base_dir=None):
    """工厂函数：默认数据库位于 <base_dir>/data/auth.db"""
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.environ.get('AUTH_DB_PATH', os.path.join(base_dir, 'data', 'auth.db'))
    return AuthManager(db_path)
