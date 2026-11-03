"""Tests for ``to_dream.schedule``: generated artefacts and the commands issued, via a recording runner."""
import os
import plistlib
import shlex
import shutil
import subprocess
import unittest
from pathlib import PurePosixPath

from td_testing import IsolatedHomeTestCase

from to_dream import schedule, version


class Recorder:
    """Injectable runner: records argv (and stdin), answers from a table of canned results."""

    def __init__(self, results=None, default=0):
        self.calls = []
        self.inputs = []
        self.results = results or {}   # tuple(argv prefix) -> (returncode, stdout)
        self.default = default

    def __call__(self, argv, input=None):
        self.calls.append(list(argv))
        self.inputs.append(input)
        for prefix, (code, out) in sorted(self.results.items(), key=lambda kv: -len(kv[0])):
            if tuple(argv[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, self.default, "", "")


class ScheduleTestCase(IsolatedHomeTestCase):
    def make_ctx(self, platform, runner=None, **kwargs):
        user_home = self.tmp / "user"
        user_home.mkdir(exist_ok=True)
        return schedule.Context(platform=platform, home=self.home, user_home=user_home,
                                python="/usr/bin/python3", path_dirs=["/opt/claude/bin"], uid=501,
                                runner=runner or Recorder(), **kwargs)

    @staticmethod
    def argv_list(recorder):
        return [" ".join(c) for c in recorder.calls]


class ParseTimeTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(schedule.parse_time("03:30"), (3, 30))
        self.assertEqual(schedule.parse_time("3:05"), (3, 5))
        self.assertEqual(schedule.parse_time("23:59"), (23, 59))

    def test_invalid(self):
        for bad in ("24:00", "12:60", "noon", "1230", "12:5", "", "-1:00"):
            with self.assertRaises(ValueError, msg=bad):
                schedule.parse_time(bad)


class WrapperTests(ScheduleTestCase):
    def test_sh_wrapper_exports_home_and_runs_all_projects_from_runtime_copy(self):
        text = schedule.build_sh_wrapper(self.make_ctx("linux"))
        script = shlex.quote(str(self.home / "runtime" / "scripts" / "to_dream.py"))
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("TO_DREAM_HOME=%s\nexport TO_DREAM_HOME" % shlex.quote(str(self.home)), text)
        self.assertIn("/usr/bin/python3 %s run --project all >> \"$LOG\" 2>&1" % script, text)
        self.assertIn("check-update --force", text)
        self.assertIn('LOG="$TO_DREAM_HOME/logs/$(date +%Y-%m-%d).log"', text)
        self.assertIn("PATH=/opt/claude/bin:\"$PATH\"", text)
        self.assertNotIn("\r", text)

    def test_sh_wrapper_quotes_paths_with_spaces(self):
        ctx = self.make_ctx("linux")
        ctx.home = PurePosixPath("/home/a b/it's")
        text = schedule.build_sh_wrapper(ctx)
        self.assertIn("TO_DREAM_HOME=%s" % shlex.quote("/home/a b/it's"), text)
        words = shlex.split([ln for ln in text.splitlines() if "run --project all" in ln][0])
        self.assertEqual(words[:2], ["/usr/bin/python3", str(ctx.runtime / "scripts" / "to_dream.py")])

    def test_cmd_wrapper(self):
        ctx = self.make_ctx("win32")
        ctx.python = r"C:\Python310\python.exe"
        text = schedule.build_cmd_wrapper(ctx)
        self.assertIn("\r\n", text)
        self.assertIn('set "TO_DREAM_HOME=%s"' % self.home, text)
        self.assertIn('"C:\\Python310\\python.exe" "%s" run --project all >> "%%LOG%%" 2>&1' % (
            ctx.runtime / "scripts" / "to_dream.py"), text)
        self.assertIn("%TO_DREAM_HOME%\\logs\\%TD_DAY%.log", text)
        self.assertIn('set "PATH=/opt/claude/bin;%PATH%"', text)


class LaunchdTests(ScheduleTestCase):
    def test_plist_content(self):
        ctx = self.make_ctx("darwin")
        data = plistlib.loads(schedule.build_plist(ctx, 4, 15))
        self.assertEqual(data["Label"], schedule.LAUNCHD_LABEL)
        self.assertEqual(data["ProgramArguments"], ["/bin/sh", str(self.home / "runtime" / "run-nightly.sh")])
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 4, "Minute": 15})

    def test_install_writes_plist_and_bootstraps(self):
        rec = Recorder()
        ctx = self.make_ctx("darwin", rec)
        code, _ = schedule.install(ctx, "04:15")
        self.assertEqual(code, 0)
        plist = ctx.user_home / "Library" / "LaunchAgents" / (schedule.LAUNCHD_LABEL + ".plist")
        self.assertEqual(plistlib.loads(plist.read_bytes())["StartCalendarInterval"], {"Hour": 4, "Minute": 15})
        self.assertEqual(rec.calls, [["launchctl", "bootout", "gui/501", str(plist)],
                                     ["launchctl", "bootstrap", "gui/501", str(plist)]])
        self.assertTrue((self.home / "runtime" / "run-nightly.sh").is_file())

    def test_install_reports_bootstrap_failure(self):
        rec = Recorder({("launchctl", "bootstrap"): (5, "")})
        code, messages = schedule.install(self.make_ctx("darwin", rec), "03:30")
        self.assertEqual(code, 1)
        self.assertIn("launchctl bootstrap failed", messages[0])

    def test_remove_boots_out_and_deletes(self):
        rec = Recorder()
        ctx = self.make_ctx("darwin", rec)
        schedule.install(ctx, "03:30")
        rec.calls.clear()
        code, _ = schedule.remove(ctx)
        self.assertEqual(code, 0)
        self.assertFalse(ctx.launchd_plist.exists())
        self.assertFalse(ctx.wrapper.exists())
        self.assertEqual(rec.calls, [["launchctl", "bootout", "gui/501", str(ctx.launchd_plist)]])

    def test_status_reads_time_back(self):
        ctx = self.make_ctx("darwin")
        self.assertFalse(schedule.status(ctx)["installed"])
        schedule.install(ctx, "05:45")
        info = schedule.status(ctx)
        self.assertEqual((info["installed"], info["mechanism"], info["time"]), (True, "launchd", "05:45"))


class SystemdTests(ScheduleTestCase):
    def test_unit_content(self):
        ctx = self.make_ctx("linux")
        units = schedule.build_systemd_units(ctx, 3, 30)
        self.assertEqual(set(units), {"to-dream.service", "to-dream.timer"})
        self.assertIn("Type=oneshot", units["to-dream.service"])
        escaped = str(self.home / "runtime" / "run-nightly.sh").replace("\\", "\\\\")  # systemd needs \ escaped
        self.assertIn('ExecStart=/bin/sh "%s"' % escaped, units["to-dream.service"])
        self.assertIn("OnCalendar=*-*-* 03:30:00", units["to-dream.timer"])
        self.assertIn("Persistent=true", units["to-dream.timer"])
        self.assertIn("WantedBy=timers.target", units["to-dream.timer"])

    def test_install_writes_units_and_enables_timer(self):
        rec = Recorder({("crontab",): (127, "")})
        ctx = self.make_ctx("linux", rec)
        code, _ = schedule.install(ctx, "02:05")
        self.assertEqual(code, 0)
        unit_dir = ctx.user_home / ".config" / "systemd" / "user"
        self.assertIn("OnCalendar=*-*-* 02:05:00", (unit_dir / "to-dream.timer").read_text())
        calls = self.argv_list(rec)
        self.assertEqual(calls[0], "systemctl --user show-environment")
        self.assertIn("systemctl --user daemon-reload", calls)
        self.assertIn("systemctl --user enable --now to-dream.timer", calls)
        self.assertLess(calls.index("systemctl --user daemon-reload"),
                        calls.index("systemctl --user enable --now to-dream.timer"))
        wrapper = ctx.wrapper
        self.assertTrue(wrapper.is_file())

    def test_install_removes_older_cron_entry_when_systemd_works(self):
        stale = "0 1 * * * /bin/sh /x/run-nightly.sh %s\n5 5 * * * other-job\n" % schedule.CRON_MARKER
        rec = Recorder({("crontab", "-l"): (0, stale)})
        schedule.install(self.make_ctx("linux", rec), "03:30")
        written = [i for c, i in zip(rec.calls, rec.inputs) if c == ["crontab", "-"]]
        self.assertEqual(written, ["5 5 * * * other-job\n"])

    def test_remove_disables_and_deletes_units(self):
        rec = Recorder({("crontab",): (127, "")})
        ctx = self.make_ctx("linux", rec)
        schedule.install(ctx, "03:30")
        rec.calls.clear()
        code, _ = schedule.remove(ctx)
        self.assertEqual(code, 0)
        unit_dir = ctx.user_home / ".config" / "systemd" / "user"
        self.assertEqual(list(unit_dir.iterdir()), [])
        self.assertEqual(self.argv_list(rec)[0], "systemctl --user disable --now to-dream.timer")

    def test_status_systemd(self):
        rec = Recorder({("crontab",): (127, "")})
        ctx = self.make_ctx("linux", rec)
        self.assertFalse(schedule.status(ctx)["installed"])
        schedule.install(ctx, "06:10")
        info = schedule.status(ctx)
        self.assertEqual((info["installed"], info["mechanism"], info["time"], info["catch_up"]),
                         (True, "systemd", "06:10", True))


class CronTests(ScheduleTestCase):
    def test_cron_line(self):
        ctx = self.make_ctx("linux")
        self.assertEqual(schedule.build_cron_line(ctx, 3, 30),
                         "30 3 * * * /bin/sh %s %s" % (shlex.quote(str(self.home / "runtime" / "run-nightly.sh")),
                                                      schedule.CRON_MARKER))

    def test_merge_crontab_replaces_only_our_line(self):
        existing = "MAILTO=me\n1 1 * * * old %s\n2 2 * * * keep\n" % schedule.CRON_MARKER
        merged = schedule.merge_crontab(existing, "30 3 * * * new " + schedule.CRON_MARKER)
        self.assertEqual(merged, "MAILTO=me\n2 2 * * * keep\n30 3 * * * new %s\n" % schedule.CRON_MARKER)
        self.assertEqual(schedule.merge_crontab(existing, None), "MAILTO=me\n2 2 * * * keep\n")
        self.assertEqual(schedule.merge_crontab("", None), "")

    def test_falls_back_to_crontab_when_systemd_unavailable(self):
        rec = Recorder({("systemctl",): (1, ""), ("crontab", "-l"): (1, "")})
        ctx = self.make_ctx("linux", rec)
        code, messages = schedule.install(ctx, "03:30")
        self.assertEqual(code, 0)
        self.assertTrue(any("crontab" in m for m in messages))
        installed = [i for c, i in zip(rec.calls, rec.inputs) if c == ["crontab", "-"]]
        self.assertEqual(installed, [schedule.build_cron_line(ctx, 3, 30) + "\n"])
        self.assertFalse((ctx.user_home / ".config").exists())

    def test_status_and_remove_for_cron(self):
        line = schedule.build_cron_line(self.make_ctx("linux"), 7, 20)
        rec = Recorder({("systemctl",): (1, ""), ("crontab", "-l"): (0, "9 9 * * * x\n" + line + "\n")})
        ctx = self.make_ctx("linux", rec)
        info = schedule.status(ctx)
        self.assertEqual((info["installed"], info["mechanism"], info["time"], info["catch_up"]),
                         (True, "cron", "07:20", False))
        schedule.remove(ctx)
        written = [i for c, i in zip(rec.calls, rec.inputs) if c == ["crontab", "-"]]
        self.assertEqual(written, ["9 9 * * * x\n"])

    def test_no_scheduler_at_all_fails_cleanly(self):
        rec = Recorder({("systemctl",): (127, ""), ("crontab",): (127, "")})
        code, messages = schedule.install(self.make_ctx("linux", rec), "03:30")
        self.assertEqual(code, 1)
        self.assertIn("neither systemd", messages[0])


class WindowsTests(ScheduleTestCase):
    def test_schtasks_argv(self):
        ctx = self.make_ctx("win32")
        argv = schedule.schtasks_create_argv(ctx, 3, 30)
        self.assertEqual(argv, ["schtasks", "/Create", "/TN", "to-dream-nightly",
                                "/TR", '"%s"' % (self.home / "runtime" / "run-nightly.cmd"),
                                "/SC", "DAILY", "/ST", "03:30", "/F"])

    def test_powershell_script_is_single_quoted_start_when_available(self):
        argv = schedule.powershell_argv()
        self.assertEqual(argv[:3], ["powershell", "-NoProfile", "-Command"])
        script = argv[3]
        self.assertNotIn('"', script)
        self.assertIn("-TaskName 'to-dream-nightly'", script)
        self.assertIn("-StartWhenAvailable", script)

    def test_install_creates_task_then_enables_catch_up(self):
        rec = Recorder()
        ctx = self.make_ctx("win32", rec)
        code, messages = schedule.install(ctx, "01:00")
        self.assertEqual(code, 0)
        self.assertEqual(rec.calls, [schedule.schtasks_create_argv(ctx, 1, 0), schedule.powershell_argv()])
        wrapper = (self.home / "runtime" / "run-nightly.cmd").read_bytes()
        self.assertIn(b"\r\n", wrapper)
        self.assertNotIn(b"\r\r", wrapper)
        self.assertTrue(any("StartWhenAvailable" in m for m in messages))

    def test_catch_up_failure_is_a_warning_not_a_failure(self):
        rec = Recorder({("powershell",): (1, "")})
        code, messages = schedule.install(self.make_ctx("win32", rec), "03:30")
        self.assertEqual(code, 0)
        self.assertTrue(any(m.startswith("warning") for m in messages))

    def test_create_failure_is_reported(self):
        rec = Recorder({("schtasks",): (1, "")})
        code, messages = schedule.install(self.make_ctx("win32", rec), "03:30")
        self.assertEqual(code, 1)
        self.assertEqual(len(rec.calls), 1)
        self.assertIn("schtasks /Create failed", messages[0])

    def test_remove(self):
        rec = Recorder()
        ctx = self.make_ctx("win32", rec)
        schedule.install(ctx, "03:30")
        rec.calls.clear()
        code, _ = schedule.remove(ctx)
        self.assertEqual(code, 0)
        self.assertEqual(rec.calls, [["schtasks", "/Delete", "/TN", "to-dream-nightly", "/F"]])
        self.assertFalse(ctx.wrapper.exists())

    def test_remove_when_not_installed_is_ok(self):
        rec = Recorder({("schtasks",): (1, "")})
        code, messages = schedule.remove(self.make_ctx("win32", rec))
        self.assertEqual(code, 0)
        self.assertEqual(messages, ["No to-dream schedule was installed."])

    def test_status_parses_task_xml(self):
        xml = ('<?xml version="1.0" encoding="UTF-16"?>\n<Task version="1.2" '
               'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers><CalendarTrigger>'
               '<StartBoundary>2026-11-03T03:30:00</StartBoundary></CalendarTrigger></Triggers>'
               '<Settings><StartWhenAvailable>true</StartWhenAvailable></Settings></Task>')
        rec = Recorder({("schtasks", "/Query"): (0, xml)})
        info = schedule.status(self.make_ctx("win32", rec))
        self.assertEqual((info["installed"], info["time"], info["catch_up"]), (True, "03:30", True))

    def test_status_not_installed(self):
        rec = Recorder({("schtasks",): (1, "")})
        self.assertFalse(schedule.status(self.make_ctx("win32", rec))["installed"])


class InstallCommonTests(ScheduleTestCase):
    def test_bad_time_touches_nothing(self):
        rec = Recorder()
        code, messages = schedule.install(self.make_ctx("linux", rec), "25:99")
        self.assertEqual((code, rec.calls), (2, []))
        self.assertIn("HH:MM", messages[0])
        self.assertFalse((self.home / "runtime").exists())

    def test_install_syncs_runtime_first(self):
        schedule.install(self.make_ctx("darwin"), "03:30")
        self.assertTrue((self.home / "runtime" / "scripts" / "to_dream.py").is_file())
        self.assertEqual((self.home / "runtime" / "VERSION").read_text().strip(),
                         version.current_version())

    def test_sh_wrapper_is_executable_and_syntactically_valid(self):
        ctx = self.make_ctx("darwin")
        schedule.install(ctx, "03:30")
        self.assertTrue(ctx.wrapper.stat().st_mode & 0o100 or os.name == "nt")
        sh = shutil.which("sh")
        if sh:
            result = subprocess.run([sh, "-n", str(ctx.wrapper)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
