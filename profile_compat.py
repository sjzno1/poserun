"""Expose stdlib profiling API without shadowing the application's runner data.

Torch imports cProfile lazily during first prediction. cProfile then imports
'profile', which resolves to the app's profile.py when run from the project.
Load the real stdlib implementation by its interpreter-owned absolute path;
do not replace sys.modules or suppress an inference exception.
"""
import importlib.util
from pathlib import Path
import sysconfig

_path = Path(sysconfig.get_path('stdlib'))/'profile.py'
_spec = importlib.util.spec_from_file_location('_poserun_stdlib_profile', _path)
if _spec is None or _spec.loader is None:
    raise ImportError('cannot locate standard library profile API')
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
Profile = _module.Profile
run = _module.run
runctx = _module.runctx
_Utils = _module._Utils
