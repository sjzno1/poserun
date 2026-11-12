"""Repeatable API audit; copied sources only, never import the production app.
Run with an EXISTING interpreter containing the project's dependencies.
Detector inference is explicitly replaced by saved-frame replay, not a model test.
"""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'calibration/results-feature-audit'

WORKER = r'''
import cProfile
import copy
import csv
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import zipfile
from unittest.mock import patch
sandbox, source, output = map(Path, sys.argv[1:])
os.chdir(sandbox)
sys.path.insert(0, str(sandbox))
import pose_analyzer
sys.modules.pop('profile', None)
import app as m
m.limiter.enabled = False
m.app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
c = m.app.test_client()
checks = []

def call(method, route, expected=200, body=None, token=None, verify=None, reason='', **kwargs):
    headers = {'Authorization': 'Bearer '+token} if token else {}
    response = c.open(route, method=method, json=body, headers=headers, **kwargs)
    data = response.get_json(silent=True)
    ok = response.status_code in (expected if isinstance(expected, tuple) else (expected,))
    detail = reason or '真实 Flask 请求，状态符合预期'
    try:
        if response.is_json:
            json.loads(response.get_data(as_text=True), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        if ok and verify:
            assert verify(data, response), '响应内容/状态回读不符合预期'
    except Exception as exc:
        ok = False
        detail += '; '+str(exc)
    if not ok:
        preview = response.get_data(as_text=True)[:500] if response.is_json or response.mimetype.startswith('text/') else repr(response.get_data()[:80])
        detail += '; response='+preview
    rule = m.app.url_map.bind('localhost').match(route.split('?')[0], method=method, return_rule=True)[0].rule
    checks.append({'method':method, 'route':route, 'route_template':rule, 'status':response.status_code,
                   'pass':ok, 'outcome':'pass' if ok else 'fail', 'reason':detail})
    (output/'api-checks.json').write_text(json.dumps({'checks':checks}, ensure_ascii=False, indent=2))
    return data or {}, response

def login(name, password):
    data, _ = call('POST','/api/auth/login',body={'username':name,'password':password},
                   verify=lambda d,r: bool(d.get('token')))
    return data.get('token', '')

admin = login(os.environ['ADMIN_USERNAME'],os.environ['ADMIN_PASSWORD'])
password = 'TEST-only-audit-password-293!'
users = {}
for name in ('test-audit-runner','test-audit-other','test-audit-delete'):
    d,_ = call('POST','/api/auth/register',201,{'username':name,'password':password,'name':name})
    users[name] = d.get('user',{}).get('id')
runner = login('test-audit-runner',password)
other = login('test-audit-other',password)
call('POST','/api/auth/register',400,{'username':'test-audit-runner','password':password})
call('POST','/api/auth/login',401,{'username':'test-audit-runner','password':'wrong'})
call('GET','/api/health',verify=lambda d,r:d.get('analysis_scope')=='single_primary_runner' and d.get('target_selection_supported') is False and d.get('subject_selection_mode')=='automatic_primary_runner')
call('GET','/api/auth/me',401)
call('GET','/api/auth/me',token=runner,verify=lambda d,r:d['user']['username']=='test-audit-runner')
call('PATCH','/api/auth/me',body={'name':'TEST audit renamed'},token=runner)
call('GET','/api/auth/me',token=runner,verify=lambda d,r:d['user']['name']=='TEST audit renamed')
call('POST','/api/auth/me/password',400,{'old_password':'wrong','new_password':password},runner)
call('POST','/api/auth/me/password',body={'old_password':password,'new_password':password+'x'},token=runner)
runner = login('test-audit-runner',password+'x')
call('GET','/api/users',403,token=runner)
call('GET','/api/users',token=admin,verify=lambda d,r:any(u['username']=='test-audit-runner' for u in d['users']))
uid=users['test-audit-other']
call('PATCH',f'/api/users/{uid}',body={'name':'TEST admin edited'},token=admin)
call('GET','/api/users',token=admin,verify=lambda d,r:any(u['id']==uid and u['name']=='TEST admin edited' for u in d['users']))
call('POST',f'/api/users/{uid}/password',body={'password':password+'reset'},token=admin)
other=login('test-audit-other',password+'reset')
call('POST','/api/upload',400,token=runner)
call('POST','/api/upload',400,token=runner,data={'video':(io.BytesIO(b'TEST'),'test.txt')})
# Real generated video for upload and decoding, no human/model accuracy assertion.
import cv2
import numpy as np
video=Path('test-audit-source.avi')
writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),20,(128,128))
assert writer.isOpened(), 'host MJPG writer unavailable'
for i in range(40):
    frame=np.zeros((128,128,3),dtype=np.uint8)
    cv2.putText(frame,'TEST',(10,64),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1)
    writer.write(frame)
writer.release()
d,_=call('POST','/api/upload',token=runner,data={'video':(io.BytesIO(video.read_bytes()),'test-audit.avi')},verify=lambda d,r:Path('uploads',d['filename']).is_file())
filename=d['filename']
call('POST','/api/video/quality',400,{},runner)
call('POST','/api/video/quality',404,{'video_file':'test-audit-missing.avi'},runner)
call('POST','/api/video/quality',body={'video_file':filename},token=runner,
     reason='真实合成视频容器预检；不进行人体推理',
     verify=lambda d,r:d.get('preflight_scope')=='container_metadata_only' and d.get('valid_frame_ratio') is None and d.get('single_person_scene_verified') is False)
for route in ('/api/analyze','/api/analyze/advanced','/api/batch'):
    for bad in ([], {}, {'video_files':'wrong'} if route.endswith('batch') else {'video_file':17}):
        call('POST',route,400,bad,runner)
    if not route.endswith('batch'):
        call('POST',route,404,{'video_file':'test-audit-missing.mp4'},runner)
        call('POST',route,400,{'video_file':filename,'target_selection':{'bbox':[0,0,0,0]}},runner)
        call('POST',route,400,{'video_file':filename,'keyframes':'not-an-int'},runner,
             reason='非法 keyframes 必须返回4xx，不能500')
from single_person import SinglePersonVideoRequired, SCOPE
# Product scope is single-person even when legacy selection fields are null.
for route in ('/api/analyze','/api/analyze/advanced','/api/batch'):
    for field in ('target_selection','target_selections'):
        for value in (None,{},False,{'frame_index':0,'box':[0,0,1,1]}):
            body={'video_files':[filename]} if route.endswith('batch') else {'video_file':filename}
            body[field]=value
            with patch.object(m,'PoseAnalyzer') as model:
                call('POST',route,400,body,runner,reason='单人产品范围：拒绝任何旧目标选择参数',
                     verify=lambda d,r:d.get('analysis_scope')==SCOPE)
                assert not model.called,'scope validation must precede model initialization'
# Synthetic multi-candidate outcomes test the real error/side-effect boundary,
# not the detector's accuracy. No partial report or history may be published.
class RejectedVideo:
    closed=0
    def process_video(self,path):raise SinglePersonVideoRequired(0)
    def close(self):type(self).closed+=1
class RejectedRows(RejectedVideo):
    def process_video(self,path):return [{'frame_index':0,'detection_status':'primary_subject_ambiguous'}]
class LostVideo(RejectedVideo):
    def process_video(self,path):raise SinglePersonVideoRequired(0,error_code='primary_subject_lost')
class LostRows(RejectedVideo):
    def process_video(self,path):return [{'frame_index':0,'detection_status':'primary_subject_lost'}]
for adapter in (RejectedVideo,RejectedRows,LostVideo,LostRows):
    for route in ('/api/analyze','/api/analyze/advanced','/api/batch'):
        body={'video_files':[filename]} if route.endswith('batch') else {'video_file':filename}
        before=m.history_store.count_records();closed=adapter.closed
        with patch.object(m,'PoseAnalyzer',adapter),patch.object(m,'ReportGenerator') as report:
            def rejection(d,r):
                error=d['errors'][0] if 'errors' in d else d
                expected='primary_subject_lost' if adapter in (LostVideo,LostRows) else 'primary_subject_ambiguous'
                return error.get('error_code')==expected and error.get('frame_index')==0
            call('POST',route,400,body,runner,reason='多候选停止，不返回部分分析成功',verify=rejection)
            assert not report.called,'rejected video must not generate a report'
        assert m.history_store.count_records()==before and adapter.closed==closed+1
with patch.object(m,'PoseAnalyzer',RejectedVideo),patch.object(m,'ReportGenerator') as report:
    call('POST','/api/batch?download=true',400,{'video_files':[filename]},runner,
         reason='全部被拒绝时不下载空成功ZIP',verify=lambda d,r:d['status']=='rejected' and not r.data.startswith(b'PK'))
    assert not report.called
frames=json.loads((source/'calibration/results-candidate-stage2/kipchoge-candidate-frames.json').read_text())
# Reject malformed/resource-excessive keyframe counts before initializing a model.
for route in ('/api/analyze','/api/analyze/advanced','/api/batch'):
    for bad in (None, True, 1.5, -1, 21, '5'):
        body = {'video_files':[filename]} if route.endswith('batch') else {'video_file':filename}
        body['keyframes'] = bad
        call('POST',route,400,body,runner,reason='keyframes类型/边界校验；不归一化错误输入')
class Replay:
    def process_video(self,path,**kwargs): return copy.deepcopy(frames)
    def close(self): pass
class EmptyKeyframes:
    def extract_key_frames(self,*args,**kwargs): return []
    def save_annotations(self,directory,*args,**kwargs):
        Path(directory).mkdir(parents=True,exist_ok=True)
        return []
ids=[]
reports=[]
with patch.object(m,'PoseAnalyzer',Replay),patch.object(m,'KeyFrameAnalyzer',EmptyKeyframes):
    for value in (0,20):
        call('POST','/api/analyze',body={'video_file':filename,'keyframes':value},token=runner,
             reason='合法keyframes边界；保存帧回放，实际聚合/报告/入库',
             verify=lambda d,r:d.get('score') is None and d.get('grade') is None)
    for route in ('/api/analyze','/api/analyze/advanced','/api/batch'):
        body={'video_files':[filename]} if route.endswith('batch') else {'video_file':filename,'history':True}
        d,r=call('POST',route,body=body,token=runner,
                 reason='明确替身：保存帧回放；报告/指标聚合/数据库为真实实现',
                 verify=lambda d,r: all(x.get('score') is None and x.get('grade') is None and x['training_plan']['weekly_plan']==[] for x in (d.get('results',[]) if 'results' in d else [d])) and d.get('failed',0)==0)
        if r.status_code==200:
            for item in d.get('results',[d]):
                reports.append(item['report_file'])
                call('GET','/api/report/'+item['report_file'],token=runner,verify=lambda d,r:len(r.data)>100)
    call('POST','/api/batch?download=true',body={'video_files':[filename]},token=runner,
         verify=lambda d,r:bool(zipfile.ZipFile(io.BytesIO(r.data)).namelist()))
# Synthetic boxes/keypoints, real decoder + selector + pipeline + API/report/DB.
from calibrated_analyzer import CalibratedPoseAnalyzer
from types import SimpleNamespace
class AutoPrimaryFixture(CalibratedPoseAnalyzer):
    def __init__(self):
        self._reset_temporal();self.backend='TEST-synthetic-detection';self.calls=0
        self._yolo_model=SimpleNamespace(predict=self.predict)
    def predict(self,frame,**kwargs):
        self.calls+=1
        boxes=[[8.,5.,60.,124.],[91.,50.,108.,85.]]
        if self.calls%2:boxes.reverse()
        def pose(b):
            x,y,r,bottom=b;w,h=r-x,bottom-y
            return [[x+w*.5,y+h*.08]]*5+[[x+w*.3,y+h*.25],[x+w*.7,y+h*.25],
                [x+w*.2,y+h*.4],[x+w*.8,y+h*.4],[x+w*.15,y+h*.55],[x+w*.85,y+h*.55],
                [x+w*.35,y+h*.55],[x+w*.65,y+h*.55],[x+w*.35,y+h*.72],[x+w*.65,y+h*.72],
                [x+w*.35,y+h*.95],[x+w*.65,y+h*.95]]
        return [SimpleNamespace(keypoints=SimpleNamespace(xy=np.array([pose(b) for b in boxes]),conf=np.ones((2,17))),boxes=SimpleNamespace(xyxy=np.array(boxes)))]
for route in ('/api/analyze','/api/analyze/advanced','/api/batch'):
    body={'video_files':[filename]} if route.endswith('batch') else {'video_file':filename}
    def automatic_selection(d,r):
        items=d.get('results',[d])
        for item in items:
            subject=item['video_timeline']['subject_selection']
            assert subject['mode']=='automatic_primary_runner' and subject['state']=='locked'
            assert subject['identity_verified'] is False and subject['heuristic_unvalidated'] is True
            assert subject['initial_frame_index']==0 and subject['selected_frame_count']==40
            assert subject['initial_box'][0]<.1 and item['score'] is None
        return bool(items)
    with patch.object(m,'PoseAnalyzer',AutoPrimaryFixture),patch.object(m,'KeyFrameAnalyzer',EmptyKeyframes):
        call('POST',route,body=body,token=runner,
             reason='合成候选但实际自动关联：大前景+小背景，候选顺序交替，单主体元数据与无评分报告读回',verify=automatic_selection)
# A distinct owned TEST upload permits verifying mixed-batch JSON and ZIP.
d,_=call('POST','/api/upload',token=runner,data={'video':(io.BytesIO(video.read_bytes()),'test-scope-rejected.avi')})
multi_filename=d['filename']
class MixedReplay(Replay):
    closed=0
    def process_video(self,path,**kwargs):
        if Path(path).name==multi_filename:raise SinglePersonVideoRequired(0)
        return super().process_video(path,**kwargs)
    def close(self):type(self).closed+=1
for download in (False,True):
    before=m.history_store.count_records();closed=MixedReplay.closed
    def mixed_result(d,r):
        if download:
            with zipfile.ZipFile(io.BytesIO(r.data)) as archive:
                d=json.loads(archive.read('batch_result.json'))
                assert len([n for n in archive.namelist() if n.endswith(('.pdf','.docx'))])==1
        return (d['status']=='partial' and d['successful']==1 and d['failed']==1 and
                d['results'][0]['video_filename']==filename and
                d['errors'][0]['filename']==multi_filename and
                d['errors'][0]['error_code']=='primary_subject_ambiguous')
    with patch.object(m,'PoseAnalyzer',MixedReplay),patch.object(m,'KeyFrameAnalyzer',EmptyKeyframes):
        call('POST','/api/batch'+('?download=true' if download else ''),
             body={'video_files':[filename,multi_filename]},token=runner,
             reason='明确合成状态/保存帧回放：混合批量不隐藏拒绝文件，ZIP清单读回',verify=mixed_result)
    assert m.history_store.count_records()==before+1 and MixedReplay.closed==closed+2
# Original history/API consumers include only the successful records.
d,_=call('GET','/api/history',token=runner,verify=lambda d,r:len(d['records'])>=3 and all(x['score'] is None and x['grade'] is None for x in d['records']))
ids=[r['id'] for r in d.get('records',[])]
call('GET','/api/history/records',token=runner,verify=lambda d,r:all(x['username']=='test-audit-runner' for x in d['records']))
call('GET','/api/history/records',token=other,verify=lambda d,r:not d['records'])
call('GET','/api/history/users',403,token=runner)
call('GET','/api/history/users',token=admin)
for route in ('/api/templates','/api/profile','/api/profile/ranges','/api/trend','/api/trend/progress'):
    call('GET',route,token=runner)
call('POST','/api/profile',body={'height_cm':178,'weight_kg':68,'notes':'TEST sandbox audit'},token=runner)
call('GET','/api/profile',token=runner,verify=lambda d,r:'TEST sandbox audit' in json.dumps(d))
call('GET','/api/profile',token=other,verify=lambda d,r:d['profile'] is None)
if len(ids)>=2:
    a,b=ids[:2]
    for route,body in [('/api/history/compare',{'record_ids':[a,b]}),('/api/trend/baseline',{'record_ids':[a,b]}),('/api/trend/compare-ab',{'record_a':a,'record_b':b}),('/api/training/session',{'record_id':a})]:
        call('POST',route,body=body,token=runner)
        call('POST',route,(400,403),body,other,reason='其他用户不可读取/比较本人记录')
    call('POST','/api/training/session',body={'record_id':a},token=runner,verify=lambda d,r:d['session']['weekly_plan']==[])
    call('GET','/api/export/json',token=runner,verify=lambda d,r:len(d['records'])==len(ids) and all(x['score'] is None for x in d['records']))
    call('GET','/api/export/csv',token=runner,verify=lambda d,r:all(row['score']=='' for row in csv.DictReader(io.StringIO(r.get_data(as_text=True)))))
    call('GET',f'/api/export/record/{a}',token=runner)
    call('GET',f'/api/export/record/{a}',403,token=other)
    call('DELETE',f'/api/history/delete/{a}',403,token=other)
    call('DELETE',f'/api/history/delete/{a}',token=runner)
    call('GET','/api/history',token=runner,verify=lambda d,r:a not in [x['id'] for x in d['records']] and d['total_records']==len(ids)-1)
# Explicit TEST download fixture, not a detector-produced keyframe.
kf=Path(m.KEYFRAMES_FOLDER)/'test-audit-download'
kf.mkdir()
cv2.imwrite(str(kf/'test.png'),np.zeros((8,8,3),dtype=np.uint8))
# This is an owned TEST fixture; ownership is not inferred from its path/name.
m.history_store.add_record({'id':users['test-audit-runner'],'username':'test-audit-runner'},
                           keyframes_dir=kf.name,metrics={'measurement_status':'insufficient_evidence'})
call('GET','/api/keyframes/test-audit-download/test.png',404,token=other)
call('GET','/api/keyframes/test-audit-download/download',404,token=other)
call('GET','/api/keyframes/test-audit-download/test.png',token=runner,verify=lambda d,r:r.data.startswith(b'\x89PNG'))
call('GET','/api/keyframes/test-audit-download/download',token=runner,verify=lambda d,r:zipfile.ZipFile(io.BytesIO(r.data)).namelist()==['test.png'])
call('GET','/api/keyframes/../auth.py',404,token=runner)
call('GET','/api/audit',403,token=runner)
call('GET','/api/audit',token=admin,verify=lambda d,r:'upload' in json.dumps(d))
call('GET','/api/audit/actions',token=admin)
call('GET','/api/backup',403,token=runner)
call('POST','/api/backup',token=admin)
call('GET','/api/backup',token=admin,verify=lambda d,r:bool(d.get('backups')))
archives=list(Path('reports').glob('_backup_data_*.tar.gz'))
assert archives and any('auth.db' in n for n in tarfile.open(archives[-1]).getnames())
call('POST','/api/delete/'+filename,token=runner)
call('POST','/api/video/quality',404,{'video_file':filename},runner)
call('POST','/api/auth/logout',token=runner)
call('DELETE',f"/api/users/{users['test-audit-delete']}",token=admin)
call('GET','/api/users',token=admin,verify=lambda d,r:all(u['username']!='test-audit-delete' for u in d['users']))
call('DELETE','/api/auth/me',token=other)
call('GET','/api/auth/me',401,token=other)
routes=[{'method':method,'route':rule.rule} for rule in m.app.url_map.iter_rules() if rule.rule.startswith('/api/') for method in sorted(rule.methods-{'HEAD','OPTIONS'})]
(output/'api-checks.json').write_text(json.dumps({'checks':checks,'runtime_routes':routes},ensure_ascii=False,indent=2))
'''


def declared_routes():
    routes = []
    for filename in ('app.py', 'auth.py'):
        tree = ast.parse((ROOT/filename).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'route' and dec.args:
                    route = ast.literal_eval(dec.args[0])
                    if not route.startswith('/api/'):
                        continue
                    methods = next((ast.literal_eval(k.value) for k in dec.keywords if k.arg == 'methods'), ['GET'])
                    for method in methods:
                        routes.append({'method':method,'route':route,'source':filename,'line':node.lineno})
    return routes


def main():
    OUTPUT.mkdir(exist_ok=True)
    scratch = Path(tempfile.gettempdir())
    scratch.mkdir(parents=True, exist_ok=True)
    declared = declared_routes()
    result_data: dict = {'checks':[], 'declared_routes':declared}
    with tempfile.TemporaryDirectory(prefix='poserun-feature-audit-',dir=scratch) as directory:
        sandbox=Path(directory)
        for source in ROOT.glob('*.py'):
            shutil.copy2(source,sandbox/source.name)
        (sandbox/'data').mkdir()
        env=dict(os.environ, ADMIN_USERNAME='test-audit-admin', ADMIN_PASSWORD=os.urandom(24).hex(),
                 AUTH_SECRET=os.urandom(32).hex(), AUTH_DB_PATH=str(sandbox/'data/auth.db'),
                 POSERUN_APP_DIR=str(sandbox),POSERUN_DATA_DIR=str(sandbox/'data'),
                 POSERUN_BACKUP_DIR=str(sandbox/'reports'),PYTHONDONTWRITEBYTECODE='1')
        # A stale prior run must never masquerade as this run's output.
        target=OUTPUT/'api-checks.json'
        if target.exists():
            target.unlink()
        try:
            proc=subprocess.run([sys.executable,'-c',WORKER,str(sandbox),str(ROOT),str(OUTPUT)],
                                cwd=scratch,env=env,capture_output=True,text=True,timeout=120)
            log=proc.stdout+proc.stderr
            code=proc.returncode
        except subprocess.TimeoutExpired as exc:
            log='TIMEOUT 120s: '+str(exc)
            code=124
        (OUTPUT/'api-test.log').write_text(log)
        if target.exists():
            result_data=json.loads(target.read_text())
            result_data['declared_routes']=declared
        result_data['worker_exit_code']=code
        result_data['blocker']=log[-4000:] if code else None
    # Runtime coverage is matched through Werkzeug where available; on import
    # failure every declared route is explicitly skipped, never marked passed.
    import re
    for route in declared:
        pattern='^'+re.sub(r'<(?:[^:>]+:)?([^>]+)>',r'[^/?]+',route['route'])+'$'
        covered=any(c['method']==route['method'] and c.get('route_template')==route['route'] for c in result_data['checks'])
        if not covered:
            result_data['checks'].append({**route,'status':None,'pass':False,'outcome':'skip',
                                         'reason':'worker blocked or scenario not reached; see blocker/log'})
    result_data['limitations']=['保存帧回放不是新鲜模型推理/硬件精度验证',
        '关键帧下载使用明确 TEST 夹具，不声称模型提取成功',
        '限流关闭以遍历接口；未验证生产限流/浏览器/摄像头/部署',
        'baseline 是计算接口，不是持久化写入；不应声称完成状态保存']
    result_data['counts']={k:sum(c['outcome']==k for c in result_data['checks']) for k in ('pass','fail','skip')}
    result_data['total']=len(result_data['checks'])
    (OUTPUT/'api-checks.json').write_text(json.dumps(result_data,ensure_ascii=False,indent=2))
    # Reparse actual JSON rather than claiming manually counted totals.
    saved=json.loads((OUTPUT/'api-checks.json').read_text())
    print(json.dumps({'counts':saved['counts'],'total':saved['total'],'worker_exit_code':code},ensure_ascii=False))
    if saved['blocker']:
        print(saved['blocker'])
    return 1 if code or saved['counts']['fail'] or saved['counts']['skip'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
