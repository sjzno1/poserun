"""Descriptive aggregates only. P1 load model is explicitly synthetic."""
from datetime import datetime, timedelta, timezone
from measurement_safety import rounded

NOTICE = '活动摘要未核验；供跑者与教练讨论，不自动调整计划，不作医疗判断，不评分。'


def dashboard(store, uid, now=None):
    now = now or datetime.now(timezone.utc)
    start = (now-timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
    end = start+timedelta(days=7)
    # Query without pagination to avoid undercounting busy weeks.
    with store.conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT id,distance_m FROM activities WHERE user_id=? AND sport='running' AND start_time>=? AND start_time<?",(uid,start.isoformat(),end.isoformat()))]
    missing = sum(r['distance_m'] is None for r in rows)
    total = None if missing else rounded(sum(r['distance_m'] for r in rows)/1000,2)
    return dict(weekly_km=total, weekly_start=start.isoformat(), week_timezone='UTC',
                weekly_evidence=[r['id'] for r in rows], weekly_missing_distance=missing,
                recent_activities=store.activities(uid,5), score=None,notice=NOTICE,
                load_trend=dict(source='demo', label='演示数据 · 非本人实测 · 未核验',
                                unit='a.u.（任意单位）', values=[21,34,28,40,32], evidence=[]))
