"""P2 切片6 数据导出（CSV/JSON）纯函数。AGPL-3.0；无 pose/model 依赖。

口径（契约锁定）：
- 仅本人未软删活动，按 start_time 升序（同刻按 id）；全部运动/来源保真，仅排软删；
- 年份按北京时间（DEFAULT_ZONE）日历年过滤，与训练统计一致；year=None=全部；
- distance_m→distance_km 无损换算后圆整到 6 位小数（毫米级，消除浮点噪声）；
- CSV：UTF-8 BOM＋固定表头，缺测留空；JSON：缺测 null，含原始 distance_m 与换算 distance_km；
- 不含 file_sha256/garmin_activity_id/laps（简洁口径）；训练日志私密备注不在导出路径。
"""
import csv
import io
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .weekly_report import DEFAULT_ZONE

CSV_HEADER = ['id', 'start_time', 'sport', 'distance_km', 'duration_s', 'avg_hr',
              'elevation_gain_m', 'source', 'notes']
_RECORD_FIELDS = ('id', 'start_time', 'sport', 'distance_m', 'duration_s', 'avg_hr',
                  'elevation_gain_m', 'source', 'notes', 'imported_at', 'revision')


def _km(distance_m):
    return round(distance_m / 1000.0, 6) if distance_m is not None else None


def export_activities(store, uid, year=None, zone=DEFAULT_ZONE):
    """本人未软删活动（start_time 升序）；year 按指定时区日历年过滤。"""
    tz = ZoneInfo(zone)
    rows = []
    with store.conn() as conn:
        cursor = conn.execute(
            'SELECT * FROM activities WHERE user_id=? AND deleted_at IS NULL '
            'ORDER BY start_time, id', (uid,))
        for raw in cursor:
            row = dict(raw)
            if year is not None:
                start = datetime.fromisoformat(row['start_time'])
                if start.astimezone(tz).year != year:
                    continue
            rows.append(row)
    return rows


def activity_record(row):
    record = {key: row[key] for key in _RECORD_FIELDS}
    record['distance_km'] = _km(row['distance_m'])
    return record


def _cell(value):
    return '' if value is None else str(value)


def build_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\n')
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow([
            _cell(row['id']), _cell(row['start_time']), _cell(row['sport']),
            _cell(_km(row['distance_m'])), _cell(row['duration_s']), _cell(row['avg_hr']),
            _cell(row['elevation_gain_m']), _cell(row['source']), _cell(row['notes']),
        ])
    return buf.getvalue().encode('utf-8-sig')  # UTF-8 BOM：Excel 中文兼容


def build_json(rows, year, exported_at=None):
    stamp = exported_at or datetime.now(timezone.utc)
    return {
        'exported_at': stamp.isoformat(),
        'year': year,
        'count': len(rows),
        'activities': [activity_record(row) for row in rows],
    }


def dumps_json(payload):
    return json.dumps(payload, ensure_ascii=False)