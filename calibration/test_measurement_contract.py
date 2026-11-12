"""Measurement invariants: not elite-score assumptions or fabricated video truth."""
import cProfile
import sys
import unittest
from pathlib import Path
import numpy as np
import torch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pose_analyzer import PoseAnalyzer,Landmark,LSTMSmoother
from gait_analyzer import GaitAnalyzer

class MeasurementContract(unittest.TestCase):
    def test_stationary_landmark_is_preserved(self):
        s=LSTMSmoother();p=np.array([.5,.75],dtype=np.float32)
        for _ in range(8):
            np.testing.assert_allclose(s.add(p),p,atol=1e-6)
    def test_smoothing_is_reproducible_without_training(self):
        a,b=LSTMSmoother(),LSTMSmoother()
        for p in [np.array([.2+i*.01,.6],dtype=np.float32)for i in range(6)]:
            np.testing.assert_allclose(a.add(p),b.add(p),atol=1e-7)
    @staticmethod
    def landmarks(w,h):
        return [Landmark(side+'_'+name,x/w,y/h,0,1)for side,off in [('LEFT',0),('RIGHT',10)]for name,x,y in [('HIP',100+off,100),('KNEE',150+off,200),('ANKLE',250+off,250),('SHOULDER',90+off,20)]]
    def test_knee_angle_does_not_depend_on_canvas_aspect(self):
        p=PoseAnalyzer.__new__(PoseAnalyzer)
        a=p._calculate_metrics(self.landmarks(400,400),(400,400,3))
        b=p._calculate_metrics(self.landmarks(800,400),(400,800,3))
        self.assertAlmostEqual(a['left_knee_angle'],b['left_knee_angle'],places=5)
    def test_gait_shin_uses_pixel_geometry(self):
        g=GaitAnalyzer();a={'frame_index':0,'landmarks':self.landmarks(400,400),'frame_width':400,'frame_height':400}
        b={'frame_index':0,'landmarks':self.landmarks(800,400),'frame_width':800,'frame_height':400}
        self.assertAlmostEqual(g._frame_features(a)['l_shin'],g._frame_features(b)['l_shin'],places=5)
    def test_shin_pixel_angle_already_invariant(self):
        p=PoseAnalyzer.__new__(PoseAnalyzer)
        a=p._calculate_metrics(self.landmarks(400,400),(400,400,3))
        b=p._calculate_metrics(self.landmarks(800,400),(400,800,3))
        self.assertAlmostEqual(a['left_shin_angle'],b['left_shin_angle'],places=5)

if __name__=='__main__':unittest.main()
