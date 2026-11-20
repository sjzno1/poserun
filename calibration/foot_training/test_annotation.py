import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from annotation import make_record, validate_dataset, validate_record, write_review_page


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.tmp = tempfile.TemporaryDirectory(dir=str(Path(tempfile.gettempdir())))
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name)/'image.png'
        Image.new('RGB', (100, 200)).save(self.image)
        self.points = [[30, 60] for _ in range(133)]
        self.scores = [.9] * 133
        self.record = make_record(self.image, self.points, self.scores, model_sha256='a'*64)

    def cleared(self, identity='subject-a', split='train'):
        r = copy.deepcopy(self.record)
        r.update(reviewed=True, reviewer='fixture-reviewer', subject_id=identity,
                 session_id=identity+'-session', split=split)
        r['rights'] = {'training_allowed': True, 'basis': 'explicit synthetic fixture permission'}
        return r

    def test_proposals_are_not_training_truth(self):
        self.assertTrue(validate_record(self.record))
        self.assertFalse(self.record['reviewed'])
        with self.assertRaises(ValueError):
            validate_record(self.record, for_training=True)

    def test_missing_or_zero_confidence_stays_unknown(self):
        for value in (0, float('nan'), float('inf'), -.1):
            scores = list(self.scores)
            scores[19] = value
            r = make_record(self.image, self.points, scores, model_sha256='a'*64)
            self.assertEqual(r['points']['left_heel']['visibility'], 0)
            self.assertIsNone(r['points']['left_heel']['x'])
            json.dumps(r, allow_nan=False)

    def test_nonfinite_or_outside_coordinate_unknown(self):
        for x in (-1, 100, float('nan'), float('inf')):
            points = copy.deepcopy(self.points)
            points[19] = [x, 60]
            r = make_record(self.image, points, self.scores, model_sha256='a'*64)
            self.assertIsNone(r['points']['left_heel']['x'])

    def test_mapping_is_anatomical_not_openpose(self):
        points = [[i/2, 60] for i in range(133)]
        r = make_record(self.image, points, self.scores, model_sha256='a'*64)
        self.assertEqual(r['points']['left_heel']['x'], 19/2)
        self.assertEqual(r['points']['right_heel']['x'], 22/2)
        self.assertEqual(len(r['points']), 10)

    def test_wrong_format_is_rejected(self):
        for n in (17, 26, 134):
            with self.assertRaises(ValueError):
                make_record(self.image, [[1, 2]]*n, [1]*n, model_sha256='a'*64)

    def test_no_event_from_still_image(self):
        for key, value in (('events', [{'type': 'initial_contact'}]),
                           ('contact', {'left':'contact','right':'unknown'})):
            r = copy.deepcopy(self.record)
            r[key] = value
            with self.assertRaises(ValueError):
                validate_record(r)

    def test_training_needs_rights_review_identity_and_split(self):
        good = self.cleared()
        self.assertTrue(validate_record(good, for_training=True))
        for field, value in [('reviewed',False),('reviewer',''),('subject_id',None),
                             ('session_id',None),('split',None),('rights',{'training_allowed':True})]:
            r = copy.deepcopy(good)
            r[field] = value
            with self.assertRaises(ValueError):
                validate_record(r, for_training=True)

    def test_subject_leakage_fails(self):
        a, b = self.cleared(), self.cleared(split='test')
        b['id'] = 'b'*64
        b['session_id'] = 'other-session'
        with self.assertRaisesRegex(ValueError, 'leakage'):
            validate_dataset([a,b], for_training=True)

    def test_session_leakage_fails(self):
        a, b = self.cleared(), self.cleared('subject-b', 'test')
        b['id'] = 'b'*64
        b['session_id'] = a['session_id']
        with self.assertRaisesRegex(ValueError, 'leakage'):
            validate_dataset([a,b], for_training=True)

    def test_duplicate_content_fails(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_dataset([self.record,self.record])

    def test_unknown_cannot_keep_fake_coordinates(self):
        r = copy.deepcopy(self.record)
        r['points']['left_heel']['visibility'] = 0
        with self.assertRaises(ValueError):
            validate_record(r)

    def test_review_html_no_external_resources_or_script_escape(self):
        r = copy.deepcopy(self.record)
        r['source_path'] = '</script><script>alert(1)</script>'
        page = Path(self.tmp.name)/'review.html'
        write_review_page([r], page)
        text = page.read_text()
        self.assertNotIn('__RECORDS__', text)
        self.assertNotIn(r['source_path'], text)
        self.assertNotIn('<script src=', text)
        self.assertNotIn('fetch(', text)
        self.assertIn('data:image/png;base64,', text)


if __name__ == '__main__':
    unittest.main()
