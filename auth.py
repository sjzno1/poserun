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
import logging
import os
import secrets
import sqlite3
import struct
import threading
import time
from datetime import datetime
from urllib.parse import quote

from flask import Blueprint, g, jsonify, request

# 令牌有效期：7天
TOKEN_TTL = 7 * 24 * 3600
PBKDF2_ITERATIONS = 200_000

DEFAULT_ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
DEFAULT_ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

# 启动警告（ui-hardening 2026-11-13）：默认口令回退生效时提示，不改播种行为、不打印口令值。
if 'ADMIN_PASSWORD' not in os.environ:
    logging.getLogger('poserun.auth').warning(
        'ADMIN_PASSWORD 环境变量未设置：首次初始化将使用内置默认管理员口令回退。'
        '请部署后立即修改管理员密码，或通过 ADMIN_PASSWORD 环境变量注入强口令。')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------------------
# TOTP 两步验证（RFC 6238，标准库实现，零第三方依赖，兼容主流认证器 App）
# ---------------------------------------------------------------------------
TOTP_PERIOD = 30      # 时间步长（秒），主流认证器默认 30s
TOTP_DIGITS = 6       # 动态码位数
TOTP_WINDOW = 1       # 允许前后各 1 个时间步，容忍时钟漂移
TOTP_ISSUER = 'PoseRun'


def _totp_generate_secret() -> str:
    """生成 160-bit 随机密钥，Base32 编码（32 字符，认证器标准）"""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _totp_at(secret_b32: str, counter: int) -> str:
    """按 RFC 4226 计算指定计数器的 HOTP 码（SHA1，动态截断）"""
    pad = '=' * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def _totp_match(secret_b32: str, code: str, window: int = TOTP_WINDOW):
    """校验动态码，返回命中的时间步计数器；不匹配返回 None。常数时间比较。"""
    if not secret_b32 or not code:
        return None
    code = str(code).strip()
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = int(time.time()) // TOTP_PERIOD
    for offset in range(-window, window + 1):
        counter = current + offset
        if hmac.compare_digest(_totp_at(secret_b32, counter), code):
            return counter
    return None


def _totp_provisioning_uri(secret_b32: str, username: str) -> str:
    """otpauth:// 链接，供认证器扫码或手动导入"""
    label = quote(f'{TOTP_ISSUER}:{username}', safe='')
    return (f'otpauth://totp/{label}?secret={secret_b32}'
            f'&issuer={quote(TOTP_ISSUER)}&algorithm=SHA1'
            f'&digits={TOTP_DIGITS}&period={TOTP_PERIOD}')


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
            # TOTP 两步验证字段（totp_secret 仅启用后写入；totp_pending 为绑定中的临时密钥）
            if 'totp_secret' not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT DEFAULT ''")
            if 'totp_enabled' not in columns:
                conn.execute('ALTER TABLE users ADD COLUMN totp_enabled INTEGER DEFAULT 0')
            if 'totp_pending' not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN totp_pending TEXT DEFAULT ''")
            if 'totp_last_step' not in columns:
                conn.execute('ALTER TABLE users ADD COLUMN totp_last_step INTEGER DEFAULT 0')
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
        sql = 'SELECT id, username, name, role, active, created_at, last_login_at, totp_enabled FROM users'
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
            'totp_enabled': bool(user.get('totp_enabled', 0)),
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
            # 两步验证：仅在口令正确后才检查，避免泄露账号是否开启 2FA
            if user.get('totp_enabled'):
                code = (data.get('totp_code') or '').strip()
                if not code:
                    return jsonify({'mfa_required': True}), 200
                matched = _totp_match(user.get('totp_secret'), code)
                if matched is None or matched <= (user.get('totp_last_step') or 0):
                    self._audit('login_mfa_failed', user=None,
                                target=str(data.get('username') or ''), detail='动态码错误或已使用')
                    return jsonify({'error': '动态码错误或已失效', 'mfa_required': True}), 401
                with self._lock, self._conn() as conn:
                    conn.execute('UPDATE users SET totp_last_step=? WHERE id=?', (matched, user['id']))
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

        # ---------- 两步验证（TOTP）：本人状态/绑定/启用/禁用 ----------

        @bp.route('/api/auth/me/2fa', methods=['GET'])
        def me_2fa_status():
            user = self.get_user_by_id(g.current_user['id'])
            return jsonify({'success': True, 'enabled': bool(user and user.get('totp_enabled'))})

        @bp.route('/api/auth/me/2fa/setup', methods=['POST'])
        def me_2fa_setup():
            uid = g.current_user['id']
            user = self.get_user_by_id(uid)
            if user.get('totp_enabled'):
                return jsonify({'error': '已开启两步验证，请先禁用后再重新绑定'}), 400
            secret = _totp_generate_secret()
            with self._lock, self._conn() as conn:
                conn.execute('UPDATE users SET totp_pending=? WHERE id=?', (secret, uid))
            self._audit('2fa_setup', user=g.current_user, target=user['username'], detail='开始绑定两步验证')
            return jsonify({'success': True, 'secret': secret,
                            'otpauth_uri': _totp_provisioning_uri(secret, user['username']),
                            'issuer': TOTP_ISSUER, 'digits': TOTP_DIGITS, 'period': TOTP_PERIOD})

        @bp.route('/api/auth/me/2fa/enable', methods=['POST'])
        def me_2fa_enable():
            uid = g.current_user['id']
            user = self.get_user_by_id(uid)
            if user.get('totp_enabled'):
                return jsonify({'error': '两步验证已开启'}), 400
            pending = user.get('totp_pending') or ''
            if not pending:
                return jsonify({'error': '请先获取绑定密钥'}), 400
            code = ((request.get_json(silent=True) or {}).get('code') or '').strip()
            matched = _totp_match(pending, code)
            if matched is None:
                self._audit('2fa_enable_failed', user=g.current_user, target=user['username'], detail='动态码错误')
                return jsonify({'error': '动态码错误，请重试'}), 400
            with self._lock, self._conn() as conn:
                conn.execute('UPDATE users SET totp_secret=?, totp_enabled=1, totp_pending=?, totp_last_step=0 WHERE id=?',
                             (pending, '', uid))
            self._audit('2fa_enabled', user=g.current_user, target=user['username'], detail='开启两步验证')
            return jsonify({'success': True, 'message': '两步验证已开启',
                            'user': self.public_view(self.get_user_by_id(uid))})

        @bp.route('/api/auth/me/2fa/disable', methods=['POST'])
        def me_2fa_disable():
            uid = g.current_user['id']
            data = request.get_json(silent=True) or {}
            user = self.get_user_by_id(uid)
            if not user.get('totp_enabled'):
                return jsonify({'error': '两步验证未开启'}), 400
            # 需同时校验口令 + 当前动态码，防止仅凭口令关闭 2FA
            if not self._verify_password(data.get('password') or '', user['salt'], user['password_hash']):
                return jsonify({'error': '密码错误'}), 400
            matched = _totp_match(user.get('totp_secret'), (data.get('code') or '').strip())
            if matched is None:
                return jsonify({'error': '动态码错误或已失效'}), 400
            with self._lock, self._conn() as conn:
                conn.execute('UPDATE users SET totp_enabled=0, totp_secret=?, totp_pending=?, totp_last_step=0 WHERE id=?',
                             ('', '', uid))
            self._audit('2fa_disabled', user=g.current_user, target=user['username'], detail='禁用两步验证')
            return jsonify({'success': True, 'message': '两步验证已禁用',
                            'user': self.public_view(self.get_user_by_id(uid))})

        @bp.route('/api/users/<int:uid>/2fa/reset', methods=['POST'])
        def admin_reset_2fa(uid):
            """管理员恢复通道：用户丢失认证器时重置其 2FA，并强制其重新登录"""
            target_user = self.get_user_by_id(uid)
            if target_user is None:
                return jsonify({'error': '用户不存在'}), 404
            with self._lock, self._conn() as conn:
                conn.execute('UPDATE users SET totp_enabled=0, totp_secret=?, totp_pending=?, totp_last_step=0, '
                             'session_version=session_version+1 WHERE id=?', ('', '', uid))
            self._audit('admin_2fa_reset', user=g.current_user, target=target_user['username'], detail='管理员重置两步验证')
            return jsonify({'success': True, 'message': '已重置该用户的两步验证',
                            'user': self.public_view(self.get_user_by_id(uid))})


def create_auth_manager(base_dir=None):
    """工厂函数：默认数据库位于 <base_dir>/data/auth.db"""
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.environ.get('AUTH_DB_PATH', os.path.join(base_dir, 'data', 'auth.db'))
    return AuthManager(db_path)
