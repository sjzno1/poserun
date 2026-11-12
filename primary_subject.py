"""Conservative automatic ONE-subject continuity, not depth or identity inference.

All thresholds below are explicit unvalidated heuristics, not calibrated accuracy
claims. Detection confidence remains 0.05 at the model boundary. Visible torso
confidence (0.5) is a separate evidence gate, never a way to discard competing
boxes. Invalid boxes/data fail closed. A small, size-disjoint competitor may
remain unmeasured; its full pose is not required to measure the primary subject.
No duplicate merging or re-identification.
A bbox may be derived from visible finite landmarks ONLY for a single candidate
when boxes are absent (legacy model fixtures); malformed supplied boxes never
fall back. Missing initial observations remain unknown; loss after lock is final.
"""
from __future__ import annotations

import cv2
import numpy as np

TORSO = [5, 6, 11, 12]
VISIBLE_CONFIDENCE = .5
MIN_VISIBLE = 8
DOMINANT_AREA_RATIO = 2.5
DOMINANT_HEIGHT_RATIO = 1.35
MAX_COMPETING_IOU = .20
MAX_OVERLAP_FRACTION = .55
MIN_TORSO_SEPARATION = .6
MAX_CENTER_MOTION = .12  # fraction of previous bbox height per decoded frame
MAX_SCALE_RATIO = 1.20
MAX_TORSO_DISPLACEMENT = .10  # previous bbox height, corresponding joint labels
MAX_APPEARANCE_CHANGE = .18  # normalized torso-patch mean absolute change
MAX_SCENE_CHANGE = .30  # normalized thumbnail mean absolute change


def array(value):
    """Accept numpy and CPU/GPU tensor model outputs without tensor state leaks."""
    if value is None:
        return None
    if hasattr(value, 'detach'):
        value = value.detach().cpu().numpy()
    return np.asarray(value, dtype=float)


def area(box):
    return float((box[2]-box[0])*(box[3]-box[1]))


def overlap(a, b):
    intersection = max(0., min(a[2], b[2])-max(a[0], b[0])) * max(0., min(a[3], b[3])-max(a[1], b[1]))
    return intersection/(area(a)+area(b)-intersection), intersection/min(area(a), area(b))


class PrimarySubjectSelector:
    def __init__(self):
        self.state = 'waiting'
        self.reason = 'no_pose'
        self.status = 'no_pose'
        self.previous = None
        self.first_locked_frame = None
        self.accepted_frames = 0
        self.initial_unknown_frames = 0
        self.box_source = None
        self.initial_box = None
        self.masked_joints = []
        self.overlap_frames = 0
        self.boundary_frames = 0
        self.masked_frames = 0
        self.suppressed_joint_observations = 0
        self.last_input_index = None
        self.velocity = None
        self.pending_outliers = 0
        self.outlier_frame_indices = []

    def metadata(self):
        return {'mode': 'automatic_primary_runner',
                'heuristic_unvalidated': True,
                'initial_frame_index': self.first_locked_frame,
                'initial_box': list(self.initial_box) if self.initial_box is not None else None,
                'selected_frame_count': self.accepted_frames,
                'threshold_validation': 'heuristic_unvalidated',
                'no_identity_verified': True, 'identity_verified': False,
                'depth_verified': False, 'automatic_reacquisition': False,
                'state': self.state, 'reason': self.reason,
                'first_locked_frame': self.first_locked_frame,
                'accepted_frames': self.accepted_frames,
                'initial_unknown_frames': self.initial_unknown_frames,
                'box_source': self.box_source,
                'projection_overlap_frames': self.overlap_frames,
                'boundary_joint_frames': self.boundary_frames,
                'measurement_masked_frames': self.masked_frames,
                'suppressed_joint_observations': self.suppressed_joint_observations,
                'overlap_policy': 'unique_track_all_competitors_retained_overlapping_joint_measurements_masked',
                'pose_outlier_frame_indices': list(self.outlier_frame_indices),
                'pending_outlier_frames': self.pending_outliers,
                'max_pose_outlier_frames': 2,
                'max_pose_outlier_confirmation_seconds': .1,
                'outlier_policy': 'unknown_no_measurement_require_same_track_motion_prediction'}

    def fail(self, reason):
        self.status = 'primary_subject_lost' if self.state == 'locked' else 'primary_subject_ambiguous'
        self.state, self.reason = 'failed', reason
        # Do not retain image/pose evidence after terminal loss.
        self.previous = None
        return None

    def _unknown(self, reason):
        if self.state == 'locked':
            return self.fail(reason)
        self.status, self.reason = 'primary_subject_unknown', reason
        self.initial_unknown_frames += 1
        return None

    def _candidate(self, xy, conf, box, frame, source):
        h, w = frame.shape[:2]
        if xy.shape != (17, 2) or conf.shape != (17,):
            return None
        if not np.isfinite(conf).all() or np.any((conf < 0) | (conf > 1)):
            return None
        valid = np.isfinite(xy).all(axis=1) & (xy[:, 0] >= 0) & (xy[:, 0] <= w) & (xy[:, 1] >= 0) & (xy[:, 1] <= h)
        visible = valid & (conf >= VISIBLE_CONFIDENCE)
        if visible.sum() < MIN_VISIBLE or not visible[TORSO].all():
            return None
        if box is None:
            box = np.r_[xy[visible].min(axis=0), xy[visible].max(axis=0)]
        box = np.asarray(box, dtype=float)
        if box.shape != (4,) or not np.isfinite(box).all():
            return None
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h and x2-x1 >= 4 and y2-y1 >= 8):
            return None
        torso = xy[TORSO]
        if not ((torso[:, 0] >= x1) & (torso[:, 0] <= x2) & (torso[:, 1] >= y1) & (torso[:, 1] <= y2)).all():
            return None
        shoulder, hip = torso[:2].mean(axis=0), torso[2:].mean(axis=0)
        length = np.linalg.norm(hip-shoulder)
        # Bilateral shoulder/hip points can coincide in a genuine side view.
        # Their 2D separation is not an identity-validity requirement.
        if length < max(4., .12*(y2-y1)):
            return None
        # Spatial torso patch captures limited clothing/appearance correspondence;
        # it is neither a face descriptor nor identity verification.
        lo, hi = torso.min(axis=0).astype(int), np.ceil(torso.max(axis=0)).astype(int)
        patch = frame[lo[1]:hi[1], lo[0]:hi[0]]
        if patch.size == 0:
            return None
        return {'box': box, 'torso': torso.copy(), 'center': torso.mean(axis=0),
                'length': float(length), 'height': float(y2-y1),
                'appearance': cv2.resize(patch, (8, 8)).astype(float)/255., 'source': source}

    def _box_only_competitor(self, xy, conf, box, frame):
        # Retain uncertain background candidates as explicit competitors. Never
        # turn their missing joints into a reason to pick a different primary.
        h, w = frame.shape[:2]
        if box is None or xy.shape != (17, 2) or conf.shape != (17,):
            return None
        if not np.isfinite(xy).all() or not np.isfinite(conf).all() or np.any((conf < 0) | (conf > 1)):
            return None
        if np.any(xy < 0) or np.any(xy[:, 0] > w) or np.any(xy[:, 1] > h):
            return None
        box = np.asarray(box, dtype=float)
        if box.shape != (4,) or not np.isfinite(box).all():
            return None
        x1,y1,x2,y2 = box
        if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h and x2-x1 >= 4 and y2-y1 >= 8):
            return None
        return {'box':box, 'center':np.array([(x1+x2)/2,(y1+y2)/2]),
                'height':float(y2-y1), 'length':float(y2-y1)*.5,
                'source':'explicit_unmeasurable_competitor', 'measurable':False}

    def _weak_pose_near_track(self, xy, confidence, candidate, frame, elapsed):
        # Box-only continuity is never a measured/selected pose. At least two
        # named torso anchors (including a hip) and the existing clothing patch
        # must support the last trusted track before a short unknown quarantine.
        p = self.previous
        mask = confidence[TORSO] >= VISIBLE_CONFIDENCE
        if p is None or mask.sum() < 2 or not mask[2:].any():
            return False
        points = xy[TORSO][mask]
        box = candidate['box']
        if not ((points[:,0] >= box[0]) & (points[:,0] <= box[2]) & (points[:,1] >= box[1]) & (points[:,1] <= box[3])).all():
            return False
        ratio = candidate['height']/p['height']
        displacement = points-p['torso'][mask]
        shift = displacement.mean(axis=0)
        if not (1/MAX_SCALE_RATIO <= ratio <= MAX_SCALE_RATIO
                and np.linalg.norm(shift) <= 2*MAX_CENTER_MOTION*p['height']*elapsed
                and np.linalg.norm(displacement,axis=1).max() <= 2*MAX_TORSO_DISPLACEMENT*p['height']*elapsed):
            return False
        if elapsed > 1 and (self.velocity is None or np.linalg.norm(shift-self.velocity*elapsed) > np.sqrt(elapsed)*MAX_CENTER_MOTION*p['height']):
            return False
        predicted_torso = p['torso']+shift
        lo, hi = predicted_torso.min(axis=0).astype(int), np.ceil(predicted_torso.max(axis=0)).astype(int)
        if lo[0] < 0 or lo[1] < 0 or hi[0] > frame.shape[1] or hi[1] > frame.shape[0]:
            return False
        patch = frame[lo[1]:hi[1],lo[0]:hi[0]]
        return bool(patch.size and np.mean(np.abs(cv2.resize(patch,(8,8)).astype(float)/255.-p['appearance'])) <= MAX_APPEARANCE_CHANGE)

    def _clearly_smaller(self, primary, other):
        # Projection containment alone is not identity ambiguity: a distant
        # person may lie inside the foreground runner's arm-spanning bbox.
        # Preserve the same predeclared size advantage; never merge candidates.
        return (area(primary['box']) >= DOMINANT_AREA_RATIO*area(other['box'])
                and primary['height'] >= DOMINANT_HEIGHT_RATIO*other['height'])

    def _separated(self, a, b):
        iou, fraction = overlap(a['box'], b['box'])
        return (iou <= MAX_COMPETING_IOU and fraction <= MAX_OVERLAP_FRACTION
                and np.linalg.norm(a['center']-b['center']) >= MIN_TORSO_SEPARATION*max(a['length'], b['length']))

    def finish(self):
        # Never return success when a final uncertain frame has no confirming
        # observation. Keep fail-closed behavior at EOF or terminal loss.
        if self.pending_outliers:
            self.fail('unconfirmed_final_pose_outlier')

    def select(self, frame, xy, confidence, boxes=None, frame_index=0, *, fps=None):
        self.masked_joints = []
        if self.state == 'failed':
            return None
        if isinstance(frame_index, bool) or not isinstance(frame_index, (int, np.integer)) or frame_index < 0:
            return self.fail('invalid_frame_index')
        if self.state == 'locked':
            p = self.previous
            if frame.shape != p['shape']:
                return self.fail('resolution_change')
            if frame_index != self.last_input_index+1:
                return self.fail('noncontiguous_frames')
        self.last_input_index = int(frame_index)
        thumbnail = cv2.resize(frame, (24, 24)).astype(float)/255.
        if self.state == 'locked' and np.mean(np.abs(thumbnail-self.previous['scene'])) > MAX_SCENE_CHANGE:
            return self.fail('scene_discontinuity')
        try:
            xy, confidence, boxes = array(xy), array(confidence), array(boxes)
            count = 0 if xy is None else len(xy)
            if not count:
                return self._unknown('missing_detection')
            if confidence is None or confidence.shape != (count, 17) or xy.shape != (count, 17, 2):
                return self.fail('invalid_candidate_evidence') if count > 1 else self._unknown('invalid_candidate_evidence')
            if boxes is not None and boxes.shape != (count, 4):
                return self.fail('invalid_boxes')
            if boxes is None and count > 1:
                return self.fail('multiple_candidates_without_boxes')
            candidates = [self._candidate(xy[i], confidence[i], None if boxes is None else boxes[i], frame,
                                         'visible_landmark_extent_single_candidate' if boxes is None else 'model_xyxy')
                          for i in range(count)]
            for i,c in enumerate(candidates):
                if c is not None:
                    c['measurable'] = True
                elif boxes is not None and (count > 1 or self.state == 'locked'):
                    candidates[i] = self._box_only_competitor(xy[i], confidence[i], boxes[i], frame)
        except (ValueError, TypeError, IndexError, OverflowError):
            return self.fail('invalid_candidate_evidence')
        if any(c is None for c in candidates):
            return self.fail('invalid_competing_candidate') if count > 1 else self._unknown('insufficient_visible_geometry')
        if self.state == 'waiting':
            selected = max(range(count), key=lambda i: area(candidates[i]['box']))
            best = candidates[selected]
            if not best['measurable']:
                return self.fail('primary_geometry_insufficient')
            for i, other in enumerate(candidates):
                if i == selected:
                    continue
                if (area(best['box']) < DOMINANT_AREA_RATIO*area(other['box'])
                        or best['height'] < DOMINANT_HEIGHT_RATIO*other['height']
                        or (not self._separated(best, other) and not self._clearly_smaller(best, other))):
                    return self.fail('no_clearly_dominant_separable_candidate')
            self.first_locked_frame = int(frame_index)
            h, w = frame.shape[:2]
            self.initial_box = [float(v) for v in best['box']/np.array([w, h, w, h])]
        else:
            p = self.previous
            matches = []
            near_outliers = []
            weak_near_outliers = []
            elapsed = int(frame_index)-p['index']
            if elapsed > 1 and (not isinstance(fps,(int,float)) or isinstance(fps,bool) or not np.isfinite(fps) or fps <= 0 or elapsed/fps > .1):
                return self.fail('pose_outlier_continuity_timeout')
            for i, c in enumerate(candidates):
                if not c['measurable']:
                    if self._weak_pose_near_track(xy[i],confidence[i],c,frame,elapsed):
                        near_outliers.append(i)
                        weak_near_outliers.append(i)
                    continue
                ratio = c['height']/p['height']
                common = (1/MAX_SCALE_RATIO <= ratio <= MAX_SCALE_RATIO
                          and 1/MAX_SCALE_RATIO <= c['length']/p['length'] <= MAX_SCALE_RATIO
                          and np.linalg.norm(c['center']-p['center']) <= MAX_CENTER_MOTION*p['height']*elapsed
                          and np.mean(np.abs(c['appearance']-p['appearance'])) <= MAX_APPEARANCE_CHANGE)
                torso_motion = np.max(np.linalg.norm(c['torso']-p['torso'],axis=1))
                predicted = (elapsed == 1 or (self.velocity is not None
                             and np.linalg.norm(c['center']-(p['center']+self.velocity*elapsed)) <= np.sqrt(elapsed)*MAX_CENTER_MOTION*p['height']))
                if common and predicted and torso_motion <= MAX_TORSO_DISPLACEMENT*p['height']*elapsed:
                    matches.append(i)
                elif common and predicted and torso_motion <= 2*MAX_TORSO_DISPLACEMENT*p['height']*elapsed:
                    near_outliers.append(i)
            if matches and weak_near_outliers:
                return self.fail('ambiguous_weak_correspondence')
            if len(matches) != 1:
                # Do not merge duplicate boxes or pick an uncertain person. A
                # uniquely near-corresponding *pose outlier* may be quarantined
                # briefly, without emitting landmarks or updating appearance.
                # True multiple matches, disappearance, cuts and malformed data
                # still terminate immediately, with no reinitialization.
                if (not matches and len(near_outliers) == 1 and self.velocity is not None
                        and self.pending_outliers < 2 and isinstance(fps,(int,float))
                        and not isinstance(fps,bool) and np.isfinite(fps) and fps > 0
                        and (elapsed+1)/fps <= .1):
                    self.pending_outliers += 1
                    self.outlier_frame_indices.append(int(frame_index))
                    self.status, self.reason = 'primary_subject_unknown', 'pose_outlier_pending_continuity'
                    return None
                return self.fail('ambiguous_correspondence' if matches else 'no_continuous_correspondence')
            selected = matches[0]
            best = candidates[selected]
            # Unique temporal correspondence, not whole-person box separation,
            # determines track continuity. Projected bbox overlap does not prove
            # physical occlusion. Retain every competitor and suppress individual
            # target joints lying in competing boxes below; never measure them.
        primary_points = xy[selected]
        has_overlap, has_boundary = False, False
        for joint, point in enumerate(primary_points):
            edge = point[0] <= 0 or point[0] >= frame.shape[1] or point[1] <= 0 or point[1] >= frame.shape[0]
            competing = any(c['box'][0] <= point[0] <= c['box'][2] and c['box'][1] <= point[1] <= c['box'][3]
                            for i,c in enumerate(candidates) if i != selected)
            has_overlap = has_overlap or competing
            has_boundary = has_boundary or edge
            if edge or competing:
                self.masked_joints.append(joint)
        self.overlap_frames += int(has_overlap)
        self.boundary_frames += int(has_boundary)
        if self.masked_joints:
            self.masked_frames += 1
            self.suppressed_joint_observations += len(self.masked_joints)
        self.state, self.status, self.reason = 'locked', 'primary_subject_detected', 'continuous_geometry_appearance_torso'
        self.box_source = best['source']
        self.accepted_frames += 1
        if self.previous is not None:
            elapsed = int(frame_index)-self.previous['index']
            self.velocity = (best['center']-self.previous['center'])/elapsed
        self.pending_outliers = 0
        self.previous = dict(best, shape=frame.shape, index=int(frame_index), scene=thumbnail)
        return selected
