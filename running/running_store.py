"""Private, per-user SQLite activities. AGPL-3.0.
P2 第三切片 schema v4：activities 增 elevation_gain_m；新增 laps 圈表（复合 owner 键）。
P2 第五切片 schema v5：additive 新增 activity_tracks 抽稀轨迹表（CREATE TABLE IF NOT EXISTS；
回滚到 v4 代码仅重置 user_version，空表留存无害，不 DROP 任何数据）。"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from measurement_safety import finite, rounded

MAX_NOTES = 2000
MAX_GEAR_NAME = 120
MAX_JOURNAL_BODY = 20000
SCHEMA_VERSION = 5

BASE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS activities (
      id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
      start_time TEXT NOT NULL, sport TEXT NOT NULL,
      distance_m REAL, duration_s REAL, avg_hr REAL, elevation_gain_m REAL,
      source TEXT NOT NULL, file_sha256 TEXT, garmin_activity_id TEXT,
      imported_at TEXT NOT NULL,
      notes TEXT, deleted_at TEXT, revision INTEGER NOT NULL DEFAULT 0,
      UNIQUE(user_id,file_sha256), UNIQUE(user_id,garmin_activity_id));
    CREATE INDEX IF NOT EXISTS activity_user_time ON activities(user_id,start_time);
    CREATE TABLE IF NOT EXISTS garmin_connection (
      user_id INTEGER PRIMARY KEY, key_encrypted TEXT NOT NULL,
      key_expiry TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sync_state (
      user_id INTEGER PRIMARY KEY, last_run_at TEXT, status TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS activity_revisions (
      id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      changed_at TEXT NOT NULL, prior_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS revision_activity ON activity_revisions(activity_id,id);
    CREATE UNIQUE INDEX IF NOT EXISTS activity_owner_id ON activities(user_id,id);
    CREATE TABLE IF NOT EXISTS gear (
      id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
      name TEXT NOT NULL, brand_model TEXT, started_on TEXT, retired_on TEXT,
      notes TEXT, max_distance_km REAL,
      created_at TEXT NOT NULL, deleted_at TEXT,
      UNIQUE(user_id,id));
    CREATE TABLE IF NOT EXISTS gear_activity (
      user_id INTEGER NOT NULL, activity_id INTEGER NOT NULL, gear_id INTEGER NOT NULL,
      linked_at TEXT NOT NULL,
      PRIMARY KEY(user_id,activity_id),
      FOREIGN KEY(user_id,activity_id) REFERENCES activities(user_id,id),
      FOREIGN KEY(user_id,gear_id) REFERENCES gear(user_id,id));
    CREATE INDEX IF NOT EXISTS gear_activity_gear ON gear_activity(user_id,gear_id);
    CREATE TABLE IF NOT EXISTS gear_default (
      user_id INTEGER PRIMARY KEY, gear_id INTEGER NOT NULL,
      FOREIGN KEY(user_id,gear_id) REFERENCES gear(user_id,id));
    CREATE TABLE IF NOT EXISTS journal (
      id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
      entry_date TEXT NOT NULL, body TEXT NOT NULL, private_notes TEXT,
      created_at TEXT NOT NULL, updated_at TEXT,
      revision INTEGER NOT NULL DEFAULT 0, deleted_at TEXT,
      UNIQUE(user_id,id));
    CREATE UNIQUE INDEX IF NOT EXISTS journal_owner_date ON journal(user_id,entry_date) WHERE deleted_at IS NULL;
    CREATE TABLE IF NOT EXISTS journal_revisions (
      id INTEGER PRIMARY KEY, journal_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      changed_at TEXT NOT NULL, prior_json TEXT NOT NULL,
      FOREIGN KEY(user_id,journal_id) REFERENCES journal(user_id,id));
    CREATE INDEX IF NOT EXISTS journal_revision_entry ON journal_revisions(journal_id,id);
    CREATE TABLE IF NOT EXISTS laps (
      user_id INTEGER NOT NULL, activity_id INTEGER NOT NULL, lap_index INTEGER NOT NULL,
      start_time TEXT, distance_m REAL, duration_s REAL, avg_hr REAL,
      PRIMARY KEY(user_id,activity_id,lap_index),
      FOREIGN KEY(user_id,activity_id) REFERENCES activities(user_id,id));
    CREATE TABLE IF NOT EXISTS activity_tracks (
      user_id INTEGER NOT NULL, activity_id INTEGER NOT NULL,
      points_json TEXT NOT NULL, point_count INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY(user_id,activity_id),
      FOREIGN KEY(user_id,activity_id) REFERENCES activities(user_id,id));
"""


class DeviceSourceError(PermissionError):
    """设备来源（fit/api）记录只读，仅手工记录可更正。"""


class JournalDateConflict(ValueError):
    """同一天已有未删除日志（每日一条，软删后可重建）。"""


def _normalize_start(value):
    if not str(value or '').strip():
        raise ValueError('开始时间必填')
    start = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if start.tzinfo is None:
        raise ValueError('活动时间必须含时区')
    return start.astimezone(timezone.utc).isoformat()


def _check_notes(value):
    if value is not None and (not isinstance(value, str) or len(value) > MAX_NOTES):
        raise ValueError('备注不能超过 %d 字' % MAX_NOTES)


class RunningStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.conn() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version < 1:
                conn.executescript(BASE_SCHEMA)
            elif version < SCHEMA_VERSION:
                # 迁移前自动备份（一致性快照，确定性文件名，重跑不重复备份）。
                self._backup_before_migration(conn, version)
                if version < 2:
                    # 增量、幂等：旧库仅补列与新表，绝不 DROP/重建，已有数据不动。
                    for column, declaration in (('notes', 'TEXT'), ('deleted_at', 'TEXT'), ('revision', 'INTEGER NOT NULL DEFAULT 0')):
                        if column not in {row[1] for row in conn.execute('PRAGMA table_info(activities)')}:
                            conn.execute('ALTER TABLE activities ADD COLUMN ' + column + ' ' + declaration)
                if version < 4:
                    if 'elevation_gain_m' not in {row[1] for row in conn.execute('PRAGMA table_info(activities)')}:
                        conn.execute('ALTER TABLE activities ADD COLUMN elevation_gain_m REAL')
                conn.executescript(BASE_SCHEMA)
            conn.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
        os.chmod(self.path, 0o600)

    def _backup_before_migration(self, conn, version):
        backup = self.path.with_name(self.path.name + '.bak-v%d' % version)
        if backup.exists():
            return
        dest = sqlite3.connect(str(backup))
        try:
            conn.backup(dest)
        finally:
            dest.close()
        os.chmod(backup, 0o600)

    @contextmanager
    def conn(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, uid, activity, laps=None):
        activity = self._clean_activity(activity)
        with self.conn() as conn:
            row, created = self._add_in_conn(conn, uid, activity)
            if created and laps:
                for lap in laps:
                    self._insert_lap(conn, uid, row['id'], lap)
        return row, created

    def _clean_activity(self, activity):
        activity = dict(activity)
        activity['start_time'] = _normalize_start(activity['start_time'])
        for field in ('distance_m','duration_s','avg_hr','elevation_gain_m'):
            value = activity.get(field)
            activity[field] = float(value) if finite(value) and value >= 0 else None
        notes = activity.get('notes')
        activity['notes'] = notes if isinstance(notes, str) else None
        return activity

    def _add_in_conn(self, conn, uid, activity):
        # Alternate files of same session: conservative exact time + near distance/duration.
        # 去重口径不变：软删的导入仍按 sha 去重（恢复为后续切片）。
        duplicate = conn.execute("""SELECT * FROM activities WHERE user_id=? AND (
            (file_sha256 IS NOT NULL AND file_sha256=?) OR
            (garmin_activity_id IS NOT NULL AND garmin_activity_id=?) OR
            (start_time=? AND sport=? AND (source='demo')=(?='demo') AND abs(distance_m-?)<=1 AND abs(duration_s-?)<=1))""",
            (uid,activity.get('file_sha256'),activity.get('garmin_activity_id'),activity['start_time'],activity['sport'],activity['source'],activity['distance_m'],activity['duration_s'])).fetchone()
        if duplicate:
            return dict(duplicate), False
        columns = ('start_time','sport','distance_m','duration_s','avg_hr','elevation_gain_m','source','file_sha256','garmin_activity_id','notes')
        cur = conn.execute('INSERT INTO activities (user_id,'+','.join(columns)+',imported_at) VALUES ('+','.join('?' for _ in range(12))+')',
            (uid,)+tuple(activity.get(k) for k in columns)+(datetime.now(timezone.utc).isoformat(),))
        row = conn.execute('SELECT * FROM activities WHERE id=?',(cur.lastrowid,)).fetchone()
        return dict(row), True

    def _insert_lap(self, conn, uid, aid, lap):
        def clean(value):
            return float(value) if finite(value) and value >= 0 else None
        conn.execute('INSERT INTO laps (user_id,activity_id,lap_index,start_time,distance_m,duration_s,avg_hr) VALUES (?,?,?,?,?,?,?)',
                     (uid, aid, int(lap['lap_index']), lap.get('start_time'),
                      clean(lap.get('distance_m')), clean(lap.get('duration_s')), clean(lap.get('avg_hr'))))

    def activity_laps(self, uid, aid):
        """本人活动的圈记录（lap_index 保序）；不存在/非本人/已软删 → None。"""
        with self.conn() as conn:
            if conn.execute('SELECT 1 FROM activities WHERE user_id=? AND id=? AND deleted_at IS NULL', (uid, aid)).fetchone() is None:
                return None
            return [dict(r) for r in conn.execute(
                'SELECT * FROM laps WHERE user_id=? AND activity_id=? ORDER BY lap_index', (uid, aid))]

    def add_manual(self, uid, payload):
        payload = dict(payload)
        if not str(payload.get('start_time') or '').strip():
            raise ValueError('开始时间必填')
        _check_notes(payload.get('notes'))
        payload['source'] = 'manual'
        payload['sport'] = 'running'
        return self.add(uid, payload)

    def update_manual(self, uid, aid, payload):
        payload = dict(payload)
        fields = ('start_time', 'distance_m', 'duration_s', 'avg_hr', 'notes')
        if not any(key in payload for key in fields):
            raise ValueError('没有可更正的字段')
        if 'start_time' in payload and not str(payload.get('start_time') or '').strip():
            raise ValueError('开始时间不能为空')
        if 'notes' in payload:
            _check_notes(payload.get('notes'))
        with self.conn() as conn:
            row = conn.execute('SELECT * FROM activities WHERE user_id=? AND id=?', (uid, aid)).fetchone()
            if row is None:
                return None
            if row['source'] != 'manual':
                raise DeviceSourceError('设备记录（FIT/佳明导入）只读，不能手工更正')
            if row['deleted_at'] is not None:
                return None
            current = {key: row[key] for key in ('start_time', 'distance_m', 'duration_s', 'avg_hr', 'notes')}
            if 'start_time' in payload:
                current['start_time'] = _normalize_start(payload['start_time'])
            for field in ('distance_m', 'duration_s', 'avg_hr'):
                if field in payload:
                    value = payload[field]
                    current[field] = float(value) if finite(value) and value >= 0 else None
            if 'notes' in payload:
                notes = payload['notes']
                current['notes'] = notes if isinstance(notes, str) else None
            # 更正留版本：改前快照旧值（含更正前版本号）
            prior = {key: row[key] for key in ('start_time', 'distance_m', 'duration_s', 'avg_hr', 'notes', 'revision')}
            conn.execute('INSERT INTO activity_revisions (activity_id,user_id,changed_at,prior_json) VALUES (?,?,?,?)',
                         (aid, uid, datetime.now(timezone.utc).isoformat(), json.dumps(prior, ensure_ascii=False)))
            conn.execute('UPDATE activities SET start_time=?,distance_m=?,duration_s=?,avg_hr=?,notes=?,revision=revision+1 WHERE user_id=? AND id=?',
                         (current['start_time'], current['distance_m'], current['duration_s'], current['avg_hr'], current['notes'], uid, aid))
            updated = conn.execute('SELECT * FROM activities WHERE user_id=? AND id=?', (uid, aid)).fetchone()
        return dict(updated)

    def soft_delete(self, uid, aid):
        """本人任意来源活动可软删。True=已删；False=已删过；None=不存在/非本人。"""
        with self.conn() as conn:
            row = conn.execute('SELECT deleted_at FROM activities WHERE user_id=? AND id=?', (uid, aid)).fetchone()
            if row is None:
                return None
            if row['deleted_at'] is not None:
                return False
            conn.execute('UPDATE activities SET deleted_at=? WHERE user_id=? AND id=?',
                         (datetime.now(timezone.utc).isoformat(), uid, aid))
        return True

    def revisions(self, uid, aid):
        """本人该活动的更正历史（owner 双条件）；软删后历史仍保留。"""
        with self.conn() as conn:
            if conn.execute('SELECT 1 FROM activities WHERE user_id=? AND id=?', (uid, aid)).fetchone() is None:
                return None
            rows = [dict(r) for r in conn.execute(
                'SELECT id,activity_id,changed_at,prior_json FROM activity_revisions WHERE user_id=? AND activity_id=? ORDER BY id',
                (uid, aid))]
        return rows

    def activities(self, uid, limit=500, offset=0):
        with self.conn() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM activities WHERE user_id=? AND deleted_at IS NULL ORDER BY start_time DESC,id DESC LIMIT ? OFFSET ?', (uid,limit,offset))]

    def activity(self, uid, aid):
        with self.conn() as conn:
            row = conn.execute('SELECT * FROM activities WHERE user_id=? AND id=? AND deleted_at IS NULL', (uid,aid)).fetchone()
        return dict(row) if row else None

    # ---- 轨迹 activity_tracks（P2 第五切片）：additive、仅本人、软删不返回 ----

    def save_track(self, uid, aid, points):
        """持久化抽稀轨迹点（2..MAX_TRACK_POINTS）。仅本人未软删活动；否则 None。"""
        from .track_map import MAX_TRACK_POINTS
        if not isinstance(points, list) or not 2 <= len(points) <= MAX_TRACK_POINTS:
            raise ValueError('轨迹点数无效')
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(points, ensure_ascii=False, separators=(',', ':'))
        with self.conn() as conn:
            if conn.execute('SELECT 1 FROM activities WHERE user_id=? AND id=? AND deleted_at IS NULL',
                            (uid, aid)).fetchone() is None:
                return None
            conn.execute('INSERT OR REPLACE INTO activity_tracks (user_id,activity_id,points_json,point_count,created_at) '
                         'VALUES (?,?,?,?,?)', (uid, aid, payload, len(points), now))
        return {'activity_id': aid, 'point_count': len(points), 'created_at': now}

    def activity_track(self, uid, aid):
        """本人未软删活动的轨迹；无轨迹/非本人/软删 → None。"""
        with self.conn() as conn:
            row = conn.execute(
                'SELECT t.points_json,t.point_count,t.created_at FROM activity_tracks t '
                'JOIN activities a ON a.user_id=t.user_id AND a.id=t.activity_id '
                'WHERE t.user_id=? AND t.activity_id=? AND a.deleted_at IS NULL', (uid, aid)).fetchone()
        if row is None:
            return None
        return {'points': json.loads(row['points_json']),
                'point_count': row['point_count'], 'created_at': row['created_at']}

    # ---- 跑鞋 gear（P2 第二切片）：复合键同主、关联唯一、里程按未删关联求和 ----

    def add_gear(self, uid, payload):
        fields = _gear_fields(payload, creating=True)
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as conn:
            cur = conn.execute('INSERT INTO gear (user_id,name,brand_model,started_on,notes,max_distance_km,created_at) VALUES (?,?,?,?,?,?,?)',
                               (uid, fields['name'], fields['brand_model'], fields['started_on'], fields['notes'], fields['max_distance_km'], now))
            row = conn.execute('SELECT * FROM gear WHERE user_id=? AND id=?', (uid, cur.lastrowid)).fetchone()
            gear = self._gear_with_stats(conn, uid, dict(row))
        return gear

    def update_gear(self, uid, gid, payload):
        if not any(key in payload for key in ('name', 'brand_model', 'started_on', 'notes', 'max_distance_km')):
            raise ValueError('没有可更新的字段')
        with self.conn() as conn:
            row = conn.execute('SELECT * FROM gear WHERE user_id=? AND id=?', (uid, gid)).fetchone()
            if row is None or row['deleted_at'] is not None:
                return None
            merged = {key: row[key] for key in ('name', 'brand_model', 'started_on', 'notes', 'max_distance_km')}
            merged.update({key: payload[key] for key in merged if key in payload})
            fields = _gear_fields(merged, creating=False)
            conn.execute('UPDATE gear SET name=?,brand_model=?,started_on=?,notes=?,max_distance_km=? WHERE user_id=? AND id=?',
                         (fields['name'], fields['brand_model'], fields['started_on'], fields['notes'], fields['max_distance_km'], uid, gid))
            updated = conn.execute('SELECT * FROM gear WHERE user_id=? AND id=?', (uid, gid)).fetchone()
            gear = self._gear_with_stats(conn, uid, dict(updated))
        return gear

    def retire_gear(self, uid, gid, retired_on=None):
        """退役=软删可审计：行保留、retired_on/deleted_at 落痕；历史关联与里程仍可查。"""
        if retired_on is not None:
            retired_on = _check_date(retired_on, '退役日期')
        with self.conn() as conn:
            row = conn.execute('SELECT deleted_at FROM gear WHERE user_id=? AND id=?', (uid, gid)).fetchone()
            if row is None:
                return None
            if row['deleted_at'] is not None:
                return False
            now = datetime.now(timezone.utc)
            conn.execute('UPDATE gear SET deleted_at=?,retired_on=? WHERE user_id=? AND id=?',
                         (now.isoformat(), retired_on or now.date().isoformat(), uid, gid))
            conn.execute('DELETE FROM gear_default WHERE user_id=? AND gear_id=?', (uid, gid))
        return True

    def gear_rows(self, uid, include_retired=False):
        with self.conn() as conn:
            sql = 'SELECT * FROM gear WHERE user_id=?' + ('' if include_retired else ' AND deleted_at IS NULL') + ' ORDER BY id'
            return [self._gear_with_stats(conn, uid, dict(r)) for r in conn.execute(sql, (uid,))]

    def gear(self, uid, gid):
        """本人详情（含已退役，供审计）；非本人/不存在 → None。"""
        with self.conn() as conn:
            row = conn.execute('SELECT * FROM gear WHERE user_id=? AND id=?', (uid, gid)).fetchone()
            if row is None:
                return None
            gear = self._gear_with_stats(conn, uid, dict(row))
            gear['activities'] = [dict(r) for r in conn.execute("""
                SELECT a.id,a.start_time,a.distance_m,a.duration_s,a.source,a.deleted_at,ga.linked_at
                FROM gear_activity ga JOIN activities a ON a.user_id=ga.user_id AND a.id=ga.activity_id
                WHERE ga.user_id=? AND ga.gear_id=? AND a.deleted_at IS NULL ORDER BY a.start_time DESC,a.id DESC""", (uid, gid))]
        return gear

    def _gear_with_stats(self, conn, uid, gear):
        # 总里程＝未删除关联活动的距离求和（读时重算，不用永增计数）；缺距离不计入且标注。
        stats = conn.execute("""SELECT COUNT(*) AS n, SUM(a.distance_m) AS total_m, SUM(a.distance_m IS NULL) AS missing
            FROM gear_activity ga JOIN activities a ON a.user_id=ga.user_id AND a.id=ga.activity_id
            WHERE ga.user_id=? AND ga.gear_id=? AND a.deleted_at IS NULL""", (uid, gear['id'])).fetchone()
        total_km = rounded((stats['total_m'] or 0) / 1000, 2)
        threshold = gear['max_distance_km']
        gear['activity_count'] = stats['n']
        gear['missing_distance_count'] = stats['missing'] or 0
        gear['total_distance_km'] = total_km
        gear['threshold_reached'] = bool(threshold is not None and total_km >= threshold)
        gear['evidence'] = dict(label='已导入活动内合计', unit='km', status='未核验',
                                scope='本人全部未删除关联活动（手工与设备导入），缺距离活动不计入',
                                window='全部历史（无时间窗截断）')
        return gear

    def link_gear(self, uid, aid, gid):
        """每活动至多一双鞋：upsert 覆盖旧关联；活动/鞋任一非本人或已删 → None（404，不泄露存在性）。"""
        with self.conn() as conn:
            if conn.execute('SELECT 1 FROM activities WHERE user_id=? AND id=? AND deleted_at IS NULL', (uid, aid)).fetchone() is None:
                return None
            if conn.execute('SELECT 1 FROM gear WHERE user_id=? AND id=? AND deleted_at IS NULL', (uid, gid)).fetchone() is None:
                return None
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("""INSERT INTO gear_activity (user_id,activity_id,gear_id,linked_at) VALUES (?,?,?,?)
                ON CONFLICT(user_id,activity_id) DO UPDATE SET gear_id=excluded.gear_id,linked_at=excluded.linked_at""",
                         (uid, aid, gid, now))
            row = conn.execute('SELECT * FROM gear_activity WHERE user_id=? AND activity_id=?', (uid, aid)).fetchone()
        return dict(row)

    def unlink_gear(self, uid, aid):
        with self.conn() as conn:
            if conn.execute('SELECT 1 FROM activities WHERE user_id=? AND id=?', (uid, aid)).fetchone() is None:
                return None
            cur = conn.execute('DELETE FROM gear_activity WHERE user_id=? AND activity_id=?', (uid, aid))
        return cur.rowcount > 0

    def activity_gear(self, uid, aid):
        with self.conn() as conn:
            if conn.execute('SELECT 1 FROM activities WHERE user_id=? AND id=? AND deleted_at IS NULL', (uid, aid)).fetchone() is None:
                return None
            row = conn.execute("""SELECT ga.*,g.name,g.brand_model,g.deleted_at AS gear_deleted
                FROM gear_activity ga JOIN gear g ON g.user_id=ga.user_id AND g.id=ga.gear_id
                WHERE ga.user_id=? AND ga.activity_id=?""", (uid, aid)).fetchone()
        return dict(row) if row else {}

    def set_default_gear(self, uid, gid):
        with self.conn() as conn:
            if conn.execute('SELECT 1 FROM gear WHERE user_id=? AND id=? AND deleted_at IS NULL', (uid, gid)).fetchone() is None:
                return None
            conn.execute('INSERT INTO gear_default (user_id,gear_id) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET gear_id=excluded.gear_id', (uid, gid))
        return gid

    def default_gear(self, uid):
        with self.conn() as conn:
            row = conn.execute('SELECT gear_id FROM gear_default WHERE user_id=?', (uid,)).fetchone()
        return row['gear_id'] if row else None

    def clear_default_gear(self, uid):
        with self.conn() as conn:
            conn.execute('DELETE FROM gear_default WHERE user_id=?', (uid,))
        return True

    # ---- 训练日志 journal：按日期一条、revision 留版本、私密备注仅本人详情可见 ----

    def add_journal(self, uid, payload):
        entry_date = _check_date(payload.get('entry_date'), '日期')
        body = _check_journal_text(payload.get('body'), '训练记录', MAX_JOURNAL_BODY, required=True)
        private_notes = _check_journal_text(payload.get('private_notes'), '私密备注', MAX_NOTES, required=False)
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.conn() as conn:
                cur = conn.execute('INSERT INTO journal (user_id,entry_date,body,private_notes,created_at) VALUES (?,?,?,?,?)',
                                   (uid, entry_date, body, private_notes, now))
                row = conn.execute('SELECT * FROM journal WHERE user_id=? AND id=?', (uid, cur.lastrowid)).fetchone()
        except sqlite3.IntegrityError:
            raise JournalDateConflict('该日期已有日志；请先编辑或删除当日记录')
        return dict(row)

    def journal_list(self, uid, date_from=None, date_to=None, limit=200):
        """列表/聚合投影：SQL 层就不取 private_notes——分享/导出路径结构上拿不到私密备注。"""
        if date_from is not None:
            date_from = _check_date(date_from, '起始日期')
        if date_to is not None:
            date_to = _check_date(date_to, '结束日期')
        sql = """SELECT id,entry_date,body,created_at,updated_at,revision,
                 (private_notes IS NOT NULL AND private_notes<>'') AS has_private_notes
                 FROM journal WHERE user_id=? AND deleted_at IS NULL"""
        params = [uid]
        if date_from is not None:
            sql += ' AND entry_date>=?'
            params.append(date_from)
        if date_to is not None:
            sql += ' AND entry_date<=?'
            params.append(date_to)
        sql += ' ORDER BY entry_date DESC,id DESC LIMIT ?'
        params.append(limit)
        with self.conn() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def journal(self, uid, jid):
        """本人详情：唯一返回 private_notes 的读路径。"""
        with self.conn() as conn:
            row = conn.execute('SELECT * FROM journal WHERE user_id=? AND id=? AND deleted_at IS NULL', (uid, jid)).fetchone()
        return dict(row) if row else None

    def update_journal(self, uid, jid, payload):
        if not any(key in payload for key in ('entry_date', 'body', 'private_notes')):
            raise ValueError('没有可更正的字段')
        with self.conn() as conn:
            row = conn.execute('SELECT * FROM journal WHERE user_id=? AND id=? AND deleted_at IS NULL', (uid, jid)).fetchone()
            if row is None:
                return None
            current = {key: row[key] for key in ('entry_date', 'body', 'private_notes')}
            if 'entry_date' in payload:
                current['entry_date'] = _check_date(payload['entry_date'], '日期')
            if 'body' in payload:
                current['body'] = _check_journal_text(payload['body'], '训练记录', MAX_JOURNAL_BODY, required=True)
            if 'private_notes' in payload:
                current['private_notes'] = _check_journal_text(payload['private_notes'], '私密备注', MAX_NOTES, required=False)
            prior = {key: row[key] for key in ('entry_date', 'body', 'private_notes', 'revision')}
            now = datetime.now(timezone.utc).isoformat()
            try:
                conn.execute('INSERT INTO journal_revisions (journal_id,user_id,changed_at,prior_json) VALUES (?,?,?,?)',
                             (jid, uid, now, json.dumps(prior, ensure_ascii=False)))
                conn.execute('UPDATE journal SET entry_date=?,body=?,private_notes=?,updated_at=?,revision=revision+1 WHERE user_id=? AND id=?',
                             (current['entry_date'], current['body'], current['private_notes'], now, uid, jid))
            except sqlite3.IntegrityError:
                raise JournalDateConflict('目标日期已有日志；请先编辑或删除当日记录')
            updated = conn.execute('SELECT * FROM journal WHERE user_id=? AND id=?', (uid, jid)).fetchone()
        return dict(updated)

    def delete_journal(self, uid, jid):
        with self.conn() as conn:
            row = conn.execute('SELECT deleted_at FROM journal WHERE user_id=? AND id=?', (uid, jid)).fetchone()
            if row is None:
                return None
            if row['deleted_at'] is not None:
                return False
            conn.execute('UPDATE journal SET deleted_at=? WHERE user_id=? AND id=?',
                         (datetime.now(timezone.utc).isoformat(), uid, jid))
        return True

    def journal_revisions(self, uid, jid):
        with self.conn() as conn:
            if conn.execute('SELECT 1 FROM journal WHERE user_id=? AND id=?', (uid, jid)).fetchone() is None:
                return None
            return [dict(r) for r in conn.execute(
                'SELECT id,journal_id,changed_at,prior_json FROM journal_revisions WHERE user_id=? AND journal_id=? ORDER BY id', (uid, jid))]


def public_journal(row):
    """分享/导出专用投影：剥离私密备注。任何非本人详情路径必须经此函数。"""
    return {key: value for key, value in dict(row).items() if key != 'private_notes'}


def _check_date(value, label):
    from datetime import date
    try:
        return date.fromisoformat(str(value or '')).isoformat()
    except ValueError:
        raise ValueError('%s须为 YYYY-MM-DD 格式' % label)


def _check_journal_text(value, label, limit, required):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError('%s必填' % label)
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('%s不能超过 %d 字' % (label, limit))
    return value


def _gear_fields(payload, creating):
    name = payload.get('name')
    if creating or 'name' in payload:
        if not isinstance(name, str) or not name.strip():
            raise ValueError('跑鞋名称必填')
        if len(name) > MAX_GEAR_NAME:
            raise ValueError('跑鞋名称不能超过 %d 字' % MAX_GEAR_NAME)
    fields = {'name': name.strip() if isinstance(name, str) else name}
    brand = payload.get('brand_model')
    if brand is not None and (not isinstance(brand, str) or len(brand) > MAX_GEAR_NAME):
        raise ValueError('品牌型号不能超过 %d 字' % MAX_GEAR_NAME)
    fields['brand_model'] = brand if isinstance(brand, str) and brand.strip() else None
    fields['started_on'] = _check_date(payload.get('started_on'), '启用日期') if payload.get('started_on') else None
    _check_notes(payload.get('notes'))
    fields['notes'] = payload.get('notes') if isinstance(payload.get('notes'), str) else None
    threshold = payload.get('max_distance_km')
    fields['max_distance_km'] = float(threshold) if finite(threshold) and threshold > 0 else None
    return fields
