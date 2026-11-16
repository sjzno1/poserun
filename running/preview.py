"""Local P1-only preview: python -m running.preview. No inference imports."""
import os
from pathlib import Path
from flask import Flask, g, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from auth import AuthManager
from . import register_running
from .themes import register_themes


def create_app(data_dir=None):
    data = Path(data_dir or os.environ.get('RUNNING_PREVIEW_DATA','data/running-preview'))
    data.mkdir(parents=True,exist_ok=True,mode=0o700)
    static = Path(__file__).resolve().parent.parent/'static'
    app=Flask(__name__,static_folder=str(static),static_url_path='')
    app.config.update(MAX_CONTENT_LENGTH=11*1024*1024,GARMIN_LIVE_ENABLED=os.environ.get('GARMIN_LIVE_ENABLED')=='1')
    auth=AuthManager(str(data/'auth.db'))
    auth.attach(app)
    limiter=Limiter(key_func=lambda: str(g.current_user['id']) if getattr(g,'current_user',None) else get_remote_address(),app=app,storage_uri='memory://')
    register_themes(app,str(data/'profiles.db'))
    register_running(app,data,limiter)

    @app.get('/')
    def index():
        return send_from_directory(str(static),'index.html')

    @app.get('/api/health')
    def health():
        return {'status':'healthy','mode':'P1-only preview','pose_inference':False}

    return app


if __name__=='__main__':
    create_app().run(host='127.0.0.1',port=int(os.environ.get('RUNNING_PREVIEW_PORT','5055')),debug=False)
