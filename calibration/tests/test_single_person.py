"""Single-primary-runner product contracts. Synthetic inputs, not accuracy."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from calibrated_analyzer import CalibratedPoseAnalyzer
from single_person import SinglePersonVideoRequired, require_single_person_request, SCOPE


class Points:
    def __init__(self,count):
        # Valid single-candidate landmark-derived bbox; duplicate multi-candidate
        # evidence is deliberately still ambiguous, never merged.
        template=np.array([[50.,10.]]*5+[[40.,25.],[60.,25.],
                          [35.,40.],[65.,40.],[30.,50.],[70.,50.],
                          [43.,55.],[57.,55.],[43.,70.],[57.,70.],
                          [43.,90.],[57.,90.]])
        self.xy=np.array([template.copy() for _ in range(count)]).reshape((-1,17,2))
        self.conf=np.ones((count,17))
    def __len__(self):return len(self.xy)


class Capture:
    def __init__(self,count):self.count=count;self.index=0;self.released=False
    def isOpened(self):return True
    def get(self,key):
        import cv2
        return 30. if key==cv2.CAP_PROP_FPS else self.count
    def read(self):
        if self.index==self.count:return False,None
        self.index+=1;return True,np.zeros((100,200,3),dtype=np.uint8)
    def release(self):self.released=True


def analyzer(counts):
    a=CalibratedPoseAnalyzer.__new__(CalibratedPoseAnalyzer);a._reset_temporal()
    a._yolo_model=SimpleNamespace(predict=Mock(side_effect=[
        [SimpleNamespace(keypoints=Points(n))] for n in counts]))
    return a


class SinglePersonTests(unittest.TestCase):
    def run_video(self,a,cap):
        with patch('video_pipeline.cv2.VideoCapture',return_value=cap):return a.process_video('TEST-single-person.mp4')

    def test_initial_missing_detection_unknown_then_continuous_lock(self):
        a=analyzer([0,1,1]);cap=Capture(3);rows=self.run_video(a,cap)
        self.assertEqual([r['pose_detected'] for r in rows],[False,True,True])
        self.assertIsNone(rows[0]['knee_flexion'])
        self.assertTrue(cap.released)
        self.assertEqual(a.last_video_metadata['analysis_scope'],SCOPE)
        self.assertFalse(a.last_video_metadata['single_person_scene_verified'])
        self.assertNotIn('target_tracking',a.last_video_metadata)
        self.assertTrue(all('selected_target_box' not in r for r in rows))

    def test_ambiguous_candidates_after_lock_abort_not_partial_success(self):
        a=analyzer([1,2,1]);cap=Capture(3)
        with self.assertRaises(SinglePersonVideoRequired) as error:self.run_video(a,cap)
        self.assertEqual(error.exception.frame_index,1)
        self.assertEqual(error.exception.error_code,'primary_subject_lost')
        self.assertEqual(a._yolo_model.predict.call_count,2)
        self.assertEqual(cap.index,2)
        self.assertTrue(cap.released)
        self.assertIsNone(a.last_video_metadata)
        self.assertEqual(a._smoothers,{})
        self.assertIsNone(a._target_tracker)

    def test_first_frame_multiple_is_explicit_zero_based_error(self):
        a=analyzer([2]);cap=Capture(1)
        with self.assertRaises(SinglePersonVideoRequired) as error:self.run_video(a,cap)
        payload=error.exception.payload()
        self.assertEqual(payload['frame_index'],0)
        self.assertEqual(payload['frame_index_base'],0)
        self.assertEqual(payload['error_code'],'primary_subject_ambiguous')
        self.assertTrue(cap.released)

    def test_a_new_video_can_run_after_a_rejected_one(self):
        a=analyzer([2]);cap=Capture(1)
        with self.assertRaises(SinglePersonVideoRequired):self.run_video(a,cap)
        a._yolo_model.predict=Mock(return_value=[SimpleNamespace(keypoints=Points(1))])
        rows=self.run_video(a,Capture(1));self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['detection_status'],'primary_subject_detected')

    def test_legacy_tracker_cannot_reenable_selection(self):
        a=analyzer([2]);a._target_selection={'frame_index':0,'box':[0,0,1,1]};a._target_tracker=Mock()
        with self.assertRaisesRegex(ValueError,'单人视频'):a.configure_target(a._target_selection)
        with self.assertRaises(SinglePersonVideoRequired):self.run_video(a,Capture(1))
        self.assertIsNone(a._target_selection)
        self.assertIsNone(a._target_tracker)

    def test_both_target_fields_are_rejected_even_null_or_empty(self):
        require_single_person_request({'video_file':'one.mp4'})
        require_single_person_request({'video_files':['one.mp4','two.mp4']})
        for field in ('target_selection','target_selections'):
            for value in (None,{},[],False,{'frame_index':0,'box':[0,0,1,1]}):
                with self.subTest(field=field,value=value),self.assertRaisesRegex(ValueError,'单人视频'):
                    require_single_person_request({field:value})


if __name__=='__main__':unittest.main()
