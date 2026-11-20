"""FIT summary import. Original GPS bytes never published or persisted in P1."""
import hashlib
import io
from datetime import timezone
from fitparse import FitFile

MAX_FIT_BYTES = 10 * 1024 * 1024


def parse_fit(data):
    if not data or len(data) > MAX_FIT_BYTES:
        raise ValueError('FIT 文件为空或超过 10 MB')
    try:
        fit = FitFile(io.BytesIO(data), check_crc=True)
        # Parse entire stream, including trailing CRC, before accepting any session.
        messages = list(fit.get_messages())
        sessions = [m.get_values() for m in messages if m.name == 'session']
        if len(sessions) != 1:
            raise ValueError('P1 仅接受单 session 活动 FIT；多运动请分别导出')
        s = sessions[0]
        start = s.get('start_time')
        if start is None:
            raise ValueError('缺少 session start_time')
        return dict(start_time=start.replace(tzinfo=timezone.utc).isoformat(),
                    sport=str(s.get('sport') or 'unknown'), distance_m=s.get('total_distance'),
                    duration_s=s.get('total_timer_time'), avg_hr=s.get('avg_heart_rate'),
                    source='fit', file_sha256=hashlib.sha256(data).hexdigest())
    except Exception:
        # Parser errors may contain uploaded bytes; do not echo/log raw exceptions.
        raise ValueError('无法解析 FIT：请检查格式、CRC 和单 session 活动摘要') from None
