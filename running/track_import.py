"""GPX/TCX 活动文件摘要解析（P2 第三切片）。AGPL-3.0

证据契约：只返回摘要与圈汇总；坐标仅用于即时累加距离/海拔，算完即弃——
本模块任何返回结构都不含 lat/lon，逐点轨迹属后续地图维度。
GPX/TCX 用 stdlib xml.etree，不新增重依赖；FIT 沿用既有 fit_import。
"""
import hashlib
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .fit_import import MAX_FIT_BYTES, parse_fit

MAX_IMPORT_BYTES = MAX_FIT_BYTES
_EARTH_R_M = 6371000.0


def _local(tag):
    """XML 标签去命名空间（容忍 GPX 1.0/1.1 与 TCX v2 命名空间差异）。"""
    return tag.rsplit('}', 1)[-1]


def _parse_time(text):
    """解析 ISO 时间；无法解析按缺测处理（返回 None），不臆造。"""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt):
    return dt.isoformat() if dt is not None else None


def _haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _num(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _check_size(data):
    if not data or len(data) > MAX_IMPORT_BYTES:
        raise ValueError('文件为空或超过 10 MB')


def parse_gpx(data):
    """trkpt → 摘要 distance(haversine 累加) / duration(首末 time) / start_utc / 海拔增益。
    无 time 元素 → start_time/duration 为 None（不算配速）。坐标累加后立即丢弃。
    返回 (summary, [])：GPX 无圈结构。"""
    _check_size(data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError('文件不是有效的 XML，无法解析')
    segments = []
    for trk in root.iter():
        if _local(trk.tag) != 'trk':
            continue
        for seg in trk:
            if _local(seg.tag) != 'trkseg':
                continue
            points = []
            for pt in seg:
                if _local(pt.tag) != 'trkpt':
                    continue
                try:
                    lat, lon = float(pt.get('lat')), float(pt.get('lon'))
                except (TypeError, ValueError):
                    raise ValueError('GPX 轨迹点缺少有效坐标')
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError('GPX 坐标超出范围')
                time = ele = None
                for child in pt:
                    name = _local(child.tag)
                    if name == 'time':
                        time = _parse_time(child.text)
                    elif name == 'ele':
                        ele = _num(child.text)
                points.append((lat, lon, time, ele))
            if points:
                segments.append(points)
    if not segments:
        raise ValueError('GPX 缺少轨迹点（trkpt）')
    distance_m = 0.0
    elevation_gain = 0.0
    has_ele = False
    times = []
    for points in segments:
        for (lat1, lon1, _t1, e1), (lat2, lon2, _t2, e2) in zip(points, points[1:]):
            distance_m += _haversine_m(lat1, lon1, lat2, lon2)  # 算完即弃坐标
            if e1 is not None and e2 is not None and e2 > e1:
                elevation_gain += e2 - e1
        for _lat, _lon, t, e in points:
            if t is not None:
                times.append(t)
            if e is not None:
                has_ele = True
    start = min(times) if times else None
    summary = {
        'start_time': _iso_utc(start),
        'sport': 'running',  # GPX 无运动类型字段，本系统按跑步归档
        'distance_m': round(distance_m, 3),
        'duration_s': (max(times) - min(times)).total_seconds() if times else None,
        'avg_hr': None,  # GPX 核心 schema 无心率 → 缺测
        'elevation_gain_m': round(elevation_gain, 3) if has_ele else None,
        'source': 'gpx',
        'file_sha256': hashlib.sha256(data).hexdigest(),
    }
    return summary, []


def _tcx_lap(index, lap):
    """单圈：优先 Lap 汇总字段；缺失时用 Track/Trackpoint 兜底（haversine/首末时间）。"""
    values = {'lap_index': index, 'start_time': None,
              'distance_m': None, 'duration_s': None, 'avg_hr': None}
    start = _parse_time(lap.get('StartTime'))
    if start is not None:
        values['start_time'] = _iso_utc(start)
    for child in lap:
        name = _local(child.tag)
        if name == 'TotalTimeSeconds':
            values['duration_s'] = _num(child.text)
        elif name == 'DistanceMeters':
            values['distance_m'] = _num(child.text)
        elif name == 'AverageHeartRateBpm':
            for sub in child:
                if _local(sub.tag) == 'Value':
                    values['avg_hr'] = _num(sub.text)
        elif name == 'Track':
            points = []
            for tp in child:
                if _local(tp.tag) != 'Trackpoint':
                    continue
                t = lat = lon = None
                for sub in tp:
                    sname = _local(sub.tag)
                    if sname == 'Time':
                        t = _parse_time(sub.text)
                    elif sname == 'Position':
                        for pos in sub:
                            pname = _local(pos.tag)
                            if pname == 'LatitudeDegrees':
                                lat = _num(pos.text)
                            elif pname == 'LongitudeDegrees':
                                lon = _num(pos.text)
                points.append((lat, lon, t))
            if values['distance_m'] is None:
                dist = 0.0
                pairs = 0
                for (la1, lo1, _), (la2, lo2, _) in zip(points, points[1:]):
                    if la1 is not None and la2 is not None:
                        dist += _haversine_m(la1, lo1, la2, lo2)  # 算完即弃坐标
                        pairs += 1
                if pairs:
                    values['distance_m'] = round(dist, 3)
            if values['duration_s'] is None:
                ts = [t for _la, _lo, t in points if t is not None]
                if ts:
                    values['duration_s'] = (max(ts) - min(ts)).total_seconds()
    return values


def parse_tcx(data):
    """Activity/Lap/Track/Trackpoint → 摘要＋laps[]（lap_index 按文档顺序保序）。
    活动汇总=圈合计；任一圈缺值则整体按缺测，不插算。"""
    _check_size(data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise ValueError('文件不是有效的 XML，无法解析')
    activities = [el for el in root.iter() if _local(el.tag) == 'Activity']
    if len(activities) != 1:
        raise ValueError('仅接受单活动 TCX；多活动请分别导出')
    activity = activities[0]
    laps = [_tcx_lap(i, lap) for i, lap in enumerate(el for el in activity if _local(el.tag) == 'Lap')]
    if not laps:
        raise ValueError('TCX 缺少 Lap 圈记录')
    sport = (activity.get('Sport') or 'unknown').strip().lower() or 'unknown'
    starts = [lap['start_time'] for lap in laps if lap['start_time']]

    def total(key):
        values = [lap[key] for lap in laps]
        return round(sum(values), 3) if all(v is not None for v in values) else None

    distance_m, duration_s = total('distance_m'), total('duration_s')
    avg_hr = None
    if duration_s and all(lap['avg_hr'] is not None and lap['duration_s'] is not None for lap in laps):
        avg_hr = round(sum(lap['avg_hr'] * lap['duration_s'] for lap in laps) / duration_s, 1)
    summary = {
        'start_time': starts[0] if starts else None,
        'sport': sport,
        'distance_m': distance_m,
        'duration_s': duration_s,
        'avg_hr': avg_hr,
        'elevation_gain_m': None,  # TCX 圈汇总无可靠海拔增益 → 缺测
        'source': 'tcx',
        'file_sha256': hashlib.sha256(data).hexdigest(),
    }
    return summary, laps


def parse_upload(filename, data):
    """扩展名优先、内容兜底分发：.fit → 既有 FIT 摘要；.gpx/.tcx → 轨迹摘要；
    未知扩展名嗅探 XML 内容（<gpx / <TrainingCenterDatabase）。
    返回 (activity_dict, laps)。解析失败抛 ValueError（安全文案，不回显原始字节）。"""
    name = (filename or '').lower()
    if name.endswith('.fit'):
        return parse_fit(data), []
    if name.endswith('.gpx'):
        return parse_gpx(data)
    if name.endswith('.tcx'):
        return parse_tcx(data)
    head = (data or b'')[:512].lstrip().lower()
    if head.startswith(b'<'):
        if b'<gpx' in head:
            return parse_gpx(data)
        if b'<trainingcenterdatabase' in head:
            return parse_tcx(data)
    raise ValueError('仅支持 .fit / .gpx / .tcx 文件')
