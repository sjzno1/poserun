"""Descriptive overview; no synthetic load series in a personal dashboard."""
from .weekly_report import DEFAULT_ZONE, REAL_SOURCE, weekly_report

NOTICE = '活动摘要未核验；供跑者与教练讨论，不自动调整计划，不作医疗判断，不评分。'


def dashboard(store, uid, now=None, zone=DEFAULT_ZONE):
    week = weekly_report(store, uid, zone, now)['current']
    with store.conn() as conn:
        recent = [dict(r) for r in conn.execute(f"""SELECT * FROM activities WHERE user_id=? AND {REAL_SOURCE} AND deleted_at IS NULL
                  ORDER BY start_time DESC,id DESC LIMIT 5""", (uid,))]
    return dict(weekly_km=week['distance_km'] if week['count'] else 0, weekly_start=week['start'], week_timezone=zone,
                weekly_evidence=[a['id'] for a in week['activities']],
                weekly_missing_distance=sum(a['distance_m'] is None for a in week['activities']),
                recent_activities=recent, score=None, notice=NOTICE,
                load_trend=dict(source=None, values=[], evidence=[]))
