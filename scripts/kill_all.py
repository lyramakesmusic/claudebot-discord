"""Kill all claudebot processes. DEPRECATED — use nuke_and_restart.py instead.

This is a thin wrapper that delegates to nuke_and_restart.py --kill.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.execv(
    sys.executable,
    [sys.executable, os.path.join(_ROOT, "scripts", "nuke_and_restart.py"), "--kill"],
)
