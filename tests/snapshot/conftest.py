"""Test setup of the planning half.

The package is imported as ``lib.snapshot``, with the plugin's root on the
path, however pytest is started.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
