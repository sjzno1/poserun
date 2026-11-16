"""
PoseRun Analyzer API - Flask API接口
提供视频上传、姿态分析和报告生成的Web服务
支持关键帧标注、历史对比、批量处理等高级功能
"""

import os
import uuid
import json
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
import shutil
import zipfile
import io
from datetime import datetime
from release_info import VERSION, CAPABILITIES
from compute_budget import (bounded_analysis, current_budget, preflight, unique_batch, BudgetExceeded)
from consistency import open_gate

from pose_analyzer import KeyFrameAnalyzer
from calibrated_analyzer import CalibratedPoseAnalyzer as PoseAnalyzer
from single_person import SinglePersonVideoRequired, require_single_person_request, SCOPE, PRIMARY_SUBJECT_FAILURE_STATUSES
import pose_analyzer as _pose_analyzer_mod
from report_generator import ReportGenerator
from measurement_safety import (finite, rounded, needs_safe_report, safe_metrics,
                                unscored, measurement_fields, strict_values)
from auth import create_auth_manager
from history import HistoryStore
from audit import AuditStore
from profile import ProfileStore, PersonalizedEngine
from trend import compute_trend, extract_alerts, compare_ab, set_baseline, get_time_series
from training_link import build_training_session, track_progress
from export_api import export_records_csv, export_records_json, export_single_record_json

# 创建Flask应用
app = Flask(__name__, static_folder='static', static_url_path='')
# Every JSON boundary rejects JavaScript-only NaN/Infinity numeric tokens.
from flask.json.provider import DefaultJSONProvider


class FiniteJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        kwargs['allow_nan'] = False
        return super().dumps(strict_values(obj), **kwargs)


app.json = FiniteJSONProvider(app)
# CORS 限定来源：通过环境变量 CORS_ORIGINS 配置（逗号分隔）。
# 默认仅允许本机开发来源；生产部署请显式设置为你的站点域名。
_cors_origins = os.environ.get(
    'CORS_ORIGINS', 'http://localhost:8080,http://127.0.0.1:8080')
CORS(app, origins=[o.strip() for o in _cors_origins.split(',') if o.strip()])

# 请求限流：按登录用户限（未登录回退到 IP），保护 CPU 密集的分析接口
def _rate_limit_key():
    try:
        return f"user:{g.current_user['id']}"
    except (RuntimeError, KeyError, TypeError):
        return f"ip:{get_remote_address()}"


# All API requests coordinate with the complete business snapshot, including
# auth route writes and audit after_request callbacks. Backup POST holds EX
# for the complete request; run_backup recognizes that owned request gate.
@app.before_request
def _snapshot_request_gate():
    if request.path.startswith('/api/'):
        try:
            exclusive = request.path == '/api/backup' and request.method == 'POST'
            g._snapshot_gate = open_gate(exclusive=exclusive, blocking=False)
            g._snapshot_exclusive = exclusive
        except BlockingIOError:
            return jsonify(error='一致备份进行中，请稍后重试', error_code='snapshot_busy'), 503


@app.teardown_request
def _snapshot_release(_error):
    lock = g.pop('_snapshot_gate', None)
    if lock is not None:
        lock.close()


# 认证与人员管理（SQLite 用户库 + 令牌鉴权）
auth_manager = create_auth_manager()
auth_manager.attach(app)

# limiter 在 auth 之后注册，限流检查时 g.current_user 已就绪
limiter = Limiter(
    key_func=_rate_limit_key,
    app=app,
    storage_uri="memory://",
    default_limits=[],
)

# 跑姿历史记录（SQLite 历史库 data/history.db，与 auth.db 同卷持久化）
history_store = HistoryStore(os.path.join('data', 'history.db'))

# 操作日志（SQLite 审计库 data/audit.db，同卷持久化）
audit_store = AuditStore(os.path.join('data', 'audit.db'))

# 跑者画像（SQLite 画像库 data/profiles.db）
profile_store = ProfileStore(os.path.join('data', 'profiles.db'))
profile_engine = PersonalizedEngine(profile_store)

from running.themes import register_themes
register_themes(app, profile_store.db_path)


def _audit(action, target='', detail='', user=None):
    """写审计日志（自动带当前登录用户与 IP），失败不影响主流程"""
    try:
        u = user if user is not None else getattr(g, 'current_user', None)
        ip = request.remote_addr or ''
        audit_store.log(action, user=u, target=target, detail=detail, ip=ip)
    except Exception as e:
        print(f'[audit] 记录失败: {e}')


# 认证模块埋点钩子（登录/注册/人员管理等在 auth.py 内发生）
auth_manager.audit = _audit

# 401/403 统一记录（限流/参数错误等 4xx/5xx 不记，避免噪音）
@app.after_request
def _audit_denied(resp):
    try:
        if resp.status_code in (401, 403) and request.path.startswith('/api/'):
            _audit('401_denied' if resp.status_code == 401 else '403_forbidden',
                   target=request.path,
                   detail=f'{request.method} {request.path}')
    except Exception:
        pass
    return resp

# 数据自动备份（每日备份 data/ 到 reports/，保留最近 7 份）
import backup as backup_mod


def _backup_audit(ok, info):
    """每日自动备份结果入审计日志（后台线程，无请求上下文）"""
    try:
        audit_store.log('backup_auto',
                        target=os.path.basename(str(info)) if ok else '',
                        detail=f'每日自动备份{"成功" if ok else "失败: " + str(info)}',
                        ip='system')
    except Exception as e:
        print(f'[audit] 备份日志记录失败: {e}')


backup_mod.audit_hook = _backup_audit


# 级联：删除用户时同时清理其历史记录
_orig_delete_user = auth_manager.delete_user


def _delete_user_with_history(uid):
    err = _orig_delete_user(uid)
    if err is None:
        try:
            n = history_store.delete_by_user(uid)
            if n:
                print(f'[history] 用户 {uid} 已删除，级联清理 {n} 条历史记录')
        except Exception as e:
            print(f'[history] 级联清理用户 {uid} 历史记录失败: {e}')
    return err


auth_manager.delete_user = _delete_user_with_history

# 配置
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 最大100MB
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'webm'}
UPLOAD_FOLDER = 'uploads'
REPORTS_FOLDER = 'reports'
# 关键帧存到 data/ 持久卷（容器重建不丢；旧版存 /app/keyframes 可写层，重建即失）
KEYFRAMES_FOLDER = os.path.join('data', 'keyframes')
HISTORY_FILE = 'history.json'

# 确保文件夹存在
for folder in [UPLOAD_FOLDER, REPORTS_FOLDER, KEYFRAMES_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# 一次性迁移：旧版关键帧在可写层 /app/keyframes，搬到持久卷
_legacy_kf = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keyframes')
if os.path.isdir(_legacy_kf) and _legacy_kf != os.path.abspath(KEYFRAMES_FOLDER):
    for _name in os.listdir(_legacy_kf):
        _src = os.path.join(_legacy_kf, _name)
        _dst = os.path.join(KEYFRAMES_FOLDER, _name)
        if os.path.isdir(_src) and not os.path.exists(_dst):
            try:
                import shutil
                shutil.move(_src, _dst)
                print(f'[keyframes] 已迁移旧目录: {_name} -> {KEYFRAMES_FOLDER}')
            except Exception as _e:
                print(f'[keyframes] 迁移失败 {_name}: {_e}')


def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def current_display_name():
    """当前登录用户的显示名（姓名优先，其次用户名），未登录返回 None"""
    user = getattr(g, 'current_user', None)
    if not user:
        return None
    return user.get('name') or user.get('username') or ''


def get_video_path(filename):
    """获取文件完整路径"""
    return os.path.join(UPLOAD_FOLDER, filename)


def get_report_path(filename):
    """获取报告文件完整路径"""
    return os.path.join(REPORTS_FOLDER, filename)


def get_keyframes_path(filename):
    """获取关键帧目录完整路径"""
    return os.path.join(KEYFRAMES_FOLDER, filename)


def safe_resolve(base_folder: str, filename: str):
    """将 filename 解析到 base_folder 内，防止路径穿越（../ 逃逸）。
    返回 (解析后的绝对路径, 是否合法)。"""
    base = os.path.realpath(base_folder)
    target = os.path.realpath(os.path.join(base, filename))
    if target == base or target.startswith(base + os.sep):
        return target, True
    return target, False


def authorized_asset_path(kind, filename):
    """先按精确资源归属授权；拒绝路径改写及任意层级符号链接。"""
    if not isinstance(filename, str) or '\\' in filename or '\x00' in filename:
        return None
    parts = filename.split('/')
    if any(part in ('', '.', '..') for part in parts):
        return None
    if kind != 'keyframes' and len(parts) != 1:
        return None
    folders = {'video': UPLOAD_FOLDER, 'report': REPORTS_FOLDER, 'keyframes': KEYFRAMES_FOLDER}
    if kind not in folders or not history_store.can_access_asset(kind, parts[0], getattr(g, 'current_user', None)):
        return None
    current = os.path.abspath(folders[kind])
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return None
    path, ok = safe_resolve(folders[kind], filename)
    return path if ok else None


def save_keyframes(key_frames_dir: str, key_frames: list, analysis_results: list):
    """保存关键帧标注截图（含指标面板）"""
    if needs_safe_report(analysis_results):
        os.makedirs(key_frames_dir, exist_ok=True)
        return []  # Legacy screenshot panels contain unvalidated technical labels.
    analyzer = KeyFrameAnalyzer()
    return analyzer.save_annotations(key_frames_dir, key_frames, analysis_results)


def parse_keyframe_count(data):
    value = data.get('keyframes', 5)
    if type(value) is not int or not 0 <= value <= 20:
        raise ValueError('keyframes须为0至20的整数')
    return value


def process_single_person_video(analyzer, video_path):
    budget = current_budget()
    if budget is not None:
        preflight([video_path], budget)
    rows = analyzer.process_video(video_path)
    for row in rows or []:
        status = row.get('detection_status')
        if status in PRIMARY_SUBJECT_FAILURE_STATUSES:
            raise SinglePersonVideoRequired(row.get('frame_index'), error_code=status)
        if status == 'rejected_multiple_people':  # Historical adapters must still fail closed.
            raise SinglePersonVideoRequired(row.get('frame_index'), reason='legacy_multiple_candidates_rejected')
    return rows


def build_history_metrics(analysis_results: list, score: int, grade: str,
                          problems: list, suggestions: list, video_path: str = None):
    """从分析结果汇总历史记录指标（三条分析路径共用）。
    若提供 video_path，则计算步态指标（步频/垂直振荡/步幅/左右对称）。"""
    if needs_safe_report(analysis_results):
        safe = safe_metrics(analysis_results)
        return dict(measurement_fields(safe),
                    avg_knee=rounded(safe['avg_knee_flexion']),
                    avg_shin=rounded(safe['avg_shin_angle']),
                    body_lean=rounded(safe['avg_body_leang']),
                    stride_level='未验证', symmetry_level='未验证',
                    running_phase=None, score=None, grade=None,
                    problems=[], suggestions=[], cadence=None, gait=None)
    latest = analysis_results[-1]
    m = {
        'avg_knee': rounded(latest.get('knee_flexion')),
        'avg_shin': rounded(latest.get('shin_angle')),
        'stride_level': latest.get('stride_analysis', {}).get('stride_level', '未知'),
        'symmetry_level': latest.get('symmetry', {}).get('symmetry_level', '未知'),
        'body_lean': rounded(latest.get('body_leang')),
        'running_phase': latest.get('running_phase', '未知'),
        'score': score,
        'grade': grade,
        'problems': problems,
        'suggestions': suggestions,
    }
    # 步态指标（步频/垂直振荡/步幅/左右对称）
    if video_path:
        try:
            from gait_metrics import compute_gait
            gait_info = compute_gait(video_path)
            gait = gait_info.get('gait')
            if gait:
                m['gait'] = gait
                m['cadence'] = gait.get('cadence')
        except Exception as e:
            print(f'[gait] 步态指标计算失败: {e}')
    return m


# 视频质量检查结果缓存（避免同一次分析重复跑姿态检测）
_quality_cache = {}


def _video_quality(video_path):
    """视频质量检查（带内存缓存）。失败时返回 None，不影响分析主流程。"""
    try:
        if video_path in _quality_cache:
            return _quality_cache[video_path]
        from gait_metrics import check_video_quality
        q = check_video_quality(video_path)
        _quality_cache[video_path] = q
        if len(_quality_cache) > 20:
            _quality_cache.pop(next(iter(_quality_cache)))
        return q
    except Exception as e:
        print(f'[quality] 视频质量检查失败: {e}')
        return None


def save_history_record(user, video_file, report_file, keyframes_dir,
                        analysis_results, score, grade, problems, suggestions,
                        coach='', video_path=None):
    """把一次成功分析写入历史库（失败不影响分析主流程）"""
    try:
        m = build_history_metrics(analysis_results, score, grade, problems,
                                  suggestions, video_path)
        rec = history_store.add_record(
            user=user,
            video_file=video_file,
            report_file=report_file,
            keyframes_dir=keyframes_dir,
            score=score,
            grade=grade,
            metrics=m,
            problems=problems,
            suggestions=suggestions,
            total_frames=len(analysis_results),
            coach=coach,
        )
        print(f"[history] 已保存记录 {rec['id']} 用户={user.get('username') if user else '匿名'} 得分={score}")
        return rec
    except Exception as e:
        print(f'[history] 保存历史记录失败: {e}')
        return None


def load_history() -> dict:
    """加载历史数据"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_history(record_id: str, current_metrics: dict):
    """保存历史数据"""
    history = load_history()
    
    # 添加新记录
    if 'history' not in history:
        history['history'] = []
    
    history['history'].append({
        'record_id': record_id,
        'date': datetime.now().strftime('%Y-%m-%d'),
        'metrics': current_metrics
    })
    
    # 按日期排序，只保留最近20条
    history['history'].sort(key=lambda x: x.get('date', ''), reverse=True)
    history['history'] = history['history'][:20]
    
    # 保存
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2, default=str)


@app.route('/', methods=['GET'])
def index():
    """Web 前端 dashboard"""
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/common.js', methods=['GET'])
def release_common_js():
    text = Path(app.static_folder, 'common.js').read_text(encoding='utf-8')
    response = app.response_class(text.replace('__POSERUN_VERSION__', VERSION), mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    return jsonify({
        'status': 'healthy',
        'service': 'PoseRun Analyzer',
        'version': VERSION,
        'capabilities': CAPABILITIES,
        'pose_backend': _pose_analyzer_mod.LAST_POSE_BACKEND,
        'analysis_scope': SCOPE,
        'target_selection_supported': False,
        'subject_selection_mode': 'automatic_primary_runner',
        'subject_tracking_revision': 'primary-continuity-v12',
        'features': ['自动主体分析', '二维几何（未评分）', '历史对比', '批量视频（每段一个主体）',
                     '跑者画像', '趋势查看', '数据导出']
    })


def _require_admin():
    user = getattr(g, 'current_user', None)
    if not user or user.get('role') != 'admin':
        return jsonify({'error': '需要管理员权限', 'code': 'forbidden'}), 403
    return None


# ---------- 操作日志（管理员） ----------

@app.route('/api/audit', methods=['GET'])
def audit_list():
    """管理员：查询操作日志。Query: limit / offset / action / username"""
    err = _require_admin()
    if err:
        return err
    limit = min(int(request.args.get('limit', 100) or 100), 500)
    offset = max(int(request.args.get('offset', 0) or 0), 0)
    action = (request.args.get('action') or '').strip() or None
    username = (request.args.get('username') or '').strip() or None
    return jsonify({
        'success': True,
        'total': audit_store.count(action=action, username=username),
        'events': audit_store.list(limit=limit, offset=offset,
                                   action=action, username=username),
    })


@app.route('/api/audit/actions', methods=['GET'])
def audit_actions():
    """管理员：日志中出现的操作类型（用于筛选下拉）"""
    err = _require_admin()
    if err:
        return err
    return jsonify({'success': True, 'actions': audit_store.actions()})


@app.route('/api/backup', methods=['GET'])
def backup_status():
    """备份状态（管理员）：最近一次结果 + 现有备份列表"""
    err = _require_admin()
    if err:
        return err
    return jsonify({'success': True, **backup_mod.status()})


@app.route('/api/backup', methods=['POST'])
@limiter.limit("10 per hour")
def backup_now():
    """手动触发备份（管理员）"""
    err = _require_admin()
    if err:
        return err
    ok, info = backup_mod.run_backup()
    if not ok:
        return jsonify({'error': f'备份失败: {info}'}), 500
    _audit('backup', detail='手动触发数据备份完成')
    return jsonify({'success': True, 'message': '备份完成', **backup_mod.status()})


@app.route('/api/upload', methods=['POST'])
@limiter.limit("20 per hour")
def upload_video():
    """
    上传视频文件接口
    
    Returns:
        JSON: 视频信息（文件名、大小、路径）
    """
    if 'video' not in request.files:
        return jsonify({'error': '未找到视频文件'}), 400
    
    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': '未选择视频文件'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': f'不支持的文件格式。支持: {", ".join(ALLOWED_EXTENSIONS)}'}), 400
    
    # 保存文件
    filename = secure_filename(file.filename)
    unique_filename = f"{uuid.uuid4().hex}_{filename}"
    file_path = get_video_path(unique_filename)
    created = False
    try:
        with open(file_path, 'xb') as destination:
            created = True
            file.save(destination)
        history_store.register_upload(unique_filename, g.current_user)
    except Exception:
        if created and os.path.isfile(file_path):
            os.remove(file_path)
        raise
    
    file_size = os.path.getsize(file_path)

    _audit('upload', target=unique_filename,
           detail=f'上传视频 {filename}（{round(file_size / 1024 / 1024, 2)} MB）')

    return jsonify({
        'success': True,
        'filename': unique_filename,
        'original_name': filename,
        'size_mb': round(file_size / 1024 / 1024, 2),
        'path': file_path
    })


@app.route('/api/video/quality', methods=['POST'])
@limiter.limit("20 per hour")
def video_quality_check():
    """
    视频质量预检接口（上传后、分析前调用）。
    Body: {video_file: 文件名}
    返回时长/帧率/有效帧比例 + 警告 + 重拍建议。
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请提交JSON对象'}), 400
    video_filename = data.get('video_file')
    if not isinstance(video_filename, str) or not video_filename:
        return jsonify({'error': '请提供 video_file'}), 400
    video_path = authorized_asset_path('video', video_filename)
    if not video_path or not os.path.isfile(video_path):
        return jsonify({'error': '视频不存在或无权访问'}), 404
    q = _video_quality(video_path)
    if q is None:
        return jsonify({'error': '质量检查失败'}), 500
    return jsonify({'success': True, **q, 'analysis_scope': SCOPE, 'single_person_scene_verified': False})


@app.route('/api/analyze', methods=['POST'])
@limiter.limit("10 per hour")
@bounded_analysis
def analyze_pose():
    """
    分析视频中的跑姿接口
    
    Request Body:
        video_file: 视频文件名（上传后返回的名称）
        user_name: 可选，用户姓名
        coach_name: 可选，评估教练名称
        keyframes: 可选，关键帧数量（默认5）
        report_format: 可选，报告格式 'pdf'（默认）或 'docx'
    
    Returns:
        JSON: 分析结果（关键指标、评分、问题列表、关键帧目录）
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请提交JSON对象'}), 400
    try:
        require_single_person_request(data)
    except ValueError as exc:
        return jsonify({'error': str(exc), 'analysis_scope': SCOPE}), 400

    video_filename = data.get('video_file')
    if not isinstance(video_filename, str) or not video_filename:
        return jsonify({'error': '请提供视频文件名'}), 400
    
    video_path = authorized_asset_path('video', video_filename)
    if not video_path or not os.path.isfile(video_path):
        return jsonify({'error': '视频不存在或无权访问'}), 404
    
    # 构建用户信息（未指定时默认取当前登录用户）
    user_info = {
        'name': data.get('user_name') or current_display_name() or '匿名用户',
        'coach': data.get('coach_name', '系统自动评估'),
        'score': 0  # 稍后计算
    }
    
    # 解析关键帧数量
    try:
        num_keyframes = parse_keyframe_count(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    
    try:
        # 执行姿态分析
        analyzer = PoseAnalyzer()
        
        try:
            analysis_results = process_single_person_video(analyzer, video_path)
        finally:
            analyzer.close()
        
        if not analysis_results:
            return jsonify({
                'error': '未能识别到人体姿态，请确保视频中有人跑步'
            }), 400

        # 先提取并保存关键帧标注截图（报告需要嵌入这些截图）
        keyframe_analyzer = KeyFrameAnalyzer()
        key_frames = ([] if needs_safe_report(analysis_results) else
                      keyframe_analyzer.extract_key_frames(video_path, num_keyframes))
        unique_id = f"kf_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        keyframes_dir = get_keyframes_path(unique_id)
        save_keyframes(keyframes_dir, key_frames, analysis_results)

        # 生成报告（默认PDF，可选Word，嵌入关键帧截图）
        report_format = (data.get('report_format') or 'pdf').lower()
        if report_format not in ('pdf', 'docx'):
            report_format = 'pdf'
        generator = ReportGenerator()
        report_path = generator.generate_report(
            video_path,
            user_info,
            analysis_results,
            key_frames_dir=keyframes_dir,
            fmt=report_format
        )

        # 构建返回数据
        latest_result = analysis_results[-1]
        # 计算综合得分与问题/建议（与报告口径一致）
        score_info = ReportGenerator()._calculate_score(generator.metrics)
        score = score_info['total']
        problems = generator.metrics.get('problems', [])
        suggestions = generator.metrics.get('suggestions', [])

        # 自动写入历史记录（每位用户历次跑姿入库）
        saved_rec = save_history_record(
            user=getattr(g, 'current_user', None),
            video_file=video_filename,
            report_file=os.path.basename(report_path),
            keyframes_dir=os.path.basename(keyframes_dir),
            analysis_results=analysis_results,
            score=score,
            grade=score_info['grade'],
            problems=problems,
            suggestions=suggestions,
            coach=user_info['coach'],
            video_path=video_path,
        )

        # 生成马拉松跑姿训练计划
        training_plan = ReportGenerator()._build_training_plan(
            dict(generator.metrics, gait=(saved_rec or {}).get('metrics', {}).get('gait')))

        # 训练联动（弱点识别 + 针对性练习 + 周计划）
        current_user = getattr(g, 'current_user', None)
        gait_data = (saved_rec or {}).get('metrics', {}).get('gait')
        training_session = build_training_session(
            metrics=dict(generator.metrics),
            gait=gait_data,
            profile=profile_store.get_profile(user_id=current_user['id']) if current_user else None,
            problems=problems,
        )

        # 个性化建议（基于跑者画像）
        personalized_suggestions = []
        if current_user and not unscored(generator.metrics):
            personalized_suggestions = profile_engine.generate_personalized_suggestions(
                user_id=current_user['id'],
                metrics=dict(generator.metrics),
                gait=gait_data,
            )

        summary = {
            'status': 'success',
            'video_file': video_filename,
            'user_name': user_info['name'],
            'coach_name': user_info['coach'],
            'score': score,
            'grade': score_info['grade'],
            'score_breakdown': [
                {
                    'item': b['item'],
                    'category': b['category'],
                    'result': b['result'],
                    'score': int(b['score']) if finite(b.get('score')) else None,
                } for b in score_info['breakdown']
            ],
            'total_frames': len(analysis_results),
            'keyframes_extracted': len(key_frames),
            'keyframes_dir': os.path.basename(keyframes_dir),
            'metrics': {
                'avg_knee_flexion': rounded(latest_result.get('knee_flexion')),
                'left_knee_angle': rounded(latest_result.get('left_knee_angle')),
                'right_knee_angle': rounded(latest_result.get('right_knee_angle')),
                'avg_shin_angle': rounded(latest_result.get('shin_angle')),
                'stride_level': latest_result.get('stride_analysis', {}).get('stride_level', '未知'),
                'symmetry_level': latest_result.get('symmetry', {}).get('symmetry_level', '未知'),
                'body_leang': rounded(latest_result.get('body_leang')),
                'running_phase': latest_result.get('running_phase', '未知')
            },
            'gait': (saved_rec or {}).get('metrics', {}).get('gait'),
            'training_plan': training_plan,
            'training_session': training_session,
            'personalized_suggestions': personalized_suggestions,
            'video_quality': None if unscored(generator.metrics) else _video_quality(video_path),
            'problems': problems,
            'suggestions': suggestions,
            'report_file': os.path.basename(report_path),
            'report_format': report_format,
            'history_record': saved_rec['id'] if saved_rec else None
        }

        summary.update(measurement_fields(generator.metrics))
        if unscored(generator.metrics):
            summary['metrics'].update({
                'avg_knee_flexion': rounded(generator.metrics.get('avg_knee_flexion')),
                'avg_shin_angle': rounded(generator.metrics.get('avg_shin_angle')),
                'body_leang': rounded(generator.metrics.get('avg_body_leang')),
                'stride_level': '未验证', 'symmetry_level': '未验证', 'running_phase': None,
                'left_knee_angle': None, 'right_knee_angle': None})
            summary['gait'] = None
            summary['video_quality'] = None  # Do not invoke legacy inference in this path.

        _audit('analyze', target=video_filename,
               detail=('分析完成：未评分（测量证据不足）' if score_info['total'] is None else f'分析完成：{score_info["total"]} 分（{score_info["grade"]}）'))
        summary['analysis_scope'] = SCOPE
        return jsonify(strict_values(summary))
    
    except BudgetExceeded:
        raise
    except SinglePersonVideoRequired as exc:
        _audit('analyze_rejected', target=data.get('video_file', ''), detail=json.dumps(exc.payload(), ensure_ascii=False))
        return jsonify(exc.payload()), 400
    except Exception as e:
        _audit('analyze_failed', target=data.get('video_file', ''), detail=str(e))
        return jsonify({'error': f'分析失败: {str(e)}'}), 500


@app.route('/api/analyze/advanced', methods=['POST'])
@limiter.limit("10 per hour")
@bounded_analysis
def analyze_pose_advanced():
    """
    高级分析接口（支持关键帧、历史对比）
    
    Request Body:
        video_file: 视频文件名
        user_name: 用户姓名
        coach_name: 评估教练名称
        keyframes: 关键帧数量（默认5）
        history: 是否启用历史对比（默认false）
        report_format: 可选，报告格式 'pdf'（默认）或 'docx'
    
    Returns:
        JSON: 完整分析结果（包含历史对比）
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请提交JSON对象'}), 400
    try:
        require_single_person_request(data)
    except ValueError as exc:
        return jsonify({'error': str(exc), 'analysis_scope': SCOPE}), 400

    video_filename = data.get('video_file')
    if not isinstance(video_filename, str) or not video_filename:
        return jsonify({'error': '请提供视频文件名'}), 400
    
    video_path = authorized_asset_path('video', video_filename)
    if not video_path or not os.path.isfile(video_path):
        return jsonify({'error': '视频不存在或无权访问'}), 404
    
    user_info = {
        'name': data.get('user_name') or current_display_name() or '匿名用户',
        'coach': data.get('coach_name', '系统自动评估'),
        'score': 0
    }
    
    try:
        num_keyframes = parse_keyframe_count(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    enable_history = data.get('history', False)
    
    try:
        analyzer = PoseAnalyzer()
        
        try:
            analysis_results = process_single_person_video(analyzer, video_path)
        finally:
            analyzer.close()
        
        if not analysis_results:
            return jsonify({'error': '未能识别到人体姿态'}), 400

        # 先提取并保存关键帧标注截图（报告需要嵌入）
        keyframe_analyzer = KeyFrameAnalyzer()
        key_frames = ([] if needs_safe_report(analysis_results) else
                      keyframe_analyzer.extract_key_frames(video_path, num_keyframes))
        unique_id = f"kf_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        keyframes_dir = get_keyframes_path(unique_id)
        save_keyframes(keyframes_dir, key_frames, analysis_results)

        # 生成报告（嵌入关键帧截图）
        report_format = (data.get('report_format') or 'pdf').lower()
        if report_format not in ('pdf', 'docx'):
            report_format = 'pdf'
        generator = ReportGenerator()
        report_path = generator.generate_report(
            video_path,
            user_info,
            analysis_results,
            key_frames_dir=keyframes_dir,
            fmt=report_format
        )

        # 计算综合得分与问题/建议（与报告口径一致）
        score_info = ReportGenerator()._calculate_score(generator.metrics)
        score = score_info['total']

        # 自动写入历史记录
        current_user = getattr(g, 'current_user', None)
        saved_rec = save_history_record(
            user=current_user,
            video_file=video_filename,
            report_file=os.path.basename(report_path),
            keyframes_dir=os.path.basename(keyframes_dir),
            analysis_results=analysis_results,
            score=score,
            grade=score_info['grade'],
            problems=generator.metrics.get('problems', []),
            suggestions=generator.metrics.get('suggestions', []),
            coach=user_info['coach'],
            video_path=video_path,
        )

        # 生成马拉松跑姿训练计划
        training_plan = ReportGenerator()._build_training_plan(
            dict(generator.metrics, gait=(saved_rec or {}).get('metrics', {}).get('gait')))

        # 历史对比（来自数据库，按当前用户）
        history_result = None
        if enable_history:
            if current_user:
                recent = history_store.list_for_user(current_user, limit=5)
            else:
                recent = history_store.list_records(username=user_info['name'], limit=5)
            history_result = {
                'total_records': len(recent),
                'records': recent
            }

        # 构建返回数据
        latest_result = analysis_results[-1]
        summary = {
            'status': 'success',
            'video_file': video_filename,
            'user_name': user_info['name'],
            'score': score,
            'grade': score_info['grade'],
            'score_breakdown': [
                {
                    'item': b['item'],
                    'category': b['category'],
                    'result': b['result'],
                    'score': int(b['score']) if finite(b.get('score')) else None,
                } for b in score_info['breakdown']
            ],
            'total_frames': len(analysis_results),
            'keyframes_dir': os.path.basename(keyframes_dir),
            'metrics': {
                'avg_knee_flexion': rounded(latest_result.get('knee_flexion')),
                'avg_shin_angle': rounded(latest_result.get('shin_angle')),
                'stride_level': latest_result.get('stride_analysis', {}).get('stride_level', '未知'),
                'symmetry_level': latest_result.get('symmetry', {}).get('symmetry_level', '未知'),
                'running_phase': latest_result.get('running_phase', '未知')
            },
            'gait': (saved_rec or {}).get('metrics', {}).get('gait'),
            'training_plan': training_plan,
            'video_quality': None if unscored(generator.metrics) else _video_quality(video_path),
            'problems': generator.metrics.get('problems', []),
            'suggestions': generator.metrics.get('suggestions', []),
            'report_file': os.path.basename(report_path),
            'report_format': report_format,
            'history': history_result,
            'history_record': saved_rec['id'] if saved_rec else None
        }

        summary.update(measurement_fields(generator.metrics))
        if unscored(generator.metrics):
            summary['metrics'].update({
                'avg_knee_flexion': rounded(generator.metrics.get('avg_knee_flexion')),
                'avg_shin_angle': rounded(generator.metrics.get('avg_shin_angle')),
                'body_leang': rounded(generator.metrics.get('avg_body_leang')),
                'stride_level': '未验证', 'symmetry_level': '未验证', 'running_phase': None,
                'left_knee_angle': None, 'right_knee_angle': None})
            summary['gait'] = None
            summary['video_quality'] = None  # Do not invoke legacy inference in this path.

        _audit('analyze', target=video_filename,
               detail=('高级分析完成：未评分（测量证据不足）' if score_info['total'] is None else f'高级分析完成：{score_info["total"]} 分（{score_info["grade"]}）'))
        summary['analysis_scope'] = SCOPE
        return jsonify(strict_values(summary))
    
    except BudgetExceeded:
        raise
    except SinglePersonVideoRequired as exc:
        _audit('analyze_rejected', target=data.get('video_file', ''), detail=json.dumps(exc.payload(), ensure_ascii=False))
        return jsonify(exc.payload()), 400
    except Exception as e:
        _audit('analyze_failed', target=data.get('video_file', ''), detail=str(e))
        return jsonify({'error': f'分析失败: {str(e)}'}), 500


@app.route('/api/report/<filename>', methods=['GET'])
def get_report(filename):
    """下载报告接口"""
    report_path = authorized_asset_path('report', filename)
    if not report_path or not os.path.isfile(report_path):
        return jsonify({'error': '报告文件不存在'}), 404

    _audit('report_download', target=filename, detail='下载报告')
    return send_file(
        report_path,
        as_attachment=True,
        download_name=filename
    )


@app.route('/api/keyframes/<path:filename>', methods=['GET'])
def get_keyframe(filename):
    """获取关键帧标注图片接口"""
    target_path = authorized_asset_path('keyframes', filename)
    if not target_path or not os.path.isfile(target_path):
        return jsonify({'error': '关键帧图片不存在'}), 404

    return send_file(target_path, as_attachment=True)


@app.route('/api/keyframes/<path:filename>/download', methods=['GET'])
def download_keyframes(filename):
    """下载关键帧标注图片（打包为zip）"""
    keyframes_dir = authorized_asset_path('keyframes', filename)
    if not keyframes_dir or not os.path.isdir(keyframes_dir):
        return jsonify({'error': '关键帧目录不存在'}), 404
    
    # 创建zip文件
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(keyframes_dir):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for file in files:
                if file.endswith(('.jpg', '.png', '.json')) and not os.path.islink(os.path.join(root, file)):
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, keyframes_dir)
                    zip_file.write(file_path, arcname)
    
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{filename}_keyframes.zip"
    )


@app.route('/api/history', methods=['GET'])
def get_history():
    """获取当前用户的历史跑姿记录（新→旧），?limit=&offset= 分页"""
    user = g.current_user
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = max(int(request.args.get('offset', 0)), 0)
    records = history_store.list_for_user(user, limit=limit, offset=offset)
    total = history_store.count_for_user(user)
    return jsonify({
        'status': 'success',
        'total_records': total,
        'records': records
    })


@app.route('/api/history/records', methods=['GET'])
def get_history_records():
    """
    历史记录查询。
    普通用户：仅本人记录（?username= 参数忽略）。
    管理员：?username= 可按用户名模糊查；不传则返回全部用户记录。
    """
    user = g.current_user
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = max(int(request.args.get('offset', 0)), 0)
    search = (request.args.get('username') or '').strip()
    if user['role'] == 'admin':
        records = history_store.list_records(username=search or None, limit=limit, offset=offset)
        total = history_store.count_records(username=search or None)
    else:
        records = history_store.list_for_user(user, limit=limit, offset=offset)
        total = history_store.count_for_user(user)
    return jsonify({
        'status': 'success',
        'is_admin': user['role'] == 'admin',
        'total_records': total,
        'records': records
    })


@app.route('/api/history/users', methods=['GET'])
def get_history_users():
    """管理员：按用户聚合的历史统计（总次数 / 最佳得分 / 最近分析）"""
    if g.current_user['role'] != 'admin':
        return jsonify({'error': '需要管理员权限', 'code': 'forbidden'}), 403
    return jsonify({'status': 'success', 'users': history_store.user_stats()})


@app.route('/api/history/compare', methods=['POST'])
def compare_history():
    """
    历史对比。Body: {"record_ids": [1, 2, 3]}（最多3条，按时间旧→新展示并计算趋势）
    普通用户仅能对比本人记录；管理员可对比任意记录。
    """
    data = request.get_json(silent=True) or {}
    ids = data.get('record_ids') or []
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': '请提供 record_ids 列表（2-3 条记录）'}), 400
    user = g.current_user
    is_admin = user['role'] == 'admin'
    result = history_store.compare(ids, user=user, is_admin=is_admin)
    if not result:
        return jsonify({'error': '没有可对比的记录'}), 400
    if len(result['records']) < 2:
        return jsonify({'error': '至少需要 2 条有效记录进行对比'}), 400
    return jsonify({'status': 'success', **result})


@app.route('/api/history/delete/<int:record_id>', methods=['DELETE'])
def delete_history_record(record_id: int):
    """删除历史数据记录（本人或管理员）"""
    rec = history_store.get_record(record_id)
    if not rec:
        return jsonify({'error': '记录不存在'}), 404
    user = g.current_user
    if not history_store._owns_record(rec, user, user['role'] == 'admin'):
        return jsonify({'error': '无权删除该记录', 'code': 'forbidden'}), 403
    history_store.delete_record(record_id)
    _audit('history_delete', target=str(record_id),
           detail=f'删除历史记录（{rec.get("created_at", "")}，用户 {rec.get("username", "")}）')
    return jsonify({'status': 'deleted', 'record_id': record_id})


@app.route('/api/batch', methods=['POST'])
@limiter.limit("3 per hour")
@bounded_analysis
def batch_process():
    """
    批量处理视频文件接口
    
    Request Body:
        video_files: 视频文件列表（上传后返回的名称）
        user_name: 用户姓名
        coach_name: 评估教练名称
        keyframes: 关键帧数量（默认5）
        report_format: 可选，报告格式 'pdf'（默认）或 'docx'
    
    Returns:
        JSON: 批量分析结果（打包为zip文件下载）
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请提交JSON对象'}), 400
    try:
        video_filenames = unique_batch(data.get('video_files', []))
    except BudgetExceeded as exc:
        return jsonify(error=str(exc), error_code='compute_budget_exceeded'), 422
    if (not isinstance(video_filenames, list) or not video_filenames
            or any(not isinstance(name, str) or not name for name in video_filenames)):
        return jsonify({'error': '请提供视频文件列表'}), 400
    try:
        require_single_person_request(data)
    except ValueError as exc:
        return jsonify({'error': str(exc), 'analysis_scope': SCOPE}), 400

    user_info = {
        'name': data.get('user_name') or current_display_name() or '匿名用户',
        'coach': data.get('coach_name', '系统自动评估'),
        'score': 0
    }
    
    try:
        num_keyframes = parse_keyframe_count(data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    
    results = []
    errors = []
    
    video_paths = {}
    for video_filename in video_filenames:
        path = authorized_asset_path('video', video_filename)
        if not path or not os.path.isfile(path):
            return jsonify({'error': '本批视频存在不可访问文件'}), 404
        video_paths[video_filename] = path

    preflight(list(video_paths.values()), current_budget())

    for i, video_filename in enumerate(video_filenames):
        video_path = video_paths[video_filename]
        
        if not os.path.exists(video_path):
            errors.append({
                'filename': video_filename,
                'error': '文件不存在'
            })
            continue
        
        try:
            # 执行姿态分析
            analyzer = PoseAnalyzer()
            
            try:
                analysis_results = process_single_person_video(analyzer, video_path)
            finally:
                analyzer.close()
            
            if not analysis_results:
                errors.append({
                    'filename': video_filename,
                    'error': '未能识别到人体姿态'
                })
                continue

            # 先提取并保存关键帧标注截图（报告需要嵌入）
            keyframe_analyzer = KeyFrameAnalyzer()
            key_frames = ([] if needs_safe_report(analysis_results) else
                      keyframe_analyzer.extract_key_frames(video_path, num_keyframes))
            unique_id = f"batch_kf_{i}_{uuid.uuid4().hex}"
            keyframes_dir = get_keyframes_path(unique_id)
            save_keyframes(keyframes_dir, key_frames, analysis_results)

            # 生成报告（嵌入关键帧截图）
            report_format = (data.get('report_format') or 'pdf').lower()
            if report_format not in ('pdf', 'docx'):
                report_format = 'pdf'
            generator = ReportGenerator()
            report_path = generator.generate_report(
                video_path,
                user_info,
                analysis_results,
                key_frames_dir=keyframes_dir,
                fmt=report_format
            )

            # 计算综合得分（与报告口径一致）
            score_info = ReportGenerator()._calculate_score(generator.metrics)
            score = score_info['total']

            # 自动写入历史记录
            saved_rec = save_history_record(
                user=getattr(g, 'current_user', None),
                video_file=video_filename,
                report_file=os.path.basename(report_path),
                keyframes_dir=os.path.basename(keyframes_dir),
                analysis_results=analysis_results,
                score=score,
                grade=score_info['grade'],
                problems=generator.metrics.get('problems', []),
                suggestions=generator.metrics.get('suggestions', []),
                coach=user_info['coach'],
                video_path=video_path,
            )

            # 生成训练计划
            training_plan = ReportGenerator()._build_training_plan(
                dict(generator.metrics, gait=(saved_rec or {}).get('metrics', {}).get('gait')))

            results.append({
                'video_filename': video_filename,
                'score': score,
                'grade': score_info['grade'],
                'report_file': os.path.basename(report_path),
                'keyframes_dir': os.path.basename(keyframes_dir),
                'problems': generator.metrics.get('problems', []),
                'suggestions': generator.metrics.get('suggestions', []),
                'training_plan': training_plan,
                **measurement_fields(generator.metrics),
                'metrics': (dict(measurement_fields(generator.metrics),
                                 avg_knee_flexion=rounded(generator.metrics.get('avg_knee_flexion')),
                                 avg_shin_angle=rounded(generator.metrics.get('avg_shin_angle')))
                            if unscored(generator.metrics) else analysis_results[-1])
            })
        
        except BudgetExceeded:
            raise
        except SinglePersonVideoRequired as exc:
            errors.append(dict(filename=video_filename, **exc.payload()))
        except Exception as e:
            errors.append({
                'filename': video_filename,
                'error': str(e)
            })
    
    # 整批原子语义：失败不返回任何部分结果、报告或ZIP。
    if errors:
        return jsonify(error='本批存在失败，整批不发布', successful=0, results=[], errors=errors), 422
    # 构建返回数据
    summary = {
        'status': 'partial' if errors and results else ('completed' if results else 'rejected'),
        'analysis_scope': SCOPE,
        'total_files': len(video_filenames),
        'successful': len(results),
        'failed': len(errors),
        'results': results,
        'errors': errors
    }
    _audit('batch', detail=f'批量处理 {len(video_filenames)} 个视频：成功 {len(results)}，失败 {len(errors)}')
    
    if not results:
        summary['error'] = '没有可分析的视频，请查看各文件的主体确认或处理失败原因'
        summary['analysis_scope'] = SCOPE
        return jsonify(strict_values(summary)), 400

    # 如果需要下载，创建zip文件
    if request.args.get('download') == 'true':
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr('batch_result.json', json.dumps(strict_values(summary), ensure_ascii=False))
            # 添加报告和关键帧
            for result in results:
                report_path = get_report_path(result['report_file'])
                if os.path.exists(report_path):
                    zip_file.write(report_path, f"reports/{result['report_file']}")
                
                keyframes_dir = get_keyframes_path(result['keyframes_dir'])
                if os.path.exists(keyframes_dir):
                    for root, dirs, files in os.walk(keyframes_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = f"keyframes/{os.path.basename(result['keyframes_dir'])}/{file}"
                            zip_file.write(file_path, arcname)
        
        zip_buffer.seek(0)
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"batch_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )
    
    return jsonify(strict_values(summary))


@app.route('/api/delete/<filename>', methods=['POST'])
def delete_video(filename):
    """删除上传的视频文件"""
    video_path = authorized_asset_path('video', filename)
    if video_path and os.path.isfile(video_path):
        os.remove(video_path)
        return jsonify({'status': 'deleted', 'filename': filename})
    return jsonify({'error': '文件不存在'}), 404


@app.route('/api/templates', methods=['GET'])
def get_templates():
    """获取可用的报告模板列表"""
    templates = [
        {
            'id': 'default',
            'name': '标准姿势跑法评估',
            'description': '两页营训模板，二维几何描述；未知项不评分'
        },
        {
            'id': 'simple',
            'name': '简易评估报告',
            'description': '仅包含关键指标和简要建议'
        }
    ]
    return jsonify({'templates': templates})


# ---------- 跑者画像 API ----------

@app.route('/api/profile', methods=['GET'])
def get_profile():
    """获取当前用户跑者画像"""
    user = g.current_user
    profile = profile_store.get_profile(user_id=user['id'])
    if not profile:
        return jsonify({'status': 'empty', 'profile': None})
    summary = profile_engine.get_profile_summary(user_id=user['id'])
    summary.update(leg_length_cm=profile.get('leg_length_cm'), notes=profile.get('notes', ''))
    return jsonify({'status': 'success', 'profile': summary})


@app.route('/api/profile', methods=['POST'])
def save_profile():
    """创建/更新当前用户跑者画像"""
    user = g.current_user
    data = request.get_json(silent=True) or {}
    profile = profile_store.save_profile(
        user_id=user['id'],
        username=user.get('username', ''),
        user_name=user.get('name', ''),
        height_cm=data.get('height_cm'),
        weight_kg=data.get('weight_kg'),
        leg_length_cm=data.get('leg_length_cm'),
        training_level=data.get('training_level', 'intermediate'),
        weekly_km=data.get('weekly_km'),
        years_running=data.get('years_running'),
        injury_history=data.get('injury_history', []),
        goal=data.get('goal', 'injury_prevention'),
        notes=data.get('notes', ''),
    )
    _audit('profile_save', target=user.get('username', ''),
           detail=f'保存跑者画像（{profile.get("training_level", "")}）')
    summary = profile_engine.get_profile_summary(user_id=user['id'])
    summary.update(leg_length_cm=profile.get('leg_length_cm'), notes=profile.get('notes', ''))
    return jsonify({'status': 'success', 'profile': summary})


@app.route('/api/profile/ranges', methods=['GET'])
def get_personalized_ranges():
    """获取个性化参考范围"""
    user = g.current_user
    ranges = profile_engine.get_personalized_ranges(user_id=user['id'])
    focus = profile_engine.get_focus_metrics(user_id=user['id'])
    return jsonify({'status': 'success', 'ranges': ranges, 'focus_metrics': focus})


# ---------- 趋势追踪 API ----------

@app.route('/api/trend', methods=['GET'])
def get_trend():
    """获取当前用户指标趋势（?limit= 最大30条）"""
    user = g.current_user
    limit = min(int(request.args.get('limit', 30)), 100)
    records = history_store.list_for_user(user, limit=limit)
    if not records:
        return jsonify({'status': 'empty', 'trend': {}, 'alerts': [], 'series': None})

    # 按时间 旧→新 排序
    records_asc = list(reversed(records))
    trend = compute_trend(records_asc)
    alerts = extract_alerts(trend)
    series = get_time_series(records_asc, limit=limit)

    return jsonify({
        'status': 'success',
        'total_records': len(records_asc),
        'trend': trend,
        'alerts': alerts,
        'series': series,
    })


@app.route('/api/trend/baseline', methods=['POST'])
def set_trend_baseline():
    """设定基线对比。Body: {record_ids: [1, 2, 3]}（第一条为基线）"""
    data = request.get_json(silent=True) or {}
    ids = data.get('record_ids') or []
    if not ids:
        return jsonify({'error': '请提供 record_ids 列表'}), 400

    user = g.current_user
    is_admin = user['role'] == 'admin'
    records = []
    for rid in ids:
        rec = history_store.get_record(rid)
        if not rec:
            continue
        if not history_store._owns_record(rec, user, is_admin):
            continue
        records.append(rec)

    if len(records) < 2:
        return jsonify({'error': '至少需要 2 条有效记录'}), 400

    result = set_baseline(records)
    return jsonify({'status': 'success', 'baseline': result})


@app.route('/api/trend/compare-ab', methods=['POST'])
def compare_ab_api():
    """A/B 对比。Body: {record_a: id, record_b: id}"""
    data = request.get_json(silent=True) or {}
    id_a = data.get('record_a')
    id_b = data.get('record_b')
    if not id_a or not id_b:
        return jsonify({'error': '请提供 record_a 和 record_b'}), 400

    user = g.current_user
    is_admin = user['role'] == 'admin'
    rec_a = history_store.get_record(id_a)
    rec_b = history_store.get_record(id_b)
    if not rec_a or not rec_b:
        return jsonify({'error': '记录不存在'}), 404
    if not history_store._owns_record(rec_a, user, is_admin) or \
       not history_store._owns_record(rec_b, user, is_admin):
        return jsonify({'error': '无权访问该记录', 'code': 'forbidden'}), 403

    result = compare_ab(rec_a, rec_b)
    return jsonify({'status': 'success', 'comparison': result})


@app.route('/api/trend/progress', methods=['GET'])
def get_training_progress():
    """训练进度追踪：对比历次分析验证改进效果"""
    user = g.current_user
    records = history_store.list_for_user(user, limit=50)
    if not records:
        return jsonify({'status': 'empty', 'progress': None})
    progress = track_progress(records)
    return jsonify({'status': 'success', 'progress': progress})


# ---------- 训练联动 API ----------

@app.route('/api/training/session', methods=['POST'])
def get_training_session():
    """生成训练联动方案。Body: {record_id: id}（基于某条分析记录）"""
    data = request.get_json(silent=True) or {}
    rid = data.get('record_id')
    if not rid:
        return jsonify({'error': '请提供 record_id'}), 400

    user = g.current_user
    is_admin = user['role'] == 'admin'
    rec = history_store.get_record(rid)
    if not rec:
        return jsonify({'error': '记录不存在'}), 404
    if not history_store._owns_record(rec, user, is_admin):
        return jsonify({'error': '无权访问该记录', 'code': 'forbidden'}), 403

    metrics = {
        'avg_knee_flexion': rec.get('avg_knee'),
        'avg_shin_angle': rec.get('avg_shin'),
        'avg_body_leang': rec.get('body_lean'),
        'stride_analysis': {'stride_distance': None, 'stride_level': rec.get('stride_level', '')},
        'symmetry': {'symmetry_score': None, 'symmetry_level': rec.get('symmetry_level', '')},
        'measurement_status': (rec.get('metrics') or {}).get('measurement_status', 'insufficient_evidence'),
    }
    gait = (rec.get('metrics') or {}).get('gait')
    profile = profile_store.get_profile(user_id=user['id'])

    session = build_training_session(
        metrics=metrics, gait=gait, profile=profile, problems=rec.get('problems', []))
    return jsonify({'status': 'success', 'session': session})


# ---------- 数据导出 API ----------

@app.route('/api/export/csv', methods=['GET'])
def export_csv():
    """导出当前用户历史记录为 CSV（管理员可 ?username= 导出指定用户）"""
    user = g.current_user
    limit = min(int(request.args.get('limit', 200)), 500)
    search = (request.args.get('username') or '').strip()

    if user['role'] == 'admin' and search:
        records = history_store.list_records(username=search, limit=limit)
    else:
        records = history_store.list_for_user(user, limit=limit)

    if not records:
        return jsonify({'error': '没有可导出的记录'}), 404

    csv_str, filename = export_records_csv(records)
    _audit('export_csv', target=user.get('username', ''),
           detail=f'导出 {len(records)} 条记录为 CSV')
    return (csv_str, 200, {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': f'attachment; filename={filename}',
    })


@app.route('/api/export/json', methods=['GET'])
def export_json():
    """导出当前用户历史记录为 JSON（管理员可 ?username= 导出指定用户）"""
    user = g.current_user
    limit = min(int(request.args.get('limit', 200)), 500)
    search = (request.args.get('username') or '').strip()

    if user['role'] == 'admin' and search:
        records = history_store.list_records(username=search, limit=limit)
    else:
        records = history_store.list_for_user(user, limit=limit)

    if not records:
        return jsonify({'error': '没有可导出的记录'}), 404

    json_str, filename = export_records_json(records)
    _audit('export_json', target=user.get('username', ''),
           detail=f'导出 {len(records)} 条记录为 JSON')
    return (json_str, 200, {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Disposition': f'attachment; filename={filename}',
    })


@app.route('/api/export/record/<int:record_id>', methods=['GET'])
def export_record(record_id):
    """导出单条记录为 JSON"""
    user = g.current_user
    is_admin = user['role'] == 'admin'
    rec = history_store.get_record(record_id)
    if not rec:
        return jsonify({'error': '记录不存在'}), 404
    if not history_store._owns_record(rec, user, is_admin):
        return jsonify({'error': '无权访问该记录', 'code': 'forbidden'}), 403

    json_str = export_single_record_json(rec)
    return (json_str, 200, {
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Disposition': f'attachment; filename=poserun_record_{record_id}.json',
    })


# 错误处理
@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': '文件过大，最大支持100MB'}), 413


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({'error': '请求过于频繁，请稍后再试', 'code': 'rate_limited'}), 429


@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': '服务器内部错误'}), 500


# 启动服务
if __name__ == '__main__':
    print("=" * 50)
    print("PoseRun Analyzer 启动中...")
    print("=" * 50)
    
    # 创建必要的文件夹
    for folder in [UPLOAD_FOLDER, REPORTS_FOLDER, KEYFRAMES_FOLDER]:
        os.makedirs(folder, exist_ok=True)
    
    # 启动数据自动备份（启动时立即备份一次 + 每日循环）
    backup_mod.init_backup()
    
    # HTTPS 服务（摄像头 getUserMedia 需要安全上下文）
    # 自签证书：首次访问需在浏览器点“继续访问/高级→继续前往”
    ssl_context = None
    cert_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'certs', 'server.crt')
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'certs', 'server.key')
    if os.path.exists(cert_path) and os.path.exists(key_path):
        ssl_context = (cert_path, key_path)
        import threading
        from werkzeug.serving import make_server
        def _run_https():
            srv = make_server('0.0.0.0', 8443, app, ssl_context=ssl_context)
            print("HTTPS 服务启动: https://<host>:8443/ （摄像头页面请用此地址）")
            srv.serve_forever()
        t = threading.Thread(target=_run_https, daemon=True)
        t.start()
    
    app.run(
        host='0.0.0.0',
        port=8080,
        debug=False
    )