"""Per-user appearance preferences in profiles.db, without changing legacy profiles."""
import sqlite3
from flask import g, jsonify, request
THEMES = ('clarion', 'nightlab', 'fieldnote')


def register_themes(app, db_path, limiter=None):
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS theme_preferences (user_id INTEGER PRIMARY KEY, theme TEXT NOT NULL DEFAULT 'clarion')")

    @app.route('/api/profile/theme', methods=['GET', 'PUT'])
    def profile_theme():
        user = getattr(g, 'current_user', None)
        if not user:
            return jsonify(error='请先登录'), 401
        with sqlite3.connect(db_path) as conn:
            if request.method == 'PUT':
                data = request.get_json(silent=True)
                theme = data.get('theme') if isinstance(data, dict) else None
                if theme not in THEMES:
                    return jsonify(error='未知主题'), 400
                conn.execute('INSERT INTO theme_preferences VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET theme=excluded.theme', (user['id'], theme))
            row = conn.execute('SELECT theme FROM theme_preferences WHERE user_id=?', (user['id'],)).fetchone()
        return jsonify(theme=row[0] if row else 'clarion', themes=list(THEMES))

    if limiter is not None:
        app.view_functions['profile_theme'] = limiter.limit('60 per minute')(app.view_functions['profile_theme'])
