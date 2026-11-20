"""手工活动补录：新增/更正/软删/更正历史、隔离、开关、迁移。AGPL-3.0"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from running.preview import create_app
from running.running_store import RunningStore

MANUAL_EVIDENCE = '手工记录：跑者自填，未核验'


def monday_local():
    """本周一 07:30（Asia/Shanghai），ISO 带时区——保证落在周报当前周。"""
    local = datetime.now(timezone(timedelta(hours=8)))
    return (local - timedelta(days=local.weekday())).replace(hour=7, minute=30, second=0, microsecond=0)


def make_people(app, n=2):
    auth = app.extensions['poserun_auth']
    people = []
    for i in range(n):
        user, err = auth.register_user('runner_%s' % chr(97 + i), 'Synthetic-only-test', '同名跑者')
        assert err is None
        people.append({'id': user['id'], 'header': {'Authorization': 'Bearer ' + auth.create_token(user)}})
    return people


def test_manual_crud_lifecycle(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    client = app.test_client()
    (me,) = make_people(app, 1)
    url = '/api/running/activities'
    start = monday_local().isoformat()
    # 新增
    r = client.post(url, headers=me['header'], json=dict(start_time=start, distance_km=5.2, duration_min=31, avg_hr=152, notes='晨跑'))
    assert r.status_code == 201, r.json
    body = r.get_json()
    assert body['evidence_status'] == MANUAL_EVIDENCE and body['score'] is None
    a = body['activity']
    assert a['source'] == 'manual' and a['sport'] == 'running'
    assert a['distance_m'] == 5200 and a['duration_s'] == 1860 and a['avg_hr'] == 152
    assert a['start_time'] == datetime.fromisoformat(start).astimezone(timezone.utc).isoformat()
    assert a['notes'] == '晨跑' and a['revision'] == 0
    aid = a['id']
    # detail 按来源返回证据状态
    d = client.get(url + '/' + str(aid), headers=me['header']).get_json()
    assert d['evidence_status'] == MANUAL_EVIDENCE and d['score'] is None
    # 更正：改前快照、revision+1、未改字段保持
    r = client.patch(url + '/' + str(aid), headers=me['header'], json=dict(distance_km=6.1, notes='更正为 6.1'))
    assert r.status_code == 200, r.json
    assert r.json['revision'] == 1
    assert r.json['activity']['distance_m'] == 6100
    assert r.json['activity']['duration_s'] == 1860
    assert r.json['activity']['start_time'] == a['start_time']
    # 更正历史
    revs = client.get(url + '/' + str(aid) + '/revisions', headers=me['header']).json['revisions']
    assert len(revs) == 1
    prior = json.loads(revs[0]['prior_json'])
    assert prior['distance_m'] == 5200 and prior['notes'] == '晨跑' and prior['revision'] == 0
    # 再次更正
    assert client.patch(url + '/' + str(aid), headers=me['header'], json=dict(duration_min=33)).json['revision'] == 2
    revs = client.get(url + '/' + str(aid) + '/revisions', headers=me['header']).json['revisions']
    assert len(revs) == 2
    assert json.loads(revs[1]['prior_json'])['distance_m'] == 6100
    # 软删：任意来源可删、已删 400、读路径排除、更正历史保留
    r = client.delete(url + '/' + str(aid), headers=me['header'])
    assert r.status_code == 200 and r.json == {'deleted': True}
    assert client.delete(url + '/' + str(aid), headers=me['header']).status_code == 400
    assert client.get(url + '/' + str(aid), headers=me['header']).status_code == 404
    assert client.get(url, headers=me['header']).json['activities'] == []
    assert client.get(url + '/' + str(aid) + '/revisions', headers=me['header']).json['revisions']


def test_manual_isolation_same_name(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    app.config['WEEKLY_REPORT_ENABLED'] = True
    client = app.test_client()
    a, b = make_people(app, 2)
    url = '/api/running/activities'
    start = monday_local().isoformat()
    created = []
    for person, note in ((a, '甲的晨跑'), (b, '乙的晨跑')):
        r = client.post(url, headers=person['header'], json=dict(start_time=start, distance_km=5.0, duration_min=30, notes=note))
        assert r.status_code == 201, r.json
        created.append(r.json['activity']['id'])
    # 列表/detail/更正/软删/revisions 全 owner-scoped，?owner= 注入无效
    for person, mine, foreign in ((a, created[0], created[1]), (b, created[1], created[0])):
        for suffix in ('', '?owner=' + str(mine)):
            rows = client.get(url + suffix, headers=person['header']).json['activities']
            assert [r['id'] for r in rows] == [mine]
        assert client.get(url + '/' + str(foreign) + '?owner=' + str(foreign), headers=person['header']).status_code == 404
        assert client.patch(url + '/' + str(foreign), headers=person['header'], json=dict(notes='x')).status_code == 404
        assert client.delete(url + '/' + str(foreign) + '?owner=' + str(foreign), headers=person['header']).status_code == 404
        assert client.get(url + '/' + str(foreign) + '/revisions', headers=person['header']).status_code == 404
    # 手工记录计入周报（REAL_SOURCE），双账号无交叉
    report_url = '/api/running/weekly-report'
    for person, mine in ((a, created[0]), (b, created[1])):
        report = client.get(report_url + '?owner=' + str(mine), headers=person['header']).json
        assert [r['id'] for r in report['current']['activities']] == [mine]
        assert report['current']['distance_km'] == 5.0
        assert report['current']['pace_s_km'] is not None
    # 一方软删不影响另一方
    assert client.delete(url + '/' + str(created[0]), headers=a['header']).status_code == 200
    report = client.get(report_url, headers=b['header']).json
    assert [r['id'] for r in report['current']['activities']] == [created[1]]
    report = client.get(report_url, headers=a['header']).json
    assert report['current']['count'] == 0


def test_device_source_read_only_but_deletable(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    client = app.test_client()
    store = app.extensions['running_store']
    (me,) = make_people(app, 1)
    url = '/api/running/activities'
    fit, _ = store.add(me['id'], dict(start_time=monday_local().isoformat(), sport='running', source='fit', distance_m=5000, duration_s=1800))
    r = client.patch(url + '/' + str(fit['id']), headers=me['header'], json=dict(distance_km=9.9))
    assert r.status_code == 403
    assert '只读' in r.json['error']
    # 设备记录 detail 保持原证据状态
    d = client.get(url + '/' + str(fit['id']), headers=me['header']).get_json()
    assert d['evidence_status'] == '未核验：导入摘要，不是校准实测'
    # 本人任意来源可软删
    assert client.delete(url + '/' + str(fit['id']), headers=me['header']).status_code == 200
    assert client.get(url + '/' + str(fit['id']), headers=me['header']).status_code == 404


def test_soft_delete_excluded_from_weekly_list_dashboard(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    app.config['WEEKLY_REPORT_ENABLED'] = True
    client = app.test_client()
    (me,) = make_people(app, 1)
    url = '/api/running/activities'
    r = client.post(url, headers=me['header'], json=dict(start_time=monday_local().isoformat(), distance_km=4.0, duration_min=24))
    aid = r.json['activity']['id']
    # 手工记录计入周报与总览 recent
    report = client.get('/api/running/weekly-report', headers=me['header']).json
    assert [a['id'] for a in report['current']['activities']] == [aid]
    dashboard = client.get('/api/running/dashboard', headers=me['header']).json
    assert any(a['id'] == aid for a in dashboard['recent_activities'])
    assert dashboard['weekly_km'] == 4.0
    # 软删后三处均排除
    assert client.delete(url + '/' + str(aid), headers=me['header']).status_code == 200
    report = client.get('/api/running/weekly-report', headers=me['header']).json
    assert report['current']['count'] == 0
    dashboard = client.get('/api/running/dashboard', headers=me['header']).json
    assert all(a['id'] != aid for a in dashboard['recent_activities'])
    assert client.get(url, headers=me['header']).json['activities'] == []


def test_manual_missing_fields_no_totals_no_pace(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    app.config['WEEKLY_REPORT_ENABLED'] = True
    client = app.test_client()
    (me,) = make_people(app, 1)
    url = '/api/running/activities'
    start = monday_local().isoformat()
    # 缺距离：不报总量、不报配速，时长仍报
    r = client.post(url, headers=me['header'], json=dict(start_time=start, duration_min=30))
    assert r.status_code == 201 and r.json['activity']['distance_m'] is None
    report = client.get('/api/running/weekly-report', headers=me['header']).json
    current = report['current']
    assert current['count'] == 1 and current['distance_km'] is None and current['pace_s_km'] is None
    assert current['duration_s'] == 1800
    # 缺时长：距离仍报、配速不臆造
    r = client.post(url, headers=me['header'], json=dict(start_time=start, distance_km=5.0))
    assert r.status_code == 201 and r.json['activity']['duration_s'] is None
    report = client.get('/api/running/weekly-report', headers=me['header']).json
    current = report['current']
    assert current['count'] == 2 and current['distance_km'] is None and current['pace_s_km'] is None
    # 负值/非数值走证据门归 None，不报错
    r = client.post(url, headers=me['header'], json=dict(start_time=start, distance_km=-1, avg_hr='bad'))
    assert r.status_code == 201
    assert r.json['activity']['distance_m'] is None and r.json['activity']['avg_hr'] is None


def test_manual_validation_errors(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    client = app.test_client()
    (me,) = make_people(app, 1)
    url = '/api/running/activities'
    start = monday_local().isoformat()
    # notes 超长 400
    r = client.post(url, headers=me['header'], json=dict(start_time=start, notes='跑' * 2001))
    assert r.status_code == 400 and '2000' in r.json['error']
    # start_time 无时区 400
    naive = monday_local().replace(tzinfo=None).isoformat()
    assert client.post(url, headers=me['header'], json=dict(start_time=naive)).status_code == 400
    # start_time 缺失 400
    assert client.post(url, headers=me['header'], json=dict(distance_km=5.0)).status_code == 400
    # 非 JSON 请求体 400
    assert client.post(url, headers=me['header']).status_code == 400
    # 更正：无字段 / 无时区 400
    r = client.post(url, headers=me['header'], json=dict(start_time=start, distance_km=5.0))
    aid = r.json['activity']['id']
    assert client.patch(url + '/' + str(aid), headers=me['header'], json=dict()).status_code == 400
    assert client.patch(url + '/' + str(aid), headers=me['header'], json=dict(start_time=naive)).status_code == 400
    r = client.patch(url + '/' + str(aid), headers=me['header'], json=dict(notes='跑' * 2001))
    assert r.status_code == 400
    # 校验失败不落库、不产生更正快照
    assert client.get(url + '/' + str(aid) + '/revisions', headers=me['header']).json['revisions'] == []
    assert client.get(url + '/' + str(aid), headers=me['header']).json['activity']['distance_m'] == 5000


def test_manual_switch_off_404_and_reads_unaffected(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = False
    app.config['WEEKLY_REPORT_ENABLED'] = True
    client = app.test_client()
    (me,) = make_people(app, 1)
    url = '/api/running/activities'
    start = monday_local().isoformat()
    # 开关关：mutation 与 revisions 均 404，页面 404
    assert client.post(url, headers=me['header'], json=dict(start_time=start)).status_code == 404
    assert client.patch(url + '/1', headers=me['header'], json=dict(notes='x')).status_code == 404
    assert client.delete(url + '/1', headers=me['header']).status_code == 404
    assert client.get(url + '/1/revisions', headers=me['header']).status_code == 404
    assert client.get('/manual-entry').status_code == 404
    # 读路径不受开关影响
    store = app.extensions['running_store']
    row, _ = store.add(me['id'], dict(start_time=start, sport='running', source='fit', distance_m=5000, duration_s=1800))
    assert client.get(url, headers=me['header']).status_code == 200
    assert client.get(url + '/' + str(row['id']), headers=me['header']).status_code == 200
    assert client.get('/api/running/weekly-report', headers=me['header']).status_code == 200


def test_manual_anonymous_401(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    client = app.test_client()
    url = '/api/running/activities'
    assert client.post(url, json=dict(start_time=monday_local().isoformat())).status_code == 401
    assert client.patch(url + '/1', json=dict(notes='x')).status_code == 401
    assert client.delete(url + '/1').status_code == 401
    assert client.get(url + '/1/revisions').status_code == 401
    assert client.get(url).status_code == 401


def test_manual_no_store(tmp_path):
    app = create_app(tmp_path)
    app.config['MANUAL_ENTRY_ENABLED'] = True
    client = app.test_client()
    (me,) = make_people(app, 1)
    url = '/api/running/activities'
    r = client.post(url, headers=me['header'], json=dict(start_time=monday_local().isoformat(), distance_km=5.0))
    assert r.status_code == 201 and r.cache_control.no_store
    aid = r.json['activity']['id']
    assert client.get(url + '/' + str(aid) + '/revisions', headers=me['header']).cache_control.no_store
    page = client.get('/manual-entry')
    assert page.status_code == 200 and page.cache_control.no_store


def test_migration_v1_to_v4_idempotent_and_new_db(tmp_path):
    old = tmp_path / 'old' / 'running.db'
    old.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(old))
    conn.executescript("""
        CREATE TABLE activities (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
          start_time TEXT NOT NULL, sport TEXT NOT NULL,
          distance_m REAL, duration_s REAL, avg_hr REAL,
          source TEXT NOT NULL, file_sha256 TEXT, garmin_activity_id TEXT,
          imported_at TEXT NOT NULL,
          UNIQUE(user_id,file_sha256), UNIQUE(user_id,garmin_activity_id));
        CREATE INDEX activity_user_time ON activities(user_id,start_time);
        CREATE TABLE garmin_connection (
          user_id INTEGER PRIMARY KEY, key_encrypted TEXT NOT NULL,
          key_expiry TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE sync_state (
          user_id INTEGER PRIMARY KEY, last_run_at TEXT, status TEXT, error TEXT);
        PRAGMA user_version=1;
    """)
    conn.execute("INSERT INTO activities (user_id,start_time,sport,distance_m,duration_s,avg_hr,source,file_sha256,imported_at) "
                 "VALUES (1,'2026-01-05T08:00:00+00:00','running',5000,1800,150,'fit','sha-old','2026-01-05T08:00:00+00:00')")
    conn.commit()
    conn.close()
    store = RunningStore(old)
    with store.conn() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        row = conn.execute('SELECT * FROM activities').fetchone()
        assert row['distance_m'] == 5000 and row['file_sha256'] == 'sha-old' and row['avg_hr'] == 150
        assert row['notes'] is None and row['deleted_at'] is None and row['revision'] == 0
        columns = {r[1] for r in conn.execute('PRAGMA table_info(activities)')}
        assert {'notes', 'deleted_at', 'revision', 'elevation_gain_m'} <= columns
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='activity_revisions'").fetchone()
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='laps'").fetchone()
    # 旧库升级后可正常读写
    assert [a['id'] for a in store.activities(1)] == [row['id']]
    # 幂等：重复初始化不报错、版本不变、数据不变
    store2 = RunningStore(old)
    with store2.conn() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        assert conn.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 1
    # 新库直接建为 v5
    fresh = RunningStore(tmp_path / 'fresh' / 'running.db')
    with fresh.conn() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        columns = {r[1] for r in conn.execute('PRAGMA table_info(activities)')}
        assert {'notes', 'deleted_at', 'revision', 'elevation_gain_m'} <= columns
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='activity_revisions'").fetchone()
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='laps'").fetchone()
