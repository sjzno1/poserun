"""Synthetic evidence-contract tests; no identity/duplicate ground truth claims."""
import copy
import math
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from calibration.overlap_evidence import compare, POLICY


def detection():
    xy=[[50.,10.] for _ in range(17)]
    for j,p in {5:[40.,30.],6:[60.,30.],7:[35.,60.],8:[65.,60.],9:[30.,80.],10:[70.,80.],
                11:[45.,80.],12:[55.,80.],13:[45.,120.],14:[55.,120.],15:[45.,160.],16:[55.,160.]}.items():
        xy[j]=p
    return {'box':[.2,.1,.8,.9],'xy':xy,'confidence':[1.]*17}


class OverlapEvidenceTests(unittest.TestCase):
    def test_exact_projection_is_not_identity_or_auto_merge(self):
        a=detection();r=compare(a,a,100,200)
        self.assertEqual(r['screen'],'close_projection_only')
        self.assertEqual(r['identity_relation'],'unknown')
        self.assertFalse(r['automatic_merge'])
        self.assertEqual(r['max_normalized_distance'],0)

    def test_one_missing_ankle_blocks_even_if_other_eleven_match(self):
        a,b=detection(),detection();b['confidence'][16]=0
        r=compare(a,b,100,200)
        self.assertEqual(r['screen'],'insufficient_evidence')
        self.assertEqual(r['confident_body_joint_count'],11)
        self.assertIsNone(r['max_normalized_distance'])

    def test_one_confident_conflicting_joint_is_not_hidden_by_median(self):
        a,b=detection(),detection();b['xy'][16][0]+=30
        r=compare(a,b,100,200)
        self.assertEqual(r['screen'],'geometric_disagreement')
        self.assertEqual(r['reason'],'confident_joints_disagree')

    def test_face_keypoints_are_never_used(self):
        a,b=detection(),detection()
        for j in range(5):b['xy'][j]=[None,None];b['confidence'][j]=None
        self.assertEqual(compare(a,b,100,200)['screen'],'close_projection_only')

    def test_comparison_symmetric(self):
        a,b=detection(),detection();b['xy'][8][0]+=1
        self.assertEqual(compare(a,b,100,200),compare(b,a,100,200))

    def test_uniform_scale_invariance(self):
        a,b=detection(),detection();b['xy'][8][0]+=1
        before=compare(a,b,100,200)
        for d in (a,b):d['xy']=[[x*4,y*4] for x,y in d['xy']]
        after=compare(a,b,400,800)
        self.assertEqual(before,after)

    def test_padding_invariance_uses_pixels_not_independent_normalization(self):
        a,b=detection(),detection();b['xy'][8][0]+=1
        before=compare(a,b,100,200)
        for d in (a,b):
            x1,y1,x2,y2=d['box'];d['box']=[(x1*100+30)/160,(y1*200+20)/240,(x2*100+30)/160,(y2*200+20)/240]
            d['xy']=[[x+30,y+20] for x,y in d['xy']]
        after=compare(a,b,160,240)
        self.assertEqual(before['screen'],after['screen'])
        self.assertAlmostEqual(before['max_normalized_distance'],after['max_normalized_distance'])

    def test_mirror_invariance(self):
        a,b=detection(),detection();b['xy'][8][0]+=1
        before=compare(a,b,100,200)
        for d in (a,b):
            x1,y1,x2,y2=d['box'];d['box']=[1-x2,y1,1-x1,y2]
            d['xy']=[[100-x,y] for x,y in d['xy']]
        self.assertEqual(before,compare(a,b,100,200))

    def test_malformed_and_nonfinite_inputs_fail_closed(self):
        for change in [('box',None),('xy',[]),('confidence',None)]:
            a,b=detection(),detection();b[change[0]]=change[1]
            self.assertEqual(compare(a,b,100,200)['screen'],'insufficient_evidence')
        for bad in (None,float('nan'),float('inf'),True,-1,201):
            a,b=detection(),detection();b['xy'][16][1]=bad
            self.assertEqual(compare(a,b,100,200)['screen'],'insufficient_evidence')

    def test_invalid_dimensions(self):
        for width in (0,-1,True,None,float('nan')):
            self.assertEqual(compare(detection(),detection(),width,200)['reason'],'invalid_image_dimensions')

    def test_degenerate_segments_rejected(self):
        a,b=detection(),detection();b['xy'][16]=b['xy'][14].copy()
        self.assertEqual(compare(a,b,100,200)['reason'],'degenerate_body_segments')

    def test_disjoint_boxes_are_not_dismissed_by_matching_pose(self):
        a,b=detection(),detection();b['box']=[.8,.1,1,.9]
        self.assertEqual(compare(a,b,100,200)['reason'],'boxes_not_close_enough')

    def test_input_not_mutated(self):
        a,b=detection(),detection();old=copy.deepcopy((a,b,POLICY))
        compare(a,b,100,200)
        self.assertEqual((a,b,POLICY),old)


if __name__=='__main__':unittest.main()
