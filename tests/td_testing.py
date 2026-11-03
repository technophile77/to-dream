"""Shared helpers for the packaging, update, schedule, runtime, doctor and repo-setup tests.

Importing this module puts the plugin's ``lib`` directory on ``sys.path``.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "to-dream"
LIB_DIR = PLUGIN_ROOT / "lib"

if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

ENV_KEYS = ("TO_DREAM_HOME", "TO_DREAM_MEMORY_ROOT", "TO_DREAM_NO_UPDATE_CHECK",
            "TO_DREAM_UPDATE_URL", "XDG_CONFIG_HOME")


class IsolatedHomeTestCase(unittest.TestCase):
    """Gives each test a temp ``TO_DREAM_HOME`` and ``TO_DREAM_MEMORY_ROOT`` and restores the env."""

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = Path(tempfile.mkdtemp(prefix="to-dream-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.memory_root = self.tmp / "projects"
        self.home.mkdir()
        self.memory_root.mkdir()
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["TO_DREAM_HOME"] = str(self.home)
        os.environ["TO_DREAM_MEMORY_ROOT"] = str(self.memory_root)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
