"""Opt-in offline report/API contract. Does not patch production or score gait.

Coverage denominators must include failed detection frames, not only returned
poses. A missing denominator means coverage is unknown, never implicitly 100%.
"""
from __future__ import annotations
import argparse
import html
import json
import math
from numbers import Real
from pathlib import Path
from statistics import mean

METRICS = {
    'knee_flexion': ('双侧平均膝角（二维）', '°', 0, 180),
    'shin_angle': ('双侧平均小腿与地面夹角（二维）', '°', 0, 90),
    'body_leang': ('躯干倾角（二维投影）', '°', 0, 180),
}
UNVALIDATED = {
    'cadence': '触地步频尚未验证；独立周期估计不等于接触测量',
    'ground_contact_time': '尚无独立触地/离地标注',
    'stride_length': '跟拍画面未经地面尺度标定',
    'foot_contact': 'COCO17无脚跟/足尖，无法确定触地类型',
    'symmetry': '尚未验证接触侧与相位',
    'running_phase': '尚未验证触地/离地事件',
}
from release_info import NOTICE


def finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def build_payload(frames, *, expected_frames=None):
    if not isinstance(frames, list) or any(not isinstance(r, dict) for r in frames):
        raise ValueError('frames must be a list of frame objects')
    if expected_frames is not None:
        if (isinstance(expected_frames, bool) or not isinstance(expected_frames, int)
                or expected_frames < len(frames)):
            raise ValueError('expected_frames must be an integer >= returned frame count')
    for r in frames:
        if r.get('measurement_status') != 'unscored_2d_geometry_only':
            raise ValueError('only explicit offline candidate frames are accepted')
    # Require unique, valid frame identities if a timeline denominator is supplied.
    if expected_frames is not None and frames:
        indices = [r.get('frame_index') for r in frames]
        if any(not finite(i) or int(i) != i or i < 0 or i >= expected_frames for i in indices):
            raise ValueError('frame_index must be within the expected timeline')
        if len(set(indices)) != len(indices):
            raise ValueError('duplicate frame_index')
    metrics = {}
    for key, (label, unit, low, high) in METRICS.items():
        values = [float(r[key]) for r in frames
                  if finite(r.get(key)) and low <= r[key] <= high]
        value = mean(values) if values else None
        metrics[key] = {
            'label': label, 'unit': unit, 'value': value,
            'status': 'available_2d_only' if values else 'unknown',
            'reason': None if values else '缺少有限、有效范围内的二维测量',
            'valid_frames': len(values),
            'expected_frames': expected_frames,
            'coverage': len(values) / expected_frames if expected_frames else None,
            'score': None,
        }
    labels = {'cadence': '触地步频', 'ground_contact_time': '接地时间',
              'stride_length': '米制步幅', 'foot_contact': '触地类型',
              'symmetry': '左右对称性', 'running_phase': '跑步相位'}
    for key, reason in UNVALIDATED.items():
        metrics[key] = {'label': labels[key], 'value': None, 'status': 'unvalidated',
                        'reason': reason, 'score': None}
    return {
        'schema_version': 'offline-measurement-v1', 'measurement_status': 'unscored_2d_geometry_only',
        'notice': NOTICE, 'returned_frames': len(frames), 'expected_frames': expected_frames,
        'metrics': metrics, 'score': None, 'grade': None, 'breakdown': [],
        'problems': [], 'suggestions': [],
    }


def display(value, unit=''):
    return f'{value:.1f}{unit}' if finite(value) else '未知／未验证'


def report_rows(payload):
    rows = []
    for key, metric in payload['metrics'].items():
        coverage = metric.get('coverage')
        coverage_text = f'{coverage:.1%}' if finite(coverage) else '未知'
        rows.append((metric.get('label', key), display(metric['value'], metric.get('unit', '')),
                     coverage_text, metric.get('reason') or '仅二维描述，不作触地相位或评分解释'))
    return rows


def render_html(payload):
    rows = ''.join('<tr>' + ''.join('<td>' + html.escape(cell) + '</td>' for cell in row) + '</tr>'
                   for row in report_rows(payload))
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>离线测量报告</title>'
            '<style>body{font:16px sans-serif;margin:2em}td,th{border:1px solid #aaa;padding:.6em}'
            'table{border-collapse:collapse}p{max-width:70em}</style><h1>跑姿二维测量报告</h1>'
            '<p>' + html.escape(payload['notice']) + '</p><p>总分：未知／未验证；等级：不评级</p>'
            '<table><tr><th>指标</th><th>结果</th><th>有效帧覆盖率</th><th>边界／原因</th></tr>'
            + rows + '</table></html>')


def export_reports(payload, prefix, *, user_info=None, keyframes=None):
    """Write JSON/HTML measurements and sample-layout DOCX/PDF reports."""

    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    paths = {ext: Path(str(prefix) + '.' + ext) for ext in ('json', 'html', 'docx', 'pdf')}
    paths['json'].write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    paths['html'].write_text(render_html(payload), encoding='utf-8')
    from camp_report import build_camp_data, export_camp_report
    data = build_camp_data(payload, user_info)
    evidence = '\n'.join('；'.join(row) for row in report_rows(payload))
    data['notice'] += '\n总分：未知／未验证；等级：不评级\n测量依据（覆盖率不是准确率）：\n' + evidence
    for fmt in ('docx', 'pdf'):
        export_camp_report(data, keyframes or [], paths[fmt], fmt=fmt)
    return {key: str(value) for key, value in paths.items()}


def create_preview_app():
    """Isolated Flask app for test-client integration; never auto-starts a server."""
    from flask import Flask, jsonify, request
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024

    @app.post('/offline/report-preview')
    def preview():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='JSON object required'), 400
        try:
            payload = build_payload(data.get('frames'), expected_frames=data.get('expected_frames'))
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        # Strict serialization: no JavaScript NaN/Infinity values in the API.
        return app.response_class(json.dumps(payload, ensure_ascii=False, allow_nan=False),
                                  mimetype='application/json')
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('frames', type=Path)
    parser.add_argument('--expected-frames', type=int)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    payload = build_payload(json.loads(args.frames.read_text()), expected_frames=args.expected_frames)
    print(json.dumps(export_reports(payload, args.output), ensure_ascii=False))
