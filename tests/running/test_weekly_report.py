from datetime import datetime, timedelta, timezone

from running.preview import create_app


def test_same_name_reports_are_private_across_local_midnight(tmp_path):
    app = create_app(tmp_path)
    app.config['WEEKLY_REPORT_ENABLED'] = True
    client = app.test_client()
    auth = app.extensions['poserun_auth']
    store = app.extensions['running_store']
    people = [auth.register_user(name, 'Synthetic-only-test', '同名跑者')[0]
              for name in ('runner_a', 'runner_b')]
    headers = [{'Authorization': 'Bearer '+auth.create_token(user)} for user in people]
    url = '/api/running/weekly-report'
    def get(path, header=None, status=200):
        response = client.get(path, headers=header)
        assert response.status_code == status
        assert response.cache_control.no_store
        return response.get_json()
    get(url, status=401)
    local = datetime.now(timezone(timedelta(hours=8)))
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    def add(person, when, **changes):
        row = dict(source='fit', sport='running', start_time=when.isoformat(), distance_m=5000, duration_s=1800)
        row.update(changes)
        return store.add(people[person]['id'], row)
    prev = add(0, monday-timedelta(seconds=1))[0]['id']
    a = add(0, monday, distance_m=None, file_sha256='synthetic-a')[0]['id']
    assert not add(0, monday, distance_m=None, file_sha256='synthetic-a')[1]
    b = add(1, monday, duration_s=None)[0]['id']
    add(0, monday, sport='cycling'); add(0, monday, source='demo')
    def check(week, ids, km, seconds):
        assert week['count'] == len(ids)
        assert [r['id'] for r in week['activities']] == ids
        assert (week['distance_km'], week['duration_s']) == (km, seconds)
        if km is None or seconds is None:
            assert week['pace_s_km'] is None
    report = get(url, headers[0]); assert report['timezone'] == 'Asia/Shanghai'
    check(report['current'], [a], None, 1800); check(report['previous'], [prev], 5, 1800)
    for _ in range(2):
        other = get(url+'?owner='+str(people[0]['id']), headers[1])
        check(other['current'], [b], 5, None); check(other['previous'], [], None, None)
    for header, mine, foreign in [(headers[0], a, b), (headers[1], b, a)]:
        path = '/api/running/activities/'
        get(path+str(foreign), header, 404); assert get(path+str(mine), header)['activity']['id'] == mine
    overview = get('/api/running/dashboard', headers[0])
    assert {r['source'] for r in overview['recent_activities']} == {'fit'}
    assert overview['load_trend']['values'] == []
    admin = auth.get_user_by_id(1)
    assert admin['role'] == 'admin'
    check(get(url, {'Authorization': 'Bearer '+auth.create_token(admin)})['current'], [], None, None)
    assert get(url+'?timezone=UTC', headers[0])['timezone'] == 'UTC'
    get(url+'?timezone=invalid', headers[0], 400)
    app.config['WEEKLY_REPORT_ENABLED'] = False
    get(url, headers[0], 404)
    assert client.get('/weekly-report').status_code == 404


def test_private_totp_challenge_replay_and_revocation(tmp_path):
    import time
    from auth import _totp_at
    app = create_app(tmp_path)
    app.config['WEEKLY_REPORT_ENABLED'] = True
    c = app.test_client()
    login = dict(username='weekly_totp', password='Synthetic-local-only')
    assert c.post('/api/auth/register', json=login).status_code == 201
    def bearer(response):
        return {'Authorization': 'Bearer '+response.json['token']}
    header = bearer(c.post('/api/auth/login', json=login))
    setup = c.post('/api/auth/me/2fa/setup', headers=header).json
    code = _totp_at(setup['secret'], int(time.time()) // setup['period'])
    assert c.post('/api/auth/me/2fa/enable', headers=header, json={'code':code}).status_code == 200
    assert c.post('/api/auth/login', json=login).json == {'mfa_required': True}
    verified = bearer(c.post('/api/auth/login', json=dict(login, totp_code=code)))
    assert c.post('/api/auth/login', json=dict(login, totp_code=code)).status_code == 401
    assert c.get('/api/running/weekly-report', headers=verified).status_code == 200
    assert c.post('/api/auth/logout', headers=verified).status_code == 200
    assert c.get('/api/running/weekly-report', headers=verified).status_code == 401
