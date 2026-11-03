"""Test package marker; also puts the plugin library (and this directory) on ``sys.path``.

Lets ``python -m unittest discover -s tests`` (or ``-t .``) run from the repo root without installing anything.
"""

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
LIB_DIR = TESTS_DIR.parent / "plugins" / "to-dream" / "lib"

for entry in (LIB_DIR, TESTS_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
