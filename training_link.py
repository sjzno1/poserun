"""
PoseRun Analyzer - 训练计划联动模块
- 弱点识别：从异常指标提取最需改进的方面
- 训练推荐：匹配针对性练习
- 进度追踪：将训练建议与实际分析结果关联
"""

import json
from datetime import datetime
from measurement_safety import unscored, unavailable_training, finite


# 练习库：(指标条件, 练习名, 描述, 频率, 目标指标)
DRILL_LIBRARY = [
    {
        'condition': 'cadence_low',
        'metric': 'cadence',
        'check': lambda m: m.get('cadence') is not None and m['cadence'] < 165,
        'name': '步频提升跑',
        'desc': '用节拍器或音乐（BPM = 当前步频 + 5~10%），每段 200m，做 6~8 段，组间慢走 30s',
        'frequency': '每次跑步前 10 分钟',
        'target': '提升步频至 170+',
    },
    {
        'condition': 'high_bounce',
        'metric': 'vertical_oscillation',
        'check': lambda m: m.get('bounce') is not None and m['bounce'] > 0.08,
        'name': 'Pogo Jumps',
        'desc': '快速弹跳，脚跟不触地，膝盖微弯，20~30s × 3 组',
        'frequency': '热身或力量日',
        'target': '减少垂直振荡',
    },
    {
        'condition': 'low_shin',
        'metric': 'shin_angle',
        'check': lambda m: m.get('shin_angle') is not None and m['shin_angle'] < 75,
        'name': 'High Knees + A-Skip',
        'desc': '快速交替提膝至髋部高度 15~20s×3 组 + 提膝后快速落地 15~20s×3 组',
        'frequency': '热身',
        'target': '改善落地姿态，小腿保持竖直',
    },
    {
        'condition': 'over_stride',
        'metric': 'stride',
        'check': lambda m: m.get('over_stride') in ('偏多', '明显') or (m.get('stride_dist') or 0) >= 0.10,
        'name': 'Butt Kicks + 步频跑',
        'desc': '快速交替脚跟踢臀 15~20s×3 组 + 高步频短跑 80~120m×6 段',
        'frequency': '每次跑步前',
        'target': '减少跨步，脚落在重心正下方',
    },
    {
        'condition': 'asymmetry',
        'metric': 'symmetry',
        'check': lambda m: (m.get('symmetry_score') or 100) < 70,
        'name': '单腿平衡 + 单腿跳',
        'desc': '单腿平衡 3×30s（每侧）+ 单腿跳左右各 5 次×3 组',
        'frequency': '力量日',
        'target': '改善左右对称性',
    },
    {
        'condition': 'high_lean',
        'metric': 'body_lean',
        'check': lambda m: (m.get('body_lean') or 0) > 12,
        'name': '核心稳定性训练',
        'desc': '平板支撑 3×45s + 侧平板 3×30s + 死虫 3×12（每侧）',
        'frequency': '每日或力量日',
        'target': '减小前倾角度，保持躯干稳定',
    },
    {
        'condition': 'low_lean',
        'metric': 'body_lean',
        'check': lambda m: (m.get('body_lean') or 0) < 3,
        'name': '前倾意识训练',
        'desc': '轻松跑时想象从髋部前倾，让重力帮你前进。下坡跑 3×200m 体会前倾感',
        'frequency': '每次跑步',
        'target': '适度前倾（5~10°），利用重力前进',
    },
    {
        'condition': 'knee_flexion_low',
        'metric': 'knee_flexion',
        'check': lambda m: (m.get('knee_angle') or 160) < 150,
        'name': '靠墙静蹲 + 步频跑',
        'desc': '靠墙静蹲 3×30s 强化股四头肌 + 高步频短跑 6×200m 减少屈膝缓冲',
        'frequency': '力量日 + 跑步前',
        'target': '触地时膝盖放松微弯（10~20°）',
    },
]

# 基础力量训练（始终推荐）
BASE_STRENGTH = [
    '单腿硬拉 3×10（每侧）— 强化臀肌与腘绳肌',
    '保加利亚分腿蹲 3×8（每侧）— 单腿稳定性',
    '小腿提踵 3×15 — 增强踝部刚度，提升弹性',
]


def identify_weaknesses(metrics, gait, problems):
    """从分析结果中提取 2~3 个最需改进的方面"""
    if unscored(metrics):
        return []
    weaknesses = []
    metrics = metrics or {}
    gait = gait or {}

    # 从 problems 中提取
    for p in (problems or []):
        weaknesses.append(p)

    # 从指标中提取
    cadence = gait.get('cadence')
    if cadence is not None and cadence < 160:
        weaknesses.append(f'步频偏低（{cadence:.0f} 步/分），建议提升至 170+')

    bounce = gait.get('vertical_oscillation')
    if bounce is not None and bounce > 0.10:
        weaknesses.append(f'垂直振荡偏大（{bounce*100:.1f}% 腿长），能量损耗增加')

    over_stride = gait.get('strike_posture', {}).get('over_stride_level', '')
    if over_stride in ('偏多', '明显'):
        weaknesses.append(f'跨步{over_stride}，落地位置偏前，增加刹车效应')

    shin = metrics.get('avg_shin_angle', metrics.get('shin_angle'))
    if shin is not None and shin < 70:
        weaknesses.append(f'小腿角度偏低（{shin:.0f}°），脚跟触地明显')

    symmetry = metrics.get('symmetry', {}).get('symmetry_score')
    if symmetry is not None and symmetry < 60:
        weaknesses.append(f'左右不对称（{symmetry:.0f} 分），可能存在旧伤或发力不均')

    # 去重，取前 3 个
    seen = set()
    unique = []
    for w in weaknesses:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:3]


def recommend_drills(metrics, gait, profile=None):
    """匹配针对性练习"""
    if unscored(metrics):
        return {'drills': [], 'strength': [], 'count': 0}
    metrics = metrics or {}
    gait = gait or {}

    # 构建检查用字典
    check_dict = {
        'cadence': gait.get('cadence'),
        'bounce': gait.get('vertical_oscillation'),
        'shin_angle': metrics.get('avg_shin_angle', metrics.get('shin_angle')),
        'over_stride': gait.get('strike_posture', {}).get('over_stride_level', ''),
        'stride_dist': metrics.get('stride_analysis', {}).get('stride_distance', 0),
        'symmetry_score': metrics.get('symmetry', {}).get('symmetry_score', 100),
        'body_lean': metrics.get('avg_body_leang', metrics.get('body_lean', 0)),
        'knee_angle': metrics.get('avg_knee_flexion', metrics.get('knee_flexion', 160)),
    }

    recommended = []
    for drill in DRILL_LIBRARY:
        try:
            if drill['check'](check_dict):
                recommended.append({
                    'name': drill['name'],
                    'desc': drill['desc'],
                    'frequency': drill['frequency'],
                    'target': drill['target'],
                    'metric': drill['metric'],
                })
        except Exception:
            continue

    # 基础力量
    strength = list(BASE_STRENGTH)

    return {
        'drills': recommended,
        'strength': strength,
        'count': len(recommended),
    }


def build_training_session(metrics, gait, profile=None, problems=None):
    """构建完整训练联动方案"""
    if unscored(metrics):
        return unavailable_training()
    weaknesses = identify_weaknesses(metrics, gait, problems)
    drills = recommend_drills(metrics, gait, profile)

    # 周计划（根据弱点调整）
    weekly_plan = _build_weekly_plan(weaknesses, drills)

    return {
        'weaknesses': weaknesses,
        'drills': drills['drills'],
        'strength': drills['strength'],
        'weekly_plan': weekly_plan,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def _build_weekly_plan(weaknesses, drills):
    """根据弱点生成周训练计划"""
    has_cadence = any('步频' in w for w in weaknesses)
    has_bounce = any('振荡' in w for w in weaknesses)
    has_stride = any('跨步' in w for w in weaknesses)
    has_symmetry = any('对称' in w for w in weaknesses)

    drill_names = [d['name'] for d in drills['drills']]
    tech_drill = drill_names[0] if drill_names else '跑姿技术练习'

    plan = [
        ('周一', f'轻松跑 40~50 分钟（Zone 2）+ {tech_drill}', '技术 + 有氧基础'),
        ('周二', '力量训练（下肢 + 核心）25~30 分钟', '力量'),
        ('周三', '节奏跑：5 分钟热身 + 20 分钟阈值配速 + 5 分钟放松', '速度耐力'),
        ('周四', f'轻松跑 40 分钟 + {tech_drill}', '技术 + 恢复'),
        ('周五', '休息 或 交叉训练（游泳/骑行 30 分钟）', '恢复'),
        ('周六', '长距离慢跑 60~90 分钟（Zone 1~2），注意保持步频', '有氧耐力'),
        ('周日', '轻松恢复跑 30 分钟 或 完全休息', '恢复'),
    ]

    # 根据弱点调整
    if has_cadence:
        plan[0] = ('周一', '轻松跑 40 分钟 + 步频提升跑 6×200m（节拍器）', '步频 + 有氧')
        plan[3] = ('周四', '轻松跑 30 分钟 + 步频跑 8×100m（高步频）', '步频技术')
    if has_bounce:
        plan[1] = ('周二', '力量训练 + Pogo Jumps 3×30s', '力量 + 弹性')
    if has_stride:
        plan[3] = ('周四', '轻松跑 30 分钟 + Butt Kicks 3×20s + 高步频跑', '减少跨步')
    if has_symmetry:
        plan[1] = ('周二', '力量训练 + 单腿平衡 3×30s（每侧）', '力量 + 对称性')

    return plan


def track_progress(history_records):
    """追踪训练进度：对比历次分析，验证改进效果"""
    if not history_records or len(history_records) < 2:
        return None

    records = sorted(history_records, key=lambda r: r.get('created_at', ''))
    first = records[0]
    last = records[-1]

    progress = {
        'total_analyses': len(records),
        'first_date': first.get('created_at', ''),
        'last_date': last.get('created_at', ''),
        'score_change': (last['score'] - first['score'])
                        if finite(last.get('score')) and finite(first.get('score'))
                        and not unscored(first.get('metrics')) and not unscored(last.get('metrics'))
                        else None,
        'improvements': [],
        'regressions': [],
    }

    # 对比关键指标
    comparisons = [
        ('score', '综合得分', True),
        ('cadence', '步频', True),
        ('avg_shin', '小腿角度', True),
        ('avg_knee', '膝盖角度', None),
        ('body_lean', '前倾角度', None),
    ]

    for key, label, higher_better in comparisons:
        a = first.get(key)
        b = last.get(key)
        if (not finite(a) or not finite(b) or unscored(first.get('metrics'))
                or unscored(last.get('metrics'))):
            continue
        delta = round(b - a, 1)
        if higher_better is None:
            continue
        if higher_better and delta > 0:
            progress['improvements'].append(f'{label} +{delta}')
        elif higher_better and delta < 0:
            progress['regressions'].append(f'{label} {delta}')
        elif not higher_better and delta < 0:
            progress['improvements'].append(f'{label} {delta}（降低为好）')
        elif not higher_better and delta > 0:
            progress['regressions'].append(f'{label} +{delta}（升高为好）')

    return progress
