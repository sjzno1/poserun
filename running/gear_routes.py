"""跑鞋＋训练日志路由（P2 第二切片）。GEAR_ENABLED 关闭时全部 404 回退。AGPL-3.0"""
from flask import Blueprint, current_app, g, jsonify, request
from .running_store import JournalDateConflict

GEAR_EVIDENCE = '已导入活动内合计 · 未核验'
JOURNAL_EVIDENCE = '跑者自填 · 未核验'


def make_gear_blueprint(store):
    bp = Blueprint('gear_journal', __name__, url_prefix='/api/running')

    @bp.before_request
    def gate():
        if not getattr(g, 'current_user', None):
            return jsonify(error='请先登录'), 401
        if not current_app.config['GEAR_ENABLED']:
            return jsonify(error='跑鞋与训练日志未开启'), 404

    def uid():
        return g.current_user['id']

    def json_body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else None

    # ---- 跑鞋 ----

    @bp.get('/gear')
    def gear_list():
        include_retired = request.args.get('include_retired') == '1'
        return jsonify(gear=store.gear_rows(uid(), include_retired=include_retired),
                       default_gear_id=store.default_gear(uid()),
                       evidence_status=GEAR_EVIDENCE, score=None)

    @bp.post('/gear')
    def gear_create():
        data = json_body()
        if data is None:
            return jsonify(error='请求体无效'), 400
        try:
            row = store.add_gear(uid(), data)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(gear=row, evidence_status=GEAR_EVIDENCE, score=None), 201

    @bp.get('/gear/default')
    def gear_default_get():
        gid = store.default_gear(uid())
        return jsonify(gear_id=gid, gear=store.gear(uid(), gid) if gid is not None else None)

    @bp.put('/gear/default')
    def gear_default_set():
        data = json_body()
        if data is None or 'gear_id' not in data:
            return jsonify(error='请求体无效'), 400
        try:
            gid = int(data['gear_id'])
        except (TypeError, ValueError):
            return jsonify(error='跑鞋 ID 无效'), 400
        if store.set_default_gear(uid(), gid) is None:
            return jsonify(error='跑鞋不存在'), 404
        return jsonify(gear_id=gid)

    @bp.delete('/gear/default')
    def gear_default_clear():
        store.clear_default_gear(uid())
        return jsonify(gear_id=None)

    @bp.get('/gear/<int:gid>')
    def gear_detail(gid):
        gear = store.gear(uid(), gid)
        if gear is None:
            return jsonify(error='跑鞋不存在'), 404
        return jsonify(gear=gear, evidence_status=GEAR_EVIDENCE, score=None)

    @bp.patch('/gear/<int:gid>')
    def gear_update(gid):
        data = json_body()
        if data is None:
            return jsonify(error='请求体无效'), 400
        try:
            row = store.update_gear(uid(), gid, data)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if row is None:
            return jsonify(error='跑鞋不存在或已退役'), 404
        return jsonify(gear=row)

    @bp.post('/gear/<int:gid>/retire')
    def gear_retire(gid):
        data = json_body() or {}
        try:
            result = store.retire_gear(uid(), gid, data.get('retired_on'))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if result is None:
            return jsonify(error='跑鞋不存在'), 404
        if result is False:
            return jsonify(error='该跑鞋已退役'), 400
        return jsonify(retired=True)

    # ---- 活动↔跑鞋关联（每活动至多一双鞋） ----

    @bp.get('/activities/<int:aid>/gear')
    def activity_gear_get(aid):
        link = store.activity_gear(uid(), aid)
        if link is None:
            return jsonify(error='活动不存在'), 404
        return jsonify(link=link or None)

    @bp.put('/activities/<int:aid>/gear')
    def activity_gear_put(aid):
        data = json_body()
        if data is None or 'gear_id' not in data:
            return jsonify(error='请求体无效'), 400
        try:
            gid = int(data['gear_id'])
        except (TypeError, ValueError):
            return jsonify(error='跑鞋 ID 无效'), 400
        link = store.link_gear(uid(), aid, gid)
        if link is None:
            return jsonify(error='活动或跑鞋不存在'), 404
        return jsonify(link=link)

    @bp.delete('/activities/<int:aid>/gear')
    def activity_gear_delete(aid):
        result = store.unlink_gear(uid(), aid)
        if result is None:
            return jsonify(error='活动不存在'), 404
        if result is False:
            return jsonify(error='该活动未关联跑鞋'), 404
        return jsonify(unlinked=True)

    # ---- 训练日志 ----

    @bp.get('/journal')
    def journal_list():
        try:
            limit = min(max(int(request.args.get('limit', 200)), 1), 500)
            rows = store.journal_list(uid(), request.args.get('from'), request.args.get('to'), limit)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        # 列表投影不含 private_notes（SQL 层隔离）；私密备注仅本人详情可见。
        return jsonify(journal=rows, evidence_status=JOURNAL_EVIDENCE, score=None)

    @bp.post('/journal')
    def journal_create():
        data = json_body()
        if data is None:
            return jsonify(error='请求体无效'), 400
        try:
            row = store.add_journal(uid(), data)
        except JournalDateConflict as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(journal=row, evidence_status=JOURNAL_EVIDENCE, score=None), 201

    @bp.get('/journal/<int:jid>')
    def journal_detail(jid):
        row = store.journal(uid(), jid)
        if row is None:
            return jsonify(error='日志不存在'), 404
        return jsonify(journal=row, evidence_status=JOURNAL_EVIDENCE, score=None)

    @bp.patch('/journal/<int:jid>')
    def journal_update(jid):
        data = json_body()
        if data is None:
            return jsonify(error='请求体无效'), 400
        try:
            row = store.update_journal(uid(), jid, data)
        except JournalDateConflict as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if row is None:
            return jsonify(error='日志不存在'), 404
        return jsonify(journal=row, revision=row['revision'])

    @bp.delete('/journal/<int:jid>')
    def journal_delete(jid):
        result = store.delete_journal(uid(), jid)
        if result is None:
            return jsonify(error='日志不存在'), 404
        if result is False:
            return jsonify(error='该日志已删除'), 400
        return jsonify(deleted=True)

    @bp.get('/journal/<int:jid>/revisions')
    def journal_revisions(jid):
        rows = store.journal_revisions(uid(), jid)
        if rows is None:
            return jsonify(error='日志不存在'), 404
        return jsonify(revisions=rows)

    return bp