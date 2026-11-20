from datetime import datetime, timezone
from test_activities import web


def test_dashboard_week_uses_only_running_current_user_and_references(web):
    c, uid, app = web
    store = app.extensions['running_store']
    today = datetime.now(timezone.utc).isoformat()
    base = dict(start_time=today, sport='running', distance_m=5000, duration_s=1800, source='fit')
    a, _ = store.add(1, base)
    store.add(2, dict(base, distance_m=12000))
    store.add(1, dict(base, sport='cycling', distance_m=20000))
    store.add(1, dict(base, start_time='2020-01-01T00:00:00+00:00', distance_m=9000))
    d = c.get('/api/running/dashboard').json
    assert d['weekly_km'] == 5.0
    assert d['weekly_evidence'] == [a['id']]
    assert d['score'] is None
    assert d['load_trend']['source'] is None
    assert d['load_trend']['values'] == []
    assert d['load_trend']['evidence'] == []
    assert '未核验' in d['notice']
    uid[0] = 3
    d = c.get('/api/running/dashboard').json
    assert d['weekly_km'] == 0
    assert d['recent_activities'] == []


def test_nonfinite_values_stay_unknown(web):
    c, uid, app = web
    a, _ = app.extensions['running_store'].add(1, dict(start_time=datetime.now(timezone.utc).isoformat(),sport='running',distance_m=float('nan'),duration_s=float('inf'),source='fit'))
    assert a['distance_m'] is None
    assert a['duration_s'] is None
    d=c.get('/api/running/dashboard').json
    assert d['weekly_km'] is None
    assert d['weekly_missing_distance'] == 1
