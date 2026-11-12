import tempfile
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from calibration.report_adapter import build_payload, create_preview_app, export_reports, render_html


def frame(index=0, **values):
    return dict(measurement_status='unscored_2d_geometry_only', frame_index=index, **values)


class ReportContractTests(unittest.TestCase):
    def test_all_missing_stays_null(self):
        p = build_payload([frame()], expected_frames=1)
        self.assertIsNone(p['score'])
        self.assertIsNone(p['grade'])
        self.assertEqual(p['problems'], [])
        self.assertTrue(all(m['value'] is None for m in p['metrics'].values()))
        self.assertEqual(p['metrics']['knee_flexion']['coverage'], 0)

    def test_invalid_values_are_not_normal(self):
        values = [None, float('nan'), float('inf'), float('-inf'), True, '165', -1, 181]
        p = build_payload([frame(i, knee_flexion=v) for i, v in enumerate(values)], expected_frames=len(values))
        self.assertIsNone(p['metrics']['knee_flexion']['value'])
        json.dumps(p, allow_nan=False)

    def test_zero_is_valid_and_partial_coverage_uses_timeline(self):
        p = build_payload([frame(0, body_leang=0), frame(2, body_leang=10)], expected_frames=4)
        self.assertEqual(p['metrics']['body_leang']['value'], 5)
        self.assertEqual(p['metrics']['body_leang']['coverage'], .5)

    def test_no_denominator_means_unknown_coverage(self):
        p = build_payload([frame(knee_flexion=170)])
        self.assertIsNone(p['metrics']['knee_flexion']['coverage'])

    def test_empty_is_unscored(self):
        p = build_payload([], expected_frames=0)
        self.assertIsNone(p['metrics']['shin_angle']['value'])
        self.assertIsNone(p['metrics']['shin_angle']['coverage'])

    def test_no_unvalidated_injection(self):
        p = build_payload([frame(cadence=195, score=100, symmetry={'symmetry_score': 100})])
        self.assertIsNone(p['metrics']['cadence']['value'])
        self.assertIsNone(p['metrics']['symmetry']['value'])
        self.assertIsNone(p['score'])

    def test_rejects_non_candidate(self):
        with self.assertRaises(ValueError):
            build_payload([{'knee_flexion': 170}])

    def test_rejects_bad_denominators(self):
        for n in (True, -1, 1.5, 0):
            with self.subTest(n=n), self.assertRaises(ValueError):
                build_payload([frame()], expected_frames=n)

    def test_rejects_invalid_frame_identity(self):
        for frames in ([frame(), frame()], [frame(3)], [frame(True)], [frame(-1)]):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                build_payload(frames, expected_frames=2)

    def test_geometry_has_no_score_or_technical_diagnosis(self):
        p = build_payload([frame(knee_flexion=170, shin_angle=89, body_leang=5)], expected_frames=1)
        self.assertIsNone(p['score'])
        self.assertEqual(p['breakdown'], [])
        self.assertTrue(all(m['score'] is None for m in p['metrics'].values()))
        text = render_html(p)
        for false_claim in ('基本正常', '优秀', '前掌/中足触地'):
            self.assertNotIn(false_claim, text)
        self.assertIn('未知／未验证', text)

    def test_api_roundtrip(self):
        client = create_preview_app().test_client()
        response = client.post('/offline/report-preview', json={
            'frames': [frame(knee_flexion=170), frame(2)], 'expected_frames': 4})
        self.assertEqual(response.status_code, 200)
        p = response.get_json()
        self.assertIsNone(p['score'])
        self.assertEqual(p['metrics']['knee_flexion']['coverage'], .25)
        self.assertIsNone(p['metrics']['cadence']['value'])

    def test_api_rejects_bad_input(self):
        client = create_preview_app().test_client()
        for data in ([], {}, {'frames': [{}]}, {'frames': 'invalid'}):
            with self.subTest(data=data):
                self.assertEqual(client.post('/offline/report-preview', json=data).status_code, 400)

    def test_real_report_formats_and_text(self):
        from docx import Document
        import fitz
        p = build_payload([frame()], expected_frames=2)
        with TemporaryDirectory(dir=str(Path(tempfile.gettempdir()))) as folder:
            paths = export_reports(p, Path(folder)/'report')
            self.assertEqual(json.loads(Path(paths['json']).read_text()), p)
            self.assertIn('未知／未验证', Path(paths['html']).read_text())
            doc = Document(paths['docx'])
            text = '\n'.join(x.text for x in doc.paragraphs)
            text += '\n'.join(c.text for t in doc.tables for r in t.rows for c in r.cells)
            self.assertIn('总分：未知／未验证', text)
            pdf = fitz.open(paths['pdf'])
            self.assertGreater(len(pdf), 0)
            pdf_text = ''.join(page.get_text() for page in pdf)
            pdf.close()
            self.assertIn('未知', pdf_text)
            self.assertNotIn('基本正常', pdf_text)


if __name__ == '__main__':
    unittest.main()
