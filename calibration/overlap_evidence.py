"""Offline pairwise pose evidence; NEVER merges detections or verifies identity.

Thresholds only define a conservative review screen, not validated duplicate
classification. Image-pixel geometry avoids independent x/W, y/H distortion.
"""
import math
from numbers import Real
from target_tracker import iou, valid_box

BODY_JOINTS = tuple(range(5, 17))  # COCO shoulders through ankles; no face.
POLICY = {'point_confidence': .5, 'box_iou': .7, 'max_joint_distance_over_min_box_diagonal': .03,
          'required_body_joints': list(BODY_JOINTS), 'automatic_merge': False,
          'identity_verified': False}
BONES = ((5, 11), (6, 12), (11, 13), (13, 15), (12, 14), (14, 16))


def finite(x):
    return isinstance(x, Real) and not isinstance(x, bool) and math.isfinite(x)


def compare(a, b, width, height):
    result = {'screen': 'insufficient_evidence', 'identity_relation': 'unknown',
              'automatic_merge': False, 'box_iou': None, 'joint_distances': [],
              'confident_body_joint_count': 0, 'max_normalized_distance': None}
    if not all(finite(x) and x > 0 for x in (width, height)):
        return dict(result, reason='invalid_image_dimensions')
    if not all(isinstance(d, dict) and valid_box(d.get('box', [])) for d in (a, b)):
        return dict(result, reason='invalid_box')
    result['box_iou'] = float(iou(a['box'], b['box']))
    for d in (a, b):
        if (not isinstance(d.get('xy'), list) or len(d['xy']) != 17
                or not isinstance(d.get('confidence'), list) or len(d['confidence']) != 17):
            return dict(result, reason='missing_pose_arrays')
    scale = min(math.hypot((d['box'][2]-d['box'][0])*width,
                           (d['box'][3]-d['box'][1])*height) for d in (a, b))
    valid = set()
    for j in BODY_JOINTS:
        points = [d['xy'][j] for d in (a, b)]
        conf = [d['confidence'][j] for d in (a, b)]
        ok = (all(isinstance(p, list) and len(p) == 2 and all(finite(v) for v in p)
                  and 0 <= p[0] <= width and 0 <= p[1] <= height for p in points)
              and all(finite(c) and POLICY['point_confidence'] <= c <= 1 for c in conf))
        error = math.dist(*points)/scale if ok else None
        if ok:
            valid.add(j)
        result['joint_distances'].append({'joint_index': j, 'distance_over_min_box_diagonal': error,
                                          'both_confident_and_in_bounds': ok})
    result['confident_body_joint_count'] = len(valid)
    if len(valid) != len(BODY_JOINTS):
        return dict(result, reason='missing_or_low_confidence_body_joints')
    if any(math.dist(d['xy'][i], d['xy'][j]) <= 0 for d in (a, b) for i, j in BONES):
        return dict(result, reason='degenerate_body_segments')
    maximum = max(row['distance_over_min_box_diagonal'] for row in result['joint_distances'])
    result['max_normalized_distance'] = maximum
    if result['box_iou'] < POLICY['box_iou']:
        return dict(result, screen='geometric_disagreement', reason='boxes_not_close_enough')
    if maximum > POLICY['max_joint_distance_over_min_box_diagonal']:
        return dict(result, screen='geometric_disagreement', reason='confident_joints_disagree')
    return dict(result, screen='close_projection_only', reason='close_2d_projection_is_not_identity_proof')
