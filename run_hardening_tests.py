"""Run in network-none NAS container, /scratch writable, /input read-only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import signal
signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
signal.alarm(1500)

source=Path('/input')
root=Path('/scratch/project')
shutil.copytree(source,root,ignore=shutil.ignore_patterns('__pycache__','data','reports','uploads','.git'),dirs_exist_ok=True)
os.chdir(root)
for name in ('data','reports','uploads'):Path(name).mkdir(exist_ok=True)
if Path('/model/yolo11n-pose.pt').is_file():
    (root/'data/models').mkdir(exist_ok=True)
    shutil.copy2('/model/yolo11n-pose.pt',root/'data/models/yolo11n-pose.pt')
base=dict(os.environ, PYTHONPATH=str(root), AUTH_DB_PATH=str(root/'data/auth.db'),
          ADMIN_USERNAME='isolated_dummy_admin', ADMIN_PASSWORD='isolated_dummy_password_368')
commands=[
 ('hardening',[sys.executable,'-m','unittest','discover','-s','tests','-p','test_release_hardening.py','-v']),
 ('candidate',[sys.executable,'-m','unittest','discover','-s','calibration','-p','test_candidate.py','-v']),
 ('regression',[sys.executable,'-m','unittest','discover','-s','tests','-p','test_regression.py','-v']),
 ('subject',[sys.executable,'-m','unittest','discover','-s','calibration/tests','-p','test*subject*.py','-v']),
 ('pipeline',[sys.executable,'-m','unittest','discover','-s','calibration/tests','-p','test_video_pipeline.py','-v']),
 ('worker-release',[sys.executable,'-m','unittest','discover','-s','tests','-p','test_worker_release.py','-v']),
 ('restore-consumers',[sys.executable,'restore_consumers_contract.py']),
 ('fresh-inference-report',[sys.executable,'inference_report_contract.py']),
 ('pdf-pages-text-render',[sys.executable,'pdf_contract.py']),
]
results=[]
for name,cmd in commands:
    try:
        p=subprocess.run(cmd,env=base,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=240,text=True)
        output=p.stdout
        code=p.returncode
    except subprocess.TimeoutExpired as e:
        output=str(e.stdout)
        code=124
    print('=== '+name+' ===',flush=True)
    print(output,flush=True)
    results.append(dict(suite=name,exit_code=code))
print('RESULT_JSON='+json.dumps(results),flush=True)
sys.exit(1 if any(r['exit_code'] for r in results) else 0)
