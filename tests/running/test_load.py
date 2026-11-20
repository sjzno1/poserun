"""P2 切片7 训练负荷分析（TRIMP 日负荷＋Banister EWMA CTL/ATL/TSB）契约测试（TDD 红）。AGPL-3.0

锁定：
+- 单次 TRIMP：男 t×ΔHR×0.64×e^(1.92ΔHR)／女 t×ΔHR×0.86×e^(1.67ΔHR)，
+  ΔHR=(avg_hr−HRrest)/(HRmax−HRrest) clamp[0,1]；无 avg_hr fallback=t_min×0.5；
+- HRmax/HRrest/sex 优先 profile（当前标定档案无此三字段→缺省 190/60/male，assumptions 披露）；
+- 按北京日期（UTC+8）聚合日负荷，软删排除；缺时长活动不计负荷并标 missing；
+- EWMA：CTL τ=42 天、ATL τ=7 天，种子 0，含零负荷日；TSB_n=CTL_{n−1}−ATL_{n−1}；
+- GET /api/running/load?days=90（7..365，非法 400）→ series＋summary＋assumptions；
+- 开关 LOAD_ENABLED 默认关→API/页面 404＋dashboard 旗标；匿名 401；no-store；
+- 仅本人：?owner=/?user_id= 伪造无效、同名 A/B 隔离；设备来源记录只读（PATCH 403）但计入负荷；
+- 无 schema 变更（v5，不新增表）。
+
+数学正确性：TRIMP/EWMA/TSB 全部对照独立手算参考值（Fraction 精确递推＋math 直式，
+冻结字面量见下方常量），容差 1e-6；不以自身实现互验。
"""
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask, g

from running import register_running

# ---- 独立手算参考值（冻结字面量；生成脚本见 ops evidence/tdd-log）----
# TRIMP：t 分钟、ΔHR clamp 后直式计算
REF_TRIMP_MALE_60_150 = 100.44029272374173      # 60min hr150 (190/60) ΔHR=90/130
REF_TRIMP_FEMALE_45_140 = 75.40544467823685     # 45min hr140 (185/55) ΔHR=85/130
REF_TRIMP_MALE_CLAMP_HIGH = 130.96240261038238  # 30min hr250→ΔHR=1: 30×0.64×e^1.92
REF_TRIMP_FEMALE_60_150 = 113.51678807244653    # 60min hr150 (190/60) 女性系数
# EWMA 日负荷序列 [100,0,50,0,0,80,0,0,60,0]，种子0，α=1/42/1/7，TSB_n=CTL_{n-1}-ATL_{n-1}
# 每行 (load, ctl, atl, tsb)，Fraction 精确递推后转 float
REF_EWMA_10 = [
    (100.0, 2.380952380952381, 14.285714285714286, 0.0),
    (0.0, 2.324263038548753, 12.244897959183673, -11.904761904761905),
    (50.0, 3.4593996328690206, 17.638483965014576, -9.920634920634921),
    (0.0, 3.377032974943568, 15.118700541441067, -14.179084332145557),
    (0.0, 3.2966274279211016, 12.958886178378057, -11.7416675664975),
    (80.0, 5.12289820344679, 22.536188152895477, -9.662258750456955),
    (0.0, 5.000924436698057, 19.316732702481836, -17.413289949448686),
    (0.0, 4.881854807252865, 16.557199459270148, -14.315808265783781),
    (60.0, 6.194191597556368, 22.763313822231556, -11.675344652017282),
    (0.0, 6.046710845233598, 19.511411847627045, -16.569122224675187),
]
REF_EWMA_IMPULSE = [  # [100,0,0,0,0]
    (100.0, 2.380952380952381, 14.285714285714286, 0.0),
    (0.0, 2.324263038548753, 12.244897959183673, -11.904761904761905),
    (0.0, 2.26892344239283, 10.495626822157435, -9.920634920634921),
    (0.0, 2.2149014556691915, 8.99625156184923, -8.226703379764604),
    (0.0, 2.1621657067246867, 7.711072767299339, -6.781350106180038),
]
REF_STEADY_LAST = (42.0, 41.969547763992935, 42.0, -0.03119497347064983)  # 42/天×300
# API 场景：fallback 负荷 100/50/80/60 于北京 2026-11-01/03/06/09，today=2026-11-10
REF_API_LAST = (0.0, 6.046710845233598, 19.511411847627045, -16.569122224675187)
REF_API_LOAD7D = 140.0
REF_API_RATIO = 3.2267810297241484
TOL = 1e-6


@pytest.fixture
def web(tmp_path):
    app = Flask(__name__, static_folder='../../static', static_url_path='')
    app.config['TESTING'] = True
    uid = [1]

    @app.before_request
    def user():
        if uid[0] is not None:
            g.current_user = {'id': uid[0]}

    register_running(app, data_dir=tmp_path)
    app.config['LOAD_ENABLED'] = True
    app.config['MANUAL_ENTRY_ENABLED'] = True  # 设备来源 PATCH 403 口径沿用手工补录切片
    return app.test_client(), uid, app


def _add(store, uid, when, **changes):
    row = dict(source='fit', sport='running', start_time=when, distance_m=5000, duration_s=1800)
    row.update(changes)
    return store.add(uid, row)[0]


def _seq_daily(loads, start=date(2026, 11, 1)):
    return {(start + timedelta(days=i)).isoformat(): {'load': float(v), 'activities': 1 if v else 0, 'missing': 0}
            for i, v in enumerate(loads)}


# ---------------- 旗标与页面 ----------------

def test_flag_default_off_404_and_dashboard_flag(tmp_path):
    app = Flask(__name__, static_folder='../../static', static_url_path='')
    uid = [1]

    @app.before_request
    def user():
        if uid[0] is not None:
            g.current_user = {'id': uid[0]}

    register_running(app, data_dir=tmp_path)
    assert app.config['LOAD_ENABLED'] is False  # 默认关
    c = app.test_client()
    assert c.get('/api/running/load').status_code == 404
    assert c.get('/api/running/load?days=90').status_code == 404
    assert c.get('/training-load').status_code == 404
    assert c.get('/api/running/dashboard').json['load_enabled'] is False
    # 旧端点零影响
    assert c.get('/api/running/activities').status_code == 200


def test_flag_toggle_page_and_dashboard_exposure(web):
    c, uid, app = web
    assert c.get('/api/running/dashboard').json['load_enabled'] is True
    assert c.get('/training-load').status_code == 200
    assert c.get('/api/running/load').status_code == 200
    app.config['LOAD_ENABLED'] = False
    assert c.get('/api/running/dashboard').json['load_enabled'] is False
    assert c.get('/api/running/load').status_code == 404
    assert c.get('/training-load').status_code == 404
    # 其他页面不受 LOAD 开关影响
    assert c.get('/api/running/activities').status_code == 200
    app.config['LOAD_ENABLED'] = True
    assert c.get('/api/running/load').status_code == 200
    assert c.get('/training-load').status_code == 200


# ---------------- TRIMP 手算参考值 ----------------

def test_trimp_male_reference():
    from running.load_analysis import trimp
    assert trimp(3600, 150, 190, 60, 'male') == pytest.approx(REF_TRIMP_MALE_60_150, abs=TOL)


def test_trimp_female_reference():
    from running.load_analysis import trimp
    assert trimp(2700, 140, 185, 55, 'female') == pytest.approx(REF_TRIMP_FEMALE_45_140, abs=TOL)
    assert trimp(3600, 150, 190, 60, 'female') == pytest.approx(REF_TRIMP_FEMALE_60_150, abs=TOL)


def test_trimp_clamp_high_and_low():
    from running.load_analysis import trimp
    assert trimp(1800, 250, 190, 60, 'male') == pytest.approx(REF_TRIMP_MALE_CLAMP_HIGH, abs=TOL)
    assert trimp(1800, 40, 190, 60, 'male') == 0.0  # ΔHR clamp 到 0
    assert trimp(1800, 60, 190, 60, 'male') == 0.0  # 等于静息心率


def test_trimp_fallback_and_missing():
    from running.load_analysis import trimp
    assert trimp(2400, None, 190, 60, 'male') == 20.0  # 40min×0.5
    assert trimp(2400, None, 185, 55, 'female') == 20.0  # fallback 与性别/心率参数无关
    assert trimp(None, 150, 190, 60, 'male') is None  # 缺时长无法计负荷
    assert trimp(None, None, 190, 60, 'male') is None
    assert trimp(0, 150, 190, 60, 'male') == 0.0
    assert trimp(float('nan'), 150, 190, 60, 'male') is None
    assert trimp(3600, float('inf'), 190, 60, 'male') == 30.0  # 非有限心率按缺测 fallback


def test_trimp_invalid_hr_span_falls_back():
    from running.load_analysis import trimp
    assert trimp(2400, 150, 60, 190, 'male') == 20.0  # HRmax<=HRrest → fallback
    assert trimp(2400, 150, 100, 100, 'male') == 20.0


def test_trimp_sex_defaults_to_male():
    from running.load_analysis import trimp
    for sex in (None, '', 'unknown', 'male', 'MALE'):
        assert trimp(3600, 150, 190, 60, sex) == pytest.approx(REF_TRIMP_MALE_60_150, abs=TOL), sex
    assert trimp(3600, 150, 190, 60, 'FEMALE') == pytest.approx(REF_TRIMP_FEMALE_60_150, abs=TOL)


# ---------------- EWMA/TSB 手算参考值 ----------------

def test_ewma_ten_day_reference():
    from running.load_analysis import ewma_series
    daily = _seq_daily([100, 0, 50, 0, 0, 80, 0, 0, 60, 0])
    out = ewma_series(daily, date(2026, 11, 1), date(2026, 11, 10))
    assert len(out) == 10
    for i, (point, ref) in enumerate(zip(out, REF_EWMA_10)):
        assert point['date'] == (date(2026, 11, 1) + timedelta(days=i)).isoformat()
        for key, want in zip(('daily_load', 'ctl', 'atl', 'tsb'), ref):
            assert point[key] == pytest.approx(want, abs=TOL), (i, key)


def test_ewma_impulse_reference():
    from running.load_analysis import ewma_series
    daily = _seq_daily([100, 0, 0, 0, 0])
    out = ewma_series(daily, date(2026, 11, 1), date(2026, 11, 5))
    for point, ref in zip(out, REF_EWMA_IMPULSE):
        for key, want in zip(('daily_load', 'ctl', 'atl', 'tsb'), ref):
            assert point[key] == pytest.approx(want, abs=TOL), (point['date'], key)


def test_ewma_steady_state_reference():
    from running.load_analysis import ewma_series
    daily = _seq_daily([42] * 300, start=date(2026, 1, 1))
    out = ewma_series(daily, date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=299))
    last = out[-1]
    for key, want in zip(('daily_load', 'ctl', 'atl', 'tsb'), REF_STEADY_LAST):
        assert last[key] == pytest.approx(want, abs=TOL), key


def test_ewma_seed_zero_tsb_uses_previous_day():
    from running.load_analysis import ewma_series
    daily = _seq_daily([100, 0, 50, 0, 0, 80, 0, 0, 60, 0])
    out = ewma_series(daily, date(2026, 11, 1), date(2026, 11, 10))
    assert out[0]['tsb'] == 0.0  # 种子 CTL0=ATL0=0
    for prev, cur in zip(out, out[1:]):
        assert cur['tsb'] == pytest.approx(prev['ctl'] - prev['atl'], abs=1e-9)
    # 零负荷日必须参与衰减（第2天无活动，ATL 仍衰减）
    assert out[1]['daily_load'] == 0.0 and out[1]['atl'] < out[0]['atl']


def test_ewma_empty_range():
    from running.load_analysis import ewma_series
    out = ewma_series({}, date(2026, 11, 1), date(2026, 11, 10))
    assert len(out) == 10  # 空数据→全 0 逐日点（含零负荷日）
    assert all(p['daily_load'] == 0.0 and p['ctl'] == 0.0 and p['atl'] == 0.0 and p['tsb'] == 0.0
               and p['activities'] == 0 for p in out)


# ---------------- build_load：store→北京日聚合→EWMA→payload ----------------

def _add_reference_activities(store, uid):
    # fallback 负荷（无 hr）：200min→100、100min→50、160min→80、120min→60
    _add(store, uid, '2026-11-01T02:00:00+00:00', duration_s=12000, avg_hr=None, file_sha256='synthetic-load-1')
    _add(store, uid, '2026-11-03T02:00:00+00:00', duration_s=6000, avg_hr=None, file_sha256='synthetic-load-2')
    _add(store, uid, '2026-11-06T02:00:00+00:00', duration_s=9600, avg_hr=None, file_sha256='synthetic-load-3')
    _add(store, uid, '2026-11-09T02:00:00+00:00', duration_s=7200, avg_hr=None, file_sha256='synthetic-load-4')


def test_build_load_reference_end_to_end(web):
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    _add_reference_activities(store, 1)
    d = build_load(store, 1, days=90, today=date(2026, 11, 10))
    assert d['days'] == 90 and d['timezone'] == 'Asia/Shanghai'
    series = d['series']
    assert len(series) == 90  # 固定窗口：today-89 至 today，含零负荷日
    assert series[0]['date'] == '2026-08-13' and series[-1]['date'] == '2026-11-10'
    assert series[0]['daily_load'] == 0.0 and series[0]['ctl'] == 0.0 and series[0]['activities'] == 0
    last = series[-1]
    for key, want in zip(('daily_load', 'ctl', 'atl', 'tsb'), REF_API_LAST):
        assert last[key] == pytest.approx(want, abs=TOL), key
    s = d['summary']
    assert s['current_ctl'] == pytest.approx(REF_API_LAST[1], abs=TOL)
    assert s['current_atl'] == pytest.approx(REF_API_LAST[2], abs=TOL)
    assert s['current_tsb'] == pytest.approx(REF_API_LAST[3], abs=TOL)
    assert s['load_7d'] == pytest.approx(REF_API_LOAD7D, abs=TOL)
    assert s['acute_chronic_ratio'] == pytest.approx(REF_API_RATIO, abs=TOL)
    # tsb=-16.57 → 疲劳偏高；ratio 3.23>1.5 → 伤风险后缀
    assert s['interpretation'] == '疲劳偏高：注意恢复与睡眠，谨慎追加负荷。 急性/慢性负荷比偏高（>1.5），注意伤病风险。'
    # 日聚合：11-01 负荷 100，11-02 零负荷日（窗口内按日期取点）
    by_date = {p['date']: p for p in series}
    assert by_date['2026-11-01']['daily_load'] == pytest.approx(100.0, abs=TOL)
    assert by_date['2026-11-02']['daily_load'] == 0.0 and by_date['2026-11-02']['activities'] == 0


def test_beijing_date_boundary_aggregation(web):
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    # 2025-12-31T16:00Z = 北京 2026-01-01 00:00；15:59:59Z 仍属 2025-12-31
    _add(store, 1, '2025-12-31T16:00:00+00:00', duration_s=12000, avg_hr=None, file_sha256='synthetic-bj-1')
    _add(store, 1, '2025-12-31T15:59:59+00:00', duration_s=6000, avg_hr=None, file_sha256='synthetic-bj-2')
    d = build_load(store, 1, days=365, today=date(2026, 1, 2))
    by_date = {p['date']: p for p in d['series']}
    assert by_date['2025-12-31']['daily_load'] == pytest.approx(50.0, abs=TOL)
    assert by_date['2026-01-01']['daily_load'] == pytest.approx(100.0, abs=TOL)
    assert by_date['2026-01-02']['daily_load'] == 0.0


def test_same_beijing_day_activities_sum(web):
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    _add(store, 1, '2026-11-01T01:00:00+00:00', duration_s=6000, avg_hr=None, file_sha256='synthetic-sum-1')
    _add(store, 1, '2026-11-01T09:00:00+00:00', duration_s=6000, avg_hr=None, file_sha256='synthetic-sum-2')
    d = build_load(store, 1, days=90, today=date(2026, 11, 1))
    assert d['series'][-1]['daily_load'] == pytest.approx(100.0, abs=TOL)  # 50+50
    assert d['series'][-1]['activities'] == 2


def test_hr_activity_uses_trimp_reference(web):
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    _add(store, 1, '2026-11-01T02:00:00+00:00', duration_s=3600, avg_hr=150, file_sha256='synthetic-hr-1')
    d = build_load(store, 1, days=90, today=date(2026, 11, 1))
    assert d['series'][-1]['daily_load'] == pytest.approx(REF_TRIMP_MALE_60_150, abs=TOL)


def test_missing_duration_flagged_not_zero_filled(web):
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    _add(store, 1, '2026-11-01T02:00:00+00:00', duration_s=None, avg_hr=150, file_sha256='synthetic-miss-1')
    _add(store, 1, '2026-11-01T03:00:00+00:00', duration_s=6000, avg_hr=None, file_sha256='synthetic-miss-2')
    d = build_load(store, 1, days=90, today=date(2026, 11, 1))
    p = d['series'][-1]
    assert p['daily_load'] == pytest.approx(50.0, abs=TOL)  # 缺时长不计负荷
    assert p['missing'] == 1 and p['activities'] == 2


def test_soft_deleted_excluded(web):
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    keep = _add(store, 1, '2026-11-01T02:00:00+00:00', duration_s=6000, avg_hr=None, file_sha256='synthetic-del-1')
    gone = _add(store, 1, '2026-11-01T04:00:00+00:00', duration_s=12000, avg_hr=None, file_sha256='synthetic-del-2')
    assert store.soft_delete(1, gone['id']) is True
    d = build_load(store, 1, days=90, today=date(2026, 11, 1))
    assert d['series'][-1]['daily_load'] == pytest.approx(50.0, abs=TOL)
    assert d['series'][-1]['activities'] == 1


def test_empty_user_payload(web):
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    d = build_load(store, 1, days=90, today=date(2026, 11, 10))
    assert len(d['series']) == 90  # 空数据→固定窗口全 0
    assert all(p['daily_load'] == 0.0 and p['ctl'] == 0.0 and p['atl'] == 0.0 and p['tsb'] == 0.0
               and p['activities'] == 0 for p in d['series'])
    s = d['summary']
    assert s['current_ctl'] == 0.0 and s['current_atl'] == 0.0 and s['current_tsb'] == 0.0
    assert s['acute_chronic_ratio'] is None and s['load_7d'] == 0.0
    assert s['interpretation'] == '暂无活动记录，无法评估训练负荷。'
    assert d['assumptions']['hr_max'] == 190 and d['assumptions']['hr_rest'] == 60
    assert d['assumptions']['sex'] == 'male'


def test_assumptions_defaults_and_profile_priority(web):
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    _add(store, 1, '2026-11-01T02:00:00+00:00', duration_s=3600, avg_hr=150, file_sha256='synthetic-prof-1')
    d = build_load(store, 1, days=90, today=date(2026, 11, 1))
    a = d['assumptions']
    assert a['hr_max'] == 190 and a['hr_rest'] == 60 and a['sex'] == 'male'
    assert a['hr_params_source'] == 'default'
    assert a['ewma'] == {'ctl_days': 42, 'atl_days': 7, 'seed': 0}
    assert 'TRIMP' in a['trimp'] and 'TSB' in a['tsb'] and '0.5' in a['fallback']
    # profile 优先（接口预留；当前标定档案无此三字段）
    d2 = build_load(store, 1, days=90, today=date(2026, 11, 1),
                    profile={'hr_max': 185, 'hr_rest': 55, 'sex': 'female'})
    a2 = d2['assumptions']
    assert a2['hr_max'] == 185 and a2['hr_rest'] == 55 and a2['sex'] == 'female'
    assert a2['hr_params_source'] == 'profile'
    # 女性 185/55 hr150：ΔHR=95/130，独立直式
    dhr = (150 - 55) / (185 - 55)
    want = 60 * dhr * 0.86 * math.exp(1.67 * dhr)
    assert d2['series'][-1]['daily_load'] == pytest.approx(want, abs=TOL)


def test_series_window_seeds_zero_at_window_start(web):
    """规格：EWMA 自窗口首日（today−days+1）起种子 0 逐日递推；窗口前活动不参与本窗口计算。"""
    from running.load_analysis import build_load
    c, uid, app = web
    store = app.extensions['running_store']
    _add_reference_activities(store, 1)  # 11-01:100、11-03:50、11-06:80、11-09:60
    d90 = build_load(store, 1, days=90, today=date(2026, 11, 10))
    assert len(d90['series']) == 90 and d90['series'][0]['date'] == '2026-08-13'
    d7 = build_load(store, 1, days=7, today=date(2026, 11, 10))
    assert len(d7['series']) == 7
    assert d7['series'][0]['date'] == '2026-11-04' and d7['series'][-1]['date'] == '2026-11-10'
    # 独立手算参考：7 天窗口 11-04..11-10，仅 11-06(idx2)=80、11-09(idx5)=60；
    # 窗口前的 11-01/11-03 活动必须被排除（种子 0 重置）。
    from fractions import Fraction
    ctl = atl = Fraction(0)
    ref = []
    for i in range(7):
        tsb = ctl - atl
        load = Fraction({2: 80, 5: 60}.get(i, 0))
        ctl += (load - ctl) / 42
        atl += (load - atl) / 7
        ref.append((float(load), float(ctl), float(atl), float(tsb)))
    for p, (wl, wc, wa, wt) in zip(d7['series'], ref):
        assert p['daily_load'] == pytest.approx(wl, abs=TOL)
        assert p['ctl'] == pytest.approx(wc, abs=TOL)
        assert p['atl'] == pytest.approx(wa, abs=TOL)
        assert p['tsb'] == pytest.approx(wt, abs=TOL)
    # 窗口不同→种子起点不同→末点 CTL 不同（90 天窗口含 11-01/11-03 负荷，更高）
    assert d90['series'][-1]['ctl'] > d7['series'][-1]['ctl'] + 1.0
    # 历史不足窗口：零填充至 days 点；活动日数值与更宽窗口一致（种子 0 前段全零不改变递推）
    d365 = build_load(store, 1, days=365, today=date(2026, 11, 10))
    assert len(d365['series']) == 365 and d365['series'][0]['date'] == '2025-11-11'
    m90 = {p['date']: p for p in d90['series']}
    m365 = {p['date']: p for p in d365['series']}
    for day in ('2026-11-01', '2026-11-03', '2026-11-06', '2026-11-09', '2026-11-10'):
        for key in ('daily_load', 'ctl', 'atl', 'tsb'):
            assert m365[day][key] == pytest.approx(m90[day][key], abs=1e-9), (day, key)


def test_interpretation_thresholds():
    from running.load_analysis import interpret
    assert interpret(None, None, has_data=False) == '暂无活动记录，无法评估训练负荷。'
    assert interpret(-30.0001, 1.0) == '疲劳积累较深：建议优先安排恢复日，避免连续高强度。'
    assert interpret(-30, 1.0) == '疲劳偏高：注意恢复与睡眠，谨慎追加负荷。'
    assert interpret(-10.0001, 1.0) == '疲劳偏高：注意恢复与睡眠，谨慎追加负荷。'
    assert interpret(-10, 1.0) == '负荷与恢复基本平衡：可按计划渐进训练。'
    assert interpret(5, 1.0) == '负荷与恢复基本平衡：可按计划渐进训练。'
    assert interpret(5.0001, 1.0) == '恢复充分、状态回升：适合渐进增加训练量。'
    assert interpret(15, 1.0) == '恢复充分、状态回升：适合渐进增加训练量。'
    assert interpret(15.0001, 1.0) == '充分恢复：可考虑提升强度或上量。'
    assert interpret(0, 1.5001).endswith('急性/慢性负荷比偏高（>1.5），注意伤病风险。')
    assert interpret(0, 0.7999).endswith('急性负荷低于慢性水平，近期训练量偏少。')
    assert interpret(0, None) == '负荷与恢复基本平衡：可按计划渐进训练。'
    assert interpret(0, 1.0) == '负荷与恢复基本平衡：可按计划渐进训练。'


# ---------------- API 契约 ----------------

def test_api_days_validation(web):
    c, uid, app = web
    assert c.get('/api/running/load').status_code == 200  # 缺省 90
    assert c.get('/api/running/load').json['days'] == 90
    for good in ('7', '30', '90', '180', '365'):
        assert c.get('/api/running/load?days=' + good).status_code == 200, good
    for bad in ('6', '366', '0', '-1', 'abc', '', '90.5', ' 90', '9e1', '１０'):
        assert c.get('/api/running/load?days=' + bad).status_code == 400, bad
    assert c.get('/api/running/load?days=7').json['days'] == 7


def test_api_payload_shape_and_consistency(web):
    c, uid, app = web
    store = app.extensions['running_store']
    _add_reference_activities(store, 1)
    r = c.get('/api/running/load?days=90')
    assert r.status_code == 200
    assert 'no-store' in r.headers['Cache-Control']
    d = r.json
    assert set(d) == {'days', 'generated_at', 'timezone', 'series', 'summary', 'assumptions'}
    datetime.fromisoformat(d['generated_at'])
    series = d['series']
    assert series, '有活动必须有 series'
    for p in series:
        assert set(p) == {'date', 'daily_load', 'ctl', 'atl', 'tsb', 'activities', 'missing'}
    s = d['summary']
    assert set(s) == {'current_ctl', 'current_atl', 'current_tsb', 'acute_chronic_ratio', 'load_7d', 'interpretation'}
    last = series[-1]
    assert s['current_ctl'] == last['ctl'] and s['current_atl'] == last['atl'] and s['current_tsb'] == last['tsb']
    assert s['load_7d'] == pytest.approx(sum(p['daily_load'] for p in series[-7:]), abs=1e-9)
    assert s['acute_chronic_ratio'] == pytest.approx(last['atl'] / last['ctl'], abs=1e-9)
    # 与固定 today 的模块参考值一致（today=实际北京日，固定 90 天窗口种子 0）
    from running.load_analysis import build_load
    ref = build_load(store, 1, days=90)
    assert d['series'] == ref['series'] and d['summary'] == ref['summary']


def test_api_anonymous_401_and_no_store(web):
    c, uid, app = web
    uid[0] = None
    r = c.get('/api/running/load')
    assert r.status_code == 401
    assert 'no-store' in r.headers['Cache-Control']


def test_api_owner_forgery_ignored(web):
    c, uid, app = web
    store = app.extensions['running_store']
    _add(store, 1, '2026-11-01T02:00:00+00:00', duration_s=6000, avg_hr=None, file_sha256='synthetic-own-a')
    _add(store, 2, '2026-11-01T02:00:00+00:00', duration_s=12000, avg_hr=None, file_sha256='synthetic-own-b')
    uid[0] = 2
    for qs in ('owner=1', 'user_id=1', 'owner=1&user_id=1&uid=1'):
        d = c.get('/api/running/load?days=90&' + qs).json
        by_date = {p['date']: p for p in d['series']}
        assert by_date['2026-11-01']['daily_load'] == pytest.approx(100.0, abs=TOL), qs
    uid[0] = 1
    d = c.get('/api/running/load?days=90&owner=2').json
    by_date = {p['date']: p for p in d['series']}
    assert by_date['2026-11-01']['daily_load'] == pytest.approx(50.0, abs=TOL)


def test_api_ab_isolation_real_auth(tmp_path):
    from running.preview import create_app
    app = create_app(tmp_path)
    app.config['LOAD_ENABLED'] = True
    client = app.test_client()
    auth = app.extensions['poserun_auth']
    store = app.extensions['running_store']
    people = [auth.register_user(name, 'Synthetic-only-test', '负荷同名跑者')[0]
              for name in ('load_a', 'load_b')]
    headers = [{'Authorization': 'Bearer ' + auth.create_token(u)} for u in people]
    _add(store, people[0]['id'], '2026-11-01T02:00:00+00:00', duration_s=12000, avg_hr=None,
         notes='A的私密备注', file_sha256='synthetic-ab-a')
    _add(store, people[1]['id'], '2026-11-01T02:00:00+00:00', duration_s=6000, avg_hr=None,
         file_sha256='synthetic-ab-b')
    da = client.get('/api/running/load?days=90', headers=headers[0]).json
    db = client.get('/api/running/load?days=90', headers=headers[1]).json
    assert {p['date']: p for p in da['series']}['2026-11-01']['daily_load'] == pytest.approx(100.0, abs=TOL)
    assert {p['date']: p for p in db['series']}['2026-11-01']['daily_load'] == pytest.approx(50.0, abs=TOL)
    import json as _json
    assert 'A的私密备注' not in _json.dumps(db, ensure_ascii=False)
    forged = client.get('/api/running/load?days=90&owner=%d&user_id=%d'
                        % (people[0]['id'], people[0]['id']), headers=headers[1]).json
    assert {p['date']: p for p in forged['series']}['2026-11-01']['daily_load'] == pytest.approx(50.0, abs=TOL)
    # 匿名 401（真 auth 链路）
    assert client.get('/api/running/load').status_code == 401


def test_api_device_source_readonly_but_counted(web):
    c, uid, app = web
    store = app.extensions['running_store']
    a = _add(store, 1, '2026-11-01T02:00:00+00:00', source='fit', duration_s=6000, avg_hr=None,
             file_sha256='synthetic-device')
    assert c.patch('/api/running/activities/%d' % a['id'], json={'distance_km': 9}).status_code == 403
    d = c.get('/api/running/load?days=90').json
    assert {p['date']: p for p in d['series']}['2026-11-01']['daily_load'] == pytest.approx(50.0, abs=TOL)
    assert c.post('/api/running/load', json={}).status_code == 405
    assert c.delete('/api/running/load').status_code == 405


def test_api_empty_user(web):
    c, uid, app = web
    d = c.get('/api/running/load?days=90').json
    assert len(d['series']) == 90
    assert all(p['daily_load'] == 0.0 and p['activities'] == 0 for p in d['series'])
    assert d['summary']['interpretation'] == '暂无活动记录，无法评估训练负荷。'
    assert d['summary']['acute_chronic_ratio'] is None
    assert d['assumptions']['hr_max'] == 190


def test_no_schema_change(web):
    c, uid, app = web
    from running.running_store import SCHEMA_VERSION
    assert SCHEMA_VERSION == 5
    store = app.extensions['running_store']
    _add(store, 1, '2026-11-01T02:00:00+00:00', file_sha256='synthetic-schema')
    c.get('/api/running/load?days=90')
    db = sqlite3.connect(store.path)
    try:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {'activities', 'garmin_connection', 'sync_state', 'activity_revisions',
                          'gear', 'gear_activity', 'gear_default', 'journal', 'journal_revisions',
                          'laps', 'activity_tracks'}
    finally:
        db.close()