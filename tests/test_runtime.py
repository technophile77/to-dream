"""Tests for ``to_dream.runtime``: copying the code into $TO_DREAM_HOME/runtime and refreshing it."""
import json
import unittest

from td_testing import IsolatedHomeTestCase

from to_dream import runtime, version


class FakePlugin:
    """Builds a minimal plugin tree (scripts/, lib/, .claude-plugin/plugin.json) to sync from."""

    def __init__(self, root, ver="1.0.0"):
        self.root = root
        (root / "scripts").mkdir(parents=True)
        (root / "lib" / "to_dream").mkdir(parents=True)
        (root / ".claude-plugin").mkdir()
        (root / "scripts" / "to_dream.py").write_text("print('entry')\n")
        (root / "lib" / "to_dream" / "mod.py").write_text("X = 1\n")
        self.set_version(ver)

    def set_version(self, ver):
        (self.root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "to-dream", "version": ver}))


class RuntimeTests(IsolatedHomeTestCase):
    def setUp(self):
        super().setUp()
        self.plugin = FakePlugin(self.tmp / "plugin")
        self.target = self.home / "runtime"

    def sync(self):
        return runtime.sync(root=self.plugin.root, home=self.home)

    def test_first_sync_copies_everything_and_stamps_version(self):
        path, changed = self.sync()
        self.assertEqual((path, changed), (self.target, True))
        self.assertEqual((self.target / "scripts" / "to_dream.py").read_text(), "print('entry')\n")
        self.assertEqual((self.target / "lib" / "to_dream" / "mod.py").read_text(), "X = 1\n")
        self.assertEqual((self.target / "VERSION").read_text().strip(), "1.0.0")
        self.assertEqual(json.loads((self.target / "plugin.json").read_text())["version"], "1.0.0")

    def test_second_sync_is_a_noop(self):
        self.sync()
        marker = self.target / "scripts" / "to_dream.py"
        before = marker.stat().st_mtime_ns
        _, changed = self.sync()
        self.assertFalse(changed)
        self.assertEqual(marker.stat().st_mtime_ns, before)

    def test_version_change_refreshes_and_removes_deleted_files(self):
        self.sync()
        self.plugin.set_version("1.1.0")
        (self.plugin.root / "lib" / "to_dream" / "mod.py").unlink()
        (self.plugin.root / "lib" / "to_dream" / "new.py").write_text("Y = 2\n")
        _, changed = self.sync()
        self.assertTrue(changed)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "1.1.0")
        self.assertFalse((self.target / "lib" / "to_dream" / "mod.py").exists())
        self.assertTrue((self.target / "lib" / "to_dream" / "new.py").exists())

    def test_content_change_without_version_bump_still_refreshes(self):
        self.sync()
        (self.plugin.root / "lib" / "to_dream" / "mod.py").write_text("X = 2\n")
        self.assertTrue(self.sync()[1])
        self.assertEqual((self.target / "lib" / "to_dream" / "mod.py").read_text(), "X = 2\n")

    def test_damaged_runtime_is_repaired(self):
        self.sync()
        (self.target / "scripts" / "to_dream.py").write_text("tampered")
        self.assertTrue(self.sync()[1])
        self.assertEqual((self.target / "scripts" / "to_dream.py").read_text(), "print('entry')\n")

    def test_pycache_is_not_copied(self):
        cache = self.plugin.root / "lib" / "to_dream" / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-310.pyc").write_bytes(b"\0")
        self.sync()
        self.assertFalse((self.target / "lib" / "to_dream" / "__pycache__").exists())
        self.assertFalse(self.sync()[1])

    def test_running_from_the_runtime_copy_is_a_noop(self):
        self.sync()
        path, changed = runtime.sync(root=self.target, home=self.home)
        self.assertEqual((path, changed), (self.target, False))

    def test_runtime_copy_reports_its_own_version(self):
        self.sync()
        self.assertEqual(version.current_version(self.target), "1.0.0")

    def test_real_plugin_syncs_and_runs_from_copy(self):
        path, changed = runtime.sync(home=self.home)
        self.assertTrue(changed)
        self.assertTrue((path / "scripts" / "to_dream.py").is_file())
        self.assertTrue((path / "lib" / "to_dream" / "runtime.py").is_file())
        self.assertEqual((path / "VERSION").read_text().strip(), version.current_version())


if __name__ == "__main__":
    unittest.main()
