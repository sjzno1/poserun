"""Running management extension. AGPL-3.0; no pose/model imports."""
from pathlib import Path
from .running_store import RunningStore
from .routes import make_blueprint


def register_running(app, data_dir='data', limiter=None):
    store = RunningStore(Path(data_dir) / 'running.db')
    app.extensions['running_store'] = store
    bp = make_blueprint(store)
    if limiter is not None:
        limiter.limit('60 per minute')(bp)
    app.register_blueprint(bp)
    from .garmin_routes import make_garmin_blueprint
    garmin_bp = make_garmin_blueprint(app, store, data_dir)
    if limiter is not None:
        limiter.limit('10 per minute')(garmin_bp)
    app.register_blueprint(garmin_bp)
    auth = app.extensions.get('poserun_auth')
    if auth is not None:
        original_delete = auth.delete_user

        def delete_with_running(uid):
            error = original_delete(uid)
            if error is None:
                with store.conn() as conn:
                    for table in ('activities', 'garmin_connection', 'sync_state'):
                        conn.execute('DELETE FROM '+table+' WHERE user_id=?', (uid,))
            return error
        auth.delete_user = delete_with_running
    return store

