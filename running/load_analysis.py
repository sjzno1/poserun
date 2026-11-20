"""训练负荷分析（P2 切片7）：TRIMP 日负荷＋Banister EWMA CTL/ATL/TSB。AGPL-3.0

基于现有活动实时计算，无 schema 变更、无新表。口径：
+- 单次 TRIMP：男 t×ΔHR×0.64×e^(1.92ΔHR)／女 t×ΔHR×0.86×e^(1.67ΔHR)，
+  t 为时长（分钟），ΔHR=(avg_hr−HRrest)/(HRmax−HRrest) 截断[0,1]；
+- HRmax/HRrest/sex 优先 profile（当前标定档案无此三字段→缺省 190/60/male）；
+- 无平均心率（或心率参数无效）fallback=时长(分钟)×0.5；时长缺测→None（不计负荷，标 missing）；
+- 按北京日期（Asia/Shanghai，UTC+8 固定偏移）聚合日负荷，软删排除；
+- EWMA：CTL τ=42 天、ATL τ=7 天，种子 0，含零负荷日；TSB_n=CTL_{n−1}−ATL_{n−1}；
+- 固定窗口：series 为最近 days 天（含今日），零负荷日补 0；EWMA 自窗口首日（today−days+1）种子 0 递推，
+  窗口前的活动不参与本窗口计算；空数据→全 0。
+所有假设随 payload assumptions 披露；缺测不补成结论。
"""
import math
from datetime import date, datetime, timedelta, timezone

BJT = timezone(timedelta(hours=8))  # Asia/Shanghai 自 1991 年起无夏令时，固定 UTC+8
CTL_DAYS = 42
ATL_DAYS = 7
HR_MAX_DEFAULT = 190.0
HR_REST_DEFAULT = 60.0
SEX_DEFAULT = 'male'
NO_DATA = '暂无活动记录，无法评估训练负荷。'


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def trimp(duration_s, avg_hr, hr_max=HR_MAX_DEFAULT, hr_rest=HR_REST_DEFAULT, sex=SEX_DEFAULT):
    """单次训练负荷（TRIMP）。缺时长→None；缺/无效心率→t_min×0.5 fallback。"""
    if not _finite(duration_s) or duration_s < 0:
        return None
    t_min = duration_s / 60.0
    if not _finite(avg_hr) or not _finite(hr_max) or not _finite(hr_rest) or hr_max <= hr_rest:
        return t_min * 0.5
    dhr = (avg_hr - hr_rest) / (hr_max - hr_rest)
    dhr = max(0.0, min(1.0, dhr))
    k, b = (0.86, 1.67) if str(sex).lower() == 'female' else (0.64, 1.92)
    return t_min * dhr * k * math.exp(b * dhr)


def beijing_date(start_time):
    """活动开始时间（ISO，UTC 或带偏移）→ 北京日期。"""
    dt = datetime.fromisoformat(str(start_time).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJT).date()


def daily_loads(rows, hr_max, hr_rest, sex):
    """活动行→{北京日期: {load, activities, missing}}；缺时长活动计 missing 不计负荷。"""
    daily = {}
    for row in rows:
        day = beijing_date(row['start_time']).isoformat()
        entry = daily.setdefault(day, {'load': 0.0, 'activities': 0, 'missing': 0})
        entry['activities'] += 1
        load = trimp(row.get('duration_s'), row.get('avg_hr'), hr_max, hr_rest, sex)
        if load is None:
            entry['missing'] += 1
        else:
            entry['load'] += load
    return daily


def ewma_series(daily, start, end):
    """逐日 EWMA（含零负荷日）。种子 CTL0=ATL0=0；TSB_n=CTL_{n−1}−ATL_{n−1}。空 daily→全 0 逐日点。"""
    out = []
    ctl = atl = 0.0
    day = start
    while day <= end:
        iso = day.isoformat()
        entry = daily.get(iso)
        load = entry['load'] if entry else 0.0
        tsb = ctl - atl  # 前一日值（首日为种子 0）
        ctl += (load - ctl) / CTL_DAYS
        atl += (load - atl) / ATL_DAYS
        out.append({'date': iso, 'daily_load': load, 'ctl': ctl, 'atl': atl, 'tsb': tsb,
                    'activities': entry['activities'] if entry else 0,
                    'missing': entry['missing'] if entry else 0})
        day += timedelta(days=1)
    return out


def interpret(tsb, ratio, has_data=True):
    """确定性解读（阈值固定，非医疗建议）。"""
    if not has_data or tsb is None:
        return NO_DATA
    if tsb < -30:
        base = '疲劳积累较深：建议优先安排恢复日，避免连续高强度。'
    elif tsb < -10:
        base = '疲劳偏高：注意恢复与睡眠，谨慎追加负荷。'
    elif tsb <= 5:
        base = '负荷与恢复基本平衡：可按计划渐进训练。'
    elif tsb <= 15:
        base = '恢复充分、状态回升：适合渐进增加训练量。'
    else:
        base = '充分恢复：可考虑提升强度或上量。'
    if ratio is not None:
        if ratio > 1.5:
            base += ' 急性/慢性负荷比偏高（>1.5），注意伤病风险。'
        elif ratio < 0.8:
            base += ' 急性负荷低于慢性水平，近期训练量偏少。'
    return base


def build_load(store, uid, days=90, today=None, profile=None):
    """实时计算负荷 payload：series＋summary＋assumptions。仅本人（uid 来自会话）。"""
    profile = profile or {}
    hr_max = profile['hr_max'] if _finite(profile.get('hr_max')) else HR_MAX_DEFAULT
    hr_rest = profile['hr_rest'] if _finite(profile.get('hr_rest')) else HR_REST_DEFAULT
    sex = 'female' if str(profile.get('sex') or SEX_DEFAULT).lower() == 'female' else 'male'
    source = 'profile' if any(profile.get(k) is not None for k in ('hr_max', 'hr_rest', 'sex')) else 'default'
    with store.conn() as conn:
        rows = [dict(r) for r in conn.execute(
            'SELECT start_time, duration_s, avg_hr FROM activities '
            'WHERE user_id=? AND deleted_at IS NULL ORDER BY start_time, id', (uid,))]
    daily = daily_loads(rows, hr_max, hr_rest, sex)
    if today is None:
        today = datetime.now(timezone.utc).astimezone(BJT).date()
    # 固定窗口：today-(days-1) 至 today，种子 0 自窗口首日逐日递推（含零负荷日）；
    # 窗口前/未来的活动不参与本窗口计算（规格：EWMA 自窗口首日起算，空数据→全 0）。
    window_start = today - timedelta(days=days - 1)
    series = ewma_series(daily, window_start, today)
    last = series[-1]
    ratio = last['atl'] / last['ctl'] if last['ctl'] > 0 else None
    has_data = any(p['activities'] > 0 for p in series)
    summary = {'current_ctl': last['ctl'], 'current_atl': last['atl'],
               'current_tsb': last['tsb'], 'acute_chronic_ratio': ratio,
               'load_7d': sum(p['daily_load'] for p in series[-7:]),
               'interpretation': interpret(last['tsb'], ratio, has_data=has_data)}
    assumptions = {
        'hr_max': hr_max, 'hr_rest': hr_rest, 'sex': sex, 'hr_params_source': source,
        'trimp': 'Banister TRIMP：男 t×ΔHR×0.64×e^(1.92ΔHR)，女 t×ΔHR×0.86×e^(1.67ΔHR)；'
                 'ΔHR=(avg_hr−HRrest)/(HRmax−HRrest)，截断[0,1]；t 为时长（分钟）',
        'fallback': '无平均心率（或心率参数无效）时负荷=时长(分钟)×0.5；时长缺测的活动不计负荷并标记 missing',
        'timezone': 'Asia/Shanghai（UTC+8，按北京日期聚合；软删除活动排除）',
        'ewma': {'ctl_days': CTL_DAYS, 'atl_days': ATL_DAYS, 'seed': 0},
        'tsb': 'TSB_n = CTL_{n-1} − ATL_{n-1}（当日训练前的体能-疲劳差）；EWMA 含零负荷日，自窗口首日种子 0 递推',
        'window': 'series 固定 days 点（最近 days 天含今日，北京）；零负荷日补 0；窗口首日=today−days+1，'
                  'EWMA 种子 0 自该日递推，窗口前的活动不参与本窗口计算；空数据→全 0',
        'interpretation': '解读阈值（规格三档细化）：TSB<−30 深度疲劳；−30≤TSB<−10 疲劳偏高；−10≤TSB≤5 平衡；'
                          '5<TSB≤15 恢复回升；TSB>15 充分恢复；急慢比>1.5 提示伤病风险、<0.8 提示近期偏少',
        'disclaimer': '训练负荷参考，非医疗建议；当前标定档案无心率/性别字段，采用缺省假设。',
    }
    return {'days': days, 'generated_at': datetime.now(timezone.utc).isoformat(),
            'timezone': 'Asia/Shanghai', 'series': series, 'summary': summary,
            'assumptions': assumptions}