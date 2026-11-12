"""Production source consumer gates. No production service/database is touched."""
import tempfile
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from measurement_safety import safe_metrics, strict_values, finite
from report_generator import ReportGenerator
from history import HistoryStore
from training_link import build_training_session, recommend_drills, track_progress
from trend import compute_trend


def frame(**kwargs):
    return dict(measurement_status='unscored_2d_geometry_only', knee_flexion=170,
                shin_angle=80, body_leang=0, **kwargs)


class ProductionSafetyTests(unittest.TestCase):
    def test_candidate_never_enters_legacy_scoring(self):
        r = ReportGenerator()
        m = r._calculate_comprehensive_metrics([frame()])
        self.assertEqual(m['avg_body_leang'], 0)
        self.assertIsNone(r._calculate_score(m)['total'])
        self.assertEqual(m['breakdown'], [])

    def test_even_finite_legacy_cannot_activate_old_scores(self):
        r = ReportGenerator()
        m = r._calculate_comprehensive_metrics([{'knee_flexion': 170, 'shin_angle': 90,
            'body_leang': 5, 'stride_analysis': {'stride_distance': 0, 'center_of_mass_x': .5},
            'symmetry': {'symmetry_score': 100}}])
        self.assertEqual(m['measurement_status'], 'insufficient_evidence')
        self.assertIsNone(m['avg_knee_flexion'])
        self.assertIsNone(r._calculate_score(m)['total'])

    def test_nan_cannot_become_100(self):
        r = ReportGenerator()
        for value in (None, float('nan'), float('inf'), True, '90'):
            with self.subTest(value=value):
                self.assertIsNone(r._interp(value, [(0, 0), (90, 100)]))
                b = r._mk('test', 'test', 'test', '正常', value, '建议')
                self.assertIsNone(b['score'])
                self.assertEqual(b['result'], '未验证')
                self.assertIsNone(r._weighted_total([b]))
                self.assertIsNone(r._grade_of(value))
        self.assertEqual(r._interp(0, [(0, 0), (90, 100)]), 0)

    def test_missing_score_does_not_trigger_default_scoring(self):
        self.assertIsNone(ReportGenerator()._calculate_score({'avg_knee_flexion': None})['total'])

    def test_unscored_training_has_no_prescription(self):
        m = safe_metrics([frame()])
        bad_gait = {'cadence': 90, 'vertical_oscillation': .2}
        session = build_training_session(m, bad_gait, problems=['严重跨步'])
        for key in ('drills', 'weekly_plan', 'strength', 'weaknesses'):
            self.assertEqual(session[key], [])
        self.assertEqual(recommend_drills(m, bad_gait)['count'], 0)
        self.assertEqual(ReportGenerator()._build_training_plan(m)['weekly_plan'], [])

    def test_history_roundtrip_preserves_unknown_and_evidence(self):
        with TemporaryDirectory(dir=Path(tempfile.gettempdir())) as folder:
            store = HistoryStore(str(Path(folder)/'data/history.db'))
            m = dict(safe_metrics([frame()]), avg_knee=170, cadence=None)
            rec = store.add_record(None, score=100, grade='优秀', metrics=m,
                                   problems=['问题'], suggestions=['加量'])
            got = store.get_record(rec['id'])
            self.assertIsNone(got['score'])
            self.assertIsNone(got['grade'])
            self.assertEqual(got['suggestions'], [])
            self.assertEqual(got['metrics']['measurement_status'], 'unscored_2d_geometry_only')
            self.assertIsNone(got['metrics']['measurement_coverage']['knee_flexion']['coverage'])

    def test_no_score_change_from_unknown(self):
        rows = [{'score': None, 'created_at': '1', 'metrics': {'measurement_status': 'unscored_2d_geometry_only'}},
                {'score': 90, 'created_at': '2', 'metrics': {}}]
        p = track_progress(rows)
        self.assertIsNone(p['score_change'])
        self.assertEqual(p['improvements'], [])

    def test_no_technical_trend_from_unscored_geometry(self):
        rows = [{'avg_shin': 50, 'metrics': {'measurement_status': 'unscored_2d_geometry_only'}},
                {'avg_shin': 90, 'metrics': {'measurement_status': 'unscored_2d_geometry_only'}}]
        self.assertEqual(compute_trend(rows), {})

    def test_strict_numeric_normalization_keeps_ids_and_zero(self):
        p = strict_values({'id': 9007199254740993, 'zero': 0, 'invalid': [float('nan'), float('inf')]})
        self.assertEqual(p['id'], 9007199254740993)
        self.assertIs(type(p['id']), int)
        self.assertEqual(p['zero'], 0)
        self.assertEqual(p['invalid'], [None, None])
        json.dumps(p, allow_nan=False)

    def test_production_report_entry_pdf_and_docx(self):
        from docx import Document
        import fitz
        with TemporaryDirectory(dir=Path(tempfile.gettempdir())) as folder:
            for fmt in ('pdf', 'docx'):
                path = Path(folder)/('report.'+fmt)
                r = ReportGenerator()
                got = r.generate_report('local.mp4', {'name': '测试'}, [frame()], str(path), fmt=fmt)
                self.assertEqual(str(path), got)
                self.assertIsNone(r.score_info['total'])
                if fmt == 'pdf':
                    with fitz.open(got) as doc:
                        text = ''.join(page.get_text() for page in doc)
                else:
                    doc = Document(got)
                    text = '\n'.join(p.text for p in doc.paragraphs)
                    text += '\n'.join(c.text for t in doc.tables for row in t.rows for c in row.cells)
                self.assertIn('未知', text)
                self.assertNotIn('基本正常', text)
                self.assertNotIn('保持当前良好跑姿', text)


if __name__ == '__main__':
    unittest.main()
