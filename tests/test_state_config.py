import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import helpers

from to_dream import config, state as dstate

assert helpers.LIB_DIR.is_dir()  # importing helpers puts the plugin library on sys.path
NOW = datetime(2026, 11, 2, 18, 30, 12, tzinfo=timezone.utc)


class EnvCase(unittest.TestCase):
    """Points TO_DREAM_HOME / TO_DREAM_MEMORY_ROOT at a temp dir for the test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {
            "TO_DREAM_HOME": str(self.tmp / "td"), "TO_DREAM_MEMORY_ROOT": str(self.tmp / "claude" / "projects"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)


class PathsTests(unittest.TestCase):
    def test_home_and_memory_root_default_below_the_user_home(self):
        fake_home = Path(tempfile.gettempdir()).resolve() / "fake-user"
        env = {k: v for k, v in os.environ.items() if not k.startswith("TO_DREAM_")}
        env.update(HOME=str(fake_home), USERPROFILE=str(fake_home))
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(dstate.home(), fake_home / ".claude" / "to-dream")
            self.assertEqual(dstate.memory_root(), fake_home / ".claude" / "projects")
            self.assertEqual(dstate.claude_home(), fake_home / ".claude")

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {"TO_DREAM_HOME": "/x/h", "TO_DREAM_MEMORY_ROOT": "/x/m/projects"}):
            self.assertEqual(dstate.home(), Path("/x/h"))
            self.assertEqual(dstate.memory_root(), Path("/x/m/projects"))
            self.assertEqual(dstate.claude_home(), Path("/x/m"))

    def test_layout_below_home(self):
        with mock.patch.dict(os.environ, {"TO_DREAM_HOME": "/x/h"}):
            home = Path("/x/h")
            self.assertEqual(dstate.state_path(), home / "state.json")
            self.assertEqual(dstate.run_dir("r1"), home / "runs" / "r1")
            self.assertEqual(dstate.rejected_log_path(), home / "rejected.md")
            self.assertEqual(dstate.config_path(), home / "config.json")
            self.assertEqual(dstate.worktrees_dir(), home / "worktrees")
            self.assertEqual(dstate.logs_dir(), home / "logs")
            self.assertEqual(dstate.steering_override_path(), home / "steering.md")


class StateTests(EnvCase):
    def test_roundtrip_and_missing_file(self):
        self.assertEqual(dstate.load_state(), {"last_dream": {}, "runs": {}})
        dstate.update_state(lambda s: dstate.set_last_dream(s, "p", NOW))
        self.assertEqual(dstate.get_last_dream(dstate.load_state(), "p"), NOW.replace(microsecond=0))
        self.assertIsNone(dstate.get_last_dream(dstate.load_state(), "other"))

    def test_foreign_top_level_keys_survive_updates(self):
        dstate.update_state(lambda s: s.update(update_notice={"latest": "9.9.9"}))
        dstate.update_state(lambda s: dstate.set_run(s, "r", status="pending"))
        state = dstate.load_state()
        self.assertEqual(state["update_notice"], {"latest": "9.9.9"})
        self.assertEqual(state["runs"]["r"]["status"], "pending")

    def test_corrupt_state_is_an_error_and_not_overwritten(self):
        dstate.state_path().parent.mkdir(parents=True)
        dstate.state_path().write_text("{broken", encoding="utf-8")
        with self.assertRaises(dstate.StateError):
            dstate.update_state(lambda s: None)
        self.assertEqual(dstate.state_path().read_text(encoding="utf-8"), "{broken")

    def test_non_object_state_is_an_error(self):
        dstate.state_path().parent.mkdir(parents=True)
        dstate.state_path().write_text("[1]", encoding="utf-8")
        with self.assertRaises(dstate.StateError):
            dstate.load_state()

    def test_failed_write_keeps_old_file_and_leaves_no_temp(self):
        dstate.update_state(lambda s: dstate.set_last_dream(s, "p", NOW))
        before = dstate.state_path().read_text(encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                dstate.update_state(lambda s: dstate.set_last_dream(s, "q", NOW))
        self.assertEqual(dstate.state_path().read_text(encoding="utf-8"), before)
        self.assertEqual(sorted(p.name for p in dstate.home().iterdir()), ["state.json"])

    def test_readers_never_see_a_torn_file(self):
        dstate.update_state(lambda s: dstate.set_last_dream(s, "p", NOW))
        stop = time.monotonic() + 1.0
        errors = []

        def reader():
            while time.monotonic() < stop:
                try:
                    dstate.load_state()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        for i in range(60):
            dstate.update_state(lambda s: dstate.set_run(s, "r", payload="x" * 5000, n=i))
        thread.join()
        self.assertEqual(errors, [])


class ConfigTests(EnvCase):
    def test_defaults_when_no_file(self):
        self.assertEqual(config.load(), config.DEFAULTS)
        self.assertFalse(dstate.config_path().exists())
        expected = {
            "model": "opus", "analyst_model": "sonnet", "default_days": 7, "timeout_min": 60,
            "branch_template": "dream/{run_id}", "worktree_dir": "", "worktree_template": "{run_id}",
            "steering_path": "", "schedule_time": "03:30", "update_check": True,
        }
        self.assertEqual(config.DEFAULTS, expected)

    def test_load_returns_a_copy(self):
        config.load()["model"] = "changed"
        self.assertEqual(config.get("model"), "opus")

    def test_set_get_roundtrip_with_coercion(self):
        cases = [
            ("default_days", "3", 3),
            ("timeout_min", "45", 45),
            ("timeout_min", "1.5", 1.5),
            ("timeout_min", "30.0", 30),
            ("update_check", "false", False),
            ("update_check", "ON", True),
            ("update_check", "0", False),
            ("schedule_time", "04:15", "04:15"),
            ("model", "  sonnet ", "sonnet"),
            ("branch_template", "feat/{project}-{yy}{mm}{dd}-{hhmm}", "feat/{project}-{yy}{mm}{dd}-{hhmm}"),
            ("worktree_template", "wt-{run_id}", "wt-{run_id}"),
            ("worktree_dir", "/some/where", "/some/where"),
            ("steering_path", "", ""),
        ]
        for key, raw, stored in cases:
            with self.subTest(key=key, raw=raw):
                self.assertEqual(config.set_value(key, raw), stored)
                self.assertEqual(config.get(key), stored)
                self.assertEqual(json.loads(dstate.config_path().read_text(encoding="utf-8"))[key], stored)

    def test_typed_values_are_accepted_too(self):
        self.assertEqual(config.set_value("default_days", 5), 5)
        self.assertEqual(config.set_value("update_check", False), False)
        self.assertEqual(config.set_value("timeout_min", 12.5), 12.5)

    def test_integer_is_stored_as_integer_in_json(self):
        config.set_value("default_days", "3")
        self.assertIn('"default_days": 3', dstate.config_path().read_text(encoding="utf-8"))

    def test_invalid_values_are_rejected_and_nothing_is_written(self):
        bad = [
            ("default_days", "0"), ("default_days", "-2"), ("default_days", "1.5"), ("default_days", "many"),
            ("default_days", True),
            ("timeout_min", "0"), ("timeout_min", "-5"), ("timeout_min", "soon"), ("timeout_min", "inf"),
            ("update_check", "maybe"), ("schedule_time", "25:00"), ("schedule_time", "3:30"), ("schedule_time", "0330"),
            ("model", ""), ("model", "   "), ("model", 7),
            ("branch_template", "has space/{run_id}"), ("branch_template", "dream/{nope}"),
            ("branch_template", "a..b/{run_id}"), ("branch_template", "/lead/{run_id}"),
            ("branch_template", "trail/"), ("branch_template", ""),
            ("worktree_template", "{bogus}"), ("worktree_template", ""),
            ("no_such_key", "1"),
        ]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                with self.assertRaises(config.ConfigError):
                    config.set_value(key, value)
        self.assertFalse(dstate.config_path().exists())

    def test_unknown_key_get(self):
        with self.assertRaisesRegex(config.ConfigError, "unknown config key"):
            config.get("nope")

    def test_set_preserves_other_keys_and_unknown_file_keys_are_ignored_on_load(self):
        dstate.config_path().parent.mkdir(parents=True)
        dstate.config_path().write_text(json.dumps({"default_days": 2, "future_key": "x"}), encoding="utf-8")
        config.set_value("model", "haiku")
        stored = json.loads(dstate.config_path().read_text(encoding="utf-8"))
        self.assertEqual(stored, {"default_days": 2, "future_key": "x", "model": "haiku"})
        loaded = config.load()
        self.assertEqual((loaded["default_days"], loaded["model"]), (2, "haiku"))
        self.assertNotIn("future_key", loaded)

    def test_corrupt_or_invalid_file_is_an_error(self):
        dstate.config_path().parent.mkdir(parents=True)
        for text in ("{broken", "[1, 2]", json.dumps({"default_days": "never"})):
            with self.subTest(text=text):
                dstate.config_path().write_text(text, encoding="utf-8")
                with self.assertRaises(config.ConfigError):
                    config.load()
        dstate.config_path().write_text("{broken", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.set_value("model", "x")  # never overwrites a file it cannot parse
        self.assertEqual(dstate.config_path().read_text(encoding="utf-8"), "{broken")

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        config.set_value("model", "sonnet")
        before = dstate.config_path().read_text(encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.set_value("model", "haiku")
        self.assertEqual(dstate.config_path().read_text(encoding="utf-8"), before)
        self.assertEqual(sorted(p.name for p in dstate.home().iterdir()), ["config.json"])

    def test_render_name(self):
        values = {"run_id": "p-261102-1830", "project": "p", "yy": "26", "mm": "11", "dd": "02", "hhmm": "1830"}
        self.assertEqual(config.render_name("dream/{run_id}", values), "dream/p-261102-1830")
        self.assertEqual(config.render_name("{project}/{yy}-{mm}-{dd}_{hhmm}", values), "p/26-11-02_1830")
        self.assertEqual(config.render_name("{ not a field }", values), "{ not a field }")
        with self.assertRaisesRegex(config.ConfigError, "unknown placeholder"):
            config.render_name("{who}", values)

    def test_format_value(self):
        self.assertEqual([config.format_value(v) for v in ("a b", 7, True, 1.5)], ["a b", "7", "true", "1.5"])


if __name__ == "__main__":
    unittest.main()
