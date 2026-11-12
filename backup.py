"""
PoseRun 一致业务备份：SQLite backup API + 递归资源 + manifest。
保存在私有 data/.backups；不通过报告下载，不自动删除既有备份。
源码、镜像与创建配置由独立发布备份负责。
"""
import os
import tarfile
import threading
import time
from datetime import datetime

# 路径可用环境变量覆盖（测试隔离用）
_APP_DIR = os.environ.get('POSERUN_APP_DIR', os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('POSERUN_DATA_DIR', os.path.join(_APP_DIR, 'data'))
UPLOADS_DIR = os.environ.get('POSERUN_UPLOADS_DIR', os.path.join(_APP_DIR, 'uploads'))
REPORTS_DIR = os.environ.get('POSERUN_REPORTS_DIR', os.path.join(_APP_DIR, 'reports'))
BACKUP_DIR = os.environ.get('POSERUN_BACKUP_DIR', os.path.join(DATA_DIR, '.backups'))
PREFIX = '_backup_data_'
KEEP = 7
INTERVAL_SECONDS = 24 * 3600

_lock = threading.Lock()
_scheduler_started = False
_last_result = None  # {'ok': bool, 'file': str, 'size': int, 'at': str, 'error': str}
# 审计钩子：由 app.py 注入（fn(ok, info)，在备份线程中调用，无请求上下文）
audit_hook = None


def _list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    files = [f for f in os.listdir(BACKUP_DIR)
             if f.startswith(PREFIX) and f.endswith('.tar.gz')]
    return sorted(files)


def run_backup():
    """Coordinated application snapshot. External writers must be stopped.

    SQLite backup API creates each DB snapshot while the application gate is
    exclusive. This is cross-resource consistent only for cooperating app
    writers; not a NAS-wide snapshot or protection from external DB editors.
    Source, image and creation config have a separate release backup contract.
    """
    global _last_result
    import hashlib
    import json
    import sqlite3
    import tempfile
    import uuid
    from pathlib import Path
    from consistency import gate
    from contextlib import nullcontext
    from flask import has_request_context, g
    out = None
    try:
        if not os.path.isdir(DATA_DIR):
            raise ValueError('data 目录不存在')
        os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
        os.chmod(BACKUP_DIR, 0o700)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex
        out = os.path.join(BACKUP_DIR, f'{PREFIX}{stamp}.tar.gz')
        coordination = (nullcontext() if has_request_context() and getattr(g, '_snapshot_exclusive', False)
                        else gate(exclusive=True))
        with coordination, _lock:
            with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=BACKUP_DIR) as staging:
                entries = []
                roots = {'data': DATA_DIR, 'uploads': UPLOADS_DIR, 'reports': REPORTS_DIR}
                for label, root in roots.items():
                    base = Path(root)
                    if not base.is_dir() or base.is_symlink():
                        raise ValueError(f'备份源不存在或不是实际目录: {label}')
                    for directory, dirs, files in os.walk(base, followlinks=False):
                        dirs.sort()
                        files.sort()
                        # Never recurse into our own archive/staging directory.
                        # Never archive our own archives/staging nor orphan analysis
                        # job dirs (they contain symlinks and throwaway DB copies).
                        jobs_dir = os.path.realpath(os.path.join(DATA_DIR, '.analysis-jobs'))
                        dirs[:] = [d for d in dirs
                                   if os.path.realpath(os.path.join(directory, d)) not in (os.path.realpath(BACKUP_DIR), jobs_dir)]
                        if any(Path(directory, d).is_symlink() for d in dirs):
                            raise ValueError('备份拒绝符号链接目录')
                        for name in files:
                            src = Path(directory, name)
                            if src.is_symlink() or not src.is_file():
                                raise ValueError('备份拒绝非普通文件')
                            if src.name in ('.consistency.lock', '.analysis.lock'):
                                continue
                            if src.name.endswith(('-wal', '-shm', '-journal')):
                                main = src.with_name(src.name.rsplit('-', 1)[0])
                                if main.is_file():
                                    with main.open('rb') as f:
                                        if f.read(16) == b'SQLite format 3\x00':
                                            continue
                            rel = Path(label) / src.relative_to(base)
                            dst = Path(staging) / rel
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            with src.open('rb') as f:
                                sqlite = f.read(16) == b'SQLite format 3\x00'
                            if sqlite:
                                from contextlib import closing
                                with closing(sqlite3.connect(src.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                                    with closing(sqlite3.connect(dst)) as target:
                                        source.backup(target)
                                        if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                                            raise ValueError('SQLite 快照完整性失败')
                            else:
                                import shutil
                                shutil.copyfile(src, dst)
                            os.chmod(dst, 0o600)
                            h = hashlib.sha256()
                            with dst.open('rb') as f:
                                for block in iter(lambda: f.read(1024 * 1024), b''):
                                    h.update(block)
                            entries.append({'path': rel.as_posix(), 'size': dst.stat().st_size,
                                            'sha256': h.hexdigest(), 'sqlite_backup_api': sqlite})
                manifest = {
                    'schema': 'poserun-business-backup-v1', 'files': entries,
                    'consistency': 'exclusive_cooperating_app_requests_sqlite_backup_api',
                    'external_writers': 'must_be_stopped; not coordinated by this archive',
                    'included': ['all_recursive_data_including_auth_secret_models_keyframes', 'uploads', 'reports'],
                    'excluded': ['configured_backup_directory_only', 'analysis_job_staging', 'runtime_locks', 'SQLite_sidecars_replaced_by_backup_API',
                                 'application_source', 'container_image', 'container_creation_config', 'host_secrets'],
                    'source_image_recovery': 'separate release source tar + docker image save + private creation config required',
                }
                mp = Path(staging, 'manifest.json')
                mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
                partial = out + '.partial'
                try:
                    with tarfile.open(partial, 'w:gz') as t:
                        for entry in entries:
                            t.add(Path(staging, entry['path']), arcname=entry['path'], recursive=False)
                        t.add(mp, arcname='manifest.json', recursive=False)
                    os.chmod(partial, 0o600)
                    with tarfile.open(partial, 'r:gz') as t:
                        assert len(t.getmembers()) == len(entries) + 1
                        for entry in entries:
                            h = hashlib.sha256()
                            with t.extractfile(entry['path']) as f:
                                for block in iter(lambda: f.read(1024*1024), b''):
                                    h.update(block)
                            if h.hexdigest() != entry['sha256']:
                                raise ValueError('归档哈希验证失败')
                    os.replace(partial, out)
                finally:
                    if os.path.exists(partial):
                        os.remove(partial)
        _last_result = {'ok': True, 'file': os.path.basename(out), 'size': os.path.getsize(out),
                        'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'error': None}
        return True, out
    except Exception as e:
        _last_result = {'ok': False, 'file': None, 'size': 0,
                        'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'error': str(e)}
        return False, str(e)


def status():
    """最近一次备份结果 + 现有备份列表"""
    backups = _list_backups()
    return {
        'last': _last_result,
        'backups': [
            {'file': f, 'size': os.path.getsize(os.path.join(BACKUP_DIR, f)),
             'mtime': datetime.fromtimestamp(
                 os.path.getmtime(os.path.join(BACKUP_DIR, f))
             ).strftime('%Y-%m-%d %H:%M:%S')}
            for f in backups
        ],
        'keep': None,
        'retention': 'manual_review_no_automatic_deletion',
        'scope': 'business_data_uploads_reports_not_source_or_image',
    }


def start_scheduler():
    """启动后台每日备份线程（幂等）"""
    global _scheduler_started
    if _scheduler_started:
        return

    def _loop():
        while True:
            time.sleep(INTERVAL_SECONDS)
            ok, info = run_backup()
            print(f'[backup] 每日自动备份: {"OK " + info if ok else "FAIL " + info}')
            if audit_hook:
                try:
                    audit_hook(ok, info)
                except Exception:
                    pass

    t = threading.Thread(target=_loop, daemon=True, name='poserun-backup')
    t.start()
    _scheduler_started = True
    print('[backup] 每日自动备份已启动（不自动删除，需人工审查保留）')


# 启动时立即做一次备份（确保重建后也有新备份），然后进入每日循环
def init_backup():
    ok, info = run_backup()
    print(f'[backup] 启动备份: {"OK " + info if ok else "FAIL " + info}')
    start_scheduler()
