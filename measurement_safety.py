"""Evidence gates shared by reports, API/history and training consumers."""
from numbers import Real
import math


def finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def rounded(value, places=1):
    return round(float(value), places) if finite(value) else None


def unscored(metrics):
    status = (metrics or {}).get('measurement_status', '')
    return isinstance(status, str) and (status.startswith('unscored') or status == 'insufficient_evidence')


def needs_safe_report(frames):
    """Fail closed: neither current analyzer has a validated scoring contract.

    This is deliberate for legacy AND candidate inputs. Finite geometry must
    not silently enable old fixed scores, contact diagnoses or training plans.
    """
    return True


def safe_metrics(frames):
    from measurement_report import build_payload
    candidate = bool(frames) and all(row.get('measurement_status') == 'unscored_2d_geometry_only'
                                     for row in frames)
    # The adapter's input marker is internal only. Preserve the real provenance
    # below, and explicitly distinguish malformed legacy measurements.
    normalized: list[dict] = [dict(row, measurement_status='unscored_2d_geometry_only') for row in frames]
    for row in normalized:
        if row.get('pose_detected') is False:
            row.update(knee_flexion=None, shin_angle=None, body_leang=None)
    if not candidate:
        # Legacy geometry was produced by the uncalibrated measurement chain.
        for row in normalized:
            row.update(knee_flexion=None, shin_angle=None, body_leang=None)
    timeline = None
    expected = None
    if candidate and frames:
        first = frames[0].get('video_timeline')
        if isinstance(first, dict) and all(row.get('video_timeline') == first for row in frames):
            timeline = dict(first)
            n = len(frames)
            if (first.get('schema_version') == 'decoded-timeline-v1'
                    and type(first.get('decoded_frames')) is int and first['decoded_frames'] == n
                    and type(first.get('declared_frames')) is int and first['declared_frames'] == n
                    and type(first.get('coverage_expected_frames')) is int
                    and first['coverage_expected_frames'] == n and first.get('count_matches') is True
                    and [row.get('frame_index') for row in frames] == list(range(n))
                    and all(type(row.get('frame_index')) is int for row in frames)):
                expected = n
    payload = build_payload(normalized, expected_frames=expected)
    if timeline is not None:
        payload['video_timeline'] = timeline
    has_geometry = any(payload['metrics'][key]['value'] is not None
                       for key in ('knee_flexion', 'shin_angle', 'body_leang'))
    status = 'unscored_2d_geometry_only' if candidate and has_geometry else 'insufficient_evidence'
    payload['measurement_status'] = status
    limitations = ['未验证触地相位、触地类型及力学指标，不评分、不评级，不生成训练处方。',
                   '角度仅为通过门控的帧的二维描述，不能代表被拒绝的帧或整段跑姿。']
    if expected is None:
        limitations.append('完整解码时间线缺失或计数不一致：覆盖率未知，不以检测成功帧充当分母。')
    else:
        limitations.append('覆盖率分母为全部已解码帧，包含未识别/拒绝的人体帧；不是定位准确率，计数一致不证明原片无损完整。')
    if timeline is not None:
        tracking = timeline.get('subject_selection')
        if isinstance(tracking, dict):
            limitations.append('自动选择并连续关联唯一主跑者，允许可区分的背景人物；对应歧义或主体丢失时整段拒绝，不自动换人。')
            limitations.append('前景大小、位置及外观关联是保守启发式，不是人脸识别或经验证身份；相似服装、交叉遮挡、背景漏检和切镜需独立验收。')
        else:
            limitations.append('缺少当前自动主体关联元数据；主体身份与关联准确率未经独立验证。')
        limitations.append('容器帧率不等于经认证的采集时钟；播放倍率未知时不能据此校准步频或接地时间。')
    if not candidate:
        limitations.append('原测量链未通过校准验收，其二维角度暂不作为可信测量发布。')
    payload['notice'] = '无评分二维几何报告。' + ''.join(limitations)
    metric = payload['metrics']
    return {
        'measurement_status': status, 'measurement_limitations': limitations,
        'video_timeline': timeline,
        'measurement_coverage': {key: value for key, value in metric.items() if 'valid_frames' in value},
        'measurement_payload': payload,
        'avg_knee_flexion': metric['knee_flexion']['value'],
        'avg_shin_angle': metric['shin_angle']['value'],
        'avg_body_leang': metric['body_leang']['value'],
        'stride_analysis': {'stride_distance': None, 'stride_level': '未验证'},
        'symmetry': {'symmetry_score': None, 'symmetry_level': '未验证'},
        'hip_stability': None, 'breakdown': [], 'total': None, 'grade': None,
        'problems': [], 'suggestions': [],
    }


def unavailable_training():
    return {'status': 'insufficient_evidence', 'focus_areas': [], 'weaknesses': [],
            'drills': [], 'weekly_plan': [], 'strength': [],
            'notes': ['证据不足：不根据未验证指标生成训练建议或增加训练量。']}


def measurement_fields(metrics):
    return {key: metrics[key] for key in ('measurement_status', 'measurement_limitations',
                                         'measurement_coverage', 'video_timeline') if key in metrics}


def strict_values(value):
    """Recursively convert nonfinite numeric values to JSON null (keep valid 0)."""
    if isinstance(value, dict):
        return {key: strict_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [strict_values(item) for item in value]
    if isinstance(value, Real) and not isinstance(value, bool):
        return value if finite(value) and isinstance(value, (int, float)) else (float(value) if finite(value) else None)
    return value
