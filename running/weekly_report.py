"""Owner-scoped summaries. Records are normalized to UTC at ingestion."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from measurement_safety import rounded

DEFAULT_ZONE = 'Asia/Shanghai'
# Non-demo ingestion paths that provide evidence, including runner self-reported manual entries.
REAL_SOURCE = "source IN ('fit','api','manual','gpx','tcx')"


def weekly_report(store, uid, zone=DEFAULT_ZONE, now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
    monday = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    previous, end = monday-timedelta(days=7), monday+timedelta(days=7)
    def utc(dt):
        return dt.astimezone(timezone.utc).isoformat()
    with store.conn() as conn:
        rows = [dict(r) for r in conn.execute(f"""SELECT id,source,start_time,distance_m,duration_s
            FROM activities WHERE user_id=? AND sport='running' AND {REAL_SOURCE} AND deleted_at IS NULL
            AND start_time>=? AND start_time<? ORDER BY start_time DESC,id DESC""",
            (uid, utc(previous), utc(end)))]
    boundary = utc(monday)
    def summarize(start, stop, activities):
        def total(field):
            if activities and all(a[field] is not None for a in activities):
                return sum(a[field] for a in activities)
        meters, seconds = total('distance_m'), total('duration_s')
        return dict(start=start.isoformat(), end=stop.isoformat(), activities=activities,
                    count=len(activities), distance_km=rounded(meters/1000, 2) if meters is not None else None,
                    duration_s=seconds, pace_s_km=rounded(seconds*1000/meters, 1) if meters and seconds is not None else None)
    return dict(timezone=zone, current=summarize(monday, end, [a for a in rows if a['start_time'] >= boundary]),
                previous=summarize(previous, monday, [a for a in rows if a['start_time'] < boundary]))
