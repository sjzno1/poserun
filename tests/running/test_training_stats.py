"""P2 第四切片：训练统计仪表盘（月/年聚合＋PB＋周趋势）契约测试。AGPL-3.0
口径与周报完全一致：北京时间、REAL_SOURCE 过滤、排除软删/非跑步/演示、缺测不报完整总量。
测试用 tmp_path 独立库＋合成数据（标注 synthetic），不碰生产数据。"""
from datetime import datetime, timedelta, timezone

from running.weekly_report import weekly_report
from test_activities import web

BJT = timezone(timedelta(hours=8))


def _add(store, uid, when, **changes):
    row = dict(source='fit', sport='running', start_time=when, distance_m=5000, duration_s=1800)
    row.update(changes)
    return store.add(uid, row)[0]


def test_switch_off_404_and_old_pages_unaffected(web):
    c, uid, app = web
    assert app.config['TRAINING_STATS_ENABLED'] is False
    assert c.get('/api/running/stats/overview').status_code == 404
    assert c.get('/training-stats').status_code == 404
    # 旧端点零影响
    assert c.get('/api/running/dashboard').status_code == 200
    assert c.get('/api/running/activities').status_code == 200


def test_anonymous_401_and_page_no_store(tmp_path):
    from running.preview import create_app
    app = create_app(tmp_path)
    app.config['TRAINING_STATS_ENABLED'] = True
    c = app.test_client()
    r = c.get('/api/running/stats/overview')
    assert r.status_code == 401
    assert r.cache_control.no_store
    page = c.get('/training-stats')
    assert page.status_code == 200 and 'html' in page.content_type
    assert page.cache_control.no_store


def test_month_year_totals_excluded_and_total_pace(web):
    c, uid, app = web
    app.config['TRAINING_STATS_ENABLED'] = True
    store = app.extensions['running_store']
    u = uid[0]
    _add(store, u, '2026-01-10T02:00:00+00:00', distance_m=5000, duration_s=1800)
    _add(store, u, '2026-01-20T02:00:00+00:00', distance_m=3000, duration_s=None)
    _add(store, u, '2026-02-10T02:00:00+00:00', distance_m=None, duration_s=900)
    d = c.get('/api/running/stats/overview?year=2026').json
    assert d['timezone'] == 'Asia/Shanghai' and d['year'] == 2026
    assert len(d['months']) == 12
    m1, m2 = d['months'][0], d['months'][1]
    assert m1['count'] == 2 and m1['distance_km'] == 8.0 and m1['duration_s'] == 1800
    assert m1['excluded'] == {'distance': 0, 'duration': 1}
    assert m1['avg_pace_s_per_km'] == 225.0  # 1800*1000/8000
    assert m2['count'] == 1 and m2['distance_km'] is None and m2['duration_s'] == 900
    assert m2['excluded'] == {'distance': 1, 'duration': 0}
    assert m2['avg_pace_s_per_km'] is None
    assert d['months'][3]['count'] == 0 and d['months'][3]['distance_km'] is None
    t = d['year_totals']
    assert t['count'] == 3 and t['distance_km'] == 8.0 and t['duration_s'] == 2700
    assert t['excluded'] == {'distance': 1, 'duration': 1}
    # 均配＝总时长/总距离（2700*1000/8000），不是均值的均值
    assert t['avg_pace_s_per_km'] == 337.5


def test_beijing_month_and_year_boundaries(web):
    c, uid, app = web
    app.config['TRAINING_STATS_ENABLED'] = True
    store = app.extensions['running_store']
    u = uid[0]
    _add(store, u, '2026-02-28T16:00:00+00:00', distance_m=1000, duration_s=600, file_sha256='synthetic-b1')
    _add(store, u, '2026-02-28T15:59:59+00:00', distance_m=2000, duration_s=600, file_sha256='synthetic-b2')
    _add(store, u, '2026-12-31T16:00:00+00:00', distance_m=4000, duration_s=1200, file_sha256='synthetic-b3')
    d26 = c.get('/api/running/stats/overview?year=2026').json
    assert d26['months'][1]['count'] == 1  # 2 月只有 23:59:59 北京
    assert d26['months'][2]['count'] == 1  # 3 月只有 00:00 北京
    assert d26['year_totals']['count'] == 2
    d27 = c.get('/api/running/stats/overview?year=2027').json
    assert d27['year_totals']['count'] == 1 and d27['months'][0]['count'] == 1


def test_soft_deleted_non_running_demo_excluded(web):
    c, uid, app = web
    app.config['TRAINING_STATS_ENABLED'] = True
    store = app.extensions['running_store']
    u = uid[0]
    a = _add(store, u, '2026-06-01T02:00:00+00:00', file_sha256='synthetic-del')
    assert store.soft_delete(u, a['id']) is True
    _add(store, u, '2026-06-02T02:00:00+00:00', sport='cycling', file_sha256='synthetic-cyc')
    _add(store, u, '2026-06-03T02:00:00+00:00', source='demo', file_sha256='synthetic-demo')
    d = c.get('/api/running/stats/overview?year=2026').json
    assert d['year_totals']['count'] == 0
    assert all(m['count'] == 0 for m in d['months'])
    pb = d['pb']
    assert pb['longest_distance']['value_km'] is None and pb['longest_distance']['activity'] is None
    assert pb['longest_duration']['value_s'] is None
    assert pb['fastest_pace']['value_s_per_km'] is None
    assert pb['best_week'] is None


def test_pb_values_and_boundaries(web):
    c, uid, app = web
    app.config['TRAINING_STATS_ENABLED'] = True
    store = app.extensions['running_store']
    u = uid[0]
    pace = _add(store, u, '2026-03-07T02:00:00+00:00', distance_m=6000, duration_s=1500, file_sha256='synthetic-pace')
    long_a = _add(store, u, '2026-03-05T02:00:00+00:00', distance_m=10000, duration_s=3600, file_sha256='synthetic-long')
    dur_a = _add(store, u, '2026-03-06T02:00:00+00:00', distance_m=6000, duration_s=4200, file_sha256='synthetic-dur')
    short_a = _add(store, u, '2026-03-08T02:00:00+00:00', distance_m=4999, duration_s=1000, file_sha256='synthetic-short')
    nodur_a = _add(store, u, '2026-03-09T02:00:00+00:00', distance_m=15000, duration_s=None, file_sha256='synthetic-nodur')
    # 周一 00:00 北京边界活动（2026-03-02T16:00Z）
    boundary_a = _add(store, u, '2026-03-02T16:00:00+00:00', distance_m=1000, duration_s=300, file_sha256='synthetic-boundary')
    d = c.get('/api/running/stats/overview?year=2026').json
    pb = d['pb']
    assert pb['scope'] == '已导入活动内最佳' and pb['window'] == '2026'
    assert pb['longest_distance']['value_km'] == 15.0
    assert pb['longest_distance']['activity']['distance_km'] == 15.0
    assert pb['longest_duration']['value_s'] == 4200
    # 4999m 配速 200 更快但不足 5km，不计最快均配；6000/1500=250 才是
    assert pb['fastest_pace']['value_s_per_km'] == 250.0
    assert pb['fastest_pace']['activity']['id'] == pace['id']
    assert pb['fastest_pace']['activity']['date'] == '2026-03-07'
    assert '整条活动均配' in pb['fastest_pace']['note']
    bw = pb['best_week']
    assert bw['week_start'] == '2026-03-02'
    assert bw['distance_km'] == 28.0 and bw['count'] == 5
    assert set(bw['activity_ids']) == {pace['id'], long_a['id'], dur_a['id'], short_a['id'], boundary_a['id']}
    # 与 weekly_report 周界对拍：同一活动（周一 00:00 北京）落在同一周
    wr = weekly_report(store, u, now=datetime(2026, 3, 7, tzinfo=timezone.utc))
    assert wr['current']['start'] == '2026-03-02T00:00:00+08:00'
    assert bw['week_start'] == wr['current']['start'][:10]


def test_empty_year(web):
    c, uid, app = web
    app.config['TRAINING_STATS_ENABLED'] = True
    d = c.get('/api/running/stats/overview?year=2030').json
    assert all(m['count'] == 0 and m['distance_km'] is None and m['duration_s'] is None for m in d['months'])
    t = d['year_totals']
    assert t['count'] == 0 and t['distance_km'] is None and t['duration_s'] is None and t['avg_pace_s_per_km'] is None
    pb = d['pb']
    assert pb['longest_distance']['value_km'] is None and pb['longest_duration']['value_s'] is None
    assert pb['fastest_pace']['value_s_per_km'] is None and pb['best_week'] is None
    assert len(d['weekly_trend']) == 12
    assert all(w['count'] == 0 and w['distance_km'] == 0 for w in d['weekly_trend'])


def test_weekly_trend_twelve_weeks(web):
    c, uid, app = web
    app.config['TRAINING_STATS_ENABLED'] = True
    store = app.extensions['running_store']
    u = uid[0]
    now = datetime.now(timezone.utc)
    local = now.astimezone(BJT)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    def iso(dt):
        return dt.astimezone(timezone.utc).isoformat()
    _add(store, u, iso(monday), distance_m=5000, duration_s=1800, file_sha256='synthetic-w0')
    _add(store, u, iso(monday - timedelta(days=7)), distance_m=7000, duration_s=2400, file_sha256='synthetic-w1')
    _add(store, u, iso(monday - timedelta(days=14)), distance_m=None, duration_s=600, file_sha256='synthetic-w2')
    d = c.get('/api/running/stats/overview').json
    assert d['year'] == local.year  # 缺省＝当前北京时间年
    trend = d['weekly_trend']
    assert len(trend) == 12
    assert [w['week_start'] for w in trend] == [
        (monday - timedelta(days=7*i)).date().isoformat() for i in range(11, -1, -1)]
    assert trend[-1]['count'] == 1 and trend[-1]['distance_km'] == 5.0
    assert trend[-2]['count'] == 1 and trend[-2]['distance_km'] == 7.0
    assert trend[-3]['count'] == 1 and trend[-3]['distance_km'] is None  # 缺距离不报合计
    assert trend[-4]['count'] == 0 and trend[-4]['distance_km'] == 0      # 空周为 0
    assert all(w['count'] == 0 and w['distance_km'] == 0 for w in trend[:-4])


def test_year_param_validation(web):
    c, uid, app = web
    app.config['TRAINING_STATS_ENABLED'] = True
    for bad in ('abc', '1999', '2101', '2026.5', '-1'):
        assert c.get('/api/running/stats/overview?year=' + bad).status_code == 400, bad
    assert c.get('/api/running/stats/overview?year=2025').status_code == 200


def test_imported_sources_counted(web):
    c, uid, app = web
    app.config['TRAINING_STATS_ENABLED'] = True
    store = app.extensions['running_store']
    u = uid[0]
    for i, src in enumerate(('fit', 'api', 'gpx', 'tcx', 'manual')):
        _add(store, u, '2026-04-%02dT02:00:00+00:00' % (i + 1), source=src,
             distance_m=3000, duration_s=900, file_sha256='synthetic-src-%s' % src)
    d = c.get('/api/running/stats/overview?year=2026').json
    assert d['year_totals']['count'] == 5
    assert d['year_totals']['distance_km'] == 15.0


def test_nav_flags_and_switch_toggle(web):
    c, uid, app = web
    app.config['WEEKLY_REPORT_ENABLED'] = True
    app.config['TRAINING_STATS_ENABLED'] = True
    assert c.get('/api/running/dashboard').json['training_stats_enabled'] is True
    assert c.get('/api/running/weekly-report').json['training_stats_enabled'] is True
    app.config['TRAINING_STATS_ENABLED'] = False
    assert c.get('/api/running/dashboard').json['training_stats_enabled'] is False
    assert c.get('/api/running/weekly-report').json['training_stats_enabled'] is False
    assert c.get('/api/running/stats/overview').status_code == 404
    assert c.get('/training-stats').status_code == 404


def test_same_name_ab_isolation(tmp_path):
    from running.preview import create_app
    app = create_app(tmp_path)
    app.config['TRAINING_STATS_ENABLED'] = True
    client = app.test_client()
    auth = app.extensions['poserun_auth']
    store = app.extensions['running_store']
    people = [auth.register_user(name, 'Synthetic-only-test', '同名跑者')[0]
              for name in ('stats_a', 'stats_b')]
    headers = [{'Authorization': 'Bearer ' + auth.create_token(user)} for user in people]
    _add(store, people[0]['id'], '2026-05-10T02:00:00+00:00', distance_m=10000, duration_s=2700, file_sha256='synthetic-ab-a')
    _add(store, people[1]['id'], '2026-05-11T02:00:00+00:00', distance_m=20000, duration_s=5400, file_sha256='synthetic-ab-b')
    a = client.get('/api/running/stats/overview?year=2026', headers=headers[0]).json
    b = client.get('/api/running/stats/overview?year=2026', headers=headers[1]).json
    assert a['year_totals']['count'] == 1 and a['year_totals']['distance_km'] == 10.0
    assert b['year_totals']['count'] == 1 and b['year_totals']['distance_km'] == 20.0
    assert a['pb']['longest_distance']['value_km'] == 10.0
    assert b['pb']['longest_distance']['value_km'] == 20.0
    assert a['pb']['longest_distance']['activity']['id'] != b['pb']['longest_distance']['activity']['id']
    # 管理员只看本人（空）
    admin = auth.get_user_by_id(1)
    ad = client.get('/api/running/stats/overview?year=2026',
                    headers={'Authorization': 'Bearer ' + auth.create_token(admin)}).json
    assert ad['year_totals']['count'] == 0
    # 无 owner 参数可越权
    forged = client.get('/api/running/stats/overview?year=2026&owner=' + str(people[0]['id']), headers=headers[1]).json
    assert forged['year_totals']['distance_km'] == 20.0
