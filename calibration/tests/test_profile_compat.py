"""The runner profile module must coexist with lazy cProfile/Torch imports."""
import subprocess
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]


class ProfileCompatibilityTests(unittest.TestCase):
    def check_order(self, statement):
        code = statement + '''
assert hasattr(profile, 'ProfileStore')
assert hasattr(profile, 'PersonalizedEngine')
p = cProfile.Profile()
assert p.runcall(sum, [1, 2, 3]) == 6
assert p.getstats()
assert callable(profile.run) and callable(profile.runctx)
from profile_compat import Profile as StandardProfile
p2 = StandardProfile()
assert p2.runcall(sum, [1, 2, 3]) == 6
print('profile compatibility OK')
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('profile compatibility OK', result.stdout)

    def test_application_profile_then_cprofile(self):
        self.check_order('import profile\nimport cProfile\n')

    def test_cprofile_then_application_profile(self):
        self.check_order('import cProfile\nimport profile\n')


if __name__ == '__main__':
    unittest.main()
