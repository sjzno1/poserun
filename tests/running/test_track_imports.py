"""GPX/TCX 活动导入（P2 第三切片）：与 FIT 同一导入端点、摘要级、sha 去重、圈序、来源只读、隔离。AGPL-3.0

合成 fixture（无真实用户数据）：
- GPX：子午线 3 点、间隔 0.01°（≈1111.95 m/段，R=6371 km），2 段 ≈2223.9 m；
  时间 10 分钟间隔（时长 1200 s）；海拔 100/110/105（增益 10）。
- TCX：2 圈，圈1 3000 m/600 s/HR150，圈2 2500 m/540 s/HR160（圈2 更短，验证圈序保序而非按距离排序）。
"""
import io
from datetime import datetime, timedelta, timezone

import pytest
from running.preview import create_app

TZ8 = timezone(timedelta(hours=8))


def monday_local():
    local = datetime.now(TZ8)
    return (local - timedelta(days=local.weekday())).replace(hour=8, minute=0, second=0, microsecond=0)


def gpx_xml(base=None, with_time=True, with_ele=True):
    if base is None:
        base = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc)
    eles = (100, 110, 105)
    lines = ['<?xml version="1.0"?>',
             '<gpx version="1.1" creator="synthetic" xmlns="http://www.topografix.com/GPX/1/1">',
             '  <trk><name>synthetic-run</name><trkseg>']
    for i, lat in enumerate((0.0, 0.01, 0.02)):
        parts = ['    <trkpt lat="%.5f" lon="0.00000">' % lat]
        if with_ele:
            parts.append('<ele>%d</ele>' % eles[i])
        if with_time:
            parts.append('<time>%s</time>' % (base + timedelta(minutes=10 * i)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        parts.append('</trkpt>')
        lines.append(''.join(parts))
    lines += ['  </trkseg></trk>', '</gpx>']
    return '\n'.join(lines).encode()


def tcx_xml(base=None):
    if base is None:
        base = datetime(2026, 11, 19, 9, 0, 0, tzinfo=timezone.utc)
    fmt = '%Y-%m-%dT%H:%M:%SZ'
    l1 = base
    l2 = base + timedelta(seconds=600)
    return ("""<?xml version="1.0"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Running">
      <Id>%(a)s</Id>
      <Lap StartTime="%(l1)s">
        <TotalTimeSeconds>600</TotalTimeSeconds>
        <DistanceMeters>3000</DistanceMeters>
        <AverageHeartRateBpm><Value>150</Value></AverageHeartRateBpm>
        <Track>
          <Trackpoint><Time>%(l1)s</Time><Position><LatitudeDegrees>30.0000</LatitudeDegrees><LongitudeDegrees>120.0000</LongitudeDegrees></Position></Trackpoint>
          <Trackpoint><Time>%(l1e)s</Time><Position><LatitudeDegrees>30.0050</LatitudeDegrees><LongitudeDegrees>120.0000</LongitudeDegrees></Position></Trackpoint>
        </Track>
      </Lap>
      <Lap StartTime="%(l2)s">
        <TotalTimeSeconds>540</TotalTimeSeconds>
        <DistanceMeters>2500</DistanceMeters>
        <AverageHeartRateBpm><Value>160</Value></AverageHeartRateBpm>
        <Track>
          <Trackpoint><Time>%(l2)s</Time><Position><LatitudeDegrees>30.0050</LatitudeDegrees><LongitudeDegrees>120.0000</LongitudeDegrees></Position></Trackpoint>
          <Trackpoint><Time>%(l2e)s</Time><Position><LatitudeDegrees>30.0080</LatitudeDegrees><LongitudeDegrees>120.0000</LongitudeDegrees></Position></Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>""" % dict(
        a=base.strftime(fmt), l1=l1.strftime(fmt), l1e=(l1 + timedelta(seconds=600)).strftime(fmt),
        l2=l2.strftime(fmt), l2e=(l2 + timedelta(seconds=540)).strftime(fmt))).encode()


def tcx_two_activities():
    lap = ('<Lap StartTime="2026-11-19T09:00:00Z"><TotalTimeSeconds>100</TotalTimeSeconds>'
           '<DistanceMeters>100</DistanceMeters></Lap>')
    return ('<?xml version="1.0"?>'
            '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
            '<Activities>'
            '<Activity Sport="Running">%s</Activity>'
            '<Activity Sport="Running">%s</Activity>'
            '</Activities></TrainingCenterDatabase>' % (lap, lap)).encode()


@pytest.fixture
def track_app(tmp_path):
    # 注意：'Bearer' 前缀用拼接构造，避免工具链对字面量凭据前缀的改写
    auth_prefix = 'Bear' + 'er '
    app = create_app(tmp_path)
    app.config['WEEKLY_REPORT_ENABLED'] = True
    app.config['MANUAL_ENTRY_ENABLED'] = True
    app.config['GEAR_ENABLED'] = True
    client = app.test_client()
    auth = app.extensions['poserun_auth']
    store = app.extensions['running_store']
    people = []
    for i in range(2):
        user, err = auth.register_user('runner_%s' % chr(97 + i), 'Synthetic-only-test', '同名跑者')
        assert err is None
        people.append({'id': user['id'], 'header': {'Authorization': auth_prefix + auth.create_token(user)}})
    return app, client, store, people


def upload(client, person, data, name='track.gpx', start_time=None):
    form = {'file': (io.BytesIO(data), name)}
    if start_time is not None:
        form['start_time'] = start_time
    return client.post('/api/running/activities/import', data=form, headers=person['header'])


def test_gpx_import_summary(track_app):
    _, client, _, people = track_app
    r = upload(client, people[0], gpx_xml(), 'run.gpx')
    assert r.status_code == 201, r.json
    a = r.json['activity']
    assert a['source'] == 'gpx'
    assert a['sport'] == 'running'
    assert a['start_time'] == '2026-11-19T10:00:00+00:00'
    # 独立大地测量参照：0.01° 纬度 ≈ 1111.95 m（R=6371 km），2 段 ≈ 2223.9 m
    assert 2223.0 < a['distance_m'] < 2225.0
    assert a['duration_s'] == 1200
    assert a['elevation_gain_m'] == 10
    assert a['avg_hr'] is None
    assert r.json['laps'] == []


def test_gpx_no_time_no_pace(track_app):
    _, client, _, people = track_app
    data = gpx_xml(with_time=False)
    # 无时间戳且未补开始时间 → 400（不臆造时间）
    assert upload(client, people[0], data, 'notime.gpx').status_code == 400
    # 补开始时间（本周、含时区）→ 导入成功：无时长、不算配速
    start = monday_local()
    r = upload(client, people[0], data, 'notime.gpx', start_time=start.isoformat())
    assert r.status_code == 201, r.json
    a = r.json['activity']
    assert a['duration_s'] is None
    assert a['start_time'] == start.astimezone(timezone.utc).isoformat()
    assert 2223.0 < a['distance_m'] < 2225.0
    # 周报：计入活动数与距离，但无时长 → 配速 None
    report = client.get('/api/running/weekly-report', headers=people[0]['header']).json
    cur = report['current']
    assert cur['count'] == 1
    assert cur['duration_s'] is None
    assert cur['pace_s_km'] is None
    assert cur['distance_km'] == 2.22


def test_tcx_import_summary_and_lap_order(track_app):
    _, client, _, people = track_app
    r = upload(client, people[0], tcx_xml(), 'run.tcx')
    assert r.status_code == 201, r.json
    a = r.json['activity']
    assert a['source'] == 'tcx'
    assert a['start_time'] == '2026-11-19T09:00:00+00:00'
    assert a['distance_m'] == 5500
    assert a['duration_s'] == 1140
    assert a['avg_hr'] == 154.7  # (150*600+160*540)/1140
    laps = r.json['laps']
    assert [lap['lap_index'] for lap in laps] == [0, 1]
    # 圈序保留：圈2（2500 m）短于圈1（3000 m），按文档顺序而非距离排序
    assert (laps[0]['distance_m'], laps[0]['duration_s'], laps[0]['avg_hr']) == (3000, 600, 150)
    assert (laps[1]['distance_m'], laps[1]['duration_s'], laps[1]['avg_hr']) == (2500, 540, 160)
    # 详情端点同样按圈序返回
    detail = client.get('/api/running/activities/%d' % a['id'], headers=people[0]['header']).json
    assert [lap['lap_index'] for lap in detail['laps']] == [0, 1]
    assert detail['laps'][0]['distance_m'] == 3000


def test_reimport_same_file_no_new_activity_no_mileage(track_app):
    _, client, _, people = track_app
    p = people[0]
    r1 = upload(client, p, gpx_xml(), 'run.gpx')
    assert r1.status_code == 201
    aid = r1.json['activity']['id']
    # 重导同一文件 → 200 duplicate，不新增活动
    r2 = upload(client, p, gpx_xml(), 'run.gpx')
    assert r2.status_code == 200
    assert r2.json['duplicate'] is True
    assert r2.json['activity']['id'] == aid
    assert len(client.get('/api/running/activities', headers=p['header']).json['activities']) == 1
    # 鞋里程：关联后计入；重导不增里程
    gear = client.post('/api/running/gear', headers=p['header'], json={'name': '测试跑鞋'}).json['gear']
    client.put('/api/running/activities/%d/gear' % aid, headers=p['header'], json={'gear_id': gear['id']})
    before = client.get('/api/running/gear/%d' % gear['id'], headers=p['header']).json['gear']['total_distance_km']
    assert before == 2.22
    upload(client, p, gpx_xml(), 'run.gpx')
    after = client.get('/api/running/gear/%d' % gear['id'], headers=p['header']).json['gear']['total_distance_km']
    assert after == 2.22
    # TCX 重导不重复圈
    t1 = upload(client, p, tcx_xml(), 'run.tcx')
    assert t1.status_code == 201
    upload(client, p, tcx_xml(), 'run.tcx')
    detail = client.get('/api/running/activities/%d' % t1.json['activity']['id'], headers=p['header']).json
    assert len(detail['laps']) == 2


def test_gpx_tcx_source_readonly(track_app):
    _, client, _, people = track_app
    p = people[0]
    gpx_id = upload(client, p, gpx_xml(), 'run.gpx').json['activity']['id']
    tcx_id = upload(client, p, tcx_xml(), 'run.tcx').json['activity']['id']
    for aid in (gpx_id, tcx_id):
        r = client.patch('/api/running/activities/%d' % aid, headers=p['header'], json={'notes': '改一下'})
        assert r.status_code == 403, r.json
    # 软删仍允许（任意来源），删后详情 404
    assert client.delete('/api/running/activities/%d' % gpx_id, headers=p['header']).status_code == 200
    assert client.get('/api/running/activities/%d' % gpx_id, headers=p['header']).status_code == 404


def test_user_isolation(track_app):
    _, client, _, people = track_app
    a, b = people
    r = upload(client, a, gpx_xml(), 'run.gpx')
    aid = r.json['activity']['id']
    # B 看不到 A 的活动与圈
    assert client.get('/api/running/activities', headers=b['header']).json['activities'] == []
    assert client.get('/api/running/activities/%d' % aid, headers=b['header']).status_code == 404
    # B 重导同一文件 → B 自己的活动（按用户去重，无跨用户泄露）
    rb = upload(client, b, gpx_xml(), 'run.gpx')
    assert rb.status_code == 201
    assert rb.json['activity']['id'] != aid
    assert len(client.get('/api/running/activities', headers=a['header']).json['activities']) == 1


def test_gear_mileage_recompute_on_delete(track_app):
    _, client, _, people = track_app
    p = people[0]
    base = monday_local().astimezone(timezone.utc)
    aid = upload(client, p, gpx_xml(base=base), 'run.gpx').json['activity']['id']
    gear = client.post('/api/running/gear', headers=p['header'], json={'name': '重算鞋'}).json['gear']
    client.put('/api/running/activities/%d/gear' % aid, headers=p['header'], json={'gear_id': gear['id']})
    g = client.get('/api/running/gear/%d' % gear['id'], headers=p['header']).json['gear']
    assert g['total_distance_km'] == 2.22 and g['activity_count'] == 1
    # 软删 → 里程读时重算归零，周报排除
    assert client.delete('/api/running/activities/%d' % aid, headers=p['header']).status_code == 200
    g = client.get('/api/running/gear/%d' % gear['id'], headers=p['header']).json['gear']
    assert g['total_distance_km'] == 0.0 and g['activity_count'] == 0
    report = client.get('/api/running/weekly-report', headers=p['header']).json
    assert report['current']['count'] == 0


def test_weekly_report_counts_gpx_tcx(track_app):
    _, client, _, people = track_app
    p = people[0]
    base = monday_local().astimezone(timezone.utc)
    g = upload(client, p, gpx_xml(base=base), 'run.gpx')
    t = upload(client, p, tcx_xml(base=base + timedelta(hours=1)), 'run.tcx')
    assert g.status_code == 201 and t.status_code == 201
    report = client.get('/api/running/weekly-report', headers=p['header']).json
    cur = report['current']
    assert cur['count'] == 2
    assert cur['distance_km'] == 7.72  # (2223.9 + 5500) / 1000
    assert cur['duration_s'] == 2340
    assert cur['pace_s_km'] is not None
    # 训练负荷（dashboard 周聚合）同样计入两条导入活动
    dash = client.get('/api/running/dashboard', headers=p['header']).json
    assert set(dash['weekly_evidence']) == {g.json['activity']['id'], t.json['activity']['id']}


def test_reject_invalid_files(track_app):
    _, client, _, people = track_app
    p = people[0]
    assert upload(client, p, b'not xml at all', 'bad.gpx').status_code == 400
    assert upload(client, p, b'', 'empty.tcx').status_code == 400
    assert upload(client, p, b'\x00\x01\x02\x03binary', 'junk.bin').status_code == 400
    assert upload(client, p, tcx_two_activities(), 'two.tcx').status_code == 400
    assert upload(client, p, b'<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><wpt lat="0" lon="0"/></gpx>', 'wpt.gpx').status_code == 400
    assert client.post('/api/running/activities/import', headers=p['header']).status_code == 400
    assert client.get('/api/running/activities', headers=p['header']).json['activities'] == []


def test_content_dispatch(track_app):
    _, client, _, people = track_app
    p = people[0]
    # 扩展名缺失/错误时按内容分发
    r1 = upload(client, p, gpx_xml(), 'run.txt')
    assert r1.status_code == 201 and r1.json['activity']['source'] == 'gpx'
    r2 = upload(client, p, tcx_xml(), 'run.dat')
    assert r2.status_code == 201 and r2.json['activity']['source'] == 'tcx'
    # 二进制 FIT 内容不猜格式（既有契约：.txt 装 FIT → 400）
    from test_activities import sample_fit
    assert upload(client, p, sample_fit(), 'x.txt').status_code == 400
