"""P2 切片5 地图轨迹可视化：轨迹持久化＋离线轨迹 API 契约（TDD 红）。

锁定：
- 抽稀（Douglas-Peucker＋等距兜底）≤2000 点、保留首尾；
- GPX/TCX/含GPS的FIT 导入在 TRACK_MAP_ENABLED=1 时持久化抽稀轨迹；
- 开关关：不写轨迹、页面/API 全 404；
- 仅本人数据：跨用户同名 A/B 隔离、?owner=/?user_id= 伪造无效、软删不返回、匿名 401、no-store；
- 手工/无GPS活动优雅 has_track=False；重复导入不重写轨迹；
- additive schema：activity_tracks 表，v4→v5 迁移有备份、旧数据完好。
"""
import io
import json
import math
import sqlite3
import struct

import pytest
from fitparse.records import Crc
from flask import Flask, g

from running import register_running
from running.track_map import MAX_TRACK_POINTS, extract_track_points, simplify_track


def gpx_bytes(n=30, ele=True, time=True, lat0=31.23, lon0=121.47):
    """合成 GPX：n 个轨迹点，沿东北方向小步前进，海拔正弦起伏。"""
    pts = []
    for i in range(n):
        lat = lat0 + i * 0.0004
        lon = lon0 + i * 0.0005
        attrs = f'lat="{lat:.7f}" lon="{lon:.7f}"'
        inner = ''
        if ele:
            inner += f'<ele>{10 + 5 * math.sin(i / 4):.1f}</ele>'
        if time:
            inner += f'<time>2026-11-16T00:{i:02d}:00Z</time>'
        pts.append(f'<trkpt {attrs}>{inner}</trkpt>')
    return ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="t">'
            '<trk><name>map</name><trkseg>' + ''.join(pts) + '</trkseg></trk></gpx>').encode()


def tcx_bytes(n=20, ele=True, lon0=-122.42):
    """合成 TCX：西半球负经度（抽取不得用拒绝负值的摘要口径）。"""
    tps = []
    for i in range(n):
        pos = (f'<Position><LatitudeDegrees>{37.77 + i * 0.0003:.7f}</LatitudeDegrees>'
               f'<LongitudeDegrees>{lon0 - i * 0.0004:.7f}</LongitudeDegrees></Position>')
        alt = f'<AltitudeMeters>{20 + i:.1f}</AltitudeMeters>' if ele else ''
        tps.append(f'<Trackpoint><Time>2026-11-16T01:{i:02d}:00Z</Time>{pos}{alt}'
                   f'<DistanceMeters>{i * 50}</DistanceMeters></Trackpoint>')
    return ('<?xml version="1.0"?><TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
            '<Activities><Activity Sport="Running"><Id>2026-11-16T01:00:00Z</Id>'
            '<Lap StartTime="2026-11-16T01:00:00Z"><TotalTimeSeconds>1200</TotalTimeSeconds>'
            '<DistanceMeters>5000</DistanceMeters><Track>' + ''.join(tps) + '</Track></Lap>'
            '</Activity></Activities></TrainingCenterDatabase>').encode()


def fit_gps_bytes(n=12, ts0=1163808000):
    """合成 FIT：record(20) 带 GPS（semicircles）＋session(18) 摘要。ts0 为 FIT 纪元秒。"""
    # record definition: timestamp u32, position_lat s32, position_long s32, altitude u16(scale5 offset500)
    rfields = [(253, 4, 0x86), (0, 4, 0x85), (1, 4, 0x85), (2, 2, 0x84)]
    rdef = bytes([0x40, 0, 0]) + struct.pack('<H', 20) + bytes([len(rfields)]) + b''.join(bytes(f) for f in rfields)
    rdata = b''
    for i in range(n):
        lat_semi = int((31.23 + i * 0.0004) * (2 ** 31 / 180))
        lon_semi = int((121.47 + i * 0.0005) * (2 ** 31 / 180))
        alt_raw = int((10 + i + 500) * 5)
        rdata += bytes([0]) + struct.pack('<IiiH', ts0 + i * 60, lat_semi, lon_semi, alt_raw)
    # session definition/data（与 test_activities.sample_fit 同口径）
    sfields = [(2, 4, 0x86), (5, 1, 0), (9, 4, 0x86), (8, 4, 0x86), (16, 1, 2)]
    sdef = bytes([0x41, 0, 0]) + struct.pack('<H', 18) + bytes([len(sfields)]) + b''.join(bytes(f) for f in sfields)
    sdata = bytes([1]) + struct.pack('<IBIIB', ts0 + n * 60, 1, 500000, 1800000, 145)
    body_data = rdef + rdata + sdef + sdata
    header = struct.pack('<BBHI4s', 12, 0x10, 100, len(body_data), b'.FIT')
    body = header + body_data
    return body + struct.pack('<H', Crc.calculate(body))


def fit_nogps_bytes(ts0=1163808000):
    fields = [(2, 4, 0x86), (5, 1, 0), (9, 4, 0x86), (8, 4, 0x86), (16, 1, 2)]
    definition = bytes([0x40, 0, 0]) + struct.pack('<H', 18) + bytes([len(fields)]) + b''.join(bytes(f) for f in fields)
    data = definition + bytes([0]) + struct.pack('<IBIIB', ts0, 1, 500000, 1800000, 145)
    header = struct.pack('<BBHI4s', 12, 0x10, 100, len(data), b'.FIT')
    body = header + data
    return body + struct.pack('<H', Crc.calculate(body))


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
    app.config['TRACK_MAP_ENABLED'] = True
    app.config['MANUAL_ENTRY_ENABLED'] = True
    return app.test_client(), uid, app


def upload(c, data, name, form=None):
    return c.post('/api/running/activities/import',
                  data={'file': (io.BytesIO(data), name), **(form or {})})


def track_get(c, aid, qs=''):
    return c.get(f'/api/running/activities/{aid}/track{qs}')


# ---------- 抽稀纯函数 ----------

def test_simplify_collinear_keeps_endpoints():
    pts = [{'lat': 31.0 + i * 0.001, 'lon': 121.0, 'ele': None, 'time': None} for i in range(50)]
    out = simplify_track(pts)
    assert len(out) == 2
    assert out[0]['lat'] == pts[0]['lat'] and out[-1]['lat'] == pts[-1]['lat']


def test_simplify_caps_2000_keeps_first_last():
    pts = [{'lat': 31.0 + i * 0.0001, 'lon': 121.0 + (0.0002 if i % 2 else -0.0002), 'ele': i, 'time': None}
           for i in range(5000)]
    out = simplify_track(pts)
    assert 2 <= len(out) <= MAX_TRACK_POINTS == 2000
    assert out[0]['lat'] == pts[0]['lat'] and out[0]['lon'] == pts[0]['lon']
    assert out[-1]['lat'] == pts[-1]['lat'] and out[-1]['lon'] == pts[-1]['lon']


def test_simplify_small_track_preserved_and_rounded():
    pts = [{'lat': 31.12345678, 'lon': 121.87654321, 'ele': 12.345, 'time': '2026-11-16T00:00:00Z'},
           {'lat': 31.2, 'lon': 121.9, 'ele': None, 'time': None}]
    out = simplify_track(pts)
    assert len(out) == 2
    assert out[0]['lat'] == 31.123457 and out[0]['lon'] == 121.876543
    assert out[0]['ele'] == 12.3 and out[0]['time'] == '2026-11-16T00:00:00Z'
    assert out[1]['ele'] is None
    assert simplify_track([]) == []
    assert simplify_track(pts[:1]) == []  # 少于 2 点不成轨迹


def test_simplify_keeps_shape_corner():
    # L 形拐点必须保留（DP 特性），不能等距抽样丢掉直角
    pts = ([{'lat': 31.0, 'lon': 121.0 + i * 0.0001, 'ele': None, 'time': None} for i in range(20)]
           + [{'lat': 31.0 + i * 0.0001, 'lon': 121.0019, 'ele': None, 'time': None} for i in range(1, 20)])
    out = simplify_track(pts)
    assert any(abs(p['lat'] - 31.0) < 1e-6 and abs(p['lon'] - 121.0019) < 1e-6 for p in out)


# ---------- 抽取纯函数 ----------

def test_extract_gpx_points():
    pts = extract_track_points('a.gpx', gpx_bytes(n=10))
    assert len(pts) == 10
    assert pts[0]['lat'] == pytest.approx(31.23) and pts[0]['lon'] == pytest.approx(121.47)
    assert pts[0]['ele'] is not None and pts[0]['time'] == '2026-11-16T00:00:00Z'
    noele = extract_track_points('a.gpx', gpx_bytes(n=5, ele=False, time=False))
    assert len(noele) == 5 and all(p['ele'] is None and p['time'] is None for p in noele)


def test_extract_tcx_negative_longitude():
    pts = extract_track_points('a.tcx', tcx_bytes(n=8))
    assert len(pts) == 8
    assert pts[0]['lon'] < 0 and pts[0]['lat'] == pytest.approx(37.77)
    assert pts[0]['ele'] == pytest.approx(20.0)
    assert extract_track_points('a.tcx', tcx_bytes(n=4, ele=False))[0]['ele'] is None


def test_extract_fit_gps_points():
    pts = extract_track_points('a.fit', fit_gps_bytes(n=12))
    assert len(pts) == 12
    assert pts[0]['lat'] == pytest.approx(31.23, abs=1e-5)
    assert pts[0]['lon'] == pytest.approx(121.47, abs=1e-5)
    assert pts[0]['ele'] == pytest.approx(10.0, abs=0.2)
    assert extract_track_points('a.fit', fit_nogps_bytes()) == []
    assert extract_track_points('a.txt', b'whatever') == []


# ---------- 导入持久化 ----------

def test_import_gpx_persists_track(web):
    c, uid, app = web
    r = upload(c, gpx_bytes(n=30), 'run.gpx')
    assert r.status_code == 201, r.json
    aid = r.json['activity']['id']
    t = track_get(c, aid)
    assert t.status_code == 200
    assert 'no-store' in t.headers['Cache-Control']
    body = t.json
    assert body['has_track'] is True
    assert body['activity']['id'] == aid
    pts = body['track']['points']
    assert body['track']['point_count'] == len(pts)
    assert 2 <= len(pts) <= 2000
    assert pts[0]['lat'] == pytest.approx(31.23, abs=1e-5)
    assert pts[-1]['lat'] == pytest.approx(31.23 + 29 * 0.0004, abs=1e-5)
    assert all(set(p) == {'lat', 'lon', 'ele', 'time'} for p in pts)


def test_import_tcx_persists_track(web):
    c, uid, app = web
    r = upload(c, tcx_bytes(n=20), 'run.tcx')
    assert r.status_code == 201, r.json
    t = track_get(c, r.json['activity']['id'])
    assert t.status_code == 200 and t.json['has_track'] is True
    assert t.json['track']['points'][0]['lon'] < 0


def test_import_fit_gps_persists_track(web):
    c, uid, app = web
    r = upload(c, fit_gps_bytes(n=12), 'run.fit')
    assert r.status_code == 201, r.json
    t = track_get(c, r.json['activity']['id'])
    assert t.status_code == 200 and t.json['has_track'] is True
    assert t.json['track']['point_count'] >= 2


def test_manual_and_nogps_have_no_track(web):
    c, uid, app = web
    m = c.post('/api/running/activities', json={'distance_km': 5, 'duration_min': 30,
                                                'start_time': '2026-11-15T08:00:00+08:00'})
    assert m.status_code == 201, m.json
    t = track_get(c, m.json['activity']['id'])
    assert t.status_code == 200 and t.json['has_track'] is False and t.json['track'] is None
    f = upload(c, fit_nogps_bytes(), 'nogps.fit')
    assert f.status_code == 201
    t2 = track_get(c, f.json['activity']['id'])
    assert t2.status_code == 200 and t2.json['has_track'] is False


def test_duplicate_import_does_not_rewrite_track(web):
    c, uid, app = web
    r1 = upload(c, gpx_bytes(n=30), 'run.gpx')
    aid = r1.json['activity']['id']
    created1 = track_get(c, aid).json['track']['created_at']
    r2 = upload(c, gpx_bytes(n=30), 'run.gpx')
    assert r2.status_code == 200 and r2.json['duplicate'] is True
    assert track_get(c, aid).json['track']['created_at'] == created1


def test_flag_off_writes_no_track_and_404(web):
    c, uid, app = web
    app.config['TRACK_MAP_ENABLED'] = False
    r = upload(c, gpx_bytes(n=30), 'run.gpx')
    assert r.status_code == 201
    aid = r.json['activity']['id']
    assert track_get(c, aid).status_code == 404
    assert c.get('/activity-track?id=%d' % aid).status_code == 404
    db = sqlite3.connect(app.extensions['running_store'].path)
    try:
        assert db.execute('SELECT COUNT(*) FROM activity_tracks').fetchone()[0] == 0
    finally:
        db.close()
    app.config['TRACK_MAP_ENABLED'] = True
    assert track_get(c, aid).json['has_track'] is False  # 关时导入未存轨迹，开后也不回填


def test_track_api_anonymous_401_and_page_no_store(web):
    c, uid, app = web
    r = upload(c, gpx_bytes(n=10), 'run.gpx')
    aid = r.json['activity']['id']
    uid[0] = None
    t = track_get(c, aid)
    assert t.status_code == 401 and 'no-store' in t.headers['Cache-Control']
    p = c.get('/activity-track?id=%d' % aid)
    assert p.status_code == 200 and 'no-store' in p.headers['Cache-Control']
    assert p.content_type.startswith('text/html')


def test_cross_user_same_name_isolation_and_owner_forgery(web):
    c, uid, app = web
    r = upload(c, gpx_bytes(n=30), 'same-name.gpx')
    aid_a = r.json['activity']['id']
    uid[0] = 2
    rb = upload(c, gpx_bytes(n=12, lat0=39.9, lon0=116.4), 'same-name.gpx')
    aid_b = rb.json['activity']['id']
    assert aid_b != aid_a
    assert track_get(c, aid_a).status_code == 404
    assert track_get(c, aid_a, '?owner=1').status_code == 404
    assert track_get(c, aid_a, '?user_id=1').status_code == 404
    tb = track_get(c, aid_b).json
    assert tb['has_track'] is True
    assert tb['track']['points'][0]['lat'] == pytest.approx(39.9, abs=1e-4)  # B 只见自己的点
    uid[0] = 1
    assert track_get(c, aid_b).status_code == 404
    assert track_get(c, aid_a).json['track']['points'][0]['lat'] == pytest.approx(31.23, abs=1e-4)


def test_soft_deleted_activity_track_not_returned(web):
    c, uid, app = web
    r = upload(c, gpx_bytes(n=10), 'run.gpx')
    aid = r.json['activity']['id']
    assert track_get(c, aid).status_code == 200
    assert c.delete(f'/api/running/activities/{aid}').status_code == 200
    assert track_get(c, aid).status_code == 404


def test_device_source_activity_readonly_but_track_readable(web):
    c, uid, app = web
    r = upload(c, fit_gps_bytes(n=12), 'device.fit')
    aid = r.json['activity']['id']
    assert c.patch(f'/api/running/activities/{aid}', json={'distance_km': 9}).status_code == 403
    assert track_get(c, aid).status_code == 200 and track_get(c, aid).json['has_track'] is True
    assert c.post(f'/api/running/activities/{aid}/track', json={}).status_code == 405
    assert c.delete(f'/api/running/activities/{aid}/track').status_code == 405


def test_unknown_activity_404(web):
    c, uid, app = web
    assert track_get(c, 99999).status_code == 404


def test_flag_exposed_in_dashboard_and_list(web):
    c, uid, app = web
    d = c.get('/api/running/dashboard').json
    assert d['track_map_enabled'] is True
    lst = c.get('/api/running/activities').json
    assert lst['track_map_enabled'] is True
    app.config['TRACK_MAP_ENABLED'] = False
    assert c.get('/api/running/dashboard').json['track_map_enabled'] is False
    assert c.get('/api/running/activities').json['track_map_enabled'] is False


def test_page_route_flag_gate(web):
    c, uid, app = web
    assert c.get('/activity-track?id=1').status_code == 200
    app.config['TRACK_MAP_ENABLED'] = False
    assert c.get('/activity-track?id=1').status_code == 404


# ---------- additive schema 迁移 ----------

def test_schema_v4_to_v5_migration_backup_and_data_intact(tmp_path):
    from running.running_store import RunningStore, BASE_SCHEMA, SCHEMA_VERSION
    assert SCHEMA_VERSION >= 5
    dbpath = tmp_path / 'running.db'
    # 先建当前 schema，再退回 v4 状态（删新表＋版本号）模拟旧库
    s = RunningStore(dbpath)
    conn = sqlite3.connect(dbpath)
    conn.execute('DROP TABLE activity_tracks')
    conn.execute('PRAGMA user_version=4')
    conn.execute("INSERT INTO activities (user_id, id, source, sport, start_time, distance_m, duration_s, imported_at) "
                 "VALUES (1, 1, 'gpx', 'running', '2026-11-10T00:00:00+00:00', 5000, 1800, '2026-11-10T00:00:00Z')")
    conn.commit()
    conn.close()
    s2 = RunningStore(dbpath)
    conn = sqlite3.connect(dbpath)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='activity_tracks'").fetchone()
        assert conn.execute('SELECT distance_m FROM activities WHERE id=1').fetchone()[0] == 5000
    finally:
        conn.close()
    backups = list(tmp_path.glob('running.db.bak-*'))
    assert backups, '迁移前必须备份'
    # 回滚安全：旧代码（v4）打开 v5 库不报错，空表无害
    conn = sqlite3.connect(dbpath)
    conn.execute('PRAGMA user_version=4')
    conn.commit()
    conn.close()
    s3 = RunningStore(dbpath)
    assert s3.activity(1, 1)['distance_m'] == 5000


def test_save_track_rejects_foreign_and_deleted(web):
    c, uid, app = web
    from running.running_store import RunningStore
    store = RunningStore(app.extensions['running_store'].path)
    r = upload(c, gpx_bytes(n=10), 'run.gpx')
    aid = r.json['activity']['id']
    pts = [{'lat': 31.0, 'lon': 121.0, 'ele': None, 'time': None},
           {'lat': 31.001, 'lon': 121.001, 'ele': None, 'time': None}]
    assert store.save_track(2, aid, pts) is None  # 非本人
    assert store.save_track(1, 99999, pts) is None  # 不存在
    assert store.activity_track(2, aid) is None
    assert store.save_track(1, aid, pts) is not None
    assert store.activity_track(1, aid)['point_count'] == 2
    store.soft_delete(1, aid)
    assert store.activity_track(1, aid) is None
    assert store.save_track(1, aid, pts) is None  # 软删后不可写