"""Preflight must not resurrect automatic selection from multi-person frames."""
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import cv2
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from gait_metrics import check_video_quality


class PreflightScopeTests(unittest.TestCase):
    def quality(self,fps=30.,count=300.,opened=True):
        cap=Mock();cap.isOpened.return_value=opened
        cap.get.side_effect=lambda key:{cv2.CAP_PROP_FPS:fps,cv2.CAP_PROP_FRAME_COUNT:count}[key]
        with patch('cv2.VideoCapture',return_value=cap),patch('pose_analyzer.PoseAnalyzer') as retired:
            result=check_video_quality('TEST-container-only.mp4')
            retired.assert_not_called()
        cap.release.assert_called_once();cap.read.assert_not_called()
        self.assertIsNone(result['valid_frame_ratio'])
        self.assertFalse(result['single_person_scene_verified'])
        self.assertEqual(result['preflight_scope'],'container_metadata_only')
        json.dumps(result,allow_nan=False)
        return result

    def test_known_metadata_is_not_a_single_person_certificate(self):
        result=self.quality();self.assertTrue(result['ok']);self.assertEqual(result['duration'],10.)
        self.assertTrue(any('不验证人体数量' in text for text in result['warnings']))

    def test_invalid_metadata_remains_unknown_no_default_fps_or_ratio(self):
        for fps,count in [(0,300),(None,300),(True,300),(float('nan'),300),(float('inf'),300),
                          (30,0),(30,-1),(30,.5),(30,None),(30,float('inf')),(1e-300,1e100)]:
            with self.subTest(fps=fps,count=count):
                result=self.quality(fps,count);self.assertFalse(result['ok']);self.assertIsNone(result['duration'])
        self.assertIsNone(self.quality(0,300)['fps'])

    def test_unopenable_video_releases_without_inference(self):
        result=self.quality(opened=False);self.assertFalse(result['ok']);self.assertIsNone(result['duration'])

    def test_long_video_does_not_claim_automatic_truncation(self):
        result=self.quality(count=9000.)
        self.assertFalse(any('分析将取前' in text for text in result['warnings']))


if __name__=='__main__':unittest.main()
