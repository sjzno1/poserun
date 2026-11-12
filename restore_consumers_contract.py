"""Real consumer restore drill: auth, history, uploads, reports, keyframes.
Also proves backup exclusion safety (orphan job dirs, backup-prefix business
files, symlink rejection). Isolated NAS container only; dummy data only."""
import json
import os
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

from auth import AuthManager
from history import HistoryStore


@contextmanager
def patch_env(env):
    old = os.environ.copy()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def main():
    tmp = Path(tempfile.mkdtemp())
    for name in ('data/keyframes/session', 'uploads', 'reports', 'archives', 'restored'):
        (tmp / name).mkdir(parents=True)
    (tmp / 'data' / '.auth_secret').write_bytes(b'dummy-secret-restore')
    (tmp / 'uploads' / 'runner.mp4').write_bytes(b'dummy-video-bytes')
    # Business file whose name starts with the backup prefix MUST be included.
    (tmp / 'uploads' / '_backup_data_important.txt').write_bytes(b'business-not-archive')
    (tmp / 'reports' / 'runner.pdf').write_bytes(b'%PDF-1.4 dummy report')
    (tmp / 'reports' / 'runner.docx').write_bytes(b'PK dummy docx')
    (tmp / 'data' / 'keyframes' / 'session' / 'frame.jpg').write_bytes(b'dummy-frame-bytes')
    # Orphan analysis job with symlinks: must be excluded, not break backup.
    orphan = tmp / 'data' / '.analysis-jobs' / 'job-orphan'
    orphan.mkdir(parents=True)
    (orphan / 'uploads').symlink_to(tmp / 'uploads', target_is_directory=True)
    (orphan / 'throwaway.db').write_bytes(b'not a real db')

    auth = AuthManager(str(tmp / 'data' / 'auth.db'))
    user, err = auth.register_user('dummy_runner', 'dummy_password_311', 'dummy runner')
    assert user, err
    hist = HistoryStore(str(tmp / 'data' / 'history.db'))
    hist.register_upload('runner.mp4', user)
    hist.add_record(user=user, video_file='runner.mp4', report_file='runner.pdf',
                    keyframes_dir='session', score=None, metrics={'unscored': True})
    env = {'POSERUN_CONSISTENCY_LOCK': str(tmp / 'gate.lock'),
           'POSERUN_DATA_DIR': str(tmp / 'data'),
           'POSERUN_UPLOADS_DIR': str(tmp / 'uploads'),
           'POSERUN_REPORTS_DIR': str(tmp / 'reports'),
           'POSERUN_BACKUP_DIR': str(tmp / 'archives')}
    with patch_env(env):
        # backup.py binds directories at import time: import/reload INSIDE env.
        import importlib
        import backup
        importlib.reload(backup)
        ok, out = backup.run_backup()
        assert ok, out
    with tarfile.open(out) as t:
        names = set(t.getnames())
        for required in ('data/auth.db', 'data/history.db', 'data/.auth_secret',
                         'uploads/runner.mp4', 'uploads/_backup_data_important.txt',
                         'reports/runner.pdf', 'reports/runner.docx',
                         'data/keyframes/session/frame.jpg', 'manifest.json'):
            assert required in names, required
        assert not any('.analysis-jobs' in n for n in names), 'orphan job leaked into backup'
        for entry in json.load(t.extractfile('manifest.json'))['files']:
            dest = tmp / 'restored' / entry['path']
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(t.extractfile(entry['path']).read())
    restored_auth = AuthManager(str(tmp / 'restored' / 'data' / 'auth.db'))
    row = restored_auth.get_user_by_username('dummy_runner')
    assert row and restored_auth._verify_password('dummy_password_311', row['salt'], row['password_hash'])
    login, err = restored_auth.authenticate('dummy_runner', 'dummy_password_311')
    assert login and not err, err
    assert (tmp / 'restored' / 'data' / '.auth_secret').read_bytes() == b'dummy-secret-restore'
    restored_hist = HistoryStore(str(tmp / 'restored' / 'data' / 'history.db'))
    rows = restored_hist.list_for_user({'id': row['id']})
    assert len(rows) == 1 and rows[0]['video_file'] == 'runner.mp4' and rows[0]['report_file'] == 'runner.pdf'
    assert restored_hist.can_access_asset('video', 'runner.mp4', {'id': row['id'], 'role': 'user'})
    assert restored_hist.can_access_asset('report', 'runner.pdf', {'id': row['id'], 'role': 'user'})
    assert not restored_hist.can_access_asset('video', 'runner.mp4', {'id': row['id'] + 999, 'role': 'user'})
    assert (tmp / 'restored' / 'uploads' / 'runner.mp4').read_bytes() == b'dummy-video-bytes'
    assert (tmp / 'restored' / 'uploads' / '_backup_data_important.txt').read_bytes() == b'business-not-archive'
    assert (tmp / 'restored' / 'reports' / 'runner.pdf').read_bytes() == b'%PDF-1.4 dummy report'
    assert (tmp / 'restored' / 'data' / 'keyframes' / 'session' / 'frame.jpg').read_bytes() == b'dummy-frame-bytes'
    for name in ('auth.db', 'history.db'):
        conn = sqlite3.connect(tmp / 'restored' / 'data' / name)
        assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        conn.close()
    # Symlinked business file must fail the backup closed (no silent skip).
    (tmp / 'uploads' / 'evil.mp4').symlink_to(tmp / 'uploads' / 'runner.mp4')
    with patch_env(env):
        ok2, info2 = backup.run_backup()
    assert not ok2 and ('符号链接' in info2 or '非普通文件' in info2), (ok2, info2)
    (tmp / 'uploads' / 'evil.mp4').unlink()
    print('RESTORE_CONSUMERS_PASS=' + json.dumps(
        {'auth': 'ok', 'history': 'ok', 'uploads': 'ok', 'reports': 'ok',
         'keyframes': 'ok', 'orphan_job_excluded': 'ok',
         'backup_prefix_business_file_included': 'ok', 'symlink_rejected': 'ok'}))


main()