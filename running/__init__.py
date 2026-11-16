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
    return store

