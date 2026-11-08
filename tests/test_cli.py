import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
from datetime import timedelta
from unittest import mock

from helpers import LIB_DIR, git
from test_runner import HAPPY, NOW, PASSWORD_LINE, REPORT
from to_dream_helpers import BASE_BRANCH, DreamTestCase

from to_dream import cli, config, runner, state as dstate


def load_json(result):
    """Parse the stdout of a ``CliCase.call`` made with ``--json``."""
    return json.loads(result[1])


class CliCase(DreamTestCase):
    """Base for tests that drive ``cli.main`` and capture its output."""

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def dream(self, **plan):
        self.plan(**(plan or HAPPY))
        return runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)


class CliTests(CliCase):
    def test_full_flow(self):
        self.plan(**HAPPY)
        code, out, _ = self.call("run", "--project", str(self.project_dir), "--timeout-min", "2")
        self.assertEqual(code, 0)
        run_id = next(iter(dstate.load_state()["runs"]))
        for fragment in (run_id, "report:", "branch:", f"(from {BASE_BRANCH})", "1. chore(to-dream): update",
                         "3. chore(to-dream): add", "usage (API-equivalent): $0.42", "not a charge",
                         f"to-dream apply {run_id}", f"reject {run_id} --only N", "discard"):
            self.assertIn(fragment, out)
        code, out, _ = self.call("list")
        self.assertIn("pending", out)
        self.assertIn("commits=3", out)
        code, out, _ = self.call("show", run_id)
        self.assertIn("1.", out)
        self.assertIn("b.md", out)
        code, out, _ = self.call("show", run_id, "--diff")
        self.assertIn("+prefers tabs", out)
        code, out, err = self.call("apply", run_id, "--only", "3")
        self.assertEqual(code, 0, err)
        self.assertIn(f"applied 1 commit(s) to {BASE_BRANCH}", out)
        code, out, _ = self.call("list")
        self.assertIn("partial", out)
        code, out, _ = self.call("discard", run_id)
        self.assertEqual(code, 0)
        self.assertIn("discarded", self.call("list")[1])

    def test_project_can_be_a_slug_a_path_or_all(self):
        other = self.root / "-srv-other"
        other.mkdir()
        (other / "s.jsonl").write_text("{}\n", newline="\n")
        self.plan(report="# none\n")
        for project in (self.slug, str(self.project_dir)):
            with self.subTest(project=project):
                self.distill.calls.clear()
                self.assertEqual(self.call("run", "--project", project)[0], 0)
                self.assertEqual([os.path.basename(c["project_dir"]) for c in self.distill.calls], [self.slug])
        self.distill.calls.clear()
        code, out, _ = self.call("run", "--project", "all")
        self.assertEqual(code, 0)
        self.assertEqual([os.path.basename(c["project_dir"]) for c in self.distill.calls], ["-srv-other"])

    def test_run_all_with_nothing_to_do(self):
        code, out, _ = self.call("run", "--project", "all")
        self.assertEqual(code, 0)
        self.assertIn("No projects with transcript activity", out)

    def test_run_defaults_come_from_config(self):
        config.set_value("model", "haiku")
        config.set_value("analyst_model", "sonnet-x")
        config.set_value("default_days", "3")
        config.set_value("timeout_min", "9")
        self.plan(**HAPPY)
        self.assertEqual(self.call("run", "--project", self.slug)[0], 0)
        call = self.claude_call()
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertIn("sonnet-x", call["stdin"])
        window = self.distill.calls[0]
        self.assertEqual(window["until"] - window["since"], timedelta(days=3))
        self.assertNotIn("--max-budget-usd", argv)

    def test_flags_override_config(self):
        config.set_value("model", "haiku")
        self.plan(**HAPPY)
        self.assertEqual(self.call("run", "--project", self.slug, "--model", "opus-x", "--days", "2",
                                   "--budget-usd", "1.5")[0], 0)
        argv = self.claude_call()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "opus-x")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1.5")
        self.assertEqual(self.distill.calls[0]["until"] - self.distill.calls[0]["since"], timedelta(days=2))

    def test_show_prints_usage_estimate_and_help_mentions_timeout(self):
        self.plan(**HAPPY)
        self.call("run", "--project", self.slug)
        run_id = next(iter(dstate.load_state()["runs"]))
        self.assertIn("usage (API-equivalent): $0.42", self.call("show", run_id)[1])
        self.assertEqual(load_json(self.call("list", "--json"))[0]["cost_usd"], 0.42)
        run_parser = cli.build_parser()._subparsers._group_actions[0].choices["run"]
        timeout_help = next(a for a in run_parser._actions if "--timeout-min" in a.option_strings).help
        self.assertIn("kill claude", timeout_help)
        budget_help = next(a for a in run_parser._actions if "--budget-usd" in a.option_strings).help
        self.assertIn("no cap by default", budget_help)

    def test_dry_run_prints_index(self):
        code, out, _ = self.call("run", "--project", self.slug, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("INDEX.md", out)
        self.assertIn("sessions=2", out)

    def test_run_failure_exit_code_and_errors(self):
        self.plan(exit=1)
        self.assertEqual(self.call("run", "--project", self.slug)[0], 1)
        code, _, err = self.call("run", "--project", "no-such-project")
        self.assertEqual(code, 2)
        self.assertIn("no Claude project directory", err)
        code, _, err = self.call("show", "nope")
        self.assertEqual((code, "unknown run" in err), (2, True))

    def test_invalid_config_is_a_clean_error(self):
        dstate.config_path().parent.mkdir(parents=True, exist_ok=True)
        dstate.config_path().write_text("{broken", encoding="utf-8")
        code, _, err = self.call("run", "--project", self.slug)
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "))

    def test_parser_rules(self):
        parser = cli.build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["run", "--project", "x", "--days", "2", "--since", "2026-01-01"])
        args = parser.parse_args(["run", "--project", "x", "--since", "2026-01-05"])
        self.assertEqual((args.since.year, args.since.month, args.since.day), (2026, 1, 5))
        self.assertIsNotNone(args.since.tzinfo)
        defaults = parser.parse_args(["run", "--project", "x"])
        self.assertEqual(
            (defaults.days, defaults.model, defaults.analyst_model, defaults.budget_usd, defaults.max_sessions,
             defaults.timeout_min),
            (None, None, None, None, 40, None),  # None = "take it from config"; no budget unless asked
        )
        self.assertEqual(parser.parse_args(["run", "--project", "x", "--budget-usd", "2"]).budget_usd, 2.0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["run", "--project", "x", "--since", "yesterday"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["show", "r", "--json", "--diff"])  # mutually exclusive

    def test_every_documented_command_exists(self):
        parser = cli.build_parser()
        choices = parser._subparsers._group_actions[0].choices
        for name in ("run", "list", "show", "apply", "reject", "mark-applied", "reject-suggestion", "discard", "config"):
            self.assertIn(name, choices)

    def test_list_empty(self):
        self.assertIn("No dream runs", self.call("list")[1])


class DashProjectCliTests(CliCase):
    """POSIX project slugs start with "-"; argparse must take them as --project's value."""

    SLUG = "-home-u-proj"

    def setUp(self):
        super().setUp()
        (self.root / self.SLUG / "memory").mkdir(parents=True)

    def project_calls(self):
        return [os.path.basename(c["project_dir"]) for c in self.distill.calls]

    def test_fold_option_values(self):
        fold = cli.fold_option_values
        self.assertEqual(fold(["run", "--project", "-a-b", "--dry-run"]), ["run", "--project=-a-b", "--dry-run"])
        self.assertEqual(fold(["run", "--project=-a-b"]), ["run", "--project=-a-b"])
        self.assertEqual(fold(["run", "--project", "all"]), ["run", "--project=all"])
        self.assertEqual(fold(["run", "--project"]), ["run", "--project"])
        self.assertEqual(fold(["list"]), ["list"])
        run_parser = cli.build_parser()._subparsers._group_actions[0].choices["run"]
        self.assertEqual(fold(["run", "--project", "--dry-run"], run_parser), ["run", "--project", "--dry-run"])
        self.assertEqual(fold(["run", "--project", "--hidden-x"], run_parser), ["run", "--project=--hidden-x"])

    def test_space_and_equals_forms_both_work(self):
        self.plan(report="# none\n")
        for argv in (["run", "--project", self.SLUG], ["run", f"--project={self.SLUG}"],
                     ["run", "--project", self.SLUG, "--dry-run"], ["run", "--dry-run", "--project", self.SLUG]):
            with self.subTest(argv=argv):
                self.distill.calls.clear()
                code, out, err = self.call(*argv)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.project_calls(), [self.SLUG])

    def test_a_missing_value_is_still_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["run", "--project"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["run", "--project", "--dry-run"])  # "--dry-run" is not swallowed as the project

    def test_a_double_dash_slug_is_a_value_too(self):
        (self.root / "--hidden-proj" / "memory").mkdir(parents=True)
        self.plan(report="# none\n")
        self.assertEqual(self.call("run", "--project", "--hidden-proj")[0], 0)
        self.assertEqual(self.project_calls(), ["--hidden-proj"])

    def test_posix_path_form_resolves_to_the_dash_slug(self):
        self.assertEqual(runner.resolve_slug("/home/u/proj", self.root), self.SLUG)

    def test_from_a_real_process_without_a_shell(self):
        result = subprocess.run([sys.executable, "-m", "to_dream", "run", "--project", self.SLUG, "--dry-run"],
                                cwd=LIB_DIR, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.SLUG, result.stdout)

    def test_the_documented_commands_use_the_equals_form(self):
        plugin = LIB_DIR.parent
        for rel in ("skills/run/SKILL.md", "skills/setup/SKILL.md"):
            text = (plugin / rel).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"--project <", rel)
            self.assertIn("--project=<", text, rel)




class ExtraModuleRegistrationTests(unittest.TestCase):
    def fake_module(self, command, handler):
        def add_parser(subparsers):
            sub = subparsers.add_parser(command)
            sub.set_defaults(func=handler)
        return types.SimpleNamespace(add_parser=add_parser)

    def test_contributed_commands_are_registered_and_dispatched(self):
        calls = []

        def handler(args):
            calls.append(args.command)
            return 7

        with mock.patch.dict(sys.modules, {"to_dream.doctor": self.fake_module("doctor", handler)}):
            self.assertEqual(cli.main(["doctor"]), 7)
        self.assertEqual(calls, ["doctor"])

    def test_missing_modules_are_skipped(self):
        with mock.patch.object(cli, "EXTRA_COMMAND_MODULES", ("surely_not_there",)):
            parser = cli.build_parser()
        self.assertIn("run", parser._subparsers._group_actions[0].choices)

    def test_a_broken_existing_module_is_not_hidden(self):
        def broken(name):
            raise ModuleNotFoundError("No module named 'some_dependency'", name="some_dependency")

        with mock.patch.object(cli.importlib, "import_module", broken):
            with self.assertRaises(ModuleNotFoundError):
                cli.build_parser()

    def test_modules_without_add_parser_are_ignored(self):
        with mock.patch.dict(sys.modules, {"to_dream.doctor": types.SimpleNamespace()}):
            cli.build_parser()

    def test_all_five_contributed_modules_are_looked_up(self):
        self.assertEqual(set(cli.EXTRA_COMMAND_MODULES), {"doctor", "schedule", "update", "runtime", "repo_setup"})

    def test_handler_exit_codes_and_expected_errors(self):
        def failing(args):
            raise runner.RunError("boom")

        with mock.patch.dict(sys.modules, {"to_dream.doctor": self.fake_module("doctor", failing)}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["doctor"]), 2)
        self.assertEqual(err.getvalue(), "error: boom\n")


class ConfigCommandTests(CliCase):
    def test_show_lists_every_key(self):
        code, out, _ = self.call("config", "show")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), len(config.DEFAULTS))
        self.assertIn("model = opus", lines)
        self.assertIn("default_days = 7", lines)
        self.assertIn("update_check = true", lines)
        self.assertIn("branch_template = dream/{run_id}", lines)

    def test_get_and_set_roundtrip(self):
        self.assertEqual(self.call("config", "get", "default_days")[1], "7\n")
        code, out, _ = self.call("config", "set", "default_days", "3")
        self.assertEqual((code, out), (0, "default_days = 3\n"))
        self.assertEqual(self.call("config", "get", "default_days")[1], "3\n")
        self.assertEqual(self.call("config", "set", "update_check", "no")[1], "update_check = false\n")
        self.assertEqual(self.call("config", "get", "update_check")[1], "false\n")
        self.assertEqual(self.call("config", "get", "model")[1], "opus\n")
        self.assertEqual(json.loads(dstate.config_path().read_text(encoding="utf-8")),
                         {"default_days": 3, "update_check": False})

    def test_errors_are_reported_and_nothing_changes(self):
        for argv in (("set", "default_days", "zero"), ("set", "nope", "1"), ("get", "nope"),
                     ("set", "schedule_time", "99:99"), ("set", "branch_template", "x/{bogus}")):
            with self.subTest(argv=argv):
                code, out, err = self.call("config", *argv)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("error: "), err)
        self.assertFalse(dstate.config_path().exists())

    def test_action_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["config"])


class JsonOutputTests(CliCase):
    def test_list_json(self):
        summary = self.dream()
        self.assertEqual(load_json(self.call("list", "--json")), [{
            "run_id": summary.run_id, "slug": self.slug, "status": "pending",
            "created": "2026-11-02T18:30:12+00:00", "window_since": "2026-10-26T18:30:12+00:00",
            "window_until": "2026-11-02T18:30:12+00:00", "branch": summary.branch, "base_branch": BASE_BRANCH,
            "worktree": str(summary.worktree), "report_path": str(summary.run_dir / "DREAM_REPORT.md"),
            "changes": 3, "cost_usd": 0.42,
        }])
        self.assertEqual(self.call("list")[1].count("\n"), 1)  # human output stays the default

    def test_list_json_empty_and_statuses(self):
        self.assertEqual(load_json(self.call("list", "--json")), [])
        applied = self.dream()
        self.call("apply", applied.run_id, "--only", "3")
        self.assertEqual(load_json(self.call("list", "--json"))[0]["status"], "partially-applied")
        self.call("apply", applied.run_id)
        self.assertEqual(load_json(self.call("list", "--json"))[0]["status"], "applied")
        later = NOW + timedelta(hours=1)
        self.plan(report="# none\n")
        runner.run_project(self.slug, runner.RunOptions(), now=later, echo=False)
        self.plan(exit=1)
        runner.run_project(self.slug, runner.RunOptions(), now=later + timedelta(hours=1), echo=False)
        self.call("discard", applied.run_id)
        self.assertEqual(
            [r["status"] for r in load_json(self.call("list", "--json"))], ["discarded", "no-changes", "failed"]
        )

    def test_show_json(self):
        summary = self.dream()
        data = load_json(self.call("show", summary.run_id, "--json"))
        self.assertEqual(
            {k: data[k] for k in ("run_id", "slug", "status", "report_path", "worktree", "branch", "base_branch",
                                  "live_memory_dir")},
            {"run_id": summary.run_id, "slug": self.slug, "status": "pending",
             "report_path": str(summary.run_dir / "DREAM_REPORT.md"), "worktree": str(summary.worktree),
             "branch": summary.branch, "base_branch": BASE_BRANCH, "live_memory_dir": str(self.memory)},
        )
        self.assertEqual(
            [(c["n"], c["file"], c["action"]) for c in data["changes"]],
            [(1, "MEMORY.md", "update"), (2, "b.md", "delete"), (3, "topic.md", "add")],
        )
        topic = data["changes"][2]
        self.assertEqual(topic["commit"], summary.commits[2].sha)
        self.assertEqual(topic["subject"], f"chore(to-dream): add {self.slug_short}/topic.md")
        self.assertEqual(
            topic["report_section"],
            'New durable preference found in two sessions.\n**Change:** add a "prefers tabs" preference',
        )
        self.assertIn("+prefers tabs", topic["diff"])
        self.assertIs(topic["diff_truncated"], False)
        self.assertEqual((topic["applied"], topic["rejected"], topic["marked_applied"]), (False, False, False))
        self.assertIn("-beta fact", data["changes"][1]["diff"])
        self.assertEqual(data["report_extras"], {
            "global_suggestions": "Consider a global note about tabs.",
            "stale_candidates": "- a.md may be stale",
            "not_acted_on": "- nothing else",
            "permission_suggestions": "",
            "permission_suggestions_parsed": [],
        })
        self.assertEqual(data["permission_targets"], {
            "project-local": (self.project_dir / ".claude" / "settings.local.json").as_posix(),
            "project": (self.project_dir / ".claude" / "settings.json").as_posix(),
            "user": (self.root.parent / "settings.json").as_posix(),
        })
        self.assertNotIn("update_notice", data)
        self.assertNotIn("superseded_by", data)

    def test_show_json_includes_the_update_notice_when_state_has_one(self):
        summary = self.dream()
        notice = {"current": "0.2.1", "latest": "0.3.0", "checked_at": "2026-11-02T00:00:00+00:00"}
        dstate.update_state(lambda s: s.update({runner.STATE_UPDATE_NOTICE_KEY: notice}))
        self.assertEqual(runner.STATE_UPDATE_NOTICE_KEY, "update_notice")
        data = load_json(self.call("show", summary.run_id, "--json"))
        self.assertEqual(data["update_notice"], notice)
        text = self.call("show", summary.run_id)[1]
        self.assertTrue(text.startswith("notice:   to-dream 0.3.0 is out (you have 0.2.1)\n"))
        dstate.update_state(lambda s: s.pop(runner.STATE_UPDATE_NOTICE_KEY))
        self.assertNotIn("update_notice", load_json(self.call("show", summary.run_id, "--json")))

    def test_list_json_stays_an_array_even_with_a_notice(self):
        self.dream()
        dstate.update_state(lambda s: s.update(update_notice={"latest": "9"}))
        self.assertIsInstance(load_json(self.call("list", "--json")), list)

    def test_show_json_flags_and_missing_extras(self):
        summary = self.dream(**dict(HAPPY, report=REPORT.split("## Global")[0]))
        self.call("apply", summary.run_id, "--only", "1")
        self.call("reject", summary.run_id, "--only", "2")
        data = load_json(self.call("show", summary.run_id, "--json"))
        self.assertEqual([(c["applied"], c["rejected"]) for c in data["changes"]],
                         [(True, False), (False, True), (False, False)])
        self.assertEqual(data["report_extras"], {
            "global_suggestions": "", "stale_candidates": "", "not_acted_on": "", "permission_suggestions": "",
            "permission_suggestions_parsed": [],
        })

    def test_diff_is_capped_and_flagged(self):
        big = "x" * 20000 + "\n"
        summary = self.dream(**dict(HAPPY, write={**HAPPY["write"], "topic.md": big}))
        topic = load_json(self.call("show", summary.run_id, "--json"))["changes"][2]
        self.assertTrue(topic["diff_truncated"])
        self.assertEqual(len(topic["diff"]), 8000)

    def test_show_json_after_discard_has_no_diffs(self):
        summary = self.dream()
        self.call("discard", summary.run_id)
        data = load_json(self.call("show", summary.run_id, "--json"))
        self.assertEqual(data["status"], "discarded")
        self.assertEqual([c["diff"] for c in data["changes"]], ["", "", ""])
        self.assertEqual([c["rejected"] for c in data["changes"]], [True, True, True])

    def test_print_json_is_utf8_safe_even_for_a_cp1252_stdout(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n")
        with contextlib.redirect_stdout(stream):
            cli.print_json({"star": "★ arrow →"})
            stream.flush()
        self.assertEqual(json.loads(raw.getvalue().decode("utf-8")), {"star": "★ arrow →"})


class RejectionTests(CliCase):
    def log_text(self):
        return (self.state_home / "rejected.md").read_text(encoding="utf-8")

    def test_reject_marks_state_and_appends_entry(self):
        summary = self.dream()
        code, out, err = self.call("reject", summary.run_id, "--only", "2", "--reason", "keep   it\nterse")
        self.assertEqual(code, 0, err)
        self.assertIn("rejected 1 change", out)
        record = runner.get_run(summary.run_id)
        self.assertEqual(list(record["rejected_commits"]), [summary.commits[1].sha])
        text = self.log_text()
        self.assertTrue(text.startswith("# Rejected dream changes"))
        self.assertIn(f"| {self.slug} | b.md | delete", text)
        self.assertIn("- Change: remove the stale beta fact", text)
        self.assertIn("- Reason: keep it terse", text)
        self.assertIn(f"- Run: {summary.run_id}", text)
        self.assertEqual(text.count("\n## "), 1)
        self.assertRegex(text, r"## \d{4}-\d{2}-\d{2} \| ")

    def test_reject_without_reason_and_idempotent(self):
        summary = self.dream()
        self.call("reject", summary.run_id, "--only", "1")
        self.assertIn("- Reason: no reason given", self.log_text())
        code, out, _ = self.call("reject", summary.run_id, "--only", "1")
        self.assertIn("rejected 0 change", out)
        self.assertEqual(self.log_text().count("\n## "), 1)

    def test_reject_errors(self):
        summary = self.dream()
        self.call("apply", summary.run_id, "--only", "1")
        code, _, err = self.call("reject", summary.run_id, "--only", "1")
        self.assertEqual(code, 2)
        self.assertIn("already applied", err)
        self.assertEqual(self.call("reject", summary.run_id, "--only", "9")[0], 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["reject", summary.run_id])  # --only is required

    def test_apply_refuses_rejected_unless_forced(self):
        summary = self.dream()
        self.call("reject", summary.run_id, "--only", "3")
        code, _, err = self.call("apply", summary.run_id, "--only", "3")
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        self.assertFalse((self.memory / "topic.md").exists())
        code, out, err = self.call("apply", summary.run_id, "--only", "3", "--force")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.memory / "topic.md").read_text(), "prefers tabs\n")

    def test_apply_all_skips_rejected(self):
        summary = self.dream()
        self.call("reject", summary.run_id, "--only", "3")
        code, out, err = self.call("apply", summary.run_id)
        self.assertEqual(code, 0, err)
        self.assertIn("commit 3 is marked rejected", out)
        self.assertIn("applied 2 commit", out)
        self.assertFalse((self.memory / "topic.md").exists())
        self.assertFalse((self.memory / "b.md").exists())

    def test_discard_records_only_unreviewed_changes(self):
        summary = self.dream()
        self.call("apply", summary.run_id, "--only", "1")
        self.call("reject", summary.run_id, "--only", "2", "--reason", "nope")
        code, out, _ = self.call("discard", summary.run_id)
        self.assertIn("recorded 1 unapplied change", out)
        text = self.log_text()
        self.assertEqual(text.count("\n## "), 2)
        self.assertIn("| topic.md | add", text)
        self.assertIn("- Reason: discarded with run", text)
        self.assertNotIn("| MEMORY.md |", text)  # applied one is not recorded
        self.assertEqual(text.count("- Reason: nope"), 1)  # earlier rejection not duplicated
        self.assertEqual(self.call("discard", summary.run_id)[0], 0)
        self.assertEqual(self.log_text().count("\n## "), 2)  # second discard adds nothing

    def test_refused_discard_records_nothing(self):
        summary = self.dream()
        (summary.worktree / self.slug / "memory" / "a.md").write_text("precious\n", newline="\n")
        self.assertEqual(self.call("discard", summary.run_id)[0], 2)
        self.assertFalse(self.log_text().count("\n## "))
        self.assertNotIn("rejected_commits", runner.get_run(summary.run_id))

    def test_next_run_prompt_points_at_rejection_log(self):
        summary = self.dream()
        self.call("reject", summary.run_id, "--only", "3", "--reason", "no tabs talk")
        self.plan(report="# none\n")
        runner.run_project(self.slug, runner.RunOptions(), now=NOW + timedelta(days=1), echo=False)
        call = self.claude_call()
        self.assertIn((self.state_home / "rejected.md").as_posix(), call["stdin"])
        self.assertIn("no tabs talk", self.log_text())
        argv = call["argv"]
        allowed = argv[argv.index("--allowedTools") + 1: argv.index("--disallowedTools")]
        self.assertIn(runner.read_rule(self.state_home / "rejected.md"), allowed)


class MarkAppliedAndSuggestionCommandTests(CliCase):
    def log_text(self):
        return (self.state_home / "rejected.md").read_text(encoding="utf-8")

    def test_conflict_then_mark_applied_then_discard_does_not_log_it_rejected(self):
        summary = self.dream()
        (self.memory / "MEMORY.md").write_text("# Memory\nlive change\n", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "live edit")
        code, _, err = self.call("apply", summary.run_id)
        self.assertEqual(code, 1)
        self.assertIn("did not apply cleanly", err)
        self.assertIn(f"to-dream mark-applied {summary.run_id} --only N", err)
        code, out, _ = self.call("mark-applied", summary.run_id, "--only", "1", "--note", "merged by hand")
        self.assertEqual((code, out), (0, "marked 1 change(s) as applied\n"))
        self.assertEqual(self.call("mark-applied", summary.run_id, "--only", "1")[1], "marked 0 change(s) as applied\n")
        self.assertEqual(load_json(self.call("show", summary.run_id, "--json"))["changes"][0]["marked_applied"], True)
        code, out, _ = self.call("discard", summary.run_id)
        self.assertIn("recorded 2 unapplied change(s)", out)
        self.assertNotIn("| MEMORY.md |", self.log_text())
        self.assertEqual(self.log_text().count("\n## "), 2)

    def test_mark_applied_requires_only_and_reports_errors(self):
        summary = self.dream()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["mark-applied", summary.run_id])
        code, _, err = self.call("mark-applied", summary.run_id, "--only", "9")
        self.assertEqual(code, 2)
        self.assertIn("no such commit", err)

    def test_reject_suggestion_command(self):
        summary = self.dream()
        code, out, err = self.call("reject-suggestion", summary.run_id, "--text", "Always use tabs",
                                   "--reason", "spaces please")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded the rejected global suggestion", out)
        text = self.log_text()
        self.assertRegex(text, rf"## \d{{4}}-\d{{2}}-\d{{2}} \| {self.slug} \| global suggestion")
        self.assertIn("- Suggestion: Always use tabs", text)
        self.assertIn("- Reason: spaces please", text)
        self.assertIn("already recorded", self.call("reject-suggestion", summary.run_id, "--text", "Always use tabs")[1])
        self.assertEqual(self.call("reject-suggestion", "nope", "--text", "x")[0], 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["reject-suggestion", summary.run_id])  # --text is required

    def test_reject_suggestion_kind_permission(self):
        summary = self.dream()
        code, out, err = self.call("reject-suggestion", summary.run_id, "--kind", "permission",
                                   "--text", "Bash(npm test *)", "--reason", "too broad")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded the rejected permission suggestion", out)
        text = self.log_text()
        self.assertRegex(text, rf"## \d{{4}}-\d{{2}}-\d{{2}} \| {self.slug} \| permission suggestion")
        self.assertIn("- Rule: Bash(npm test *)", text)
        self.assertIn("- Reason: too broad", text)
        self.assertNotIn("| global suggestion", text)
        self.assertIn("already recorded", self.call(
            "reject-suggestion", summary.run_id, "--kind", "permission", "--text", "Bash(npm test *)")[1])
        # the same words as a global suggestion are a different entry
        self.assertIn("recorded the rejected global suggestion",
                      self.call("reject-suggestion", summary.run_id, "--text", "Bash(npm test *)")[1])
        self.assertEqual(self.log_text().count("\n## "), 2)

    def test_reject_suggestion_rejects_an_unknown_kind(self):
        summary = self.dream()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["reject-suggestion", summary.run_id, "--text", "x", "--kind", "other"])

    def test_show_json_parses_permission_suggestions_from_the_report(self):
        report = REPORT + (
            "\n## Permission suggestions\n"
            "- **Rule:** `Bash(npm test *)` · **Kind:** allow · **Scope:** project-local\n"
            "  - **Evidence:** s0.md L12, L40 · blocked 4×, approved after block 3× in 2 sessions\n"
            "  - **Risk:** runs the project's own tests\n"
            "  - **Why:** prompted in every session\n"
        )
        summary = self.dream(**dict(HAPPY, report=report))
        extras = load_json(self.call("show", summary.run_id, "--json"))["report_extras"]
        self.assertEqual(extras["permission_suggestions_parsed"], [{
            "rule": "Bash(npm test *)", "kind": "allow", "scope": "project-local",
            "evidence": "s0.md L12, L40 · blocked 4×, approved after block 3× in 2 sessions",
            "risk": "runs the project's own tests", "why": "prompted in every session",
        }])
        self.assertIn("**Rule:** `Bash(npm test *)`", extras["permission_suggestions"])
        self.assertEqual(extras["global_suggestions"], "Consider a global note about tabs.")

    def test_superseded_run_is_listed_with_its_replacement(self):
        first = self.dream()
        self.plan(report="# none\n")
        second = runner.run_project(self.slug, runner.RunOptions(), now=NOW + timedelta(days=1), echo=False)
        rows = {r["run_id"]: r for r in load_json(self.call("list", "--json"))}
        self.assertEqual(rows[first.run_id]["status"], "superseded")
        self.assertEqual(rows[first.run_id]["superseded_by"], second.run_id)
        self.assertNotIn("superseded_by", rows[second.run_id])
        self.assertEqual(load_json(self.call("show", first.run_id, "--json"))["superseded_by"], second.run_id)
        code, _, err = self.call("apply", first.run_id)
        self.assertEqual(code, 2)
        self.assertIn("superseded", err)

    def test_summary_names_the_superseded_runs(self):
        first = self.dream()
        self.plan(**HAPPY)
        second = runner.run_project(self.slug, runner.RunOptions(), now=NOW + timedelta(days=1), echo=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_summary(second)
        self.assertIn(f"superseded earlier unreviewed run(s): {first.run_id}", out.getvalue())


class ApplyOutputTests(CliCase):
    def test_apply_reports_the_snapshot_and_refusals(self):
        summary = self.dream()
        (self.memory / "a.md").write_text("local edit\n", newline="\n")
        (self.memory / "b.md").write_text(PASSWORD_LINE + "\n", newline="\n")
        code, out, err = self.call("apply", summary.run_id, "--only", "3")
        self.assertEqual(code, 0, err)
        self.assertIn("committed 1 uncommitted memory file(s) first", out)
        self.assertIn(f"{self.slug}/memory/b.md:1 generic-secret-assignment", err)
        self.assertNotIn("hunter2", out + err)

    def test_apply_on_the_wrong_branch_is_a_clean_error(self):
        summary = self.dream()
        git(self.root, "checkout", "-q", "-b", "elsewhere")
        code, _, err = self.call("apply", summary.run_id)
        self.assertEqual(code, 2)
        self.assertIn(f"based on '{BASE_BRANCH}'", err)


class FormatDriftCliTests(CliCase):
    USE_STUB_DISTILL = False

    def test_exit_code_and_message(self):
        (self.root / self.slug / "s.jsonl").write_text('{"kind": "turn"}\n', encoding="utf-8", newline="\n")
        self.plan(**HAPPY)
        code, out, err = self.call("run", "--project", self.slug)
        self.assertEqual(code, 1)
        self.assertIn("transcript format may have changed; to-dream may need an update", err)
        self.assertFalse(self.claude_log.exists())
        self.assertEqual(self.branches(), [BASE_BRANCH])

    def test_run_all_exits_non_zero_when_any_project_drifted(self):
        (self.root / self.slug / "s.jsonl").write_text('{"kind": "turn"}\n', encoding="utf-8", newline="\n")
        code, _, err = self.call("run", "--project", "all")
        self.assertEqual(code, 1)
        self.assertIn("transcript format may have changed", err)


class OutputTests(CliCase):
    FANCY = "★ starred → arrow — dash"  # none of these exist in cp1252

    def dream_fancy(self):
        report = REPORT.replace('add a "prefers tabs" preference', f"add {self.FANCY} preference")
        write = {**HAPPY["write"], "topic.md": self.FANCY + "\n"}
        return self.dream(**dict(HAPPY, write=write, report=report))

    def test_why_is_the_change_line(self):
        summary = self.dream_fancy()
        self.assertEqual(
            [c.why for c in summary.commits],
            ["link the new topic from the index", "remove the stale beta fact", f"add {self.FANCY} preference"],
        )

    def test_summary_and_show_list_change_lines_and_command_hints(self):
        summary = self.dream_fancy()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_summary(summary)
        text = out.getvalue()
        self.assertIn(f"1. chore(to-dream): update {self.slug_short}/MEMORY.md -- link the new topic from the index", text)
        self.assertNotIn("**", text)
        for verb in ("show", "apply", "reject", "discard"):
            self.assertIn(f"to-dream {verb} {summary.run_id}", text)
        self.assertNotIn("python", text)
        shown = self.call("show", summary.run_id)[1]
        self.assertIn(f"chore(to-dream): delete {self.slug_short}/b.md -- remove the stale beta fact", shown)
        self.assertNotIn("**", shown)
        self.assertIn("b.md", shown.split("2.")[1])  # --stat still listed under the change

    def test_why_falls_back_to_first_line_and_is_capped(self):
        long_report = "### MEMORY.md\nplain first line\n\n### topic.md\n**Change:** " + "z" * 300 + "\n"
        summary = self.dream(**dict(HAPPY, report=long_report))
        whys = {c.file: c.why for c in summary.commits}
        self.assertEqual(whys["MEMORY.md"], "plain first line")
        self.assertEqual(len(whys["topic.md"]), 100)

    def test_piped_output_is_utf8_without_pythonioencoding(self):
        summary = self.dream_fancy()
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        for args in (["show", summary.run_id, "--json"], ["show", summary.run_id], ["show", summary.run_id, "--diff"],
                     ["list", "--json"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, "-m", "to_dream", *args], cwd=LIB_DIR, env=env,
                                        capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                self.assertEqual(result.stderr, b"")
                text = result.stdout.decode("utf-8")
                if "--json" in args:
                    data = json.loads(text)
                    if args[0] == "show":
                        topic = data["changes"][2]
                        self.assertIn(self.FANCY, topic["diff"])
                        self.assertIn(self.FANCY, topic["report_section"])
                elif "--diff" in args:
                    self.assertIn(self.FANCY, text)

    def test_use_utf8_skips_streams_without_reconfigure(self):
        cli.use_utf8(io.StringIO())  # must not raise


class EntryPointTests(DreamTestCase):
    def test_python_dash_m_to_dream(self):
        result = subprocess.run([sys.executable, "-m", "to_dream", "list"], cwd=LIB_DIR, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No dream runs", result.stdout)

    def test_no_command_is_a_usage_error(self):
        result = subprocess.run([sys.executable, "-m", "to_dream"], cwd=LIB_DIR, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)

    def test_parse_since(self):
        self.assertEqual(cli.parse_since("2026-11-02").date().isoformat(), "2026-11-02")
        with self.assertRaises(argparse.ArgumentTypeError):
            cli.parse_since("11/02/2026")

    def test_notice_text(self):
        self.assertEqual(cli.notice_text("plain"), "plain")
        self.assertEqual(cli.notice_text({"message": "m"}), "m")
        self.assertEqual(cli.notice_text({"current": "1", "latest": "2"}), "to-dream 2 is out (you have 1)")
        self.assertEqual(cli.notice_text({"x": 1}), '{"x": 1}')


if __name__ == "__main__":
    unittest.main()
