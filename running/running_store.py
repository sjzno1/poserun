"""Private, per-user SQLite activities. AGPL-3.0."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from measurement_safety import finite


class RunningStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS activities (
                  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                  start_time TEXT NOT NULL, sport TEXT NOT NULL,
                  distance_m REAL, duration_s REAL, avg_hr REAL,
                  source TEXT NOT NULL, file_sha256 TEXT, garmin_activity_id TEXT,
                  imported_at TEXT NOT NULL,
                  UNIQUE(user_id,file_sha256), UNIQUE(user_id,garmin_activity_id));
                CREATE INDEX IF NOT EXISTS activity_user_time ON activities(user_id,start_time);
                CREATE TABLE IF NOT EXISTS garmin_connection (
                  user_id INTEGER PRIMARY KEY, key_encrypted TEXT NOT NULL,
                  key_expiry TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sync_state (
                  user_id INTEGER PRIMARY KEY, last_run_at TEXT, status TEXT, error TEXT);
                PRAGMA user_version=1;
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def conn(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, uid, activity):
        activity = dict(activity)
        start = datetime.fromisoformat(activity['start_time'].replace('Z','+00:00'))
        if start.tzinfo is None:
            raise ValueError('活动时间必须含时区')
        activity['start_time'] = start.astimezone(timezone.utc).isoformat()
        for field in ('distance_m','duration_s','avg_hr'):
            value = activity.get(field)
            activity[field] = float(value) if finite(value) and value >= 0 else None
        with self.conn() as conn:
            # Alternate files of same session: conservative exact time + near distance/duration.
            duplicate = conn.execute("""SELECT * FROM activities WHERE user_id=? AND (
                (file_sha256 IS NOT NULL AND file_sha256=?) OR
                (garmin_activity_id IS NOT NULL AND garmin_activity_id=?) OR
                (start_time=? AND sport=? AND (source='demo')=(?='demo') AND abs(distance_m-?)<=1 AND abs(duration_s-?)<=1))""",
                (uid,activity.get('file_sha256'),activity.get('garmin_activity_id'),activity['start_time'],activity['sport'],activity['source'],activity['distance_m'],activity['duration_s'])).fetchone()
            if duplicate:
                return dict(duplicate), False
            columns = ('start_time','sport','distance_m','duration_s','avg_hr','source','file_sha256','garmin_activity_id')
            cur = conn.execute('INSERT INTO activities (user_id,'+','.join(columns)+',imported_at) VALUES ('+','.join('?' for _ in range(10))+')',
                (uid,)+tuple(activity.get(k) for k in columns)+(datetime.now(timezone.utc).isoformat(),))
            row = conn.execute('SELECT * FROM activities WHERE id=?',(cur.lastrowid,)).fetchone()
        return dict(row), True

    def activities(self, uid, limit=500, offset=0):
        with self.conn() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM activities WHERE user_id=? ORDER BY start_time DESC,id DESC LIMIT ? OFFSET ?', (uid,limit,offset))]

    def activity(self, uid, aid):
        with self.conn() as conn:
            row = conn.execute('SELECT * FROM activities WHERE user_id=? AND id=?',(uid,aid)).fetchone()
        return dict(row) if row else None
