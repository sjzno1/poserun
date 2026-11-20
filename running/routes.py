"""Additive routes; no dependency on the video inference stack. AGPL-3.0"""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .weekly_report import weekly_report, DEFAULT_ZONE
from .training_stats import training_stats
from .running_store import DeviceSourceError
from flask import Blueprint, current_app, g, jsonify, request
from .fit_import import MAX_FIT_BYTES
from .track_import import parse_upload
from .track_map import extract_track_points, simplify_track
from .export_data import export_activities, build_csv, build_json, dumps_json
from measurement_safety import finite

MANUAL_EVIDENCE = '手工记录：跑者自填，未核验'
IMPORT_EVIDENCE = '未核验：导入摘要，不是校准实测'


def _manual_payload(data):
    """API 单位（km/分钟）转存储单位（m/s）；证据门在 store 内兜底。"""
    payload = {}
    for key, factor, field in (('distance_km', 1000.0, 'distance_m'), ('duration_min', 60.0, 'duration_s')):
        if key in data:
            value = data[key]
            payload[field] = value * factor if finite(value) else None
    for key in ('start_time', 'avg_hr', 'notes'):
        if key in data:
            payload[key] = data[key]
    return payload


def make_blueprint(store):
    bp = Blueprint('running', __name__, url_prefix='/api/running')

    @bp.before_request
    def login_required():
        if not getattr(g, 'current_user', None):
            return jsonify(error='请先登录'), 401

    @bp.get('/activities')
    def activities():
        try:
            limit = min(max(int(request.args.get('limit',100)),1),500)
            offset = max(int(request.args.get('offset',0)),0)
        except ValueError:
            return jsonify(error='分页参数无效'), 400
        return jsonify(activities=store.activities(g.current_user['id'],limit,offset),limit=limit,offset=offset,
                       track_map_enabled=current_app.config['TRACK_MAP_ENABLED'])

    @bp.get('/activities/<int:aid>')
    def detail(aid):
        activity = store.activity(g.current_user['id'], aid)
        if activity is None:
            return jsonify(error='活动不存在'), 404
        evidence = MANUAL_EVIDENCE if activity['source'] == 'manual' else IMPORT_EVIDENCE
        laps = store.activity_laps(g.current_user['id'], aid) or []
        return jsonify(activity=activity, laps=laps, evidence_status=evidence, score=None)

    @bp.post('/activities/import')
    def import_activity():
        """同一导入端点接受 .fit / .gpx / .tcx（扩展名优先、内容兜底分发）。
        无时间戳的文件（如无 time 的 GPX）须在表单补 start_time（含时区），不臆造。"""
        upload = request.files.get('file')
        if not upload or not upload.filename:
            return jsonify(error='请选择 .fit / .gpx / .tcx 文件'), 400
        raw = upload.read(MAX_FIT_BYTES + 1)
        try:
            activity, laps = parse_upload(upload.filename, raw)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if activity.get('start_time') is None:
            if not request.form.get('start_time'):
                return jsonify(error='文件无时间戳：请在表单提供 start_time（含时区）'), 400
            activity['start_time'] = request.form['start_time']
        try:
            row, created = store.add(g.current_user['id'], activity, laps)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if created and current_app.config['TRACK_MAP_ENABLED']:
            # additive best-effort：仅新导入存抽稀轨迹；原始文件不落盘；失败不影响摘要。
            try:
                points = simplify_track(extract_track_points(upload.filename, raw))
                if len(points) >= 2:
                    store.save_track(g.current_user['id'], row['id'], points)
            except ValueError:
                pass
        laps_out = store.activity_laps(g.current_user['id'], row['id']) or []
        return jsonify(activity=row, laps=laps_out, duplicate=not created), 201 if created else 200

    @bp.get('/activities/<int:aid>/track')
    def activity_track(aid):
        """离线轨迹地图数据：仅本人未软删活动；开关关 → 404；无轨迹 → has_track=False。"""
        if not current_app.config['TRACK_MAP_ENABLED']:
            return jsonify(error='轨迹地图未开启'), 404
        activity = store.activity(g.current_user['id'], aid)
        if activity is None:
            return jsonify(error='活动不存在'), 404
        track = store.activity_track(g.current_user['id'], aid)
        return jsonify(activity=activity, track=track, has_track=track is not None)

    @bp.post('/activities')
    def create_manual():
        if not current_app.config['MANUAL_ENTRY_ENABLED']:
            return jsonify(error='手工补录未开启'), 404
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='请求体无效'), 400
        try:
            row, created = store.add_manual(g.current_user['id'], _manual_payload(data))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(activity=row, evidence_status=MANUAL_EVIDENCE, score=None, duplicate=not created), 201 if created else 200

    @bp.patch('/activities/<int:aid>')
    def correct_manual(aid):
        if not current_app.config['MANUAL_ENTRY_ENABLED']:
            return jsonify(error='手工补录未开启'), 404
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='请求体无效'), 400
        try:
            row = store.update_manual(g.current_user['id'], aid, _manual_payload(data))
        except DeviceSourceError as exc:
            return jsonify(error=str(exc)), 403
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if row is None:
            return jsonify(error='活动不存在或不可更正'), 404
        return jsonify(activity=row, revision=row['revision'])

    @bp.delete('/activities/<int:aid>')
    def delete_manual(aid):
        if not current_app.config['MANUAL_ENTRY_ENABLED']:
            return jsonify(error='手工补录未开启'), 404
        result = store.soft_delete(g.current_user['id'], aid)
        if result is None:
            return jsonify(error='活动不存在'), 404
        if result is False:
            return jsonify(error='该记录已删除'), 400
        return jsonify(deleted=True)

    @bp.get('/activities/<int:aid>/revisions')
    def revisions(aid):
        if not current_app.config['MANUAL_ENTRY_ENABLED']:
            return jsonify(error='手工补录未开启'), 404
        rows = store.revisions(g.current_user['id'], aid)
        if rows is None:
            return jsonify(error='活动不存在'), 404
        return jsonify(revisions=rows)

    @bp.get('/dashboard')
    def overview():
        from .running_analysis import dashboard
        return summary(dashboard)

    def summary(build):
        zone = request.args.get('timezone', DEFAULT_ZONE)
        try:
            data = build(store, g.current_user['id'], zone=zone)
        except (ZoneInfoNotFoundError, ValueError):
            return jsonify(error='未知时区'), 400
        data['weekly_report_enabled'] = current_app.config['WEEKLY_REPORT_ENABLED']
        data['gear_enabled'] = current_app.config['GEAR_ENABLED']
        data['training_stats_enabled'] = current_app.config['TRAINING_STATS_ENABLED']
        data['track_map_enabled'] = current_app.config['TRACK_MAP_ENABLED']
        data['export_enabled'] = current_app.config['EXPORT_ENABLED']
        data['load_enabled'] = current_app.config['LOAD_ENABLED']
        return jsonify(data)

    @bp.get('/weekly-report')
    def report():
        if not current_app.config['WEEKLY_REPORT_ENABLED']:
            return jsonify(error='周报入口未开启'), 404
        zone = request.args.get('timezone', DEFAULT_ZONE)
        try:
            data = weekly_report(store, g.current_user['id'], zone=zone)
        except (ZoneInfoNotFoundError, ValueError):
            return jsonify(error='未知时区'), 400
        data['training_stats_enabled'] = current_app.config['TRAINING_STATS_ENABLED']
        return jsonify(data)

    @bp.get('/stats/overview')
    def stats_overview():
        if not current_app.config['TRAINING_STATS_ENABLED']:
            return jsonify(error='训练统计入口未开启'), 404
        from datetime import datetime, timezone as _tz
        try:
            if 'year' in request.args:
                year = int(request.args['year'])
            else:
                year = datetime.now(_tz.utc).astimezone(ZoneInfo(DEFAULT_ZONE)).year  # 缺省＝当前北京时间年
            if not 2000 <= year <= 2100:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify(error='年份参数无效'), 400
        return jsonify(training_stats(store, g.current_user['id'], year=year))

    @bp.get('/export')
    def export_data():
        """P2 切片6：本人活动数据导出（CSV/JSON）。开关关→404；仅会话 uid，owner 参数无效。"""
        if not current_app.config['EXPORT_ENABLED']:
            return jsonify(error='数据导出未开启'), 404
        fmt = request.args.get('format', '')
        if fmt not in ('csv', 'json'):
            return jsonify(error='format 须为 csv 或 json'), 400
        year = None
        if 'year' in request.args:
            try:
                year = int(request.args['year'])
                if not 2000 <= year <= 2100:
                    raise ValueError
            except (ValueError, TypeError):
                return jsonify(error='年份参数无效'), 400
        rows = export_activities(store, g.current_user['id'], year=year)
        suffix = str(year) if year is not None else 'all'
        filename = 'poserun-activities-%s.%s' % (suffix, fmt)
        if fmt == 'csv':
            resp = current_app.response_class(build_csv(rows),
                                              content_type='text/csv; charset=utf-8')
        else:
            resp = current_app.response_class(dumps_json(build_json(rows, year)),
                                              content_type='application/json; charset=utf-8')
        resp.headers['Content-Disposition'] = 'attachment; filename=' + filename
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @bp.get('/load')
    def training_load():
        """P2 切片7：本人训练负荷（TRIMP 日负荷＋EWMA CTL/ATL/TSB）。开关关→404；仅会话 uid。"""
        if not current_app.config['LOAD_ENABLED']:
            return jsonify(error='训练负荷未开启'), 404
        raw = request.args.get('days', '90')
        if not re.fullmatch(r'[0-9]+', raw):
            return jsonify(error='days 须为 7..365 的整数'), 400
        days = int(raw)
        if not 7 <= days <= 365:
            return jsonify(error='days 须为 7..365 的整数'), 400
        from .load_analysis import build_load
        return jsonify(build_load(store, g.current_user['id'], days=days))

    return bp
