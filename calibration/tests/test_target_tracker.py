"""Historical association mechanics only; public analyzer now rejects selection.

This library is not an enabled multi-person product feature or accuracy proof.
"""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from target_tracker import SelectedTargetTracker, validate_selection
from calibrated_analyzer import CalibratedPoseAnalyzer

BOX = [.3, .1, .6, .9]
OTHER = [.72, .1, .95, .9]


def image(colour=(0, 200, 0), background=0):
    frame = np.full((200, 200, 3), background, dtype=np.uint8)
    frame[20:180, 60:120] = colour
    return frame


def tracker():
    return SelectedTargetTracker({'frame_index': 0, 'box': list(BOX)})


class TargetTrackerTests(unittest.TestCase):
    def test_explicit_seed_selects_second_detection_not_first(self):
        t = tracker()
        self.assertEqual(t.select([OTHER, BOX], image(), 0), 1)
        self.assertEqual(t.box, BOX)
        self.assertFalse(t.metadata()['identity_verified'])

    def test_detection_order_change_does_not_change_target(self):
        t = tracker()
        self.assertEqual(t.select([OTHER, BOX], image(), 0), 1)
        self.assertEqual(t.select([BOX, OTHER], image(), 1), 0)
        self.assertEqual(t.accepted_frames, 2)

    def test_missing_target_latches_loss_no_reacquisition(self):
        t = tracker()
        t.select([BOX], image(), 0)
        self.assertIsNone(t.select([], image(), 1))
        self.assertIsNone(t.select([BOX], image(), 2))
        self.assertEqual(t.accepted_frames, 1)
        self.assertEqual(t.first_lost_frame, 1)
        self.assertEqual(t.reason, 'lost_no_matching_detection')
        self.assertFalse(t.metadata()['automatic_reacquisition'])

    def test_ambiguity_stops_instead_of_switching(self):
        t = tracker()
        t.select([BOX], image(), 0)
        near = [.31, .1, .61, .9]
        self.assertIsNone(t.select([near, BOX], image(), 1))
        self.assertEqual(t.reason, 'lost_ambiguous_detections')
        self.assertIsNone(t.select([BOX], image(), 2))

    def test_seed_ambiguity_and_wrong_location_refuse(self):
        for candidates in ([BOX, [.31, .1, .61, .9]], [OTHER], []):
            with self.subTest(candidates=candidates):
                t = tracker()
                self.assertIsNone(t.select(candidates, image(), 0))
                self.assertEqual(t.state, 'lost')

    def test_colour_change_is_not_accepted_as_same_identity(self):
        t = tracker()
        t.select([BOX], image(), 0)
        self.assertIsNone(t.select([BOX], image(colour=(0, 0, 200)), 1))
        self.assertEqual(t.reason, 'lost_appearance_mismatch')

    def test_scene_discontinuity_stops_even_same_torso(self):
        t = tracker()
        t.select([BOX], image(), 0)
        self.assertIsNone(t.select([BOX], image(background=255), 1))
        self.assertEqual(t.reason, 'lost_scene_discontinuity')

    def test_frame_gap_is_terminal(self):
        t = tracker()
        t.select([BOX], image(), 0)
        self.assertIsNone(t.select([BOX], image(), 2))
        self.assertEqual(t.reason, 'lost_noncontiguous_frames')

    def test_motion_and_scale_jump_stop(self):
        for box in ([.41, .1, .71, .9], [.195, .1, .705, .9]):
            with self.subTest(box=box):
                t = tracker()
                t.select([BOX], image(), 0)
                self.assertIsNone(t.select([box], image(), 1))
                self.assertEqual(t.reason, 'lost_motion_or_scale_jump')

    def test_bad_frame_index_and_outside_box_refuse(self):
        t = tracker()
        self.assertIsNone(t.select([BOX], image(), True))
        self.assertEqual(t.reason, 'lost_invalid_frame_index')
        t = tracker()
        self.assertIsNone(t.select([[.3, -.1, .6, .9]], image(), 0))
        self.assertEqual(t.state, 'lost')

    def test_selection_requires_valid_literal_first_frame_xyxy(self):
        bad = [True, [], {}, {'frame_index': '0', 'box': BOX},
               {'frame_index': False, 'box': BOX}, {'frame_index': 1, 'box': BOX},
               {'frame_index': 0, 'box': ['0.3', .1, .6, .9]},
               {'frame_index': 0, 'box': [.3, .1, float('nan'), .9]},
               {'frame_index': 0, 'box': [.6, .1, .3, .9]},
               {'frame_index': 0, 'box': [.3, .1, .301, .9]},
               {'frame_index': 0, 'box': BOX, 'identity_verified': True}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_selection(value)
        self.assertIsNone(validate_selection(None))
        self.assertEqual(validate_selection({'frame_index': 0, 'box': [0, 0, 1, 1]})['box'], [0, 0, 1, 1])

    def test_configuration_is_copied(self):
        seed = {'frame_index': 0, 'box': list(BOX)}
        t = SelectedTargetTracker(seed)
        seed['box'][0] = 0
        self.assertEqual(t.selection['box'], BOX)

    def test_public_analyzer_refuses_legacy_target_selection(self):
        a = CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer)
        with self.assertRaisesRegex(ValueError, '单人视频'):
            a.configure_target({'frame_index': 0, 'box': BOX})
        a.configure_target(None)
        a._target_selection = {'frame_index': 0, 'box': BOX}
        a._begin_video()
        self.assertIsNone(a._target_tracker)
        self.assertIsNone(a._target_selection)
        a._end_video()

    def test_float32_model_boxes_do_not_leak_numpy_into_json(self):
        import json
        t = tracker()
        self.assertEqual(t.select(np.array([BOX], dtype=np.float32), image(), 0), 0)
        assert t.box is not None
        self.assertTrue(all(type(v) is float for v in t.box))
        json.dumps({'box': t.box, 'tracking': t.metadata()}, allow_nan=False)

    def test_public_detector_invalid_multiple_candidates_still_ambiguous(self):
        class Points:
            xy = np.array([[[150., 50.]]*17, [[80., 50.]]*17])
            conf = np.ones((2, 17))
            def __len__(self): return 2
        a = CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer)
        a._reset_temporal()
        a._target_tracker = Mock()  # A legacy object cannot reactivate selection.
        a._yolo_model = SimpleNamespace(predict=Mock(return_value=[SimpleNamespace(keypoints=Points())]))
        found, _ = a._detect_landmarks(image())
        self.assertIsNone(found)
        self.assertEqual(a._detection_status, 'primary_subject_ambiguous')
        a._target_tracker.select.assert_not_called()


if __name__ == '__main__':
    unittest.main()
