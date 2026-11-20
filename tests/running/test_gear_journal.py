"""跑鞋 gear＋训练日志 journal（P2 第二切片）：CRUD、里程重算、隔离、隐私、开关、迁移。AGPL-3.0"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from running.preview import create_app
from running.running_store import RunningStore, public_journal

GEAR_EVIDENCE = '已导入活动内合计 · 未核验'
JOURNAL_EVIDENCE = '跑者自填 · 未核验'
SECRET = '私密标记-绝不外泄-9f3k'


def monday_local():
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


def gear_app(tmp_path, enabled=True):
    app = create_app(tmp_path)
    app.config['GEAR_ENABLED'] = enabled
    app.config['MANUAL_ENTRY_ENABLED'] = True
    app.config['WEEKLY_REPORT_ENABLED'] = True
    return app, app.test_client()


def add_activity(store, uid, distance_m=5000, sha=None, start=None):
    row, created = store.add(uid, dict(start_time=(start or monday_local()).isoformat(), sport='running',
                                       source='fit', distance_m=distance_m, duration_s=1800, file_sha256=sha))
    return row, created


def link(client, person, aid, gid):
    return client.put('/api/running/activities/%d/gear' % aid, headers=person['header'], json=dict(gear_id=gid))


# ---- 跑鞋 CRUD ----

def test_gear_crud_lifecycle(tmp_path):
    app, client = gear_app(tmp_path)
    (me,) = make_people(app, 1)
    url = '/api/running/gear'
    r = client.post(url, headers=me['header'], json=dict(name='日常训练鞋', brand_model='示例牌 X1',
                                                         started_on='2026-01-01', max_distance_km=600, notes='合成测试数据'))
    assert r.status_code == 201, r.json
    body = r.get_json()
    assert body['evidence_status'] == GEAR_EVIDENCE and body['score'] is None
    g = body['gear']
    assert g['name'] == '日常训练鞋' and g['deleted_at'] is None and g['retired_on'] is None
    assert g['total_distance_km'] == 0 and g['activity_count'] == 0 and g['threshold_reached'] is False
    assert g['evidence']['label'] == '已导入活动内合计' and g['evidence']['unit'] == 'km' and g['evidence']['status'] == '未核验'
    gid = g['id']
    # 编辑：未改字段保持
    r = client.patch(url + '/%d' % gid, headers=me['header'], json=dict(brand_model='示例牌 X2', notes=None))
    assert r.status_code == 200, r.json
    assert r.json['gear']['brand_model'] == '示例牌 X2' and r.json['gear']['name'] == '日常训练鞋'
    assert r.json['gear']['notes'] is None
    # 退役：软删可审计
    r = client.post(url + '/%d/retire' % gid, headers=me['header'], json=dict(retired_on='2026-11-01'))
    assert r.status_code == 200 and r.json == {'retired': True}
    assert client.post(url + '/%d/retire' % gid, headers=me['header'], json={}).status_code == 400
    rows = client.get(url, headers=me['header']).json['gear']
    assert rows == []
    all_rows = client.get(url + '?include_retired=1', headers=me['header']).json['gear']
    assert len(all_rows) == 1 and all_rows[0]['deleted_at'] and all_rows[0]['retired_on'] == '2026-11-01'
    # 退役后详情仍可审计，编辑拒绝
    detail = client.get(url + '/%d' % gid, headers=me['header'])
    assert detail.status_code == 200 and detail.json['gear']['retired_on'] == '2026-11-01'
    assert client.patch(url + '/%d' % gid, headers=me['header'], json=dict(name='x')).status_code == 404


def test_gear_validation_no_partial_writes(tmp_path):
    app, client = gear_app(tmp_path)
    (me,) = make_people(app, 1)
    url = '/api/running/gear'
    for bad in ({}, {'name': '  '}, {'name': '鞋' * 121}, {'name': '鞋', 'started_on': '2026-13-40'},
                {'name': '鞋', 'notes': '备' * 2001}, {'name': '鞋', 'brand_model': '型' * 121}):
        assert client.post(url, headers=me['header'], json=bad).status_code == 400, bad
    assert client.get(url, headers=me['header']).json['gear'] == []
    # 阈值非正/非数 → 存 None（仅提示字段，不报错）
    r = client.post(url, headers=me['header'], json=dict(name='鞋', max_distance_km=0))
    assert r.status_code == 201 and r.json['gear']['max_distance_km'] is None
    gid = r.json['gear']['id']
    assert client.patch(url + '/%d' % gid, headers=me['header'], json=dict()).status_code == 400
    assert client.patch(url + '/%d' % gid, headers=me['header'], json=dict(started_on='bad')).status_code == 400


# ---- 里程重算语义（验收样例核心） ----

def test_mileage_recalc_semantics(tmp_path):
    app, client = gear_app(tmp_path)
    (me,) = make_people(app, 1)
    store = app.extensions['running_store']
    uid = me['id']
    gurl = '/api/running/gear'
    a = client.post(gurl, headers=me['header'], json=dict(name='鞋A')).json['gear']['id']
    b = client.post(gurl, headers=me['header'], json=dict(name='鞋B')).json['gear']['id']
    fit1, _ = add_activity(store, uid, 5000, sha='sha-1')
    fit2, _ = add_activity(store, uid, None, sha='sha-2')  # 缺距离
    man = client.post('/api/running/activities', headers=me['header'],
                      json=dict(start_time=monday_local().isoformat(), distance_km=2.0, duration_min=12)).json['activity']
    for aid in (fit1['id'], fit2['id'], man['id']):
        assert link(client, me, aid, a).status_code == 200
    ga = client.get(gurl + '/%d' % a, headers=me['header']).json['gear']
    assert ga['total_distance_km'] == 7.0 and ga['activity_count'] == 3
    assert ga['missing_distance_count'] == 1 and ga['evidence']['label'] == '已导入活动内合计'
    # 同文件重导：去重不增活动、不增里程
    dup, created = add_activity(store, uid, 5000, sha='sha-1')
    assert created is False and dup['id'] == fit1['id']
    assert client.get(gurl + '/%d' % a, headers=me['header']).json['gear']['total_distance_km'] == 7.0
    # 换鞋：A 减、B 增，关联唯一（每活动至多一双）
    assert link(client, me, fit1['id'], b).status_code == 200
    assert client.get(gurl + '/%d' % a, headers=me['header']).json['gear']['total_distance_km'] == 2.0
    assert client.get(gurl + '/%d' % b, headers=me['header']).json['gear']['total_distance_km'] == 5.0
    with store.conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM gear_activity WHERE user_id=? AND activity_id=?', (uid, fit1['id'])).fetchone()[0] == 1
    # 更正距离后重算
    r = client.patch('/api/running/activities/%d' % man['id'], headers=me['header'], json=dict(distance_km=4.0))
    assert r.status_code == 200
    assert client.get(gurl + '/%d' % a, headers=me['header']).json['gear']['total_distance_km'] == 4.0
    # 活动软删后重算（缺距离那条删除不影响合计，但计数减一）
    assert client.delete('/api/running/activities/%d' % fit2['id'], headers=me['header']).status_code == 200
    ga = client.get(gurl + '/%d' % a, headers=me['header']).json['gear']
    assert ga['total_distance_km'] == 4.0 and ga['activity_count'] == 1 and ga['missing_distance_count'] == 0
    # 解除关联后重算
    assert client.delete('/api/running/activities/%d/gear' % man['id'], headers=me['header']).status_code == 200
    assert client.get(gurl + '/%d' % a, headers=me['header']).json['gear']['total_distance_km'] == 0
    # 阈值提示：B 已达 5km 阈值
    client.patch(gurl + '/%d' % b, headers=me['header'], json=dict(max_distance_km=5))
    gb = client.get(gurl + '/%d' % b, headers=me['header']).json['gear']
    assert gb['threshold_reached'] is True


def test_link_unique_404_and_retired(tmp_path):
    app, client = gear_app(tmp_path)
    a, b = make_people(app, 2)
    store = app.extensions['running_store']
    act_a, _ = add_activity(store, a['id'], 5000, sha='a-1')
    act_b, _ = add_activity(store, b['id'], 5000, sha='b-1')
    gear_a = client.post('/api/running/gear', headers=a['header'], json=dict(name='甲鞋')).json['gear']['id']
    gear_b = client.post('/api/running/gear', headers=b['header'], json=dict(name='乙鞋')).json['gear']['id']
    # 跨账号活动/鞋 → 404（不泄露存在性）
    assert link(client, a, act_b['id'], gear_a).status_code == 404
    assert link(client, a, act_a['id'], gear_b).status_code == 404
    assert client.get('/api/running/activities/%d/gear' % act_b['id'], headers=a['header']).status_code == 404
    assert client.delete('/api/running/activities/%d/gear' % act_b['id'], headers=a['header']).status_code == 404
    # 未关联时 GET 返回 null；解除未关联 → 404
    assert client.get('/api/running/activities/%d/gear' % act_a['id'], headers=a['header']).json['link'] is None
    assert client.delete('/api/running/activities/%d/gear' % act_a['id'], headers=a['header']).status_code == 404
    # 退役鞋不接受新关联
    assert link(client, a, act_a['id'], gear_a).status_code == 200
    client.post('/api/running/gear/%d/retire' % gear_a, headers=a['header'], json={})
    act_a2, _ = add_activity(store, a['id'], 3000, sha='a-2')
    assert link(client, a, act_a2['id'], gear_a).status_code == 404
    # 历史关联仍可见（含退役标注）
    cur = client.get('/api/running/activities/%d/gear' % act_a['id'], headers=a['header']).json['link']
    assert cur['gear_id'] == gear_a and cur['gear_deleted']


def test_default_gear(tmp_path):
    app, client = gear_app(tmp_path)
    a, b = make_people(app, 2)
    url = '/api/running/gear'
    assert client.get(url + '/default', headers=a['header']).json == {'gear_id': None, 'gear': None}
    gid = client.post(url, headers=a['header'], json=dict(name='默认鞋')).json['gear']['id']
    foreign = client.post(url, headers=b['header'], json=dict(name='乙鞋')).json['gear']['id']
    assert client.put(url + '/default', headers=a['header'], json=dict(gear_id=foreign)).status_code == 404
    assert client.put(url + '/default', headers=a['header'], json=dict(gear_id=gid)).status_code == 200
    assert client.get(url + '/default', headers=a['header']).json['gear_id'] == gid
    assert client.get(url, headers=a['header']).json['default_gear_id'] == gid
    # 退役自动清除默认
    client.post(url + '/%d/retire' % gid, headers=a['header'], json={})
    assert client.get(url + '/default', headers=a['header']).json['gear_id'] is None
    assert client.put(url + '/default', headers=a['header'], json=dict(gear_id=gid)).status_code == 404
    # 清除默认
    gid2 = client.post(url, headers=a['header'], json=dict(name='鞋2')).json['gear']['id']
    client.put(url + '/default', headers=a['header'], json=dict(gear_id=gid2))
    assert client.delete(url + '/default', headers=a['header']).status_code == 200
    assert client.get(url + '/default', headers=a['header']).json['gear_id'] is None


# ---- 训练日志 ----

def test_journal_crud_revisions_and_privacy(tmp_path):
    app, client = gear_app(tmp_path)
    (me,) = make_people(app, 1)
    url = '/api/running/journal'
    r = client.post(url, headers=me['header'], json=dict(entry_date='2026-11-18', body='今天节奏稳定。', private_notes=SECRET))
    assert r.status_code == 201, r.json
    assert r.json['evidence_status'] == JOURNAL_EVIDENCE and r.json['score'] is None
    jid = r.json['journal']['id']
    assert r.json['journal']['revision'] == 0
    # 列表投影：无 private_notes 键，只有布尔标记
    rows = client.get(url, headers=me['header']).json['journal']
    assert len(rows) == 1 and 'private_notes' not in rows[0] and rows[0]['has_private_notes'] == 1
    assert SECRET not in client.get(url, headers=me['header']).get_data(as_text=True)
    # 详情：唯一可见私密备注的路径
    detail = client.get(url + '/%d' % jid, headers=me['header']).json['journal']
    assert detail['private_notes'] == SECRET
    # 更正：revision+1、旧版本（含私密备注）留痕
    r = client.patch(url + '/%d' % jid, headers=me['header'], json=dict(body='更正：后半程掉速。'))
    assert r.status_code == 200 and r.json['revision'] == 1
    revs = client.get(url + '/%d/revisions' % jid, headers=me['header']).json['revisions']
    assert len(revs) == 1
    prior = json.loads(revs[0]['prior_json'])
    assert prior['body'] == '今天节奏稳定。' and prior['private_notes'] == SECRET and prior['revision'] == 0
    # 私密备注可清空
    r = client.patch(url + '/%d' % jid, headers=me['header'], json=dict(private_notes=None))
    assert r.status_code == 200 and r.json['journal']['private_notes'] is None
    assert client.get(url, headers=me['header']).json['journal'][0]['has_private_notes'] == 0
    # 软删：列表排除、详情 404、历史保留、重复删 400
    assert client.delete(url + '/%d' % jid, headers=me['header']).status_code == 200
    assert client.delete(url + '/%d' % jid, headers=me['header']).status_code == 400
    assert client.get(url, headers=me['header']).json['journal'] == []
    assert client.get(url + '/%d' % jid, headers=me['header']).status_code == 404
    assert client.get(url + '/%d/revisions' % jid, headers=me['header']).json['revisions']


def test_journal_one_per_date_and_date_move(tmp_path):
    app, client = gear_app(tmp_path)
    (me,) = make_people(app, 1)
    url = '/api/running/journal'
    r = client.post(url, headers=me['header'], json=dict(entry_date='2026-11-18', body='第一条'))
    assert r.status_code == 201
    jid = r.json['journal']['id']
    assert client.post(url, headers=me['header'], json=dict(entry_date='2026-11-18', body='重复日')).status_code == 409
    jid2 = client.post(url, headers=me['header'], json=dict(entry_date='2026-11-17', body='另一天')).json['journal']['id']
    # 改日期撞车 → 409 且不产生更正快照
    assert client.patch(url + '/%d' % jid2, headers=me['header'], json=dict(entry_date='2026-11-18')).status_code == 409
    assert client.get(url + '/%d/revisions' % jid2, headers=me['header']).json['revisions'] == []
    # 软删后可重建同日（部分唯一索引）
    client.delete(url + '/%d' % jid, headers=me['header'])
    assert client.post(url, headers=me['header'], json=dict(entry_date='2026-11-18', body='重建')).status_code == 201
    # 日期筛选
    rows = client.get(url + '?from=2026-11-18&to=2026-11-18', headers=me['header']).json['journal']
    assert [r['body'] for r in rows] == ['重建']
    assert client.get(url + '?from=bad-date', headers=me['header']).status_code == 400


def test_journal_validation(tmp_path):
    app, client = gear_app(tmp_path)
    (me,) = make_people(app, 1)
    url = '/api/running/journal'
    for bad in ({}, {'entry_date': '2026-11-18'}, {'entry_date': '2026-11-18', 'body': '  '},
                {'entry_date': '2026-13-01', 'body': 'x'}, {'entry_date': '2026-11-18', 'body': '字' * 20001},
                {'entry_date': '2026-11-18', 'body': 'x', 'private_notes': '密' * 2001}):
        assert client.post(url, headers=me['header'], json=bad).status_code == 400, str(bad)[:60]
    assert client.get(url, headers=me['header']).json['journal'] == []
    jid = client.post(url, headers=me['header'], json=dict(entry_date='2026-11-18', body='x')).json['journal']['id']
    assert client.patch(url + '/%d' % jid, headers=me['header'], json=dict()).status_code == 400
    assert client.patch(url + '/%d' % jid, headers=me['header'], json=dict(body='')).status_code == 400


def test_private_notes_isolated_from_all_other_paths(tmp_path):
    """私密备注绝不进入列表/活动/周报/首页汇总/公共投影。"""
    app, client = gear_app(tmp_path)
    (me,) = make_people(app, 1)
    store = app.extensions['running_store']
    client.post('/api/running/journal', headers=me['header'],
                json=dict(entry_date='2026-11-18', body='公开训练记录', private_notes=SECRET))
    add_activity(store, me['id'], 5000, sha='p-1')
    for path in ('/api/running/journal', '/api/running/activities', '/api/running/dashboard?timezone=Asia/Shanghai',
                 '/api/running/weekly-report', '/api/running/gear'):
        text = client.get(path, headers=me['header']).get_data(as_text=True)
        assert SECRET not in text, path
    # 公共投影函数剥离私密备注
    row = store.journal(me['id'], store.journal_list(me['id'])[0]['id'])
    pub = public_journal(row)
    assert 'private_notes' not in pub and pub['body'] == '公开训练记录'
    # store 列表层就不取私密备注
    assert all('private_notes' not in r for r in store.journal_list(me['id']))


# ---- 隔离与匿名 ----

def test_gear_journal_isolation_same_name(tmp_path):
    app, client = gear_app(tmp_path)
    a, b = make_people(app, 2)
    store = app.extensions['running_store']
    act_a, _ = add_activity(store, a['id'], 5000, sha='iso-a')
    act_b, _ = add_activity(store, b['id'], 8000, sha='iso-b')
    gurl, jurl = '/api/running/gear', '/api/running/journal'
    ga = client.post(gurl, headers=a['header'], json=dict(name='同名鞋')).json['gear']['id']
    gb = client.post(gurl, headers=b['header'], json=dict(name='同名鞋')).json['gear']['id']
    ja = client.post(jurl, headers=a['header'], json=dict(entry_date='2026-11-18', body='甲的日志', private_notes=SECRET)).json['journal']['id']
    jb = client.post(jurl, headers=b['header'], json=dict(entry_date='2026-11-18', body='乙的日志', private_notes=SECRET)).json['journal']['id']
    assert link(client, a, act_a['id'], ga).status_code == 200
    assert link(client, b, act_b['id'], gb).status_code == 200
    client.put(gurl + '/default', headers=a['header'], json=dict(gear_id=ga))
    client.put(gurl + '/default', headers=b['header'], json=dict(gear_id=gb))
    for person, mine_g, foreign_g, mine_j, foreign_j, mine_act, foreign_act, km in (
            (a, ga, gb, ja, jb, act_a['id'], act_b['id'], 5.0),
            (b, gb, ga, jb, ja, act_b['id'], act_a['id'], 8.0)):
        h = person['header']
        # 列表零交叉（含 ?owner= 注入无效）
        for suffix in ('', '?owner=%d' % foreign_g):
            rows = client.get(gurl + suffix, headers=h).json['gear']
            assert [r['id'] for r in rows] == [mine_g]
            assert client.get(jurl + suffix.replace(str(foreign_g), str(foreign_j)), headers=h).json['journal'][0]['id'] == mine_j
        assert client.get(gurl + '/%d' % foreign_g, headers=h).status_code == 404
        assert client.patch(gurl + '/%d' % foreign_g, headers=h, json=dict(name='x')).status_code == 404
        assert client.post(gurl + '/%d/retire' % foreign_g, headers=h, json={}).status_code == 404
        assert client.put(gurl + '/default', headers=h, json=dict(gear_id=foreign_g)).status_code == 404
        assert client.get(jurl + '/%d' % foreign_j, headers=h).status_code == 404
        assert client.patch(jurl + '/%d' % foreign_j, headers=h, json=dict(body='x')).status_code == 404
        assert client.delete(jurl + '/%d' % foreign_j, headers=h).status_code == 404
        assert client.get(jurl + '/%d/revisions' % foreign_j, headers=h).status_code == 404
        assert link(client, person, foreign_act, mine_g).status_code == 404
        # 聚合零交叉：里程只含本人活动
        gear = client.get(gurl + '/%d' % mine_g, headers=h).json['gear']
        assert gear['total_distance_km'] == km and gear['activity_count'] == 1
        assert [x['id'] for x in gear['activities']] == [mine_act]
        assert client.get(gurl + '/default', headers=h).json['gear_id'] == mine_g
    # 一方退役/删除不影响另一方
    client.post(gurl + '/%d/retire' % ga, headers=a['header'], json={})
    client.delete(jurl + '/%d' % ja, headers=a['header'])
    assert client.get(gurl + '/%d' % gb, headers=b['header']).json['gear']['total_distance_km'] == 8.0
    assert len(client.get(jurl, headers=b['header']).json['journal']) == 1


def test_gear_journal_anonymous_401(tmp_path):
    app, client = gear_app(tmp_path)
    for method, path in (('get', '/api/running/gear'), ('post', '/api/running/gear'),
                         ('get', '/api/running/gear/1'), ('patch', '/api/running/gear/1'),
                         ('post', '/api/running/gear/1/retire'), ('get', '/api/running/gear/default'),
                         ('put', '/api/running/gear/default'), ('delete', '/api/running/gear/default'),
                         ('get', '/api/running/activities/1/gear'), ('put', '/api/running/activities/1/gear'),
                         ('delete', '/api/running/activities/1/gear'), ('get', '/api/running/journal'),
                         ('post', '/api/running/journal'), ('get', '/api/running/journal/1'),
                         ('patch', '/api/running/journal/1'), ('delete', '/api/running/journal/1'),
                         ('get', '/api/running/journal/1/revisions')):
        r = getattr(client, method)(path, json={})
        assert r.status_code == 401, (method, path, r.status_code)


# ---- 开关与回退 ----

def test_switch_off_404_and_existing_unaffected(tmp_path):
    app, client = gear_app(tmp_path, enabled=False)
    (me,) = make_people(app, 1)
    store = app.extensions['running_store']
    act, _ = add_activity(store, me['id'], 5000, sha='off-1')
    for method, path in (('get', '/api/running/gear'), ('post', '/api/running/gear'),
                         ('get', '/api/running/gear/1'), ('patch', '/api/running/gear/1'),
                         ('post', '/api/running/gear/1/retire'), ('get', '/api/running/gear/default'),
                         ('put', '/api/running/gear/default'), ('delete', '/api/running/gear/default'),
                         ('get', '/api/running/activities/1/gear'), ('put', '/api/running/activities/1/gear'),
                         ('delete', '/api/running/activities/1/gear'), ('get', '/api/running/journal'),
                         ('post', '/api/running/journal'), ('get', '/api/running/journal/1'),
                         ('patch', '/api/running/journal/1'), ('delete', '/api/running/journal/1'),
                         ('get', '/api/running/journal/1/revisions')):
        r = getattr(client, method)(path, headers=me['header'], json={})
        assert r.status_code == 404, (method, path, r.status_code)
    assert client.get('/gear').status_code == 404
    assert client.get('/journal').status_code == 404
    # 旧功能全保留
    assert client.get('/').status_code == 200
    assert client.get('/api/running/activities', headers=me['header']).status_code == 200
    assert client.get('/api/running/weekly-report', headers=me['header']).status_code == 200
    assert client.post('/api/running/activities', headers=me['header'],
                       json=dict(start_time=monday_local().isoformat(), distance_km=3.0)).status_code == 201
    assert client.get('/manual-entry').status_code == 200
    assert client.get('/weekly-report').status_code == 200
    assert client.get('/api/running/dashboard?timezone=Asia/Shanghai', headers=me['header']).json['gear_enabled'] is False


def test_pages_no_store_and_evidence(tmp_path):
    app, client = gear_app(tmp_path)
    (me,) = make_people(app, 1)
    for page in ('/gear', '/journal'):
        r = client.get(page)
        assert r.status_code == 200 and r.cache_control.no_store and r.cache_control.private
        text = r.get_data(as_text=True)
        assert '澄明训练台' in text and '未核验' in text
    r = client.post('/api/running/gear', headers=me['header'], json=dict(name='鞋'))
    assert r.cache_control.no_store
    r = client.post('/api/running/journal', headers=me['header'], json=dict(entry_date='2026-11-18', body='x'))
    assert r.cache_control.no_store
    summary = client.get('/api/running/dashboard?timezone=Asia/Shanghai', headers=me['header']).json
    assert summary['gear_enabled'] is True


# ---- 迁移与约束 ----

def _make_v2_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE activities (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
          start_time TEXT NOT NULL, sport TEXT NOT NULL,
          distance_m REAL, duration_s REAL, avg_hr REAL,
          source TEXT NOT NULL, file_sha256 TEXT, garmin_activity_id TEXT,
          imported_at TEXT NOT NULL, notes TEXT, deleted_at TEXT, revision INTEGER NOT NULL DEFAULT 0,
          UNIQUE(user_id,file_sha256), UNIQUE(user_id,garmin_activity_id));
        CREATE INDEX activity_user_time ON activities(user_id,start_time);
        CREATE TABLE activity_revisions (
          id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          changed_at TEXT NOT NULL, prior_json TEXT NOT NULL);
        CREATE TABLE garmin_connection (
          user_id INTEGER PRIMARY KEY, key_encrypted TEXT NOT NULL,
          key_expiry TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE sync_state (
          user_id INTEGER PRIMARY KEY, last_run_at TEXT, status TEXT, error TEXT);
        PRAGMA user_version=2;
    """)
    conn.execute("INSERT INTO activities (user_id,start_time,sport,distance_m,duration_s,avg_hr,source,file_sha256,imported_at,notes) "
                 "VALUES (1,'2026-01-05T08:00:00+00:00','running',5000,1800,150,'fit','sha-v2','2026-01-05T08:00:00+00:00','旧备注')")
    conn.commit()
    conn.close()


def test_migration_v2_to_v4_idempotent_backup_and_fk(tmp_path):
    old = tmp_path / 'old' / 'running.db'
    _make_v2_db(old)
    store = RunningStore(old)
    with store.conn() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        row = conn.execute('SELECT * FROM activities').fetchone()
        assert row['distance_m'] == 5000 and row['notes'] == '旧备注' and row['file_sha256'] == 'sha-v2'
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'gear', 'gear_activity', 'gear_default', 'journal', 'journal_revisions', 'laps', 'activity_tracks'} <= tables
        assert 'elevation_gain_m' in {r[1] for r in conn.execute('PRAGMA table_info(activities)')}
    # 迁移前自动备份（v2 快照），且备份本身仍是 v2
    backup = old.with_name(old.name + '.bak-v2')
    assert backup.exists()
    bconn = sqlite3.connect(str(backup))
    assert bconn.execute('PRAGMA user_version').fetchone()[0] == 2
    assert bconn.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 1
    bconn.close()
    # 幂等：重跑不增行、不重复备份
    store2 = RunningStore(old)
    with store2.conn() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        assert conn.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 1
    assert sorted(p.name for p in old.parent.glob('*.bak-*')) == [old.name + '.bak-v2']
    # 升级后 gear/journal 可正常读写
    g = store.add_gear(1, dict(name='迁移后的鞋'))
    assert store.link_gear(1, 1, g['id']) is not None
    assert store.gear(1, g['id'])['total_distance_km'] == 5.0
    j = store.add_journal(1, dict(entry_date='2026-11-18', body='迁移后的日志'))
    assert store.journal(1, j['id'])['body'] == '迁移后的日志'


def test_composite_owner_fk_constraints(tmp_path):
    app, client = gear_app(tmp_path)
    a, b = make_people(app, 2)
    store = app.extensions['running_store']
    act_b, _ = add_activity(store, b['id'], 5000, sha='fk-b')
    gear_a = store.add_gear(a['id'], dict(name='甲鞋'))
    # 跨主引用：gear_activity 双侧 FK 拒绝（即使绕过 API 直写 SQL）
    with store.conn() as conn:
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        try:
            conn.execute('INSERT INTO gear_activity (user_id,activity_id,gear_id,linked_at) VALUES (?,?,?,?)',
                         (a['id'], act_b['id'], gear_a['id'], '2026-11-19T00:00:00+00:00'))
            raise AssertionError('跨主 gear_activity 应被 FK 拒绝')
        except sqlite3.IntegrityError:
            pass
    # 同主活动换鞋 upsert 后仍唯一
    act_a, _ = add_activity(store, a['id'], 3000, sha='fk-a')
    gear_a2 = store.add_gear(a['id'], dict(name='甲鞋2'))
    store.link_gear(a['id'], act_a['id'], gear_a['id'])
    store.link_gear(a['id'], act_a['id'], gear_a2['id'])
    with store.conn() as conn:
        rows = conn.execute('SELECT gear_id FROM gear_activity WHERE user_id=? AND activity_id=?', (a['id'], act_a['id'])).fetchall()
        assert len(rows) == 1 and rows[0]['gear_id'] == gear_a2['id']
    # journal_revisions 跨主同样被拒
    j = store.add_journal(a['id'], dict(entry_date='2026-11-18', body='x'))
    with store.conn() as conn:
        try:
            conn.execute('INSERT INTO journal_revisions (journal_id,user_id,changed_at,prior_json) VALUES (?,?,?,?)',
                         (j['id'], b['id'], '2026-11-19T00:00:00+00:00', '{}'))
            raise AssertionError('跨主 journal_revisions 应被 FK 拒绝')
        except sqlite3.IntegrityError:
            pass


def test_delete_user_cleans_gear_journal(tmp_path):
    app, client = gear_app(tmp_path)
    a, b = make_people(app, 2)
    store = app.extensions['running_store']
    auth = app.extensions['poserun_auth']
    act_a, _ = add_activity(store, a['id'], 5000, sha='del-a')
    ga = store.add_gear(a['id'], dict(name='甲鞋'))
    store.link_gear(a['id'], act_a['id'], ga['id'])
    store.set_default_gear(a['id'], ga['id'])
    ja = store.add_journal(a['id'], dict(entry_date='2026-11-18', body='甲', private_notes=SECRET))
    store.update_journal(a['id'], ja['id'], dict(body='甲改'))
    gb = store.add_gear(b['id'], dict(name='乙鞋'))
    jb = store.add_journal(b['id'], dict(entry_date='2026-11-18', body='乙'))
    assert auth.delete_user(a['id']) is None
    with store.conn() as conn:
        for table in ('gear', 'gear_activity', 'gear_default', 'journal', 'journal_revisions', 'activities'):
            assert conn.execute('SELECT COUNT(*) FROM %s WHERE user_id=?' % table, (a['id'],)).fetchone()[0] == 0, table
        assert conn.execute('SELECT COUNT(*) FROM gear WHERE user_id=?', (b['id'],)).fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM journal WHERE user_id=?', (b['id'],)).fetchone()[0] == 1
    assert store.gear(b['id'], gb['id'])['name'] == '乙鞋'
    assert store.journal(b['id'], jb['id'])['body'] == '乙'