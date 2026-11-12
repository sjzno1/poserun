"""
PoseRun Analyzer - 跑者画像模块
- SQLite 用户档案（data/profiles.db）
- 个性化基准：根据身体参数、训练背景、伤病历史调整指标权重和参考范围
- 规则引擎：基于条件组合匹配建议模板
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
# Keep runner profiles compatible with cProfile's lazy stdlib imports.
# This does not alter runner records, reference ranges or personalization.
from profile_compat import Profile, run, runctx, _Utils


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# 默认参考范围（无个人档案时使用）
DEFAULT_RANGES = {
    'cadence': {'min': 160, 'max': 180, 'excellent_min': 180, 'excellent_max': 200},
    'vertical_oscillation': {'min': 8, 'max': 15, 'excellent_min': 12, 'excellent_max': 18},
    'ground_contact': {'min': 200, 'max': 300, 'excellent_min': 180, 'excellent_max': 250},
    'stride_length': {'min': 1.5, 'max': 2.0, 'excellent_min': 1.8, 'excellent_max': 2.2},
    'body_lean': {'min': -5, 'max': 5, 'excellent_min': -2, 'excellent_max': 2},
    'shin_angle': {'min': 70, 'max': 90, 'excellent_min': 80, 'excellent_max': 90},
    'knee_flexion': {'min': 145, 'max': 175, 'excellent_min': 155, 'excellent_max': 170},
}

# 伤病 → 重点关注指标映射
INJURY_FOCUS = {
    '膝痛': ['knee_flexion', 'vertical_oscillation', 'shin_angle'],
    '跟腱炎': ['shin_angle', 'ground_contact'],
    '足底筋膜炎': ['shin_angle', 'stride_length'],
    '髂胫束综合征': ['symmetry', 'body_lean'],
    '髋关节痛': ['body_lean', 'stride_length'],
    '小腿胫骨应力性骨折': ['vertical_oscillation', 'ground_contact'],
}

# 训练水平 → 参考范围调整系数
LEVEL_ADJUSTMENTS = {
    'beginner': {'range_factor': 1.3, 'cadence_target': 165},
    'intermediate': {'range_factor': 1.0, 'cadence_target': 175},
    'advanced': {'range_factor': 0.85, 'cadence_target': 185},
    'elite': {'range_factor': 0.7, 'cadence_target': 190},
}

# 建议模板库：(条件描述, 建议文本)
SUGGESTION_TEMPLATES = {
    'knee_pain_low_knee': '有膝伤史 + 膝盖角度偏低：触地时膝盖保持放松微弯（10~20°），避免过度屈膝缓冲。建议做靠墙静蹲 3×30s 强化股四头肌。',
    'knee_pain_high_bounce': '有膝伤史 + 垂直振荡偏大：减少上下颠簸，让能量更多用于水平推进。建议做 Pogo Jumps 20s×3 组提升踝部刚度。',
    'knee_pain_low_shin': '有膝伤史 + 小腿角度偏低（脚跟触地）：落地点前移，让脚落在重心正下方。建议做 High Knees 15s×3 组改善落地姿态。',
    'achilles_low_shin': '有跟腱炎史 + 小腿角度偏低：避免脚跟先着地，前掌先触地。建议做小腿提踵（直腿+屈膝各 3×15）强化跟腱。',
    'plantar_low_shin': '有足底筋膜炎史 + 小腿角度偏低：缩短跨步，前掌先着地，减少足底冲击。建议做脚趾抓毛巾 2 分钟/天。',
    'itbs_asymmetry': '有髂胫束综合征史 + 左右不对称：注意左右腿对称发力，单腿平衡训练 3×30s（每侧）。',
    'hip_high_lean': '有髋关节痛史 + 前倾角度偏大：减小前倾角度，保持躯干稳定。建议做平板支撑 3×45s + 侧平板 3×30s。',
    'shin_splint_high_bounce': '有胫骨应力性骨折史 + 垂直振荡偏大：减少跑步量，做 Pogo Jumps 提升踝部刚度。建议每周跑量增加不超过 10%。',
    'low_cadence': '步频偏低：用节拍器或音乐（BPM = 当前步频 + 5~10%），每段 200m，做 6~8 段，组间慢走 30s。',
    'high_bounce': '垂直振荡偏大：做 Pogo Jumps 20~30s×3 组，保持轻盈落地。',
    'over_stride': '跨步明显：跑步时想象脚在身体正下方"接住"自己，而不是向前"够"地。收紧身体框架。',
    'low_shin': '小腿角度偏低（脚跟触地）：落地点前移，缩短跨步，前掌先着地。建议做 A-Skip 15s×3 组。',
    'high_knee': '膝盖角度偏小（过度屈膝）：触地时膝盖放松微弯即可，避免过度屈膝缓冲。',
    'low_knee': '膝盖角度偏大（过于伸直）：落地时膝盖保持放松微弯（10~20°）。',
    'asymmetry': '左右不对称：单腿跳左右各 5 次×3 组，单腿硬拉 3×10（每侧）。',
    'high_lean': '前倾角度偏大：减小前倾角度，保持躯干稳定。建议做核心稳定性训练。',
    'low_lean': '前倾角度不足：身体适度前倾（从髋部），利用重力前进。',
}


class ProfileStore:
    """跑者画像存储（SQLite）"""

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
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    username TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL DEFAULT '',
                    height_cm REAL,
                    weight_kg REAL,
                    leg_length_cm REAL,
                    training_level TEXT NOT NULL DEFAULT 'intermediate',
                    weekly_km REAL,
                    years_running REAL,
                    injury_history_json TEXT NOT NULL DEFAULT '[]',
                    goal TEXT NOT NULL DEFAULT 'injury_prevention',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_profile_user ON profiles(user_id)')

    def get_profile(self, user_id=None, username=None):
        """获取用户画像。user_id 优先，其次 username。"""
        with self._lock, self._conn() as conn:
            if user_id is not None:
                row = conn.execute(
                    'SELECT * FROM profiles WHERE user_id=?', (int(user_id),)
                ).fetchone()
            elif username:
                row = conn.execute(
                    'SELECT * FROM profiles WHERE username=?', (username,)
                ).fetchone()
            else:
                return None
        if not row:
            return None
        return self._row_to_dict(row)

    def save_profile(self, user_id=None, username='', user_name='',
                     height_cm=None, weight_kg=None, leg_length_cm=None,
                     training_level='intermediate', weekly_km=None,
                     years_running=None, injury_history=None,
                     goal='injury_prevention', notes=''):
        """创建或更新用户画像"""
        injury_history = injury_history or []
        now = _now()
        with self._lock, self._conn() as conn:
            existing = None
            if user_id is not None:
                existing = conn.execute(
                    'SELECT id FROM profiles WHERE user_id=?', (int(user_id),)
                ).fetchone()
            if not existing and username:
                existing = conn.execute(
                    'SELECT id FROM profiles WHERE username=?', (username,)
                ).fetchone()

            if existing:
                pid = existing['id']
                conn.execute(
                    'UPDATE profiles SET user_name=?, height_cm=?, weight_kg=?, leg_length_cm=?, '
                    'training_level=?, weekly_km=?, years_running=?, injury_history_json=?, goal=?, notes=?, updated_at=? '
                    'WHERE id=?',
                    (
                        user_name, height_cm, weight_kg, leg_length_cm,
                        training_level, weekly_km, years_running,
                        json.dumps(injury_history, ensure_ascii=False),
                        goal, notes, now, pid,
                    )
                )
                row = conn.execute('SELECT * FROM profiles WHERE id=?', (pid,)).fetchone()
            else:
                cur = conn.execute(
                    'INSERT INTO profiles (user_id, username, user_name, height_cm, weight_kg, '
                    'leg_length_cm, training_level, weekly_km, years_running, injury_history_json, '
                    'goal, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        user_id, username, user_name, height_cm, weight_kg, leg_length_cm,
                        training_level, weekly_km, years_running,
                        json.dumps(injury_history, ensure_ascii=False),
                        goal, notes, now, now,
                    )
                )
                row = conn.execute('SELECT * FROM profiles WHERE id=?', (cur.lastrowid,)).fetchone()
        return self._row_to_dict(row)

    def delete_profile(self, user_id):
        with self._lock, self._conn() as conn:
            cur = conn.execute('DELETE FROM profiles WHERE user_id=?', (int(user_id),))
            return cur.rowcount > 0

    @staticmethod
    def _row_to_dict(row):
        d = dict(row)
        try:
            d['injury_history'] = json.loads(d.pop('injury_history_json') or '[]')
        except Exception:
            d['injury_history'] = []
        return d


class PersonalizedEngine:
    """个性化规则引擎：根据画像调整指标参考范围和建议"""

    def __init__(self, profile_store: ProfileStore):
        self.store = profile_store

    def get_personalized_ranges(self, user_id=None, username=None):
        """获取个性化参考范围"""
        profile = self.store.get_profile(user_id=user_id, username=username)
        if not profile:
            return dict(DEFAULT_RANGES)

        level = profile.get('training_level', 'intermediate')
        adj = LEVEL_ADJUSTMENTS.get(level, LEVEL_ADJUSTMENTS['intermediate'])
        factor = adj['range_factor']

        ranges = {}
        for key, val in DEFAULT_RANGES.items():
            mid = (val['min'] + val['max']) / 2
            half = (val['max'] - val['min']) / 2 * factor
            ranges[key] = {
                'min': round(mid - half, 1),
                'max': round(mid + half, 1),
                'excellent_min': val['excellent_min'],
                'excellent_max': val['excellent_max'],
            }

        # 步频目标按训练水平调整
        ranges['cadence']['min'] = adj['cadence_target'] - 10
        ranges['cadence']['max'] = adj['cadence_target'] + 15
        ranges['cadence']['excellent_min'] = adj['cadence_target'] + 5
        ranges['cadence']['excellent_max'] = adj['cadence_target'] + 25

        return ranges

    def get_focus_metrics(self, user_id=None, username=None):
        """获取重点关注指标（基于伤病历史）"""
        profile = self.store.get_profile(user_id=user_id, username=username)
        if not profile:
            return []
        injuries = profile.get('injury_history', [])
        focus = []
        for inj in injuries:
            if inj in INJURY_FOCUS:
                for m in INJURY_FOCUS[inj]:
                    if m not in focus:
                        focus.append(m)
        return focus

    def generate_personalized_suggestions(self, user_id=None, username=None,
                                          metrics=None, gait=None):
        """根据画像 + 当前指标生成个性化建议"""
        profile = self.store.get_profile(user_id=user_id, username=username)
        if not profile:
            return []

        suggestions = []
        injuries = profile.get('injury_history', [])
        metrics = metrics or {}
        gait = gait or {}

        # 提取当前指标值
        shin_angle = metrics.get('avg_shin_angle', metrics.get('shin_angle', 90))
        knee_angle = metrics.get('avg_knee_flexion', metrics.get('knee_flexion', 160))
        body_lean = metrics.get('avg_body_leang', metrics.get('body_lean', 0))
        cadence = gait.get('cadence', 170)
        bounce = gait.get('vertical_oscillation', 0.08)
        stride_dist = metrics.get('stride_analysis', {}).get('stride_distance', 0.05)
        symmetry = metrics.get('symmetry', {}).get('symmetry_score', 80)
        over_stride = gait.get('strike_posture', {}).get('over_stride_level', '')

        # 基于伤病的个性化建议
        for inj in injuries:
            if inj == '膝痛':
                if knee_angle < 150:
                    suggestions.append(SUGGESTION_TEMPLATES['knee_pain_low_knee'])
                if bounce and bounce > 0.08:
                    suggestions.append(SUGGESTION_TEMPLATES['knee_pain_high_bounce'])
                if shin_angle < 75:
                    suggestions.append(SUGGESTION_TEMPLATES['knee_pain_low_shin'])
            elif inj == '跟腱炎':
                if shin_angle < 75:
                    suggestions.append(SUGGESTION_TEMPLATES['achilles_low_shin'])
            elif inj == '足底筋膜炎':
                if shin_angle < 75:
                    suggestions.append(SUGGESTION_TEMPLATES['plantar_low_shin'])
            elif inj == '髂胫束综合征':
                if symmetry < 70:
                    suggestions.append(SUGGESTION_TEMPLATES['itbs_asymmetry'])
            elif inj == '髋关节痛':
                if abs(body_lean) > 10:
                    suggestions.append(SUGGESTION_TEMPLATES['hip_high_lean'])
            elif inj == '小腿胫骨应力性骨折':
                if bounce and bounce > 0.08:
                    suggestions.append(SUGGESTION_TEMPLATES['shin_splint_high_bounce'])

        # 通用建议（与伤病无关）
        if cadence < 160:
            suggestions.append(SUGGESTION_TEMPLATES['low_cadence'])
        if bounce and bounce > 0.10:
            suggestions.append(SUGGESTION_TEMPLATES['high_bounce'])
        if over_stride in ('偏多', '明显') or stride_dist >= 0.10:
            suggestions.append(SUGGESTION_TEMPLATES['over_stride'])
        if shin_angle < 70:
            suggestions.append(SUGGESTION_TEMPLATES['low_shin'])
        if knee_angle < 145:
            suggestions.append(SUGGESTION_TEMPLATES['high_knee'])
        elif knee_angle > 175:
            suggestions.append(SUGGESTION_TEMPLATES['low_knee'])
        if symmetry < 60:
            suggestions.append(SUGGESTION_TEMPLATES['asymmetry'])
        if body_lean > 12:
            suggestions.append(SUGGESTION_TEMPLATES['high_lean'])
        elif body_lean < 2:
            suggestions.append(SUGGESTION_TEMPLATES['low_lean'])

        # 去重
        seen = set()
        unique = []
        for s in suggestions:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique

    def get_profile_summary(self, user_id=None, username=None):
        """获取画像摘要（用于前端展示）"""
        profile = self.store.get_profile(user_id=user_id, username=username)
        if not profile:
            return None
        return {
            'height_cm': profile.get('height_cm'),
            'weight_kg': profile.get('weight_kg'),
            'training_level': profile.get('training_level', 'intermediate'),
            'training_level_label': {
                'beginner': '新手', 'intermediate': '进阶',
                'advanced': '高级', 'elite': '精英'
            }.get(profile.get('training_level', 'intermediate'), '进阶'),
            'weekly_km': profile.get('weekly_km'),
            'years_running': profile.get('years_running'),
            'injury_history': profile.get('injury_history', []),
            'goal': profile.get('goal', 'injury_prevention'),
            'goal_label': {
                'injury_prevention': '减伤优先', 'efficiency': '效率优先',
                'performance': '成绩提升'
            }.get(profile.get('goal', 'injury_prevention'), '减伤优先'),
            'focus_metrics': self.get_focus_metrics(user_id, username),
            'personalized_ranges': self.get_personalized_ranges(user_id, username),
        }
