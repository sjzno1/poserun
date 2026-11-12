"""Internal fresh-exec entry; never an HTTP endpoint."""
import json,os,sys,inspect,uuid
from pathlib import Path
job=Path(sys.argv[1]).resolve()
os.chdir(job)
spec=json.loads((job/'job.json').read_text())
import app as api
from compute_budget import Budget,_context
from flask import g
allowed={'analyze_pose','analyze_pose_advanced','batch_process'}
if spec['endpoint'] not in allowed:raise ValueError('invalid worker endpoint')
# Private worker paths; real source uploads remain read-only by convention.
api.REPORTS_FOLDER=str(job/'reports')
api.KEYFRAMES_FOLDER=str(job/'data'/'keyframes')
# Keyframe names already carry uuids; job KEYFRAMES_FOLDER gives isolation.
fn=inspect.unwrap(getattr(api,spec['endpoint']))
with api.app.test_request_context(spec['path']+'?'+spec['query'],method='POST',json=spec['body']):
    g.current_user=spec['user']
    token=_context.set(Budget())
    try:
        response=api.app.make_response(fn())
        response.direct_passthrough=False
        body=response.get_data()
        payload=response.get_json(silent=True) if response.mimetype=='application/json' else None
        failed=(payload or {}).get('errors') if spec['endpoint']=='batch_process' else None
        headers={k:v for k,v in response.headers.items() if k.lower() in ('content-type','content-disposition')}
        (job/'response.body').write_bytes(body)
        (job/'response.json').write_text(json.dumps({'status':response.status_code,'headers':headers,'batch_failed':failed}))
    finally:_context.reset(token)
