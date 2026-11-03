"""Tests for ``to_dream.doctor``: individual checks with injected runners, plus an end-to-end JSON run."""
import argparse
import contextlib
import io
import json
import shutil
import subprocess
import unittest
from unittest import mock

from td_testing import IsolatedHomeTestCase
from test_runtime import FakePlugin

from to_dream import doctor, repo_setup


def result(code=0, out=""):
    return subprocess.CompletedProcess([], code, out, "")


class CheckTests(IsolatedHomeTestCase):
    def test_python(self):
        self.assertEqual(doctor.check_python((3, 10, 0)).status, "ok")
        self.assertEqual(doctor.check_python((3, 13, 1)).status, "ok")
        old = doctor.check_python((3, 9, 18))
        self.assertEqual(old.status, "fail")
        self.assertIn("3.10", old.fix)

    def test_git(self):
        self.assertEqual(doctor.check_git(lambda argv: result(0, "git version 2.50.0")).detail, "git version 2.50.0")
        missing = doctor.check_git(lambda argv: result(127))
        self.assertEqual(missing.status, "fail")
        self.assertTrue(missing.fix)

    def test_claude(self):
        found = doctor.check_claude(lambda argv: result(0, "2.1.312 (Claude Code)"), lambda name: "/bin/claude")
        self.assertEqual((found.status, found.detail), ("ok", "2.1.312 (Claude Code)"))
        self.assertEqual(doctor.check_claude(lambda a: result(), lambda n: None).status, "fail")
        self.assertEqual(doctor.check_claude(lambda a: result(1), lambda n: "/bin/claude").status, "warn")

    def test_memory_root(self):
        (self.memory_root / "proj").mkdir()
        (self.memory_root / ".git").mkdir()
        ok = doctor.check_memory_root(self.memory_root)
        self.assertEqual(ok.status, "ok")
        self.assertIn("1 project", ok.detail)
        self.assertEqual(doctor.check_memory_root(self.tmp / "nope").status, "fail")

    def test_update_statuses(self):
        base = {"current": "0.1.0", "latest": None, "update_available": False}
        self.assertEqual(doctor.check_update({**base, "status": "unknown"}).status, "ok")
        self.assertEqual(doctor.check_update({**base, "status": "disabled"}).status, "ok")
        self.assertEqual(doctor.check_update({**base, "latest": "0.1.0", "status": "ok"}).status, "ok")
        newer = doctor.check_update({**base, "latest": "0.2.0", "update_available": True, "status": "ok"})
        self.assertEqual(newer.status, "warn")
        self.assertIn("/plugin marketplace update to-dream", newer.fix)

    def _runtime_check(self, runtime_version, plugin_version):
        plugin = FakePlugin(self.tmp / ("plugin-" + plugin_version), plugin_version)
        (self.home / "runtime").mkdir()
        (self.home / "runtime" / "VERSION").write_text(runtime_version + "\n")
        with mock.patch.object(doctor.version, "ROOT", plugin.root):
            return doctor.check_runtime()

    def test_runtime_newer_than_plugin_warns_with_update_hint(self):
        check = self._runtime_check("0.1.2", "0.1.1")
        self.assertEqual(check.status, "warn")
        self.assertIn("0.1.2", check.detail)
        self.assertIn("0.1.1", check.detail)
        self.assertIn("/plugin marketplace update to-dream", check.fix)
        self.assertIn("/reload-plugins", check.fix)

    def test_runtime_older_than_plugin_still_suggests_sync(self):
        check = self._runtime_check("0.1.0", "0.1.1")
        self.assertEqual((check.status, check.fix), ("warn", "Run `to-dream sync-runtime`."))

    def test_schedule_statuses(self):
        self.assertEqual(doctor.check_schedule({"installed": False}).status, "warn")
        ok = doctor.check_schedule({"installed": True, "detail": "systemd user timer", "time": "03:30"})
        self.assertEqual((ok.status, ok.detail), ("ok", "systemd user timer at 03:30"))

    def test_exit_code_and_render(self):
        checks = [doctor.Check("a", "ok", "fine"), doctor.Check("b", "warn", "hmm", "do x")]
        self.assertEqual(doctor.overall_exit_code(checks), 0)
        self.assertEqual(doctor.render(checks), "[ok] a: fine\n[warn] b: hmm\n      fix: do x")
        checks.append(doctor.Check("c", "fail", "bad", "fix it"))
        self.assertEqual(doctor.overall_exit_code(checks), 1)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class MemoryRepoCheckTests(IsolatedHomeTestCase):
    def test_not_a_repo_warns_with_init_hint(self):
        check = doctor.check_memory_repo(self.memory_root)
        self.assertEqual(check.status, "warn")
        self.assertIn("init-repo", check.fix)

    def test_good_repo_is_ok_and_reports_branch(self):
        repo_setup.init_repo(self.memory_root, "mem")
        check = doctor.check_memory_repo(self.memory_root)
        self.assertEqual(check.status, "ok")
        self.assertIn("mem", check.detail)

    def test_repo_without_ignore_rules_warns(self):
        subprocess.run(["git", "init"], cwd=str(self.memory_root), capture_output=True)
        check = doctor.check_memory_repo(self.memory_root)
        self.assertEqual(check.status, "warn")
        self.assertIn(".gitignore", check.detail)


class EndToEndTests(IsolatedHomeTestCase):
    def test_json_output_has_every_check(self):
        out = io.StringIO()
        with mock.patch.object(doctor.update, "check_update",
                               return_value={"current": "0.1.0", "latest": None, "update_available": False,
                                             "status": "unknown"}), \
                mock.patch.object(doctor.schedule, "status", return_value={"installed": False}), \
                contextlib.redirect_stdout(out):
            code = doctor._handle(argparse.Namespace(json=True))
        data = json.loads(out.getvalue())
        names = [c["name"] for c in data["checks"]]
        self.assertEqual(names, ["python", "git", "claude", "memory-root", "memory-repo", "runtime",
                                 "update", "schedule"])
        self.assertEqual(data["home"], str(self.home))
        self.assertTrue(all(c["status"] in ("ok", "warn", "fail") for c in data["checks"]))
        self.assertEqual(code, 0 if data["ok"] else 1)
        self.assertEqual(data["ok"], all(c["status"] != "fail" for c in data["checks"]))


if __name__ == "__main__":
    unittest.main()
