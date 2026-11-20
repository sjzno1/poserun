"""P2 切片6 数据导出（CSV/JSON）契约测试（TDD 红）。AGPL-3.0

锁定：
- GET /api/running/export?format=csv|json&year=YYYY（year 可选=全部）；
- 仅本人未软删活动，按 start_time 升序；全部运动/来源（数据导出保真），软删排除；
- CSV：UTF-8 BOM＋表头 id,start_time,sport,distance_km,duration_s,avg_hr,elevation_gain_m,source,notes；
  distance_m→km 无损换算，缺测留空；notes 含逗号/换行/中文正确引号；
- JSON：{exported_at, year, count, activities}，缺测 null，原始 distance_m＋换算 distance_km；
  不含 file_sha256/garmin_activity_id/laps（简洁口径）；
- Content-Disposition attachment＋filename=poserun-activities-<year|all>.<ext>；no-store；
- format/year 非法 400；匿名 401；开关 EXPORT_ENABLED 默认关→404＋dashboard 旗标；
- 仅本人：跨用户同名 A/B 隔离、?owner=/?user_id= 伪造无效；
- 设备来源记录只读口径（PATCH 403）不变，但可导出（本人数据读路径）；
- 无 schema 变更（v5，不新增表）。
"""
import csv
import io
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from flask import Flask, g

from running import register_running

CSV_HEADER = ['id', 'start_time', 'sport', 'distance_km', 'duration_s', 'avg_hr',
              'elevation_gain_m', 'source', 'notes']
BOM = b'\xef\xbb\xbf'


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
    app.config['EXPORT_ENABLED'] = True
    app.config['MANUAL_ENTRY_ENABLED'] = True
    app.config['GEAR_ENABLED'] = True
    return app.test_client(), uid, app


def _add(store, uid, when, **changes):
    row = dict(source='fit', sport='running', start_time=when, distance_m=5000, duration_s=1800)
    row.update(changes)
    return store.add(uid, row)[0]


def csv_rows(resp):
    raw = resp.data
    assert raw.startswith(BOM), 'CSV 必须带 UTF-8 BOM'
    text = raw.decode('utf-8-sig')
    return list(csv.reader(io.StringIO(text)))


def test_flag_default_off_404_and_dashboard_flag(tmp_path):
    app = Flask(__name__, static_folder='../../static', static_url_path='')
    uid = [1]

    @app.before_request
    def user():
        if uid[0] is not None:
            g.current_user = {'id': uid[0]}

    register_running(app, data_dir=tmp_path)
    assert app.config['EXPORT_ENABLED'] is False  # 默认关
    c = app.test_client()
    assert c.get('/api/running/export?format=csv').status_code == 404
    assert c.get('/api/running/export?format=json').status_code == 404
    assert c.get('/api/running/dashboard').json['export_enabled'] is False
    # 旧端点零影响
    assert c.get('/api/running/activities').status_code == 200


def test_csv_bom_header_conversion_and_missing_blank(web):
    c, uid, app = web
    store = app.extensions['running_store']
    a = _add(store, 1, '2026-03-01T02:00:00+00:00', distance_m=5123.4, duration_s=1800.5,
             avg_hr=145, elevation_gain_m=120.5, notes='节奏跑', file_sha256='synthetic-csv-1')
    b = _add(store, 1, '2026-03-02T02:00:00+00:00', distance_m=None, duration_s=None,
             avg_hr=None, elevation_gain_m=None, notes=None, file_sha256='synthetic-csv-2')
    r = c.get('/api/running/export?format=csv')
    assert r.status_code == 200
    assert r.content_type.startswith('text/csv')
    assert 'charset=utf-8' in r.content_type.lower()
    assert 'no-store' in r.headers['Cache-Control']
    assert r.headers['Content-Disposition'] == 'attachment; filename=poserun-activities-all.csv'
    rows = csv_rows(r)
    assert rows[0] == CSV_HEADER
    assert len(rows) == 3
    r1, r2 = rows[1], rows[2]
    assert r1[0] == str(a['id']) and r2[0] == str(b['id'])
    assert r1[3] == '5.1234'  # distance_m→km 无损换算
    assert r1[4] == '1800.5' and r1[5] == '145.0' and r1[6] == '120.5'
    assert r1[7] == 'fit' and r1[8] == '节奏跑'
    # 缺测留空（不是 0、不是 None 字面量）
    assert r2[3] == '' and r2[4] == '' and r2[5] == '' and r2[6] == '' and r2[8] == ''


def test_csv_notes_quoting_comma_newline_chinese(web):
    c, uid, app = web
    store = app.extensions['running_store']
    notes = '间歇, 4组\n第二行"引号"与中文'
    _add(store, 1, '2026-03-01T02:00:00+00:00', notes=notes, file_sha256='synthetic-quote')
    r = c.get('/api/running/export?format=csv')
    rows = csv_rows(r)
    assert rows[1][8] == notes  # csv 引号转义后可完整还原


def test_json_structure_null_and_raw_fields(web):
    c, uid, app = web
    store = app.extensions['running_store']
    a = _add(store, 1, '2026-03-01T02:00:00+00:00', distance_m=5000, duration_s=1800,
             avg_hr=145, elevation_gain_m=100, notes='测试', file_sha256='synthetic-json-1')
    _add(store, 1, '2026-03-02T02:00:00+00:00', distance_m=None, duration_s=None,
         avg_hr=None, elevation_gain_m=None, notes=None, file_sha256='synthetic-json-2')
    r = c.get('/api/running/export?format=json')
    assert r.status_code == 200
    assert r.content_type.startswith('application/json')
    assert 'no-store' in r.headers['Cache-Control']
    assert r.headers['Content-Disposition'] == 'attachment; filename=poserun-activities-all.json'
    d = r.json
    assert set(d) == {'exported_at', 'year', 'count', 'activities'}
    assert d['year'] is None and d['count'] == 2
    datetime.fromisoformat(d['exported_at'])  # 可解析 ISO 时间
    acts = d['activities']
    assert len(acts) == 2
    first = acts[0]
    assert first['id'] == a['id']
    assert first['distance_m'] == 5000 and first['distance_km'] == 5.0
    assert first['duration_s'] == 1800 and first['avg_hr'] == 145
    assert first['elevation_gain_m'] == 100 and first['source'] == 'fit' and first['notes'] == '测试'
    assert 'start_time' in first and 'sport' in first and 'imported_at' in first and 'revision' in first
    second = acts[1]
    for key in ('distance_m', 'distance_km', 'duration_s', 'avg_hr', 'elevation_gain_m', 'notes'):
        assert second[key] is None, key
    # 简洁口径：不含 sha/garmin id/laps
    for act in acts:
        assert 'file_sha256' not in act and 'garmin_activity_id' not in act and 'laps' not in act


def test_year_filter_beijing_boundary(web):
    c, uid, app = web
    store = app.extensions['running_store']
    # 2025-12-31T16:00Z = 2026-01-01T00:00 北京 → 属 2026
    _add(store, 1, '2025-12-31T16:00:00+00:00', distance_m=1000, file_sha256='synthetic-y-boundary')
    _add(store, 1, '2025-12-31T15:59:59+00:00', distance_m=2000, file_sha256='synthetic-y-2025')
    _add(store, 1, '2026-06-01T02:00:00+00:00', distance_m=3000, file_sha256='synthetic-y-2026')
    d25 = c.get('/api/running/export?format=json&year=2025').json
    assert d25['year'] == 2025 and d25['count'] == 1
    assert d25['activities'][0]['distance_m'] == 2000
    d26 = c.get('/api/running/export?format=json&year=2026').json
    assert d26['year'] == 2026 and d26['count'] == 2
    assert d26['activities'][0]['distance_m'] == 1000  # 北京元旦零点那根
    assert c.get('/api/running/export?format=csv&year=2025').headers['Content-Disposition'] == \
        'attachment; filename=poserun-activities-2025.csv'
    dall = c.get('/api/running/export?format=json').json
    assert dall['count'] == 3 and dall['year'] is None


def test_soft_deleted_excluded_and_sorted_asc(web):
    c, uid, app = web
    store = app.extensions['running_store']
    late = _add(store, 1, '2026-05-03T02:00:00+00:00', file_sha256='synthetic-sort-3')
    early = _add(store, 1, '2026-05-01T02:00:00+00:00', file_sha256='synthetic-sort-1')
    mid = _add(store, 1, '2026-05-02T02:00:00+00:00', file_sha256='synthetic-sort-2')
    deleted = _add(store, 1, '2026-05-04T02:00:00+00:00', file_sha256='synthetic-sort-del')
    assert store.soft_delete(1, deleted['id']) is True
    d = c.get('/api/running/export?format=json').json
    assert d['count'] == 3
    assert [a['id'] for a in d['activities']] == [early['id'], mid['id'], late['id']]
    rows = csv_rows(c.get('/api/running/export?format=csv'))
    assert [r[0] for r in rows[1:]] == [str(early['id']), str(mid['id']), str(late['id'])]
    assert str(deleted['id']) not in [r[0] for r in rows[1:]]


def test_all_sports_and_sources_exported(web):
    c, uid, app = web
    store = app.extensions['running_store']
    for i, (sport, src) in enumerate([('running', 'fit'), ('cycling', 'gpx'), ('running', 'manual'),
                                      ('running', 'demo'), ('running', 'api')]):
        _add(store, 1, '2026-04-%02dT02:00:00+00:00' % (i + 1), sport=sport, source=src,
             file_sha256='synthetic-sport-%d' % i)
    d = c.get('/api/running/export?format=json').json
    assert d['count'] == 5  # 数据导出保真：不过滤运动/来源，仅排软删
    assert {a['sport'] for a in d['activities']} == {'running', 'cycling'}


def test_empty_export_still_valid(web):
    c, uid, app = web
    r = c.get('/api/running/export?format=csv')
    assert r.status_code == 200
    rows = csv_rows(r)
    assert rows == [CSV_HEADER]  # BOM＋仅表头
    d = c.get('/api/running/export?format=json').json
    assert d['count'] == 0 and d['activities'] == [] and d['year'] is None
    d26 = c.get('/api/running/export?format=json&year=2026').json
    assert d26['count'] == 0 and d26['year'] == 2026


def test_format_and_year_validation(web):
    c, uid, app = web
    for bad in ('xml', 'CSV', 'Json', '', 'pdf', 'csv;'):
        assert c.get('/api/running/export?format=' + bad).status_code == 400, bad
    assert c.get('/api/running/export').status_code == 400  # format 必填
    for bad in ('abc', '1999', '2101', '2026.5', '-1', ''):
        assert c.get('/api/running/export?format=json&year=' + bad).status_code == 400, bad
    assert c.get('/api/running/export?format=json&year=2025').status_code == 200
    assert c.get('/api/running/export?format=csv&year=2000').status_code == 200
    assert c.get('/api/running/export?format=csv&year=2100').status_code == 200


def test_anonymous_401_and_no_store(web):
    c, uid, app = web
    uid[0] = None
    for fmt in ('csv', 'json'):
        r = c.get('/api/running/export?format=' + fmt)
        assert r.status_code == 401
        assert 'no-store' in r.headers['Cache-Control']


def test_owner_forgery_ignored(web):
    c, uid, app = web
    store = app.extensions['running_store']
    _add(store, 1, '2026-05-01T02:00:00+00:00', distance_m=1000, file_sha256='synthetic-own-a')
    _add(store, 2, '2026-05-02T02:00:00+00:00', distance_m=9000, file_sha256='synthetic-own-b')
    uid[0] = 2
    for qs in ('?owner=1', '?user_id=1', '?owner=1&user_id=1&uid=1'):
        d = c.get('/api/running/export?format=json&' + qs.lstrip('?')).json
        assert d['count'] == 1 and d['activities'][0]['distance_m'] == 9000, qs
    uid[0] = 1
    d = c.get('/api/running/export?format=json&owner=2').json
    assert d['count'] == 1 and d['activities'][0]['distance_m'] == 1000


def test_same_name_ab_isolation_real_auth(tmp_path):
    from running.preview import create_app
    app = create_app(tmp_path)
    app.config['EXPORT_ENABLED'] = True
    client = app.test_client()
    auth = app.extensions['poserun_auth']
    store = app.extensions['running_store']
    people = [auth.register_user(name, 'Synthetic-only-test', '导出同名跑者')[0]
              for name in ('export_a', 'export_b')]
    headers = [{'Authorization': 'Bearer ' + auth.create_token(u)} for u in people]
    a = _add(store, people[0]['id'], '2026-05-10T02:00:00+00:00', distance_m=10000,
             notes='A的私密训练备注', file_sha256='synthetic-ab-a')
    b = _add(store, people[1]['id'], '2026-05-10T02:00:00+00:00', distance_m=20000,
             notes='B的备注', file_sha256='synthetic-ab-b')
    da = client.get('/api/running/export?format=json', headers=headers[0]).json
    db = client.get('/api/running/export?format=json', headers=headers[1]).json
    assert da['count'] == 1 and da['activities'][0]['id'] == a['id']
    assert db['count'] == 1 and db['activities'][0]['id'] == b['id']
    assert 'B的备注' not in json.dumps(da, ensure_ascii=False)
    assert 'A的私密训练备注' not in json.dumps(db, ensure_ascii=False)
    ca = csv_rows(client.get('/api/running/export?format=csv', headers=headers[0]))
    assert len(ca) == 2 and ca[1][0] == str(a['id'])
    # B 伪造 owner 指向 A 仍只见本人
    forged = client.get('/api/running/export?format=json&owner=%d&user_id=%d'
                        % (people[0]['id'], people[0]['id']), headers=headers[1]).json
    assert forged['count'] == 1 and forged['activities'][0]['id'] == b['id']


def test_device_source_readonly_but_exportable(web):
    """设备账号口径（沿用切片5）：设备来源记录 PATCH 403 只读；导出是读路径，含本人设备记录。"""
    c, uid, app = web
    store = app.extensions['running_store']
    a = _add(store, 1, '2026-05-01T02:00:00+00:00', source='fit', file_sha256='synthetic-device')
    assert c.patch('/api/running/activities/%d' % a['id'], json={'distance_km': 9}).status_code == 403
    d = c.get('/api/running/export?format=json').json
    assert d['count'] == 1 and d['activities'][0]['source'] == 'fit'
    assert c.post('/api/running/export', json={}).status_code == 405
    assert c.delete('/api/running/export?format=csv').status_code == 405


def test_flag_toggle_and_dashboard_exposure(web):
    c, uid, app = web
    assert c.get('/api/running/dashboard').json['export_enabled'] is True
    app.config['EXPORT_ENABLED'] = False
    assert c.get('/api/running/dashboard').json['export_enabled'] is False
    assert c.get('/api/running/export?format=csv').status_code == 404
    assert c.get('/api/running/export?format=json').status_code == 404
    # journal 页面本身不受 EXPORT 开关影响（GEAR 门）
    assert c.get('/journal').status_code == 200
    app.config['EXPORT_ENABLED'] = True
    assert c.get('/api/running/export?format=csv').status_code == 200


def test_no_schema_change(web):
    c, uid, app = web
    from running.running_store import SCHEMA_VERSION
    assert SCHEMA_VERSION == 5
    store = app.extensions['running_store']
    _add(store, 1, '2026-05-01T02:00:00+00:00', file_sha256='synthetic-schema')
    c.get('/api/running/export?format=csv')
    c.get('/api/running/export?format=json')
    db = sqlite3.connect(store.path)
    try:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {'activities', 'garmin_connection', 'sync_state', 'activity_revisions',
                          'gear', 'gear_activity', 'gear_default', 'journal', 'journal_revisions',
                          'laps', 'activity_tracks'}
    finally:
        db.close()