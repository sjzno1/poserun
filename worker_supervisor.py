"""Fresh exec worker, killable native calls; parent owns locks and publication.
No fork of a threaded Flask interpreter. Worker receives only a private job path.
"""
import json, os, signal, sqlite3, subprocess, sys, tempfile, time, shutil, uuid
from pathlib import Path
from contextlib import contextmanager
from compute_budget import BudgetExceeded, LIMITS

def run_process(command, *, timeout, cwd=None, env=None):
    # A separate session makes native subprocess descendants part of the kill unit.
    with tempfile.TemporaryFile() as log:
        p=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.DEVNULL,
                           stdout=log,stderr=log,start_new_session=True,close_fds=True)
        try:
            try: code=p.wait(timeout=max(0.01,timeout))
            except subprocess.TimeoutExpired:
                try: os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                p.wait(timeout=5)
                raise BudgetExceeded('分析工作进程硬超时；整批结果未发布')
            if code:
                raise RuntimeError('分析工作进程失败（退出码 %s）；结果未发布'%code)
        finally:
            # Also reap descendants when the main worker exits early.
            try: os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            p.wait(timeout=5)

def snapshot_db(src,dst):
    if src.is_file():
        from contextlib import closing
        with closing(sqlite3.connect(src.resolve().as_uri()+'?mode=ro',uri=True)) as a:
            with closing(sqlite3.connect(dst)) as b:a.backup(b)

def execute_request(fn):
    from flask import request,g,current_app
    import app as api
    root=Path.cwd().resolve()
    # Jobs never reside in publicly served reports or uploads.
    jobs=root/'data'/'.analysis-jobs'
    jobs.mkdir(mode=0o700,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='job-',dir=jobs) as temp:
        job=Path(temp).resolve()
        (job/'data').mkdir(); (job/'reports').mkdir(); (job/'uploads').symlink_to(root/'uploads',target_is_directory=True)
        for name in ('auth.db','history.db','profiles.db','audit.db'):
            snapshot_db(root/'data'/name,job/'data'/name)
        secret=root/'data'/'.auth_secret'
        if secret.is_file():shutil.copyfile(secret,job/'data'/'.auth_secret')
        models=root/'data'/'models'
        if models.is_dir():(job/'data'/'models').symlink_to(models,target_is_directory=True)
        with sqlite3.connect(job/'data'/'history.db') as db:
            prior=db.execute('SELECT COALESCE(MAX(id),0) FROM history_records').fetchone()[0]
        prior_audit=0
        if (job/'data'/'audit.db').is_file():
            with sqlite3.connect(job/'data'/'audit.db') as db:
                try: prior_audit=db.execute('SELECT COALESCE(MAX(id),0) FROM audit_log').fetchone()[0]
                except sqlite3.OperationalError: prior_audit=0
        spec={'endpoint':fn.__name__,'path':request.path,'query':request.query_string.decode(),
              'body':request.get_json(silent=True),'user':getattr(g,'current_user',None),'prior':prior,
              'prior_audit':prior_audit}
        (job/'job.json').write_text(json.dumps(spec))
        env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','YOLO_CONFIG_DIR')}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parent),PYTHONDONTWRITEBYTECODE='1',
                   AUTH_DB_PATH=str(job/'data'/'auth.db'),POSERUN_REPORTS_DIR=str(job/'reports'),
                   POSERUN_ANALYSIS_WORKER='1')
        run_process([sys.executable,str(Path(__file__).with_name('analysis_worker.py')),str(job)],
                    timeout=LIMITS.wall_seconds,cwd=job,env=env)
        result=json.loads((job/'response.json').read_text())
        if result['status']>=400:
            return current_app.response_class((job/'response.body').read_bytes(),status=result['status'],headers=result['headers'])
        # A worker may only publish an entirely successful batch.
        if result.get('batch_failed'):
            return {'error':'批量任一视频失败，整批不发布','errors':result['batch_failed'],'successful':0,'results':[]},422
        publish(job,root,prior,prior_audit)
        return current_app.response_class((job/'response.body').read_bytes(),status=result['status'],headers=result['headers'])

def publish(job,root,prior,prior_audit=0):
    """Artifacts first, one SQLite commit last. Authorization sees no half batch.
    On ordinary failures rollback every new artifact. Process/power loss before
    commit can leave inaccessible orphans, never history pointing to half a batch.
    """
    moved=[]
    db=sqlite3.connect(root/'data'/'history.db',timeout=10)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('ATTACH DATABASE ? AS staged',(str(job/'data'/'history.db'),))
        cols=[r[1] for r in db.execute('PRAGMA table_info(history_records)') if r[1]!='id']
        names=','.join('"'+c+'"' for c in cols)
        # Single writer analysis lock ensures copied IDs do not diverge through analysis.
        # Deletions do not reset SQLite AUTOINCREMENT. Abort rather than return wrong IDs.
        rows=db.execute('SELECT id FROM staged.history_records WHERE id>? ORDER BY id',(prior,)).fetchall()
        seq=db.execute("SELECT seq FROM sqlite_sequence WHERE name='history_records'").fetchone()
        if rows and rows[0][0] != (seq[0] if seq else 0)+1:
            raise RuntimeError('历史写入并发冲突；整批未发布')
        for label,source in [('reports',job/'reports'),('data/keyframes',job/'data'/'keyframes')]:
            target=root/label;target.mkdir(parents=True,exist_ok=True)
            if not source.exists():continue
            for item in source.iterdir():
                dest=target/item.name
                if item.is_symlink() or dest.exists():raise RuntimeError('发布资源冲突')
                # Parent is sole analysis writer and names carry a random job prefix.
                os.rename(item,dest);moved.append(dest)
        db.execute('INSERT INTO history_records (id,'+names+') SELECT id,'+names+' FROM staged.history_records WHERE id>?',(prior,))
        db.commit()
    except BaseException:
        db.rollback()
        for p in reversed(moved):
            if p.is_dir():shutil.rmtree(p)
            else:p.unlink(missing_ok=True)
        raise
    finally:db.close()
    # Audit is best-effort AFTER the history commit: WAL attached databases do
    # not share one atomic commit, and a lost audit row must not undo a valid
    # batch. New ids are assigned locally to avoid concurrent-audit collisions.
    try:
        staged_audit=job/'data'/'audit.db'
        if staged_audit.is_file():
            from contextlib import closing
            with closing(sqlite3.connect(root/'data'/'audit.db',timeout=10)) as adb:
                adb.execute("CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user_id INTEGER, username TEXT NOT NULL DEFAULT '', user_name TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '')")
                adb.execute('ATTACH DATABASE ? AS sa',(str(staged_audit),))
                adb.execute('INSERT INTO audit_log (ts,user_id,username,user_name,action,target,detail,ip) SELECT ts,user_id,username,user_name,action,target,detail,ip FROM sa.audit_log WHERE id>?',(prior_audit,))
                adb.commit()
    except Exception as exc:
        print('[publish] 审计行同步失败（不影响已发布结果）: %s'%exc)
