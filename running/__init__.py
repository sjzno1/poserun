"""Running management extension. AGPL-3.0; no pose/model imports."""
import os
from pathlib import Path
from flask import request, abort, send_file
from .running_store import RunningStore
from .routes import make_blueprint


def register_running(app, data_dir='data', limiter=None):
    store = RunningStore(Path(data_dir) / 'running.db')
    app.extensions['running_store'] = store
    app.config.setdefault('WEEKLY_REPORT_ENABLED', os.environ.get('WEEKLY_REPORT_ENABLED') == '1')
    app.config.setdefault('MANUAL_ENTRY_ENABLED', os.environ.get('MANUAL_ENTRY_ENABLED') == '1')
    app.config.setdefault('GEAR_ENABLED', os.environ.get('GEAR_ENABLED') == '1')
    app.config.setdefault('TRAINING_STATS_ENABLED', os.environ.get('TRAINING_STATS_ENABLED') == '1')
    app.config.setdefault('TRACK_MAP_ENABLED', os.environ.get('TRACK_MAP_ENABLED') == '1')
    app.config.setdefault('EXPORT_ENABLED', os.environ.get('EXPORT_ENABLED') == '1')
    app.config.setdefault('LOAD_ENABLED', os.environ.get('LOAD_ENABLED') == '1')

    @app.get('/weekly-report')
    def weekly_page():
        if not app.config['WEEKLY_REPORT_ENABLED']:
            abort(404)
        return send_file(Path(__file__).with_name('weekly-report.html'))

    @app.get('/manual-entry')
    def manual_entry_page():
        if not app.config['MANUAL_ENTRY_ENABLED']:
            abort(404)
        return send_file(Path(__file__).with_name('manual-entry.html'))

    @app.get('/gear')
    def gear_page():
        if not app.config['GEAR_ENABLED']:
            abort(404)
        return send_file(Path(__file__).with_name('gear.html'))

    @app.get('/journal')
    def journal_page():
        if not app.config['GEAR_ENABLED']:
            abort(404)
        return send_file(Path(__file__).with_name('journal.html'))

    @app.get('/training-stats')
    def training_stats_page():
        if not app.config['TRAINING_STATS_ENABLED']:
            abort(404)
        return send_file(Path(__file__).with_name('training-stats.html'))

    @app.get('/activity-track')
    def activity_track_page():
        if not app.config['TRACK_MAP_ENABLED']:
            abort(404)
        return send_file(Path(__file__).with_name('activity-track.html'))

    @app.get('/training-load')
    def training_load_page():
        if not app.config['LOAD_ENABLED']:
            abort(404)
        return send_file(Path(__file__).with_name('load.html'))

    @app.after_request
    def private_response(response):
        if request.path.startswith('/api/') or request.path in ('/weekly-report', '/manual-entry', '/gear', '/journal', '/training-stats', '/activity-track', '/training-load'):
            response.headers['Cache-Control'] = 'private, no-store'
        return response

    bp = make_blueprint(store)
    if limiter is not None:
        limiter.limit('60 per minute')(bp)
    app.register_blueprint(bp)
    from .gear_routes import make_gear_blueprint
    gear_bp = make_gear_blueprint(store)
    if limiter is not None:
        limiter.limit('60 per minute')(gear_bp)
    app.register_blueprint(gear_bp)
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
                    # 先删引用侧（gear_activity/journal_revisions/gear_default/activity_tracks），再删被引用侧，foreign_keys=ON 下不违约。
                    for table in ('gear_activity', 'gear_default', 'journal_revisions', 'activity_tracks', 'activities', 'activity_revisions', 'gear', 'journal', 'garmin_connection', 'sync_state'):
                        conn.execute('DELETE FROM '+table+' WHERE user_id=?', (uid,))
            return error
        auth.delete_user = delete_with_running
    return store
