"""Synthetic continuity and measurement-suppression tests, not identity accuracy."""
import unittest
import calibration.tests.test_primary_subject as fixtures
from primary_subject import PrimarySubjectSelector
from single_person import SinglePersonVideoRequired
from calibration.tests.test_primary_subject import (MAIN, SMALL, result, image,
                                                   analyzer, Capture, run_video,
                                                   )


def pose_outlier(shift=30.):
    packet=result([MAIN])
    packet[0].keypoints.xy[0,:,0]+=shift
    return packet


def weak_pose(shift=0.):
    packet=result([MAIN])
    packet[0].keypoints.xy[0,:,0]+=shift
    packet[0].keypoints.conf[0,[5,6]]=.1
    return packet


class ContinuityImprovementTests(unittest.TestCase):
    assert_clean=fixtures.PrimarySubjectTests.assert_clean

    def test_sideview_bilateral_shoulders_and_hips_can_coincide(self):
        packet=result([MAIN])
        packet[0].keypoints.xy[0,6]=packet[0].keypoints.xy[0,5]
        packet[0].keypoints.xy[0,12]=packet[0].keypoints.xy[0,11]
        a=analyzer([packet]);cap=Capture([image()])
        rows=run_video(a,cap)
        self.assertTrue(rows[0]['pose_detected'])
        self.assertIsNotNone(rows[0]['body_leang'])
        self.assert_clean(a,cap)

    def test_armswing_bbox_width_not_an_identity_scale_measure(self):
        packet=result([MAIN]);packet[0].boxes.xyxy[0]=[20.,20.,180.,280.]
        a=analyzer([result([MAIN]),packet]);cap=Capture([image()]*2)
        rows=run_video(a,cap)
        self.assertTrue(all(r['pose_detected'] for r in rows))
        self.assertEqual(a.last_video_metadata['subject_selection']['selected_frame_count'],2)
        self.assert_clean(a,cap)

    def test_nested_background_joint_measurements_suppressed_not_filled(self):
        for weak in (False,True):
            with self.subTest(weak=weak):
                packet=result([MAIN,[65.,80.,105.,190.]])
                if weak:packet[0].keypoints.conf[1,5]=.1
                a=analyzer([packet]);cap=Capture([image()])
                row=run_video(a,cap)[0]
                self.assertTrue(row['pose_detected'])
                self.assertEqual(row['landmarks'][5].visibility,0.)
                self.assertEqual(row['landmarks'][11].visibility,0.)
                self.assertEqual(row['landmarks'][12].visibility,0.)
                self.assertIsNone(row['body_leang'])
                self.assertIsNone(row['knee_flexion'])
                self.assertIsNone(row['score'])
                self.assertGreater(a.last_video_metadata['subject_selection']['suppressed_joint_observations'],0)
                self.assert_clean(a,cap)

    def test_unique_track_can_overlap_other_bbox_without_using_overlap_joints(self):
        a=analyzer([result([MAIN]),result([MAIN,[70.,20.,170.,280.]])])
        cap=Capture([image()]*2);rows=run_video(a,cap)
        self.assertTrue(rows[1]['pose_detected'])
        self.assertEqual(rows[1]['landmarks'][5].x,rows[0]['landmarks'][5].x)
        self.assertEqual(rows[1]['landmarks'][5].visibility,0.)
        self.assertIsNone(rows[1]['body_leang'])
        self.assertIsNone(rows[1]['knee_flexion'])
        self.assert_clean(a,cap)

    def test_two_plausible_tracks_always_stop_even_with_history(self):
        a=analyzer([result([MAIN]),result([MAIN]),result([MAIN,[60.,20.,160.,280.]])])
        cap=Capture([image()]*3)
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.reason,'ambiguous_correspondence')
        self.assertEqual(exc.exception.frame_index,2)
        self.assertIsNone(a.last_video_metadata)
        self.assert_clean(a,cap)

    def test_full_pose_outlier_is_unknown_then_two_sided_confirmed(self):
        a=analyzer([result([MAIN]),result([MAIN]),pose_outlier(),result([[44.,20.,144.,280.]])])
        cap=Capture([image()]*4);rows=run_video(a,cap)
        self.assertEqual([r['pose_detected'] for r in rows],[True,True,False,True])
        self.assertEqual(rows[2]['landmarks'],[])
        self.assertEqual(rows[2]['unknown_reason'],'pose_outlier_pending_continuity')
        for key in ('body_leang','knee_flexion','score','running_phase'):
            self.assertIsNone(rows[2][key])
        m=a.last_video_metadata['subject_selection']
        self.assertEqual(m['pose_outlier_frame_indices'],[2])
        self.assertEqual(m['pending_outlier_frames'],0)
        self.assertFalse(m['automatic_reacquisition'])
        self.assertEqual(rows[3]['temporal_smoothing'],'contiguous_index_weighted')
        self.assertEqual(rows[3]['landmarks'][5].x,.185)  # no cross-unknown smoothing
        self.assert_clean(a,cap)

    def test_weak_shoulders_not_used_as_measurements_during_quarantine(self):
        a=analyzer([result([MAIN]),result([MAIN]),weak_pose(10),result([MAIN])])
        cap=Capture([image()]*4);rows=run_video(a,cap)
        self.assertFalse(rows[2]['pose_detected'])
        self.assertEqual(rows[2]['landmarks'],[])
        self.assertIsNone(rows[2]['knee_flexion'])
        self.assertTrue(rows[3]['pose_detected'])
        self.assert_clean(a,cap)

    def test_weak_plausible_competitor_vetoes_apparently_good_match(self):
        packet=result([MAIN,[50.,20.,150.,280.]])
        packet[0].keypoints.conf[1,[5,6]]=.1
        a=analyzer([result([MAIN]),result([MAIN]),packet]);cap=Capture([image()]*3)
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.reason,'ambiguous_weak_correspondence')
        self.assertIsNone(a.last_video_metadata)
        self.assert_clean(a,cap)

    def test_terminal_outlier_has_no_success_report_or_partial_metadata(self):
        a=analyzer([result([MAIN]),result([MAIN]),pose_outlier()]);cap=Capture([image()]*3)
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.reason,'unconfirmed_final_pose_outlier')
        self.assertEqual(exc.exception.frame_index,2)
        self.assertIsNone(a.last_video_metadata)
        self.assert_clean(a,cap)

    def test_max_two_outliers_and_confirmation_time_bound(self):
        for fps,expected_index in ((60.,4),(25.,3),(10.,2)):
            with self.subTest(fps=fps):
                packets=[result([MAIN]),result([MAIN]),weak_pose(5),weak_pose(5),weak_pose(5),result([MAIN])]
                a=analyzer(packets);cap=Capture([image()]*6,fps=fps)
                with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
                self.assertEqual(exc.exception.frame_index,expected_index)
                self.assertEqual(cap.index,expected_index+1)
                self.assertIsNone(a.last_video_metadata)
                self.assert_clean(a,cap)

    def test_no_fabricated_fps_to_enable_outlier_quarantine(self):
        for fps in (float('nan'),0.):
            a=analyzer([result([MAIN]),result([MAIN]),pose_outlier(),result([MAIN])])
            cap=Capture([image()]*4,fps=fps)
            with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
            self.assertEqual(exc.exception.frame_index,2)
            self.assert_clean(a,cap)

    def test_disappearance_not_treated_as_short_pose_outlier(self):
        a=analyzer([result([MAIN]),result([MAIN]),result([]),result([MAIN])])
        cap=Capture([image()]*4)
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.reason,'missing_detection')
        self.assertEqual(cap.index,3)
        self.assert_clean(a,cap)

    def test_future_geometry_must_agree_with_track_prediction(self):
        a=analyzer([result([MAIN]),result([MAIN]),pose_outlier(),result([[100.,20.,200.,280.]])])
        cap=Capture([image()]*4)
        with self.assertRaises(SinglePersonVideoRequired) as exc:run_video(a,cap)
        self.assertEqual(exc.exception.frame_index,3)
        self.assertIsNone(a.last_video_metadata)
        self.assert_clean(a,cap)

    def test_torso_scale_change_cannot_hide_in_same_person_bbox(self):
        packet=result([MAIN]);torso=packet[0].keypoints.xy[0,[5,6,11,12]].copy()
        center=torso.mean(axis=0)
        packet[0].keypoints.xy[0,[5,6,11,12]]=center+(torso-center)*.5
        a=analyzer([result([MAIN]),result([MAIN]),packet]);cap=Capture([image()]*3)
        with self.assertRaises(SinglePersonVideoRequired):run_video(a,cap)
        self.assertIsNone(a.last_video_metadata)
        self.assert_clean(a,cap)

    def test_confirming_frame_cannot_jump_input_frame_number(self):
        s=PrimarySubjectSelector()
        for index in (0,1):
            p=result([MAIN])[0];self.assertEqual(s.select(image(),p.keypoints.xy,p.keypoints.conf,p.boxes.xyxy,index,fps=30.),0)
        p=pose_outlier()[0]
        self.assertIsNone(s.select(image(),p.keypoints.xy,p.keypoints.conf,p.boxes.xyxy,2,fps=30.))
        p=result([MAIN])[0]
        self.assertIsNone(s.select(image(),p.keypoints.xy,p.keypoints.conf,p.boxes.xyxy,4,fps=30.))
        self.assertEqual(s.reason,'noncontiguous_frames')
        self.assertIsNone(s.previous)

    def test_border_points_are_unknown_not_clipped_measurements(self):
        packet=result([MAIN]);packet[0].keypoints.xy[0,15,0]=0.
        a=analyzer([packet]);cap=Capture([image()]);row=run_video(a,cap)[0]
        self.assertTrue(row['pose_detected'])
        self.assertEqual(row['landmarks'][15].visibility,0.)
        self.assertIsNone(row['left_knee_angle'])
        self.assertIsNone(row['knee_flexion'])
        self.assert_clean(a,cap)


if __name__=='__main__':unittest.main()
