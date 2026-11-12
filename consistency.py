"""App-owned cooperative cross-process snapshot gate (Linux flock)."""
import fcntl
import os
from contextlib import contextmanager


def open_gate(exclusive=False, blocking=True):
    path = os.environ.get('POSERUN_CONSISTENCY_LOCK', 'data/.consistency.lock')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    f = open(path, 'a+b')
    try:
        fcntl.flock(f, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                    | (0 if blocking else fcntl.LOCK_NB))
        return f
    except BaseException:
        f.close()
        raise


@contextmanager
def gate(exclusive=False, blocking=True):
    f = open_gate(exclusive, blocking)
    try:
        yield
    finally:
        f.close()
