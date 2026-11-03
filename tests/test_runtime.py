"""Tests for ``to_dream.runtime``: copying the code into $TO_DREAM_HOME/runtime and refreshing it."""
import argparse
import contextlib
import io
import json
import random
import shutil
import unittest
from unittest import mock

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

    def snapshot(self):
        """Every runtime file's bytes, keyed by relative path."""
        return {p.relative_to(self.target).as_posix(): p.read_bytes()
                for p in sorted(self.target.rglob("*")) if p.is_file()}

    def sync_at(self, runtime_version, plugin_version, **kwargs):
        """Install the runtime at ``runtime_version``, then sync a plugin at ``plugin_version``."""
        self.plugin.set_version(runtime_version)
        self.sync()
        self.plugin.set_version(plugin_version)
        (self.plugin.root / "lib" / "to_dream" / "mod.py").write_text("X = 'from %s'\n" % plugin_version)
        return runtime.sync(root=self.plugin.root, home=self.home, **kwargs)

    def test_newer_runtime_is_not_downgraded(self):
        self.plugin.set_version("0.1.2")
        self.sync()
        before = self.snapshot()
        self.plugin.set_version("0.1.1")
        (self.plugin.root / "lib" / "to_dream" / "mod.py").write_text("X = 'old'\n")
        path, changed = self.sync()
        self.assertEqual((path, changed), (self.target, False))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.1.2")
        self.assertEqual(runtime.newer_runtime(self.plugin.root, self.target), "0.1.2")

    def test_force_downgrades(self):
        _, changed = self.sync_at("0.1.2", "0.1.1", force=True)
        self.assertTrue(changed)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.1.1")
        self.assertEqual((self.target / "lib" / "to_dream" / "mod.py").read_text(), "X = 'from 0.1.1'\n")

    def test_same_version_different_content_refreshes(self):
        _, changed = self.sync_at("0.1.2", "0.1.2")
        self.assertTrue(changed)
        self.assertEqual((self.target / "lib" / "to_dream" / "mod.py").read_text(), "X = 'from 0.1.2'\n")

    def test_older_runtime_is_upgraded(self):
        _, changed = self.sync_at("0.1.1", "0.1.2")
        self.assertTrue(changed)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.1.2")

    def test_numeric_not_lexical_comparison(self):
        # "0.9.0" > "0.10.0" as strings, but 0.10.0 is the newer version
        _, changed = self.sync_at("0.9.0", "0.10.0")
        self.assertTrue(changed)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.10.0")

    def test_unparseable_versions_refresh(self):
        for stamped, plugin_version in (("garbage", "0.1.1"), ("0.1.2", "not-semver"), ("", "0.1.1")):
            with self.subTest(stamped=stamped, plugin=plugin_version):
                self.plugin.set_version("0.0.1")
                self.sync()
                (self.target / "VERSION").write_text(stamped + "\n")
                self.plugin.set_version(plugin_version)
                self.assertTrue(self.sync()[1])
                self.assertEqual((self.target / "VERSION").read_text().strip(), plugin_version)

    def test_missing_version_stamp_refreshes(self):
        self.plugin.set_version("0.1.1")
        self.sync()
        (self.target / "VERSION").unlink()
        (self.target / "plugin.json").write_text(json.dumps({"version": "9.9.9"}))  # not a stamp: ignored
        self.assertTrue(self.sync()[1])
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.1.1")

    def test_refresh_happens_iff_force_or_not_newer_target_property(self):
        rng = random.Random(20261103)
        for _ in range(60):
            target_v, source_v = ("%d.%d.%d" % (rng.randint(0, 3), rng.randint(0, 12), rng.randint(0, 12))
                                  for _ in range(2))
            target_key = tuple(int(n) for n in target_v.split("."))
            source_key = tuple(int(n) for n in source_v.split("."))
            for force in (False, True):
                with self.subTest(target=target_v, source=source_v, force=force):
                    shutil.rmtree(self.target, ignore_errors=True)
                    _, changed = self.sync_at(target_v, source_v, force=force)
                    self.assertEqual(changed, force or not target_key > source_key)
                    expected = source_v if changed else target_v
                    self.assertEqual((self.target / "VERSION").read_text().strip(), expected)

    def run_handler(self, *argv):
        """Run ``sync-runtime`` through the real argparse wiring against the fake plugin."""
        parser = argparse.ArgumentParser()
        runtime.add_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["sync-runtime", *argv])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(version, "ROOT", self.plugin.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()

    def test_cli_skip_keeps_stdout_to_the_path_and_notes_on_stderr(self):
        self.plugin.set_version("0.1.2")
        self.sync()
        self.plugin.set_version("0.1.1")
        code, out, err = self.run_handler()
        self.assertEqual((code, out), (0, str(self.target) + "\n"))
        self.assertEqual(
            err.strip(),
            "to-dream: runtime is 0.1.2, newer than this plugin (0.1.1); left as is. Update this session with "
            "/plugin marketplace update to-dream then /reload-plugins, or pass --force to downgrade.")
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.1.2")

    def test_cli_force_downgrades_silently(self):
        self.plugin.set_version("0.1.2")
        self.sync()
        self.plugin.set_version("0.1.1")
        code, out, err = self.run_handler("--force")
        self.assertEqual((code, out, err), (0, str(self.target) + "\n", ""))
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.1.1")

    def test_cli_normal_sync_has_no_stderr_note(self):
        self.plugin.set_version("0.1.1")
        code, out, err = self.run_handler()
        self.assertEqual((code, out, err), (0, str(self.target) + "\n", ""))

    def test_real_plugin_syncs_and_runs_from_copy(self):
        path, changed = runtime.sync(home=self.home)
        self.assertTrue(changed)
        self.assertTrue((path / "scripts" / "to_dream.py").is_file())
        self.assertTrue((path / "lib" / "to_dream" / "runtime.py").is_file())
        self.assertEqual((path / "VERSION").read_text().strip(), version.current_version())


if __name__ == "__main__":
    unittest.main()
