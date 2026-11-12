"""Hard-timeout, atomic publication and batch fail-closed at public seams.
Run only in the NAS isolated test container (needs flask)."""
import inspect
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

WORK = Path(__file__).resolve().parent.parent


def _read_proc_state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except FileNotFoundError:
        return None


class WorkerProcessKill(unittest.TestCase):
    def test_hard_timeout_kills_sigterm_ignoring_group(self):
        import worker_supervisor as ws
        from compute_budget import BudgetExceeded
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp, 'pids')
            child = (
                "import os,signal,subprocess,sys,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
                "open(sys.argv[1],'w').write(f'{os.getpid()} {p.pid}');"
                "time.sleep(300)"
            )
            t0 = time.monotonic()
            with self.assertRaises(BudgetExceeded):
                ws.run_process([sys.executable, '-c', child, str(pidfile)], timeout=2)
            self.assertLess(time.monotonic() - t0, 15)
            pids = [int(x) for x in pidfile.read_text().split()]
            self.assertEqual(len(pids), 2)
            deadline = time.monotonic() + 5
            for pid in pids:
                while time.monotonic() < deadline:
                    state = _read_proc_state(pid)
                    if state in (None, 'Z'):
                        break
                    time.sleep(0.1)
                self.assertIn(_read_proc_state(pid), (None, 'Z'),
                              f'pid {pid} survived hard timeout')

    def test_analysis_lock_released_after_budget_failure(self):
        import flask
        from compute_budget import BudgetExceeded, bounded_analysis
        with tempfile.TemporaryDirectory() as tmp:
            lock = str(Path(tmp, 'data', '.analysis.lock'))
            app = flask.Flask(__name__)

            @bounded_analysis
            def ok():
                return {'ok': True}

            with patch.dict(os.environ, {'POSERUN_ANALYSIS_LOCK': lock}):
                with patch('worker_supervisor.execute_request',
                           side_effect=BudgetExceeded('hard timeout')):
                    with app.test_request_context('/api/analyze', method='POST'):
                        self.assertEqual(app.make_response(ok()).status_code, 422)
                # Lock must be free immediately for the next request.
                with patch('worker_supervisor.execute_request',
                           return_value={'ok': True}):
                    with app.test_request_context('/api/analyze', method='POST'):
                        self.assertEqual(app.make_response(ok()).status_code, 200)


class AtomicPublish(unittest.TestCase):
    """Publication seam: all artifacts + all history rows, or nothing."""

    def _root(self, tmp):
        from history import HistoryStore
        root = Path(tmp)
        (root / 'data').mkdir(parents=True)
        (root / 'reports').mkdir()
        (root / 'data' / 'keyframes').mkdir()
        store = HistoryStore(str(root / 'data' / 'history.db'))
        store.add_record(user={'id': 1, 'username': 'u'}, video_file='base.mp4')
        return root

    def _stage(self, root, ids=(2, 3)):
        from history import HistoryStore
        job = root / 'job'
        (job / 'data').mkdir(parents=True)
        (job / 'reports').mkdir()
        (job / 'data' / 'keyframes').mkdir()
        store = HistoryStore(str(job / 'data' / 'history.db'))
        for i in ids:
            store.add_record(user={'id': 1, 'username': 'u'},
                             video_file=f'v{i}.mp4', report_file=f'r{i}.pdf',
                             keyframes_dir=f'k{i}')
        # shift staged ids to the requested range (park first, avoid UNIQUE clash)
        with sqlite3.connect(job / 'data' / 'history.db') as db:
            existing = sorted(r[0] for r in db.execute('SELECT id FROM history_records'))
            for cur in existing:
                db.execute('UPDATE history_records SET id=? WHERE id=?', (cur + 1000, cur))
            for cur, target in zip(existing, ids):
                db.execute('UPDATE history_records SET id=? WHERE id=?', (target, cur + 1000))
        for i in ids:
            (job / 'reports' / f'r{i}.pdf').write_bytes(b'%PDF-1.4 dummy')
            kf = job / 'data' / 'keyframes' / f'k{i}'
            kf.mkdir()
            (kf / 'frame.jpg').write_bytes(b'dummy-frame')
        return job

    def _ids(self, root):
        with sqlite3.connect(root / 'data' / 'history.db') as db:
            return [r[0] for r in db.execute('SELECT id FROM history_records ORDER BY id')]

    def test_publish_moves_all_rows_and_artifacts(self):
        import worker_supervisor as ws
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            job = self._stage(root)
            ws.publish(job, root, 1)
            self.assertEqual(self._ids(root), [1, 2, 3])
            self.assertTrue((root / 'reports' / 'r2.pdf').is_file())
            self.assertTrue((root / 'reports' / 'r3.pdf').is_file())
            self.assertTrue((root / 'data' / 'keyframes' / 'k3' / 'frame.jpg').is_file())
            self.assertFalse((job / 'reports' / 'r2.pdf').exists())

    def test_publish_conflict_rolls_back_every_artifact_and_row(self):
        import worker_supervisor as ws
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            job = self._stage(root)
            (root / 'reports' / 'r3.pdf').write_bytes(b'pre-existing')
            with self.assertRaises(RuntimeError):
                ws.publish(job, root, 1)
            self.assertEqual(self._ids(root), [1])
            self.assertEqual(sorted(p.name for p in (root / 'reports').iterdir()), ['r3.pdf'])
            self.assertEqual(list((root / 'data' / 'keyframes').iterdir()), [])
            self.assertEqual((root / 'reports' / 'r3.pdf').read_bytes(), b'pre-existing')

    def test_publish_id_divergence_aborts_without_partial(self):
        import worker_supervisor as ws
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            job = self._stage(root, ids=(5, 6))
            with self.assertRaises(RuntimeError):
                ws.publish(job, root, 1)
            self.assertEqual(self._ids(root), [1])
            self.assertEqual(list((root / 'reports').iterdir()), [])


class BatchFailClosed(unittest.TestCase):
    def test_batch_missing_asset_404_before_any_analysis(self):
        import app as api
        with patch.object(api, 'authorized_asset_path', return_value=None):
            with api.app.test_request_context('/api/batch', method='POST',
                                              json={'video_files': ['a.mp4']}):
                resp = api.app.make_response(inspect.unwrap(api.batch_process)())
        self.assertEqual(resp.status_code, 404)

    def test_batch_all_videos_fail_returns_422_empty_results(self):
        import app as api

        class DummyAnalyzer:
            def close(self):
                pass

        with patch.object(api, 'authorized_asset_path',
                          side_effect=lambda kind, name: '/fake/' + name), \
             patch.object(os.path, 'isfile', return_value=True), \
             patch.object(api, 'preflight', return_value=None), \
             patch.object(api, 'PoseAnalyzer', DummyAnalyzer), \
             patch.object(api, 'process_single_person_video',
                          side_effect=RuntimeError('dummy failure')):
            with api.app.test_request_context(
                    '/api/batch', method='POST',
                    json={'video_files': ['a.mp4', 'b.mp4']}):
                resp = api.app.make_response(inspect.unwrap(api.batch_process)())
        self.assertEqual(resp.status_code, 422)
        payload = resp.get_json()
        self.assertEqual(payload['results'], [])
        self.assertEqual(payload['successful'], 0)
        self.assertEqual(len(payload['errors']), 2)

    def test_keyframe_path_roundtrip_no_double_prefix(self):
        import app as api
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(api, 'KEYFRAMES_FOLDER', tmp):
                p = api.get_keyframes_path('batch_kf_0_deadbeef')
                self.assertEqual(api.get_keyframes_path(os.path.basename(p)), p)


class WorkerSourceContract(unittest.TestCase):
    def test_worker_has_no_keyframe_prefix_wrapper(self):
        src = (WORK / 'analysis_worker.py').read_text(encoding='utf-8')
        self.assertNotIn('get_keyframes_path=lambda', src)
        self.assertIn("api.KEYFRAMES_FOLDER=str(job/'data'/'keyframes')", src)

    def test_supervisor_publishes_audit_and_query(self):
        src = (WORK / 'worker_supervisor.py').read_text(encoding='utf-8')
        self.assertIn('prior_audit', src)
        self.assertIn("request.query_string.decode()", src)


if __name__ == '__main__':
    unittest.main()