"""
PoseRun Analyzer - 趋势追踪模块
- 跨时间维度的跑姿变化追踪
- A/B 对比 + 基线对比
- 变化提醒（指标显著变化检测）
"""

import json
import math
from datetime import datetime, timedelta
from measurement_safety import unscored, finite


# 指标定义：(key, label, unit, higher_is_better)
TREND_METRICS = [
    ('score', '综合得分', '', True),
    ('cadence', '步频', 'spm', True),
    ('avg_knee', '膝盖弯曲角度', '°', None),  # 无明确方向
    ('avg_shin', '小腿角度', '°', True),
    ('body_lean', '身体前倾角度', '°', None),
    ('vertical_oscillation', '垂直振荡', '%腿长', False),
    ('ground_contact_time_ms', '着地时间', 'ms', False),
    ('stride_length_m', '步幅', 'm', None),
    ('step_symmetry', '左右对称', '%', True),
]

# 显著变化阈值（绝对值变化超过此值视为"显著"）
SIGNIFICANT_CHANGE = {
    'score': 5,
    'cadence': 10,
    'avg_knee': 5,
    'avg_shin': 5,
    'body_lean': 3,
    'vertical_oscillation': 0.02,
    'ground_contact_time_ms': 30,
    'stride_length_m': 0.1,
    'step_symmetry': 10,
}


def compute_trend(records):
    """计算指标趋势。records 按时间 旧→新 排列。
    返回 {metric_key: {values: [...], first, last, delta, pct_change, direction, significant}}"""
    if not records or len(records) < 2:
        return {}

    result = {}
    for key, label, unit, higher_better in TREND_METRICS:
        values = []
        for r in records:
            v = _extract_metric(r, key)
            values.append(v)

        valid = [v for v in values if v is not None]
        if len(valid) < 2:
            continue

        first = valid[0]
        last = valid[-1]
        delta = round(last - first, 2)
        pct_change = round((delta / first * 100), 1) if first else None

        # 方向判断
        if higher_better is None:
            direction = 'neutral'
        elif higher_better:
            direction = 'improved' if delta > 0 else ('worsened' if delta < 0 else 'flat')
        else:
            direction = 'improved' if delta < 0 else ('worsened' if delta > 0 else 'flat')

        # 显著性
        threshold = SIGNIFICANT_CHANGE.get(key, 5)
        significant = abs(delta) >= threshold

        result[key] = {
            'label': label,
            'unit': unit,
            'values': values,
            'first': first,
            'last': last,
            'delta': delta,
            'pct_change': pct_change,
            'direction': direction,
            'significant': significant,
        }

    return result


def extract_alerts(trend):
    """从趋势中提取变化提醒"""
    alerts = []
    for key, data in trend.items():
        if not data.get('significant'):
            continue
        label = data['label']
        delta = data['delta']
        unit = data['unit']
        direction = data['direction']

        if direction == 'improved':
            alerts.append({
                'metric': key,
                'label': label,
                'type': 'improvement',
                'message': f'{label}改善 {abs(delta):.1f}{unit}（{data["first"]} → {data["last"]}）',
            })
        elif direction == 'worsened':
            alerts.append({
                'metric': key,
                'label': label,
                'type': 'warning',
                'message': f'{label}下降 {abs(delta):.1f}{unit}（{data["first"]} → {data["last"]}）',
            })
    return alerts


def compare_ab(rec_a, rec_b):
    """A/B 对比：返回 {metrics: {key: {a, b, delta, direction}}, summary: str}"""
    if not rec_a or not rec_b:
        return None

    metrics = {}
    for key, label, unit, higher_better in TREND_METRICS:
        va = _extract_metric(rec_a, key)
        vb = _extract_metric(rec_b, key)
        if va is None or vb is None:
            continue
        delta = round(vb - va, 2)
        if higher_better is None:
            direction = 'neutral'
        elif higher_better:
            direction = 'improved' if delta > 0 else ('worsened' if delta < 0 else 'flat')
        else:
            direction = 'improved' if delta < 0 else ('worsened' if delta > 0 else 'flat')
        metrics[key] = {
            'label': label,
            'unit': unit,
            'a': va,
            'b': vb,
            'delta': delta,
            'direction': direction,
        }

    # 摘要
    improved = sum(1 for m in metrics.values() if m['direction'] == 'improved')
    worsened = sum(1 for m in metrics.values() if m['direction'] == 'worsened')
    if improved > worsened:
        summary = f'整体改善（{improved} 项提升 / {worsened} 项下降）'
    elif worsened > improved:
        summary = f'整体下降（{worsened} 项下降 / {improved} 项提升）'
    else:
        summary = '变化不大'

    return {'metrics': metrics, 'summary': summary}


def set_baseline(records):
    """设定基线：取最早一条记录作为基线，其余记录与基线对比"""
    if not records or len(records) < 1:
        return None
    baseline = records[0]
    comparisons = []
    for r in records[1:]:
        cmp = compare_ab(baseline, r)
        if cmp:
            cmp['record_id'] = r.get('id')
            cmp['date'] = r.get('created_at', '')
            comparisons.append(cmp)
    return {
        'baseline_id': baseline.get('id'),
        'baseline_date': baseline.get('created_at', ''),
        'baseline_score': baseline.get('score'),
        'comparisons': comparisons,
    }


def _extract_metric(record, key):
    """从记录中提取指标值（支持嵌套 gait 指标）"""
    if unscored(record.get('metrics')):
        return None
    if key in record:
        v = record.get(key)
        return float(v) if finite(v) else None
    # gait 嵌套指标
    gait = (record.get('metrics') or {}).get('gait') or {}
    if key == 'vertical_oscillation':
        v = gait.get('vertical_oscillation')
        return round(v * 100, 1) if v is not None else None
    if key == 'ground_contact_time_ms':
        return gait.get('ground_contact_time_ms')
    if key == 'stride_length_m':
        return gait.get('stride_length_m')
    if key == 'step_symmetry':
        v = gait.get('step_symmetry')
        return round(v * 100, 1) if v is not None else None
    return None


def get_time_series(records, limit=30):
    """获取时间序列数据（用于折线图）"""
    records = sorted(records, key=lambda r: r.get('created_at', ''))[-limit:]
    series = {key: [] for key, _, _, _ in TREND_METRICS}
    dates = []
    for r in records:
        date = (r.get('created_at') or '')[:10]
        dates.append(date)
        for key, _, _, _ in TREND_METRICS:
            series[key].append(_extract_metric(r, key))
    return {'dates': dates, 'series': series}
