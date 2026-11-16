"""Explicit live opt-in, isolated demo data, encrypted connection and manual sync."""
import os
from datetime import date, datetime, timedelta, timezone
from flask import Blueprint, g, jsonify, request
from .garmin_client import GarminClient, DemoGarminClient, GarminError, validate_key
from .key_vault import KeyVault

ENDPOINT_NOTICE = '端点待真实 key 实测；Personal API 仅个人非商业只读使用。'


def make_garmin_blueprint(app, store, data_dir):
    bp=Blueprint('running_garmin',__name__,url_prefix='/api/running/garmin')
    vault=KeyVault(data_dir, use_keyring=app.config.get('GARMIN_USE_KEYRING',True))

    def connection(uid):
        with store.conn() as conn:
            row=conn.execute('SELECT * FROM garmin_connection WHERE user_id=?',(uid,)).fetchone()
        return dict(row) if row else None

    def client(row, key=None):
        if row['mode']=='demo': return DemoGarminClient()
        if not app.config.get('GARMIN_LIVE_ENABLED',False):
            raise ValueError('真实连接未启用；端点待真实 key 实测')
        factory=app.config.get('GARMIN_CLIENT_FACTORY',GarminClient)
        return factory(key if key is not None else vault.decrypt(row['key_encrypted']))

    @bp.before_request
    def require_login():
        if not getattr(g,'current_user',None):
            return jsonify(error='请先登录'),401

    @bp.after_request
    def private(response):
        response.headers['Cache-Control']='no-store'
        return response

    @bp.get('')
    def status():
        row=connection(g.current_user['id'])
        if not row: return jsonify(status='disconnected',notice=ENDPOINT_NOTICE)
        days=(date.fromisoformat(row['key_expiry'])-date.today()).days
        with store.conn() as conn:
            sync=conn.execute('SELECT last_run_at,status,error FROM sync_state WHERE user_id=?',(g.current_user['id'],)).fetchone()
        return jsonify(status='expired' if days<=0 else row['status'],mode=row['mode'],
                       expiry=row['key_expiry'],days_remaining=max(days,0),renewal_due=days<=30,
                       sync=dict(sync) if sync else None,notice=ENDPOINT_NOTICE)

    @bp.post('/connect')
    def connect():
        data=request.get_json(silent=True)
        if not isinstance(data,dict): return jsonify(error='需要 JSON 对象'),400
        try:
            expiry=date.fromisoformat(data.get('expiry',''))
            if not date.today()<expiry<=date.today()+timedelta(days=365):
                raise ValueError()
        except (TypeError,ValueError):
            return jsonify(error='有效期须为未来日期且不超过 365 天（由用户声明，未核验）'),400
        mode=data.get('mode','live')
        if mode not in ('demo','live'): return jsonify(error='未知模式'),400
        if mode=='demo' and data.get('key'):
            return jsonify(error='演示模式不接受真实 key'),400
        if mode=='live':
            try: key=validate_key(data.get('key'))
            except ValueError as exc: return jsonify(error=str(exc)),400
            if not app.config.get('GARMIN_LIVE_ENABLED',False):
                return jsonify(error='真实连接默认关闭；端点待真实 key 实测'),409
        else: key=None
        row=dict(mode=mode,key_expiry=expiry.isoformat())
        try:
            client(row,key).test_key()
            encrypted=vault.encrypt(key) if mode=='live' else ''
        except GarminError:
            return jsonify(error='佳明测试失败；连接未保存，凭证和响应已隐藏'),502
        except (ValueError,OSError):
            return jsonify(error='钥匙库不可用；连接未保存'),503
        with store.conn() as conn:
            conn.execute('INSERT INTO garmin_connection VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET key_encrypted=excluded.key_encrypted,key_expiry=excluded.key_expiry,mode=excluded.mode,status=excluded.status',
                         (g.current_user['id'],encrypted,expiry.isoformat(),mode,'demo' if mode=='demo' else 'active'))
            conn.execute('DELETE FROM sync_state WHERE user_id=?',(g.current_user['id'],))
        return jsonify(success=True,mode=mode,vault_backend=vault.backend,notice=ENDPOINT_NOTICE)

    @bp.delete('')
    def disconnect():
        with store.conn() as conn:
            conn.execute('DELETE FROM garmin_connection WHERE user_id=?',(g.current_user['id'],))
            conn.execute('DELETE FROM sync_state WHERE user_id=?',(g.current_user['id'],))
        return jsonify(success=True,notice='连接与同步状态已删除；已导入活动保留。')

    @bp.post('/sync')
    def sync():
        uid=g.current_user['id']
        row=connection(uid)
        if not row: return jsonify(error='尚未连接'),409
        if date.fromisoformat(row['key_expiry'])<=date.today():
            return jsonify(error='连接已过期，请续期'),409
        try:
            api=client(row)
            added=0
            # Bounded manual refresh of latest 100; NOT historical backfill/automatic scheduling.
            rows=api.list_activities(start=0,limit=100)
            normalized=[]
            for item in rows:
                start=datetime.fromisoformat(item['startTimeGMT'].replace('Z','+00:00'))
                if start.tzinfo is None: start=start.replace(tzinfo=timezone.utc)
                aid=item['activityId']
                if not isinstance(aid,(str,int)) or isinstance(aid,bool): raise ValueError()
                normalized.append(dict(start_time=start.isoformat(),sport=item.get('activityType',{}).get('typeKey','unknown'),
                    distance_m=item.get('distance'),duration_s=item.get('duration'),avg_hr=item.get('averageHR'),
                    source='demo' if row['mode']=='demo' else 'api',garmin_activity_id=str(aid)))
            for activity in normalized:
                _,created=store.add(uid,activity);added+=int(created)
        except (GarminError,ValueError,KeyError,TypeError,AttributeError):
            with store.conn() as conn:
                conn.execute('INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?)',(uid,datetime.now(timezone.utc).isoformat(),'error','同步失败；上游结构或连接待核验，未输出敏感错误'))
            return jsonify(error='同步失败；上游结构或连接待核验，未输出敏感错误'),502
        with store.conn() as conn:
            conn.execute('INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?)',(uid,datetime.now(timezone.utc).isoformat(),'ok',None))
        return jsonify(added=added,received=len(rows),limit=100,notice='仅刷新最近 100 条；历史请导入 FIT。'+ENDPOINT_NOTICE)

    return bp
