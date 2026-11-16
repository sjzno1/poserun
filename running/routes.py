"""Additive routes; no dependency on the video inference stack."""
from flask import Blueprint, g, jsonify, request
from .fit_import import parse_fit, MAX_FIT_BYTES


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
        return jsonify(activities=store.activities(g.current_user['id'],limit,offset),limit=limit,offset=offset)

    @bp.get('/activities/<int:aid>')
    def detail(aid):
        activity = store.activity(g.current_user['id'], aid)
        if activity is None:
            return jsonify(error='活动不存在'), 404
        return jsonify(activity=activity, evidence_status='未核验：导入摘要，不是校准实测', score=None)

    @bp.post('/activities/import')
    def import_fit():
        upload = request.files.get('file')
        if not upload or not upload.filename.lower().endswith('.fit'):
            return jsonify(error='请选择 .fit 文件'), 400
        try:
            activity = parse_fit(upload.read(MAX_FIT_BYTES+1))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        row, created = store.add(g.current_user['id'], activity)
        return jsonify(activity=row, duplicate=not created), 201 if created else 200

    @bp.get('/dashboard')
    def overview():
        from .running_analysis import dashboard
        return jsonify(dashboard(store, g.current_user['id']))

    return bp
