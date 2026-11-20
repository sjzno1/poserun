"""P2 切片5：轨迹抽取＋抽稀（离线轨迹地图的数据层）。AGPL-3.0

additive：摘要导入契约（parse_upload）不变；原始文件仍不落盘。
仅当 TRACK_MAP_ENABLED=1 时，导入成功后 best-effort 持久化抽稀轨迹点。
抽稀策略：超长轨迹先等距预抽稀（≤2×上限）→ Douglas-Peucker（米制投影，
容差 2m，迭代实现防递归溢出）→ 仍超限则等距封顶；每一步都保留首尾。
"""
import io
import math
from xml.etree import ElementTree

MAX_TRACK_POINTS = 2000
DP_EPSILON_M = 2.0
_EARTH_R = 6371000.0
_DEG2M = _EARTH_R * math.pi / 180.0


def _valid_coord(lat, lon):
    return (lat is not None and lon is not None
            and math.isfinite(lat) and math.isfinite(lon)
            and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0)


def _float_or_none(value):
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _gpx_points(data):
    root = ElementTree.fromstring(data)
    points = []
    for node in root.iter():
        if not node.tag.endswith('}trkpt') and node.tag != 'trkpt':
            continue
        lat = _float_or_none(node.get('lat'))
        lon = _float_or_none(node.get('lon'))
        if not _valid_coord(lat, lon):
            continue
        ele = time = None
        for child in node:
            tag = child.tag.rsplit('}', 1)[-1]
            if tag == 'ele':
                ele = _float_or_none(child.text)
            elif tag == 'time':
                time = (child.text or '').strip() or None
        points.append({'lat': lat, 'lon': lon, 'ele': ele, 'time': time})
    return points


def _tcx_points(data):
    root = ElementTree.fromstring(data)
    points = []
    for node in root.iter():
        if not node.tag.endswith('}Trackpoint') and node.tag != 'Trackpoint':
            continue
        lat = lon = ele = time = None
        for child in node:
            tag = child.tag.rsplit('}', 1)[-1]
            if tag == 'Position':
                for pos in child:
                    ptag = pos.tag.rsplit('}', 1)[-1]
                    if ptag == 'LatitudeDegrees':
                        lat = _float_or_none(pos.text)
                    elif ptag == 'LongitudeDegrees':
                        lon = _float_or_none(pos.text)
            elif tag == 'AltitudeMeters':
                ele = _float_or_none(child.text)
            elif tag == 'Time':
                time = (child.text or '').strip() or None
        if not _valid_coord(lat, lon):
            continue
        points.append({'lat': lat, 'lon': lon, 'ele': ele, 'time': time})
    return points


def _fit_points(data):
    try:
        from fitparse import FitFile
        fit = FitFile(io.BytesIO(data), check_crc=False)
        points = []
        for msg in fit.get_messages('record'):
            lat = msg.get_value('position_lat')
            lon = msg.get_value('position_long')
            if lat is None or lon is None:
                continue
            # FIT 位置单位为 semicircles：deg = semi * 180 / 2^31
            lat = lat * (180.0 / 2 ** 31)
            lon = lon * (180.0 / 2 ** 31)
            if not _valid_coord(lat, lon):
                continue
            ele = msg.get_value('enhanced_altitude')
            if ele is None:
                ele = msg.get_value('altitude')
            ts = msg.get_value('timestamp')
            points.append({'lat': lat, 'lon': lon,
                           'ele': _float_or_none(ele),
                           'time': ts.isoformat() if hasattr(ts, 'isoformat') else None})
        return points
    except Exception:
        return []  # best-effort：FIT 轨迹抽取失败不影响摘要导入


def extract_track_points(filename, data):
    """按扩展名分发抽取轨迹点（与 parse_upload 同口径）；无 GPS/失败 → []。"""
    ext = str(filename or '').lower().rsplit('.', 1)[-1]
    try:
        if ext == 'gpx':
            return _gpx_points(data)
        if ext == 'tcx':
            return _tcx_points(data)
        if ext == 'fit':
            return _fit_points(data)
    except ElementTree.ParseError:
        return []
    except Exception:
        return []
    return []


def _uniform_indices(n, cap):
    """等距抽稀索引，保留首尾（n>cap）。"""
    if n <= cap:
        return list(range(n))
    seen = []
    last = -1
    for i in range(cap):
        idx = round(i * (n - 1) / (cap - 1))
        if idx != last:
            seen.append(idx)
            last = idx
    return seen


def _dp_keep(xy, epsilon):
    """迭代 Douglas-Peucker：返回保留点索引（含首尾）。xy 为米制平面坐标。"""
    n = len(xy)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        dmax, idx = -1.0, a
        for i in range(a + 1, b):
            px, py = xy[i]
            if seg == 0.0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / seg
            if d > dmax:
                dmax, idx = d, i
        if dmax > epsilon:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [i for i in range(n) if keep[i]]


def simplify_track(points, max_points=MAX_TRACK_POINTS):
    """抽稀到 ≤max_points，保留首尾；lat/lon 6 位小数（≈0.11m）、ele 1 位。

    少于 2 点返回 []（不成轨迹）。超长轨迹先等距预抽稀到 2×上限，
    再做 DP（容差 2m），最后等距封顶——最坏 O((2×上限)²)，导入耗时可控。
    """
    pts = [p for p in points if _valid_coord(p.get('lat'), p.get('lon'))]
    n = len(pts)
    if n < 2:
        return []
    if n > 2 * max_points:
        pts = [pts[i] for i in _uniform_indices(n, 2 * max_points)]
        n = len(pts)
    lat0 = pts[0]['lat']
    k = math.cos(math.radians(lat0))
    xy = [(p['lon'] * k * _DEG2M, p['lat'] * _DEG2M) for p in pts]
    kept = _dp_keep(xy, DP_EPSILON_M)
    if len(kept) > max_points:
        kept = [kept[i] for i in _uniform_indices(len(kept), max_points)]
    out = []
    for i in kept:
        p = pts[i]
        ele = p.get('ele')
        out.append({'lat': round(p['lat'], 6), 'lon': round(p['lon'], 6),
                    'ele': round(ele, 1) if isinstance(ele, (int, float)) and math.isfinite(ele) else None,
                    'time': p.get('time')})
    return out