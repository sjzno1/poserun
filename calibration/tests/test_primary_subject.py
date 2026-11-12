"""Synthetic model + real selector/analyzer/pipeline; NOT accuracy validation."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from calibrated_analyzer import CalibratedPoseAnalyzer
from primary_subject import PrimarySubjectSelector
from single_person import SinglePersonVideoRequired, SCOPE

MAIN = [40., 20., 140., 280.]
SMALL = [240., 110., 275., 200.]
SIMILAR = [240., 20., 340., 280.]
LARGER = [235., 0., 385., 300.]


def pose(box):
    x, y, right, bottom = box
    w, h = right-x, bottom-y
    # Anatomical labels remain corresponding under candidate order changes.
    return np.array([[x+w*.5,y+h*.08]]*5 +
                    [[x+w*.3,y+h*.25],[x+w*.7,y+h*.25],
                     [x+w*.2,y+h*.4],[x+w*.8,y+h*.4],
                     [x+w*.15,y+h*.55],[x+w*.85,y+h*.55],
                     [x+w*.35,y+h*.55],[x+w*.65,y+h*.55],
                     [x+w*.35,y+h*.72],[x+w*.65,y+h*.72],
                     [x+w*.35,y+h*.95],[x+w*.65,y+h*.95]])


def result(boxes, *, confidence=True, include_boxes=True):
    xy = np.array([pose(b) for b in boxes]).reshape((-1,17,2))
    points = SimpleNamespace(xy=xy, conf=np.ones((len(boxes),17)) if confidence else None)
    r = SimpleNamespace(keypoints=points)
    if include_boxes:
        r.boxes = SimpleNamespace(xyxy=np.array(boxes).reshape((-1,4)))
    return [r]


def image(colour=0, shape=(300,400,3)):
    return np.full(shape, colour, dtype=np.uint8)


def analyzer(results):
    a = CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer)
    a._reset_temporal()
    a._yolo_model = SimpleNamespace(predict=Mock(side_effect=results))
    return a


class Capture:
    def __init__(self, frames, fps=30., declared=None):
        self.frames = frames
        self.index = 0
        self.fps = fps
        self.declared = len(frames) if declared is None else declared
        self.released = False
    def isOpened(self): return True
    def get(self, key):
        import cv2
        return self.fps if key == cv2.CAP_PROP_FPS else self.declared
    def read(self):
        if self.index == len(self.frames): return False, None
        f = self.frames[self.index]
        self.index += 1
        return True, f
    def release(self): self.released = True


def run_video(a, cap):
    with patch('video_pipeline.cv2.VideoCapture', return_value=cap):
        return a.process_video('synthetic-model-mechanics.mp4')


class PrimarySubjectTests(unittest.TestCase):
    def select(self, selector, boxes, index=0, frame=None):
        r = result(boxes)[0]
        return selector.select(image() if frame is None else frame,
                               r.keypoints.xy, r.keypoints.conf, r.boxes.xyxy, index)

    def assert_clean(self, a, cap):
        self.assertTrue(cap.released)
        self.assertIsNone(a._primary_subject)
        self.assertIsNone(a._target_tracker)
        self.assertIsNone(a._video_frame_index)
        self.assertEqual(a._smoothers,{})
        self.assertIsNone(a._last_context)

    def test_small_background_and_preceding_following_separable_people(self):
        for others in ([SMALL], [[170.,100.,205.,190.], SMALL]):
            with self.subTest(others=others):
                s=PrimarySubjectSelector()
                self.assertEqual(self.select(s,others+[MAIN]), len(others))
                self.assertEqual(s.state,'locked')
                self.assertFalse(s.metadata()['identity_verified'])
                self.assertFalse(s.metadata()['depth_verified'])

    def test_similar_size_not_center_or_confidence_priority(self):
        for boxes in ([MAIN,SIMILAR],[SIMILAR,MAIN]):
            s=PrimarySubjectSelector()
            self.assertIsNone(self.select(s,boxes))
            self.assertEqual(s.status,'primary_subject_ambiguous')
        r=result([MAIN,SIMILAR])[0]
        r.keypoints.conf[0]=.7
        s=PrimarySubjectSelector()
        self.assertIsNone(s.select(image(),r.keypoints.xy,r.keypoints.conf,r.boxes.xyxy))

    def test_duplicate_boxes_never_merged(self):
        s=PrimarySubjectSelector()
        self.assertIsNone(self.select(s,[MAIN,MAIN]))
        self.assertEqual(s.status,'primary_subject_ambiguous')
        s=PrimarySubjectSelector();self.select(s,[MAIN])
        self.assertIsNone(self.select(s,[MAIN,MAIN],1))
        self.assertEqual(s.status,'primary_subject_lost')

    def test_small_nested_projection_is_not_identity_ambiguity(self):
        s=PrimarySubjectSelector()
        self.assertEqual(self.select(s,[MAIN,[65.,80.,105.,190.]]),0)
        self.assertEqual(s.state,'locked')
        self.assertTrue(set([5,11,12]).issubset(s.masked_joints))
        self.assertEqual(s.metadata()['projection_overlap_frames'],1)

    def test_order_reverse_and_larger_incoming_never_switch(self):
        a=analyzer([result([SMALL,MAIN]),result([MAIN,SMALL]),result([LARGER,MAIN])])
        cap=Capture([image()]*3)
        rows=run_video(a,cap)
        self.assertEqual([r['landmarks'][5].x for r in rows],[.175]*3)
        self.assertEqual(a._yolo_model.predict.call_count,3)
        self.assertTrue(all(c.kwargs['conf']==.05 for c in a._yolo_model.predict.call_args_list))
        self.assertTrue(all(r['score'] is None and r['running_phase'] is None for r in rows))
        self.assert_clean(a,cap)

    def test_geometry_correspondence_with_small_motion(self):
        s=PrimarySubjectSelector();self.select(s,[MAIN])
        moved=[v+2 if i%2==0 else v for i,v in enumerate(MAIN)]
        self.assertEqual(self.select(s,[SMALL,moved],1),1)

    def test_missing_occluded_crossing_and_jump_are_terminal(self):
        scenarios=[[], [SMALL], [MAIN,[60.,20.,160.,280.]],
                   [[120.,20.,220.,280.]], [[40.,0.,180.,300.]]]
        for boxes in scenarios:
            with self.subTest(boxes=boxes):
                s=PrimarySubjectSelector();self.select(s,[MAIN])
                self.assertIsNone(self.select(s,boxes,1))
                self.assertEqual(s.status,'primary_subject_lost')
                self.assertIsNone(self.select(s,[MAIN],2))
                self.assertEqual(s.accepted_frames,1)
                self.assertIsNone(s.previous)

    def test_initial_empty_unknown_then_lock_metadata_normalized(self):
        a=analyzer([result([]),result([]),result([SMALL,MAIN]),result([MAIN])])
        cap=Capture([image()]*4)
        rows=run_video(a,cap)
        self.assertEqual([r['pose_detected'] for r in rows],[False,False,True,True])
        self.assertIsNone(rows[0]['knee_flexion'])
        self.assertEqual(rows[0]['detection_status'],'primary_subject_unknown')
        m=a.last_video_metadata
        self.assertEqual(m['analysis_scope'],'single_primary_runner')
        s=m['subject_selection']
        self.assertEqual(s['mode'],'automatic_primary_runner')
        self.assertFalse(s['identity_verified']);self.assertTrue(s['heuristic_unvalidated'])
        self.assertEqual(s['initial_frame_index'],2)
        np.testing.assert_allclose(s['initial_box'],np.array(MAIN)/[400,300,400,300])
        self.assertEqual(s['selected_frame_count'],2)
        self.assertEqual(s['initial_unknown_frames'],2)
        self.assertEqual(s['state'],'locked')
        self.assertEqual(rows[0]['video_timeline']['subject_selection'],s)
        json.dumps(m,allow_nan=False)
        self.assert_clean(a,cap)

    def test_all_initial_empty_never_fabricates_selection(self):
        a=analyzer([result([])]*2)
        rows=run_video(a,Capture([image()]*2))
        self.assertFalse(any(r['pose_detected'] for r in rows))
        m=a.last_video_metadata['subject_selection']
        self.assertEqual(m['state'],'waiting');self.assertIsNone(m['initial_box'])
        self.assertEqual(m['selected_frame_count'],0)

    def test_first_last_failure_cleanup_no_partial_metadata(self):
        for results,expected_index,code in [([result([MAIN,SIMILAR])],0,'primary_subject_ambiguous'),
                                            ([result([MAIN]),result([])],1,'primary_subject_lost')]:
            a=analyzer(results);cap=Capture([image()]*len(results))
            with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
            self.assertEqual(exc.exception.error_code,code)
            self.assertEqual(exc.exception.frame_index,expected_index)
            self.assertEqual(exc.exception.payload()['analysis_scope'],SCOPE)
            self.assertEqual(exc.exception.payload()['frame_index_base'],0)
            self.assert_clean(a,cap);self.assertIsNone(a.last_video_metadata)

    def test_early_loss_stops_decoding_and_no_reacquisition(self):
        a=analyzer([result([MAIN]),result([]),result([MAIN])]);cap=Capture([image()]*3)
        with self.assertRaises(SinglePersonVideoRequired):run_video(a,cap)
        self.assertEqual(cap.index,2);self.assertEqual(a._yolo_model.predict.call_count,2)
        self.assert_clean(a,cap)

    def test_video_to_video_different_subject_has_fresh_state(self):
        a=analyzer([result([MAIN]),result([SIMILAR]),result([MAIN,SIMILAR]),result([SMALL])])
        run_video(a,Capture([image()]))
        rows=run_video(a,Capture([image()]))
        self.assertAlmostEqual(rows[0]['landmarks'][5].x, .675)
        with self.assertRaises(SinglePersonVideoRequired):run_video(a,Capture([image()]))
        self.assertIsNone(a.last_video_metadata)
        run_video(a,Capture([image()]))
        self.assertEqual(a.last_video_metadata['subject_selection']['initial_frame_index'],0)
        self.assertEqual(a.last_video_metadata['subject_selection']['selected_frame_count'],1)

    def test_cut_resolution_and_appearance_change_stop_whole_video(self):
        changed=image();changed[85:163,70:110]=255
        for frame in (image(255),image(shape=(600,800,3)),changed):
            a=analyzer([result([MAIN]),result([MAIN])]);cap=Capture([image(),frame])
            with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
            self.assertEqual(exc.exception.error_code,'primary_subject_lost')
            self.assert_clean(a,cap);self.assertIsNone(a.last_video_metadata)

    def test_occluded_torso_after_lock_fails_closed(self):
        r=result([MAIN]);r[0].keypoints.conf[0,5]=.1
        a=analyzer([result([MAIN]),r]);cap=Capture([image()]*2)
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.error_code,'primary_subject_lost')
        self.assert_clean(a,cap)

    def test_invalid_geometry_and_confidence_not_silent_competitor_filter(self):
        for kind in ('nan_box','infinite_point','reversed_box','outside_box','nan_conf','low_torso','missing_conf','wrong_shape'):
            for multiple in (False,True):
                with self.subTest(kind=kind,multiple=multiple):
                    r=result([MAIN,SMALL] if multiple else [MAIN]);last=-1
                    if kind=='nan_box':r[0].boxes.xyxy[last,0]=np.nan
                    if kind=='infinite_point':r[0].keypoints.xy[last,5,0]=np.inf
                    if kind=='reversed_box':r[0].boxes.xyxy[last]=[100,50,10,80]
                    if kind=='outside_box':r[0].boxes.xyxy[last,0]=-1
                    if kind=='nan_conf':r[0].keypoints.conf[last,5]=np.nan
                    if kind=='low_torso':r[0].keypoints.conf[last,5]=.1
                    if kind=='missing_conf':r[0].keypoints.conf=None
                    if kind=='wrong_shape':r[0].keypoints.xy=r[0].keypoints.xy[:,:16]
                    a=analyzer([r]);cap=Capture([image()])
                    if multiple and kind == 'low_torso':
                        rows=run_video(a,cap)
                        self.assertTrue(rows[0]['pose_detected'])
                        self.assertEqual(rows[0]['landmarks'][5].x,.175)
                    elif multiple:
                        with self.assertRaises(SinglePersonVideoRequired):run_video(a,cap)
                        self.assertIsNone(a.last_video_metadata)
                    else:
                        rows=run_video(a,cap)
                        self.assertFalse(rows[0]['pose_detected'])
                        self.assertIsNone(rows[0]['knee_flexion'])
                    self.assert_clean(a,cap)

    def test_single_missing_box_fallback_only_not_invalid_supplied_or_multi(self):
        a=analyzer([result([MAIN],include_boxes=False)])
        rows=run_video(a,Capture([image()]))
        self.assertTrue(rows[0]['pose_detected'])
        self.assertEqual(a.last_video_metadata['subject_selection']['box_source'],'visible_landmark_extent_single_candidate')
        a=analyzer([result([MAIN,SMALL],include_boxes=False)])
        with self.assertRaises(SinglePersonVideoRequired):run_video(a,Capture([image()]))

    def test_manual_tracker_never_called_for_valid_multiple_model_candidates(self):
        a=analyzer([result([SMALL,MAIN])]);legacy=Mock();a._target_tracker=legacy
        rows=run_video(a,Capture([image()]))
        self.assertTrue(rows[0]['pose_detected']);legacy.select.assert_not_called()
        self.assertIsNone(a._target_tracker)

    def test_frame_gap_latches_and_no_reacquisition(self):
        s=PrimarySubjectSelector();self.select(s,[MAIN])
        self.assertIsNone(self.select(s,[MAIN],2))
        self.assertEqual(s.reason,'noncontiguous_frames')
        self.assertIsNone(self.select(s,[MAIN],3))

    def test_public_analyze_frame_respects_explicit_frame_gap(self):
        a=analyzer([result([MAIN]),result([MAIN]),result([MAIN])])
        self.assertIsNotNone(a.analyze_frame(image(),frame_index=4,fps=30.))
        self.assertIsNone(a.analyze_frame(image(),frame_index=6,fps=30.))
        self.assertEqual(a._detection_status,'primary_subject_lost')
        self.assertEqual(a._primary_subject.reason,'noncontiguous_frames')
        self.assertIsNone(a.analyze_frame(image(),frame_index=7,fps=30.))

    def test_larger_entrant_is_not_substitute_after_original_leaves(self):
        a=analyzer([result([MAIN]),result([LARGER,MAIN]),result([LARGER])])
        cap=Capture([image()]*3)
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.error_code,'primary_subject_lost')
        self.assertEqual(exc.exception.frame_index,2)
        self.assert_clean(a,cap);self.assertIsNone(a.last_video_metadata)

    def test_corresponding_torso_not_only_box_or_colour(self):
        changed=result([MAIN]);p=changed[0].keypoints.xy[0]
        p[[5,6]]=p[[6,5]]
        a=analyzer([result([MAIN]),changed]);cap=Capture([image()]*2)
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.error_code,'primary_subject_lost')
        self.assert_clean(a,cap)

    def test_unknown_fps_does_not_disable_primary_lock(self):
        a=analyzer([result([MAIN]),result([])]);cap=Capture([image()]*2,fps=float('nan'))
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.error_code,'primary_subject_lost')
        self.assert_clean(a,cap)

    def test_inference_and_capture_creation_errors_reset_state(self):
        a=analyzer([]);a._yolo_model.predict.side_effect=RuntimeError('synthetic inference failure')
        cap=Capture([image()])
        with self.assertRaisesRegex(RuntimeError,'synthetic inference failure'):run_video(a,cap)
        self.assert_clean(a,cap);self.assertIsNone(a.last_video_metadata)
        with patch('video_pipeline.cv2.VideoCapture',side_effect=RuntimeError('capture construction')):
            with self.assertRaisesRegex(RuntimeError,'capture construction'):a.process_video('synthetic.mp4')
        self.assertIsNone(a._primary_subject);self.assertEqual(a._smoothers,{})


if __name__=='__main__':unittest.main()
