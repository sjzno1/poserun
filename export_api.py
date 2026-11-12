"""
PoseRun Analyzer - 数据导出与 API 模块
- CSV/JSON 导出
- REST API 端点
"""

import csv
import io
import json
from datetime import datetime


def export_records_csv(records):
    """将历史记录导出为 CSV。返回 (csv_string, filename)"""
    if not records:
        return '', 'empty.csv'

    # 收集所有字段
    fieldnames = [
        'id', 'record_id', 'username', 'user_name', 'coach',
        'created_at', 'score', 'grade',
        'avg_knee', 'avg_shin', 'body_lean',
        'stride_level', 'symmetry_level', 'running_phase',
        'cadence', 'total_frames',
        'video_file', 'report_file',
    ]

    # gait 子字段
    gait_fields = [
        'gait_vertical_oscillation', 'gait_ground_contact_ms',
        'gait_stride_length_m', 'gait_step_symmetry',
        'gait_cadence_level', 'gait_over_stride_level',
    ]
    fieldnames.extend(gait_fields)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()

    for r in records:
        row = dict(r)
        gait = (r.get('metrics') or {}).get('gait') or {}
        row['gait_vertical_oscillation'] = gait.get('vertical_oscillation')
        row['gait_ground_contact_ms'] = gait.get('ground_contact_time_ms')
        row['gait_stride_length_m'] = gait.get('stride_length_m')
        row['gait_step_symmetry'] = gait.get('step_symmetry')
        row['gait_cadence_level'] = gait.get('cadence_level')
        row['gait_over_stride_level'] = gait.get('strike_posture', {}).get('over_stride_level')
        writer.writerow(row)

    csv_str = output.getvalue()
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'poserun_history_{ts}.csv'
    return csv_str, filename


def export_records_json(records, include_gait=True):
    """将历史记录导出为 JSON。返回 (json_string, filename)"""
    data = {
        'exported_at': datetime.now().isoformat(),
        'total_records': len(records),
        'records': [],
    }

    for r in records:
        rec = {
            'id': r.get('id'),
            'record_id': r.get('record_id'),
            'username': r.get('username'),
            'user_name': r.get('user_name'),
            'coach': r.get('coach'),
            'created_at': r.get('created_at'),
            'score': r.get('score'),
            'grade': r.get('grade'),
            'metrics': {
                'avg_knee': r.get('avg_knee'),
                'avg_shin': r.get('avg_shin'),
                'body_lean': r.get('body_lean'),
                'stride_level': r.get('stride_level'),
                'symmetry_level': r.get('symmetry_level'),
                'running_phase': r.get('running_phase'),
                'cadence': r.get('cadence'),
            },
            'problems': r.get('problems', []),
            'suggestions': r.get('suggestions', []),
            'video_file': r.get('video_file'),
            'report_file': r.get('report_file'),
        }
        if include_gait:
            gait = (r.get('metrics') or {}).get('gait')
            if gait:
                rec['gait'] = gait
        data['records'].append(rec)

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'poserun_history_{ts}.json'
    return json_str, filename


def export_single_record_json(record, include_gait=True):
    """导出单条记录为 JSON"""
    rec = {
        'id': record.get('id'),
        'record_id': record.get('record_id'),
        'username': record.get('username'),
        'user_name': record.get('user_name'),
        'coach': record.get('coach'),
        'created_at': record.get('created_at'),
        'score': record.get('score'),
        'grade': record.get('grade'),
        'metrics': {
            'avg_knee': record.get('avg_knee'),
            'avg_shin': record.get('avg_shin'),
            'body_lean': record.get('body_lean'),
            'stride_level': record.get('stride_level'),
            'symmetry_level': record.get('symmetry_level'),
            'running_phase': record.get('running_phase'),
            'cadence': record.get('cadence'),
        },
        'problems': record.get('problems', []),
        'suggestions': record.get('suggestions', []),
        'video_file': record.get('video_file'),
        'report_file': record.get('report_file'),
        'keyframes_dir': record.get('keyframes_dir'),
    }
    if include_gait:
        gait = (record.get('metrics') or {}).get('gait')
        if gait:
            rec['gait'] = gait

    return json.dumps(rec, ensure_ascii=False, indent=2)
