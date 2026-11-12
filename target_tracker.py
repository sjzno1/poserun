"""Explicit user-seeded geometric/colour association, NOT identity recognition.

A missing, ambiguous, discontinuous or visually incompatible observation is
terminal for this video. Never auto-reacquire or choose a new first person.
"""
import math
from numbers import Real
import cv2
import numpy as np

MIN_BOX_SPAN = .02
MIN_SEED_IOU = .5
MIN_TRACK_IOU = .3
MIN_IOU_MARGIN = .15
MAX_APPEARANCE_DISTANCE = .45
MAX_SCENE_DIFFERENCE = .25


def valid_box(box, min_span=0.):
    return (isinstance(box, (list, tuple, np.ndarray)) and len(box) == 4
            and all(isinstance(v, Real) and not isinstance(v, (bool, np.bool_)) and math.isfinite(v) for v in box)
            and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1
            and box[2]-box[0] >= min_span and box[3]-box[1] >= min_span)


def validate_selection(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'frame_index', 'box'}
            or type(value['frame_index']) is not int or value['frame_index'] != 0
            or not valid_box(value['box'], MIN_BOX_SPAN)):
        raise ValueError('target_selection须为首帧frame_index=0及归一化xyxy全身框；宽高至少0.02')
    return {'frame_index': 0, 'box': list(value['box'])}


def iou(a, b):
    x = max(0., min(a[2], b[2])-max(a[0], b[0]))
    y = max(0., min(a[3], b[3])-max(a[1], b[1]))
    intersection = x*y
    union = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
    return intersection/union if union > 0 else 0.


def appearance(frame, box):
    """Central torso HSV histogram, no face or biometric embedding."""
    if not valid_box(box):
        return None
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    dx, dy = x2-x1, y2-y1
    left, right = int((x1+.25*dx)*w), int((x2-.25*dx)*w)
    top, bottom = int((y1+.25*dy)*h), int((y1+.55*dy)*h)
    if right-left < 4 or bottom-top < 4:
        return None
    hsv = cv2.cvtColor(frame[top:bottom, left:right], cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [12, 8], [0, 180, 0, 256]).reshape(-1)
    total = float(hist.sum())
    return hist/total if total > 0 else None


def distance(a, b):
    if a is None or b is None or a.shape != b.shape:
        return None
    coefficient = float(np.sqrt(a*b).sum())
    return math.sqrt(max(0., 1.-min(1., coefficient)))


def scene_signature(frame):
    return cv2.resize(frame, (32, 18)).astype(np.float32)/255.


class SelectedTargetTracker:
    def __init__(self, selection):
        self.selection = validate_selection(selection)
        if self.selection is None:
            raise ValueError('explicit selection required')
        self.state = 'waiting_for_seed'
        self.reason = None
        self.box = None
        self.seed_appearance = None
        self.last_appearance = None
        self.last_scene = None
        self.last_index = None
        self.accepted_frames = 0
        self.first_lost_frame = None
        self.last_selected_index = None

    def lose(self, reason, frame_index=None):
        if self.state != 'lost':
            self.state, self.reason = 'lost', reason
            self.first_lost_frame = frame_index
        self.last_selected_index = None
        return None

    def select(self, boxes, frame, frame_index):
        self.last_selected_index = None
        if self.state == 'lost':
            return None
        if type(frame_index) is not int or frame_index < 0:
            return self.lose('lost_invalid_frame_index', frame_index)
        if self.last_index is None:
            if frame_index != 0:
                return self.lose('lost_seed_frame_unavailable', frame_index)
        elif frame_index != self.last_index+1:
            return self.lose('lost_noncontiguous_frames', frame_index)
        current_scene = scene_signature(frame)
        if self.last_scene is not None and float(np.mean(np.abs(current_scene-self.last_scene))) > MAX_SCENE_DIFFERENCE:
            return self.lose('lost_scene_discontinuity', frame_index)
        reference = self.selection['box'] if self.state == 'waiting_for_seed' else self.box
        threshold = MIN_SEED_IOU if self.state == 'waiting_for_seed' else MIN_TRACK_IOU
        candidates = [(iou(reference, box), index, [float(value) for value in box]) for index, box in enumerate(boxes)
                      if valid_box(box)]
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates or candidates[0][0] < threshold:
            return self.lose('lost_no_matching_detection', frame_index)
        if len(candidates) > 1 and candidates[0][0]-candidates[1][0] < MIN_IOU_MARGIN:
            return self.lose('lost_ambiguous_detections', frame_index)
        score, index, box = candidates[0]
        if self.state == 'tracking':
            rw, rh = reference[2]-reference[0], reference[3]-reference[1]
            bw, bh = box[2]-box[0], box[3]-box[1]
            shift = math.hypot(((box[0]+box[2])-(reference[0]+reference[2]))/(2*rw),
                               ((box[1]+box[3])-(reference[1]+reference[3]))/(2*rh))
            if not (.6 <= bw/rw <= 1/.6 and .6 <= bh/rh <= 1/.6) or shift > .35:
                return self.lose('lost_motion_or_scale_jump', frame_index)
        signature = appearance(frame, box)
        if signature is None:
            return self.lose('lost_no_torso_appearance', frame_index)
        if self.state == 'tracking':
            distances = [distance(signature, old) for old in (self.seed_appearance, self.last_appearance)]
            if any(value is None or value > MAX_APPEARANCE_DISTANCE for value in distances):
                return self.lose('lost_appearance_mismatch', frame_index)
        else:
            self.seed_appearance = signature.copy()
        self.state = 'tracking'
        self.box, self.last_appearance, self.last_scene = box, signature.copy(), current_scene
        self.last_index, self.last_selected_index = frame_index, index
        self.accepted_frames += 1
        return index

    def metadata(self):
        return {'policy': 'explicit_seed_iou_torso_colour_latched_loss_v1',
                'selection': self.selection, 'state': self.state, 'lost_reason': self.reason,
                'first_lost_frame': self.first_lost_frame, 'associated_frames': self.accepted_frames,
                'identity_verified': False, 'automatic_reacquisition': False,
                'thresholds': {'seed_iou': MIN_SEED_IOU, 'track_iou': MIN_TRACK_IOU,
                               'iou_margin': MIN_IOU_MARGIN, 'appearance_distance': MAX_APPEARANCE_DISTANCE,
                               'scene_difference': MAX_SCENE_DIFFERENCE},
                'limitations': ['Geometric/torso-colour association, not verified identity',
                                'Similar clothing, occlusion and undetected cuts may defeat gates',
                                'Loss requires a new explicit selection, never automatic reassignment']}
