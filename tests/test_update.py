"""Tests for ``to_dream.update``: semver, the HTTP check against a local server, cache, opt-outs, notice."""
import argparse
import contextlib
import io
import json
import os
import random
import socket
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from td_testing import IsolatedHomeTestCase

from to_dream import config, state, update


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        server.hits += 1
        if server.delay:
            time.sleep(server.delay)
        self.send_response(server.status)
        self.end_headers()
        try:
            self.wfile.write(server.body.encode("utf-8"))
        except OSError:
            pass

    def log_message(self, *args):
        pass


class UpdateServerTestCase(IsolatedHomeTestCase):
    """Starts a local HTTP server per test; ``self.serve(...)`` configures what it returns."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.hits, self.server.delay, self.server.status, self.server.body = 0, 0, 200, ""
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.url = "http://127.0.0.1:%d/plugin.json" % self.server.server_address[1]
        os.environ["TO_DREAM_UPDATE_URL"] = self.url
        patcher = mock.patch.object(update.version, "current_version", return_value="1.2.3")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def serve(self, version=None, body=None, status=200, delay=0):
        self.server.body = body if body is not None else json.dumps({"name": "to-dream", "version": version})
        self.server.status, self.server.delay = status, delay

    def notice(self):
        return state.load_state().get("update_notice")


class CheckResultTests(UpdateServerTestCase):
    def test_newer_version_is_reported_and_recorded(self):
        self.serve("1.3.0")
        info = update.check_update(force=True)
        self.assertEqual((info["current"], info["latest"], info["update_available"], info["status"]),
                         ("1.2.3", "1.3.0", True, "ok"))
        self.assertTrue(info["changelog_url"].endswith("/blob/HEAD/CHANGELOG.md"))
        self.assertRegex(info["checked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.notice(), {"current": "1.2.3", "latest": "1.3.0", "checked_at": info["checked_at"]})

    def test_same_version_clears_a_stale_notice(self):
        state.update_state(lambda s: s.update(update_notice={"current": "1.0.0", "latest": "1.2.3", "checked_at": "x"}))
        self.serve("1.2.3")
        info = update.check_update(force=True)
        self.assertFalse(info["update_available"])
        self.assertEqual(info["status"], "ok")
        self.assertIsNone(self.notice())

    def test_older_published_version_is_not_an_update(self):
        self.serve("1.2.2")
        self.assertFalse(update.check_update(force=True)["update_available"])

    def test_prerelease_of_current_is_not_an_update(self):
        self.serve("1.2.3-rc.1")
        self.assertFalse(update.check_update(force=True)["update_available"])

    def test_notice_preserves_other_state(self):
        state.update_state(lambda s: s["last_dream"].update(proj="2026-11-01T00:00:00+00:00"))
        self.serve("2.0.0")
        update.check_update(force=True)
        self.assertEqual(state.load_state()["last_dream"], {"proj": "2026-11-01T00:00:00+00:00"})


class FailureTests(UpdateServerTestCase):
    def assert_unknown(self, info):
        self.assertEqual((info["latest"], info["update_available"], info["status"]), (None, False, "unknown"))

    def test_garbage_body(self):
        self.serve(body="<html>not json</html>")
        self.assert_unknown(update.check_update(force=True))

    def test_json_without_version(self):
        self.serve(body='{"name": "to-dream"}')
        self.assert_unknown(update.check_update(force=True))

    def test_version_that_is_not_semver(self):
        self.serve("banana")
        self.assert_unknown(update.check_update(force=True))

    def test_http_error(self):
        self.serve(status=404, body="nope")
        self.assert_unknown(update.check_update(force=True))

    def test_timeout_returns_quickly(self):
        self.serve("9.0.0", delay=1.5)
        with mock.patch.object(update, "TIMEOUT_SECONDS", 0.2):
            started = time.monotonic()
            info = update.check_update(force=True)
        self.assertLess(time.monotonic() - started, 1.2)
        self.assert_unknown(info)

    def test_offline(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        os.environ["TO_DREAM_UPDATE_URL"] = "http://127.0.0.1:%d/x" % port  # nothing listens here
        self.assert_unknown(update.check_update(force=True))

    def test_failure_keeps_an_existing_notice(self):
        notice = {"current": "1.2.3", "latest": "1.9.0", "checked_at": "t"}
        state.update_state(lambda s: s.update(update_notice=notice))
        self.serve(status=500, body="")
        update.check_update(force=True)
        self.assertEqual(self.notice(), notice)

    def test_handler_exits_zero_when_offline(self):
        self.serve(status=500, body="")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = update._handle(argparse.Namespace(json=True, force=True))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "unknown")


class CacheTests(UpdateServerTestCase):
    def test_second_call_uses_cache(self):
        self.serve("1.3.0")
        first = update.check_update()
        second = update.check_update()
        self.assertEqual(self.server.hits, 1)
        self.assertEqual(first, second)
        self.assertTrue((self.home / "update-check.json").is_file())

    def test_cache_still_updates_the_notice(self):
        self.serve("1.3.0")
        update.check_update()
        state.update_state(lambda s: s.pop("update_notice", None))
        update.check_update()  # served from cache, notice restored
        self.assertEqual(self.server.hits, 1)
        self.assertEqual(self.notice()["latest"], "1.3.0")

    def test_force_bypasses_cache(self):
        self.serve("1.3.0")
        update.check_update()
        self.serve("1.4.0")
        info = update.check_update(force=True)
        self.assertEqual((self.server.hits, info["latest"]), (2, "1.4.0"))

    def test_cache_expires_after_24_hours(self):
        self.serve("1.3.0")
        update.check_update()
        later = lambda: datetime.now(timezone.utc) + timedelta(hours=25)
        update.check_update(now=later)
        self.assertEqual(self.server.hits, 2)

    def test_cache_valid_just_under_24_hours(self):
        self.serve("1.3.0")
        update.check_update()
        later = lambda: datetime.now(timezone.utc) + timedelta(hours=23)
        update.check_update(now=later)
        self.assertEqual(self.server.hits, 1)

    def test_cache_from_another_running_version_is_ignored(self):
        self.serve("1.3.0")
        update.check_update()
        with mock.patch.object(update.version, "current_version", return_value="1.3.0"):
            info = update.check_update()
        self.assertEqual(self.server.hits, 2)
        self.assertFalse(info["update_available"])

    def test_failures_are_cached_briefly(self):
        self.serve(status=500, body="")
        update.check_update()
        update.check_update()
        self.assertEqual(self.server.hits, 1)
        later = lambda: datetime.now(timezone.utc) + timedelta(hours=2)
        update.check_update(now=later)
        self.assertEqual(self.server.hits, 2)


class OptOutTests(UpdateServerTestCase):
    def test_env_var_disables_without_network(self):
        self.serve("9.0.0")
        os.environ["TO_DREAM_NO_UPDATE_CHECK"] = "1"
        info = update.check_update(force=True)
        self.assertEqual((info["status"], info["update_available"], self.server.hits), ("disabled", False, 0))

    def test_config_false_disables_without_network(self):
        self.serve("9.0.0")
        config.set_value("update_check", "false")
        info = update.check_update(force=True)
        self.assertEqual((info["status"], self.server.hits), ("disabled", 0))

    def test_config_true_and_env_zero_stay_enabled(self):
        self.serve("9.0.0")
        config.set_value("update_check", True)
        os.environ["TO_DREAM_NO_UPDATE_CHECK"] = "0"
        self.assertEqual(update.check_update(force=True)["status"], "ok")


class UrlTests(unittest.TestCase):
    def test_raw_url_is_derived_from_repository(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TO_DREAM_UPDATE_URL", None)
            self.assertEqual(
                update.manifest_url("https://github.com/someone/to-dream"),
                "https://raw.githubusercontent.com/someone/to-dream/HEAD/plugins/to-dream/.claude-plugin/plugin.json")
            self.assertEqual(update.manifest_url("https://github.com/someone/to-dream.git/"),
                             update.manifest_url("https://github.com/someone/to-dream"))

    def test_non_github_repository_is_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TO_DREAM_UPDATE_URL", None)
            with self.assertRaises(ValueError):
                update.manifest_url("https://example.com/x/y")


class SemverTests(unittest.TestCase):
    def test_known_orderings(self):
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                   "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0", "10.0.0"]
        for lower, higher in zip(ordered, ordered[1:]):
            self.assertEqual(update.compare_versions(lower, higher), -1, (lower, higher))
            self.assertEqual(update.compare_versions(higher, lower), 1, (higher, lower))

    def test_double_digit_parts_compare_numerically(self):
        self.assertEqual(update.compare_versions("0.9.0", "0.10.0"), -1)

    def test_equality_ignores_v_prefix_and_build_metadata(self):
        self.assertEqual(update.compare_versions("v1.2.3", "1.2.3+build.5"), 0)

    def test_invalid_versions_raise(self):
        for bad in ("1.2", "1.2.3.4", "01.2.3", "a.b.c", "", "1.2.3-", "latest"):
            with self.assertRaises(ValueError, msg=bad):
                update.compare_versions(bad, "1.0.0")
            self.assertIsNone(update.parse_semver(bad))

    def test_random_pairs_match_independent_comparator(self):
        rng = random.Random(20261102)

        def random_version():
            core = "%d.%d.%d" % (rng.randint(0, 12), rng.randint(0, 12), rng.randint(0, 12))
            if rng.random() < 0.5:
                return core, core, ()
            idents = [str(rng.randint(0, 20)) if rng.random() < 0.5 else rng.choice(["alpha", "beta", "rc", "x-1"])
                      for _ in range(rng.randint(1, 3))]
            return core + "-" + ".".join(idents), core, tuple(idents)

        def key(parts):
            # Independent: a release sorts after its pre-releases; pre-release identifiers become
            # tuples where numbers sort before text and tuple comparison handles prefix length.
            _, core, idents = parts
            nums = tuple(int(n) for n in core.split("."))
            if not idents:
                return nums, (1,)
            return nums, (0, tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in idents))

        for _ in range(3000):
            a, b = random_version(), random_version()
            expected = (key(a) > key(b)) - (key(a) < key(b))
            self.assertEqual(update.compare_versions(a[0], b[0]), expected, (a[0], b[0]))
            self.assertEqual(update.compare_versions(b[0], a[0]), -expected)


class ReminderTests(unittest.TestCase):
    def test_reminder_line_names_both_versions_and_the_update_command(self):
        line = update.reminder_line({"update_available": True, "latest": "0.3.0", "current": "0.2.1"})
        self.assertIn("0.3.0", line)
        self.assertIn("0.2.1", line)
        self.assertIn("/plugin marketplace update to-dream", line)
        self.assertIn("/reload-plugins", line)

    def test_no_reminder_when_up_to_date(self):
        self.assertEqual(update.reminder_line({"update_available": False}), "")


if __name__ == "__main__":
    unittest.main()
