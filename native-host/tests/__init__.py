"""Test package for the native messaging host.

The host lives in ``ytdlp_host/`` next to this directory rather than under
``src/``, so make the project root importable no matter how the tests are
launched (``python -m unittest``, ``pytest``, an IDE runner...).
"""

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
