"""Owner-scoped monthly/yearly training stats. Reuses weekly_report filters and week boundaries. AGPL-3.0
只用 activities 表摘要级字段；schema 不变（v4）；北京时间固定；缺测不报完整总量、不插算分段 PB。"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from measurement_safety import rounded
from .weekly_report import DEFAULT_ZONE, REAL_SOURCE

PB_MIN_DISTANCE_M = 5000  # 最快均配资格：整条活动距离 ≥ 5km（无逐点数据，不插算分段）


def training_stats(store, uid, year, zone=DEFAULT_ZONE, now=None):
    """所选年的月度/年度聚合＋PB＋最近 12 周趋势；全部按 uid 过滤。"""
    zone_info = ZoneInfo(zone)
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone_info)
    year_start = datetime(year, 1, 1, tzinfo=zone_info)
    year_end = datetime(year + 1, 1, 1, tzinfo=zone_info)

    def utc(dt):
        return dt.astimezone(timezone.utc).isoformat()

    def local_of(row):
        return datetime.fromisoformat(row['start_time']).astimezone(zone_info)

    with store.conn() as conn:
        rows = [dict(r) for r in conn.execute(f"""SELECT id,start_time,distance_m,duration_s
            FROM activities WHERE user_id=? AND sport='running' AND {REAL_SOURCE} AND deleted_at IS NULL
            AND start_time>=? AND start_time<? ORDER BY start_time ASC,id ASC""",
            (uid, utc(year_start), utc(year_end)))]
        # 周趋势相对当前时间，可能跨两个日历年的活动，单独取最近 12 周窗口
        monday = (local_now.replace(hour=0, minute=0, second=0, microsecond=0)
                  - timedelta(days=local_now.weekday()))
        trend_rows = [dict(r) for r in conn.execute(f"""SELECT id,start_time,distance_m
            FROM activities WHERE user_id=? AND sport='running' AND {REAL_SOURCE} AND deleted_at IS NULL
            AND start_time>=? AND start_time<? ORDER BY start_time ASC,id ASC""",
            (uid, utc(monday - timedelta(days=77)), utc(monday + timedelta(days=7))))]

    def summarize(activities):
        """缺距离/缺时长的活动不计入对应合计，excluded 计数说明；不完整合计不得当完整总量。"""
        count = len(activities)
        meters = [a['distance_m'] for a in activities if a['distance_m'] is not None]
        seconds = [a['duration_s'] for a in activities if a['duration_s'] is not None]
        distance_km = rounded(sum(meters) / 1000, 2) if meters else None
        duration_s = sum(seconds) if seconds else None
        pace = None
        if meters and seconds:
            total_m, total_s = sum(meters), sum(seconds)
            if total_m > 0 and total_s > 0:
                pace = rounded(total_s * 1000 / total_m, 1)  # 总时长/总距离，非均值的均值
        return dict(count=count, distance_km=distance_km, duration_s=duration_s,
                    avg_pace_s_per_km=pace,
                    excluded={'distance': count - len(meters), 'duration': count - len(seconds)})

    months = [dict(month=m, **summarize([a for a in rows
                                         if local_of(a).year == year and local_of(a).month == m]))
              for m in range(1, 13)]
    year_totals = summarize(rows)

    # ---- PB：已导入活动内最佳（所选年窗口），每项附活动 id/日期/距离 ----
    def activity_ref(a):
        local = local_of(a)
        return dict(id=a['id'], date=local.date().isoformat(),
                    distance_km=rounded(a['distance_m'] / 1000, 2) if a['distance_m'] is not None else None)

    longest = max((a for a in rows if a['distance_m'] is not None), key=lambda a: a['distance_m'], default=None)
    longest_dur = max((a for a in rows if a['duration_s'] is not None), key=lambda a: a['duration_s'], default=None)
    eligible = [a for a in rows if a['distance_m'] is not None and a['distance_m'] >= PB_MIN_DISTANCE_M
                and a['duration_s'] is not None and a['duration_s'] > 0]
    fastest = min(eligible, key=lambda a: a['duration_s'] * 1000 / a['distance_m'], default=None)
    # 最佳周：周界与 weekly_report.weekly_report 同公式（本地周一 00:00）；仅全部活动距离非空的周可比
    weeks = {}
    for a in rows:
        local = local_of(a)
        ws = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
        weeks.setdefault(ws, []).append(a)
    best_week, best_km = None, None
    for ws in sorted(weeks):
        acts = weeks[ws]
        if not all(a['distance_m'] is not None for a in acts):
            continue
        cand_km = round(sum(a['distance_m'] for a in acts) / 1000, 2)  # 全部距离非空，finite 保证
        if best_km is None or cand_km > best_km:
            best_km = cand_km
            best_week = dict(week_start=ws.date().isoformat(), distance_km=cand_km,
                             count=len(acts), activity_ids=[a['id'] for a in acts])
    pb = dict(scope='已导入活动内最佳', window=str(year),
              longest_distance=dict(value_km=rounded(longest['distance_m'] / 1000, 2) if longest else None,
                                    activity=activity_ref(longest) if longest else None),
              longest_duration=dict(value_s=longest_dur['duration_s'] if longest_dur else None,
                                    activity=activity_ref(longest_dur) if longest_dur else None),
              fastest_pace=dict(value_s_per_km=rounded(fastest['duration_s'] * 1000 / fastest['distance_m'], 1) if fastest else None,
                                activity=activity_ref(fastest) if fastest else None,
                                note='整条活动均配 · 仅统计距离≥5km 且时长非空的活动'),
              best_week=best_week)

    # ---- 周趋势：最近 12 周（含本周），北京时间周一 00:00 分周；空周为 0，缺距离周不报合计 ----
    weekly_trend = []
    for i in range(11, -1, -1):
        ws = monday - timedelta(days=7 * i)
        we = ws + timedelta(days=7)
        acts = [a for a in trend_rows if ws <= local_of(a) < we]
        if not acts:
            weekly_trend.append(dict(week_start=ws.date().isoformat(), distance_km=0, count=0))
            continue
        meters = [a['distance_m'] for a in acts if a['distance_m'] is not None]
        weekly_trend.append(dict(week_start=ws.date().isoformat(),
                                 distance_km=rounded(sum(meters) / 1000, 2) if meters else None,
                                 count=len(acts)))

    return dict(timezone=zone, year=year, months=months, year_totals=year_totals, pb=pb, weekly_trend=weekly_trend)
