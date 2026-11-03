import json
import os
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from helpers import git, install_hook
from to_dream_helpers import BASE_BRANCH, SEED_FILES, DreamTestCase

from to_dream import config, gitops, runner, state as dstate

# Fake secrets are assembled at runtime so no key-shaped literal exists in this file.
SECRET = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"
PASSWORD_LINE = "password: " + "hunter2" + "hunter2"
NOW = datetime(2026, 11, 2, 18, 30, 12, tzinfo=timezone.utc)
REPORT = """# Dream report

### MEMORY.md
Index now lists topic.
**Change:** link the new topic from the index
- evidence: session s0 turn 4

### topic.md
New durable preference found in two sessions.
**Change:** add a "prefers tabs" preference

### b.md
Superseded by topic.md; sessions s1 contradicts it.
**Change:** remove the stale beta fact

## Global suggestions
Consider a global note about tabs.

## Stale candidates
- a.md may be stale

## Not acted on
- nothing else
"""
HAPPY = {
    "write": {
        "MEMORY.md": "# Memory\n- [A](a.md)\n- [Topic](topic.md)\n",
        "topic.md": "prefers tabs\n",
        "ghost.md": "",
    },
    "blank": ["b.md"],
    "report": REPORT,
}


def run_id_for(short: str, moment: datetime = NOW) -> str:
    """The run id the runner must produce for a project short name at ``moment`` (local time)."""
    return f"{short}-{moment.astimezone():%y%m%d-%H%M}"


class PureHelperTests(unittest.TestCase):
    def test_slug_from_path(self):
        self.assertEqual(runner.slug_from_path("/home/sam/my-proj"), "-home-sam-my-proj")
        self.assertEqual(runner.slug_from_path("E:\\My Data\\x_y.z\\"), "E--My-Data-x-y-z")
        self.assertEqual(runner.slug_from_path("/home/sam/proj/"), "-home-sam-proj")

    def test_slug_short(self):
        cases = [
            ("-home-sam-code-my-proj", "/home/sam", "code-my-proj"),
            ("-home-sam-Proj", "/home/sam", "proj"),
            ("C--Users-sam-RiftsOnline", "C:\\Users\\sam", "riftsonline"),
            ("c--users-sam-x", "C:\\Users\\sam", "x"),
            ("E--Other-Place-proj", "/home/sam", "other-place-proj"),
            ("-srv-app", "/home/sam", "srv-app"),
            ("-home-sam", "/home/sam", "home-sam"),  # the project is the home dir itself: keep it whole
            ("-home-samantha-x", "/home/sam", "home-samantha-x"),  # prefix must end at a component boundary
        ]
        for slug, home, expected in cases:
            with self.subTest(slug=slug):
                self.assertEqual(runner.slug_short(slug, home), expected)

    def test_render_template(self):
        values = {name: f"<{name}>" for name in runner.PLACEHOLDERS}
        self.assertEqual(runner.render_template("a {{slug}} b {{ memory_dir }}", values), "a <slug> b <memory_dir>")

    def test_render_template_does_not_reexpand_values(self):
        values = {"slug": "{{memory_dir}}", "memory_dir": "m"}
        self.assertEqual(runner.render_template("{{slug}}", values), "{{memory_dir}}")

    def test_render_template_unknown_placeholder(self):
        with self.assertRaisesRegex(runner.TemplateError, "unknown.*bogus"):
            runner.render_template("{{slug}} {{bogus}}", {"slug": "x"})

    def test_render_template_missing_value(self):
        with self.assertRaisesRegex(runner.TemplateError, "no value.*memory_dir"):
            runner.render_template("{{slug}} {{memory_dir}}", {"slug": "x"})

    def test_bundled_prompt_renders_with_all_placeholders(self):
        template = runner.ORCHESTRATOR_PROMPT.read_text(encoding="utf-8")
        values = {name: f"V-{name}" for name in runner.PLACEHOLDERS}
        self.assertNotRegex(runner.render_template(template, values), r"\{\{")

    def test_bundled_prompts_are_generic(self):
        for path in (runner.ORCHESTRATOR_PROMPT, runner.DEFAULT_STEERING):
            text = path.read_text(encoding="utf-8")
            for private in ("Alex", "technophile", "MyData", "E:\\", "E:/", "Git Bash", "/mes/", "docket"):
                with self.subTest(file=path.name, term=private):
                    self.assertNotIn(private, text)

    def test_resolve_window(self):
        last = NOW - timedelta(days=2)
        explicit = NOW - timedelta(days=30)
        self.assertEqual(runner.resolve_window(last, None, 7, NOW), (last, NOW))
        self.assertEqual(runner.resolve_window(None, None, 7, NOW), (NOW - timedelta(days=7), NOW))
        self.assertEqual(runner.resolve_window(last, explicit, 7, NOW), (explicit, NOW))

    def test_read_rule(self):
        self.assertEqual(runner.read_rule(Path("C:/Users/a/proj"), recursive=True), "Read(//c/Users/a/proj/**)")
        self.assertEqual(runner.read_rule("E:\\x\\CLAUDE.md"), "Read(//e/x/CLAUDE.md)")
        if os.name != "nt":
            self.assertEqual(runner.read_rule("/home/a"), "Read(//home/a)")
            self.assertEqual(runner.read_rule("/home/a/p", recursive=True), "Read(//home/a/p/**)")

    def test_build_claude_command(self):
        cmd = runner.build_claude_command("claude", "opus", 5.0, Path("/run"), ["Read(//e/p/**)"])
        self.assertEqual(cmd[0], "claude")
        self.assertEqual(cmd[1:3], ["-p", "--output-format"])
        i = cmd.index("--allowedTools")
        allowed = cmd[i + 1: cmd.index("--disallowedTools")]
        self.assertEqual(allowed, ["Read", "Glob", "Grep", "Agent", "Read(//e/p/**)"])
        self.assertNotIn("Edit", allowed)  # a blanket Edit/Write allow would permit writes anywhere
        self.assertNotIn("Write", allowed)
        j = cmd.index("--disallowedTools")
        self.assertEqual(cmd[j + 1: j + 5], ["Bash", "PowerShell", "WebFetch", "WebSearch"])
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "5")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(cmd[-2:], ["--add-dir", str(Path("/run"))])

    def test_no_budget_flag_unless_given(self):
        bare = runner.build_claude_command("claude", "opus", None, Path("/run"), [])
        self.assertNotIn("--max-budget-usd", bare)
        self.assertEqual(bare[bare.index("--model") + 1: bare.index("--model") + 3], ["opus", "--allowedTools"])
        self.assertEqual(runner.budget_args(None), [])
        self.assertEqual(runner.budget_args(2.5), ["--max-budget-usd", "2.5"])

    def test_interpret_output(self):
        ok = json.dumps({"is_error": False, "total_cost_usd": 1.5})
        self.assertEqual(runner.interpret_output(0, ok, "", False, 60), runner.ClaudeOutcome(True, 1.5))
        bad = runner.interpret_output(0, json.dumps({"is_error": True, "subtype": "error_max_budget_usd"}), "", False, 60)
        self.assertFalse(bad.ok)
        self.assertIn("error_max_budget_usd", bad.error)
        self.assertFalse(runner.interpret_output(1, ok, "boom", False, 60).ok)
        self.assertFalse(runner.interpret_output(0, "not json", "", False, 60).ok)
        timed = runner.interpret_output(-9, "", "", True, 2)
        self.assertTrue(timed.timed_out)
        self.assertIn("2 min", timed.error)

    def test_select_commits(self):
        commits = [("a", "x"), ("b", "y"), ("c", "z")]
        self.assertEqual([n for n, *_ in runner.select_commits(commits, None)], [1, 2, 3])
        self.assertEqual([s for _, s, _ in runner.select_commits(commits, [3, 1])], ["a", "c"])
        with self.assertRaises(runner.RunError):
            runner.select_commits(commits, [4])
        with self.assertRaises(runner.RunError):
            runner.select_commits(commits, [0])

    def test_name_fields(self):
        fields = runner.name_fields("st", "st-261102-1830", datetime(2026, 11, 2, 18, 30))
        self.assertEqual(fields, {"run_id": "st-261102-1830", "project": "st", "yy": "26", "mm": "11", "dd": "02",
                                  "hhmm": "1830"})
        self.assertEqual(set(fields), set(config.TEMPLATE_FIELDS))

    def test_display_status_counts_marked_commits(self):
        record = {"status": "pending", "commits": [{}, {}, {}], "applied": {"a": "x"}, "marked_applied": {"b": {}}}
        self.assertEqual(runner.display_status(record), "partially-applied")
        record["marked_applied"]["c"] = {}
        self.assertEqual(runner.display_status(record), "applied")
        self.assertEqual(runner.display_status({"status": "pending", "commits": [{}]}), "pending")

    def test_pick_extras_keyword_matching(self):
        sections = {"changes": "c", "stale_candidates_unverified": "s", "notes": "n",
                    "patterns_noticed_but_not_acted_on": "p", "suggestions_for_global_claude_md": "g"}
        self.assertEqual(runner.pick_extras(sections),
                         {"global_suggestions": "g", "stale_candidates": "s", "not_acted_on": "p"})
        self.assertEqual(runner.pick_extras({}), {"global_suggestions": "", "stale_candidates": "", "not_acted_on": ""})

    def test_report_extras_match_the_headings_of_the_bundled_prompt(self):
        template = runner.ORCHESTRATOR_PROMPT.read_text(encoding="utf-8")
        headings = [h for h in re.findall(r"^## (.+?)\s*$", template, re.MULTILINE)
                    if re.search(r"global|stale|not acted on", h, re.IGNORECASE)]
        self.assertEqual(len(headings), 3, headings)  # the prompt must still define all three
        report = "".join(f"## {h}\nbody of {i}\n\n" for i, h in enumerate(headings))
        extras = runner.pick_extras(gitops.level2_sections(report))
        by_heading = {h: f"body of {i}" for i, h in enumerate(headings)}
        self.assertEqual(extras["global_suggestions"], next(b for h, b in by_heading.items() if "global" in h.lower()))
        self.assertEqual(extras["stale_candidates"], next(b for h, b in by_heading.items() if h.lower().startswith("stale")))
        self.assertEqual(extras["not_acted_on"], next(b for h, b in by_heading.items() if "not acted on" in h.lower()))


class SlugResolutionTests(DreamTestCase):
    def test_resolves_slug_path_case_and_dot(self):
        for text in (self.slug, self.slug.upper(), str(self.project_dir), str(self.project_dir).upper()):
            with self.subTest(text=text):
                self.assertEqual(runner.resolve_slug(text, self.root), self.slug)

    def test_resolves_an_existing_directory_given_relatively(self):
        self.project_dir.mkdir(parents=True)
        old = Path.cwd()
        os.chdir(self.project_dir)
        try:
            self.assertEqual(runner.resolve_slug(".", self.root), self.slug)
        finally:
            os.chdir(old)

    def test_trailing_separator_is_ignored(self):
        self.assertEqual(runner.resolve_slug(str(self.project_dir) + os.sep, self.root), self.slug)

    def test_unknown_project(self):
        with self.assertRaises(runner.RunError):
            runner.resolve_slug(str(self.tmp / "nope"), self.root)


class EndToEndTests(DreamTestCase):
    def run_happy(self, **options):
        self.plan(**HAPPY)
        return runner.run_project(self.slug, runner.RunOptions(**options), now=NOW, echo=False)

    def run_id(self):
        return run_id_for(self.slug_short)

    def test_happy_path_commits_each_file(self):
        summary = self.run_happy()
        self.assertEqual(summary.status, runner.STATUS_PENDING)
        self.assertEqual(summary.exit_code, 0)
        self.assertEqual(summary.cost, 0.42)
        self.assertEqual(summary.run_id, self.run_id())
        self.assertEqual(summary.branch, f"dream/{self.run_id()}")
        self.assertEqual(summary.base_branch, BASE_BRANCH)
        self.assertEqual(summary.worktree, self.state_home / "worktrees" / self.run_id())
        short = self.slug_short
        subjects = [c.subject for c in summary.commits]
        self.assertEqual(subjects, [
            f"chore(to-dream): update {short}/MEMORY.md",
            f"chore(to-dream): delete {short}/b.md",
            f"chore(to-dream): add {short}/topic.md",
        ])
        self.assertEqual(summary.commits[2].why, "add a \"prefers tabs\" preference")
        git_subjects = [s for _, s in gitops.list_commits(self.root, BASE_BRANCH, summary.branch)]
        self.assertEqual(git_subjects, subjects)
        body = git(self.root, "log", "-1", "--format=%b", summary.commits[2].sha)
        self.assertIn("New durable preference found in two sessions.", body)
        self.assertIn(f"To-Dream-Run: {self.run_id()}", body)
        self.assertNotIn("Superseded", body)  # only this file's section
        prefix = f"{self.slug}/memory/"
        self.assertEqual(git(self.root, "show", f"{summary.branch}:{prefix}topic.md"), "prefers tabs\n")
        self.assertFalse((summary.worktree / self.slug / "memory" / "ghost.md").exists())
        self.assertEqual(
            git(self.root, "ls-tree", "-r", "--name-only", summary.branch, prefix).split(),
            [prefix + "MEMORY.md", prefix + "a.md", prefix + "topic.md"],
        )

    def test_live_memory_untouched_and_artifacts_written(self):
        summary = self.run_happy()
        self.assertEqual(git(self.root, "rev-parse", BASE_BRANCH).strip(), self.seed_head)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        for name, text in SEED_FILES.items():
            self.assertEqual((self.memory / name).read_text(encoding="utf-8"), text)
        run_dir = summary.run_dir
        self.assertEqual(run_dir, self.state_home / "runs" / summary.run_id)
        for name in ("prompt.md", "claude-output.json", "run.log", "DREAM_REPORT.md"):
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertEqual((run_dir / "DREAM_REPORT.md").read_text(encoding="utf-8"), REPORT)
        self.assertFalse((summary.worktree / self.slug / "memory" / "DREAM_REPORT.md").exists())
        self.assertEqual(json.loads((run_dir / "claude-output.json").read_text())["total_cost_usd"], 0.42)
        self.assertTrue((run_dir / "digests" / "INDEX.md").is_file())

    def test_claude_invocation(self):
        summary = self.run_happy(model="opus", analyst_model="haiku", budget_usd=3.5)
        call = self.claude_call()
        argv = call["argv"]
        self.assertEqual(Path(call["cwd"]).resolve(), (summary.worktree / self.slug / "memory").resolve())
        self.assertEqual(call["auto_memory"], "1")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3.5")
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(summary.run_dir))
        allowed = argv[argv.index("--allowedTools") + 1: argv.index("--disallowedTools")]
        self.assertEqual([a for a in allowed if not a.startswith("Read(")], ["Read", "Glob", "Grep", "Agent"])
        project = self.project_dir.as_posix()
        if os.name == "nt":
            project = "/" + project[0].lower() + project[2:]
        self.assertIn(f"Read(/{project}/**)", allowed)  # read-only access to the project source
        self.assertEqual(sum(1 for a in allowed if a.startswith("Read(") and a.endswith("/**)")), 2)  # + live memory
        self.assertNotIn("Edit", allowed)
        prompt = call["stdin"]
        self.assertEqual(prompt, (summary.run_dir / "prompt.md").read_text(encoding="utf-8"))
        self.assertNotRegex(prompt, r"\{\{")
        self.assertIn((summary.worktree / self.slug / "memory" / "DREAM_REPORT.md").as_posix(), prompt)
        self.assertIn("haiku", prompt)
        self.assertIn(self.project_dir.as_posix(), prompt)
        rejected_log = self.state_home / "rejected.md"
        self.assertIn(rejected_log.as_posix(), prompt)
        self.assertTrue(rejected_log.read_text(encoding="utf-8").startswith("# Rejected dream changes"))
        self.assertIn(runner.read_rule(rejected_log), allowed)  # readable, not editable
        self.assertIn((self.root.parent / "CLAUDE.md").as_posix(), prompt)

    def test_no_budget_flag_by_default(self):
        self.run_happy()
        argv = self.claude_call()["argv"]
        self.assertNotIn("--max-budget-usd", argv)
        self.assertIn("--permission-mode", argv)

    def test_state_updated_and_window_chains(self):
        self.run_happy(days=3)
        first = self.distill.calls[0]
        self.assertEqual(first["until"], NOW)
        self.assertEqual(first["since"], NOW - timedelta(days=3))
        self.assertEqual(first["max_sessions"], 40)
        state = dstate.load_state()
        self.assertEqual(dstate.get_last_dream(state, self.slug), NOW.replace(microsecond=0))
        later = NOW + timedelta(days=1, minutes=5)
        runner.run_project(self.slug, runner.RunOptions(days=30), now=later, echo=False)
        self.assertEqual(self.distill.calls[1]["since"], NOW)  # last dream wins over --days
        explicit = later - timedelta(days=9)
        runner.run_project(self.slug, runner.RunOptions(since=explicit), now=later + timedelta(minutes=5), echo=False)
        self.assertEqual(self.distill.calls[2]["since"], explicit)  # explicit --since wins over state

    def test_run_record_in_state(self):
        summary = self.run_happy()
        record = dstate.load_state()["runs"][summary.run_id]
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["branch"], summary.branch)
        self.assertEqual(record["base_branch"], BASE_BRANCH)
        self.assertEqual(record["base"], self.seed_head)
        self.assertEqual([c["sha"] for c in record["commits"]], [c.sha for c in summary.commits])
        self.assertEqual(record["files"], ["MEMORY.md", "b.md", "topic.md"])

    def test_out_of_scope_edits_reverted_with_warning(self):
        plan = dict(HAPPY, write={**HAPPY["write"], "../../other-proj/memory/x.md": "sneaky\n",
                                  "../../.gitignore": "# hacked\n"})
        self.plan(**plan)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(len(summary.commits), 3)
        self.assertTrue(any("other-proj/memory/x.md" in w for w in summary.warnings))
        self.assertTrue(any(".gitignore" in w for w in summary.warnings))
        changed = git(self.root, "diff", "--name-only", f"{BASE_BRANCH}..{summary.branch}").split()
        self.assertEqual(sorted(changed), sorted(f"{self.slug}/memory/{n}" for n in ("MEMORY.md", "b.md", "topic.md")))
        self.assertFalse((summary.worktree / "other-proj").exists())
        self.assertNotIn("hacked", (summary.worktree / ".gitignore").read_text())

    def test_dry_run_creates_no_branch_no_worktree_and_no_snapshot(self):
        self.plan(**HAPPY)
        (self.memory / "a.md").write_text("dirty\n", newline="\n")
        summary = runner.run_project(self.slug, runner.RunOptions(dry_run=True), now=NOW, echo=False)
        self.assertEqual(summary.status, runner.STATUS_DRY_RUN)
        self.assertEqual(self.branches(), [BASE_BRANCH])
        self.assertFalse((self.state_home / "worktrees").exists())
        self.assertFalse(self.claude_log.exists())  # claude never invoked
        self.assertTrue(summary.index_path.is_file())
        self.assertEqual(summary.stats["sessions"], 2)
        self.assertEqual(summary.stats["chars"], 2000)
        self.assertEqual(summary.stats["human_prompts"], 6)
        self.assertEqual(dstate.load_state(), {"last_dream": {}, "runs": {}})
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), self.seed_head)  # dry run commits nothing

    def test_no_sessions_exits_cleanly_without_snapshot(self):
        self.distill.count = 0
        (self.memory / "a.md").write_text("dirty\n", newline="\n")
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(summary.status, runner.STATUS_NO_SESSIONS)
        self.assertEqual(summary.exit_code, 0)
        self.assertFalse(self.claude_log.exists())
        self.assertEqual(self.branches(), [BASE_BRANCH])
        self.assertEqual(dstate.load_state()["last_dream"], {})
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), self.seed_head)

    def test_zero_change_run_cleans_up_but_records_success(self):
        self.plan(report="# nothing to do\n")
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(summary.status, runner.STATUS_EMPTY)
        self.assertEqual(summary.commits, ())
        self.assertIsNone(summary.branch)
        self.assertEqual(self.branches(), [BASE_BRANCH])
        worktrees = self.state_home / "worktrees"
        self.assertFalse(worktrees.exists() and any(worktrees.iterdir()))
        self.assertEqual(git(self.root, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertTrue(summary.run_dir.is_dir())
        self.assertTrue((summary.run_dir / "DREAM_REPORT.md").is_file())
        self.assertEqual(dstate.get_last_dream(dstate.load_state(), self.slug), NOW.replace(microsecond=0))

    def test_failures_do_not_update_state(self):
        cases = {
            "nonzero exit": dict(HAPPY, exit=3),
            "is_error": dict(HAPPY, is_error=True),
            "garbage output": dict(HAPPY, stdout="not json at all"),
        }
        for label, plan in cases.items():
            with self.subTest(label):
                self.plan(**plan)
                now = NOW + timedelta(hours=len(label))
                summary = runner.run_project(self.slug, runner.RunOptions(), now=now, echo=False)
                self.assertEqual(summary.status, runner.STATUS_FAILED)
                self.assertEqual(summary.exit_code, 1)
                self.assertEqual(summary.commits, ())
                self.assertEqual(dstate.load_state()["last_dream"], {})
                self.assertEqual(dstate.load_state()["runs"][summary.run_id]["status"], "failed")
                self.assertTrue(summary.worktree.exists())  # kept for inspection; `discard` removes it
                self.assertEqual(gitops.list_commits(self.root, BASE_BRANCH, summary.branch), [])

    def test_timeout_kills_claude_and_fails(self):
        self.plan(sleep=30, **HAPPY)
        started = time.monotonic()
        summary = runner.run_project(self.slug, runner.RunOptions(timeout_min=0.05), now=NOW, echo=False)
        self.assertLess(time.monotonic() - started, 25)
        self.assertEqual(summary.status, runner.STATUS_FAILED)
        self.assertIn("timed out", summary.message)
        self.assertEqual(dstate.load_state()["last_dream"], {})

    def test_second_run_in_same_minute_gets_unique_ids(self):
        first = self.run_happy()
        self.plan(report="# none\n")
        second = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertTrue(second.run_id.endswith("-2"))

    def test_run_all_skips_inactive_and_dream_projects(self):
        active = self.root / "-srv-active"
        idle = self.root / "-srv-idle"
        own = self.root / (runner.slug_from_path(str(self.state_home / "worktrees")) + "-x-memory")
        for directory in (active, idle, own):
            directory.mkdir()
            (directory / "s.jsonl").write_text("{}\n")
        old = time.time() - 40 * 86400
        os.utime(idle / "s.jsonl", (old, old))
        self.plan(report="# none\n")
        results = runner.run_all(runner.RunOptions(days=7), now=datetime.now(timezone.utc), echo=False)
        self.assertEqual([r.slug for r in results], ["-srv-active"])
        called = [Path(c["project_dir"]).name for c in self.distill.calls]
        self.assertEqual(called, ["-srv-active"])

    def test_run_all_preflights_once_and_raises(self):
        git(self.root, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(runner.PreflightError, "detached"):
            runner.run_all(runner.RunOptions(), now=NOW, echo=False)


class BaseBranchTests(DreamTestCase):
    """No branch name is hardcoded: the checked-out branch is the base, recorded per run."""

    def test_runs_from_whatever_branch_is_checked_out_and_records_it(self):
        git(self.root, "checkout", "-q", "-b", "claude-memory-sync")
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(summary.base_branch, "claude-memory-sync")
        record = dstate.load_state()["runs"][summary.run_id]
        self.assertEqual(record["base_branch"], "claude-memory-sync")
        self.assertEqual(record["base"], self.seed_head)
        self.assertEqual(len(gitops.list_commits(self.root, "claude-memory-sync", summary.branch)), 3)

    def test_apply_lands_on_the_recorded_base_branch(self):
        git(self.root, "checkout", "-q", "-b", "claude-memory-sync")
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        runner.apply_run(summary.run_id)
        self.assertEqual((self.memory / "topic.md").read_text(), "prefers tabs\n")
        self.assertEqual(git(self.root, "branch", "--show-current").strip(), "claude-memory-sync")
        git(self.root, "checkout", "-q", BASE_BRANCH)
        self.assertFalse((self.memory / "topic.md").exists())  # the original branch never got it

    def test_apply_refuses_when_a_different_branch_is_checked_out(self):
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        git(self.root, "checkout", "-q", "-b", "scratch")
        with self.assertRaisesRegex(runner.RunError, f"'scratch' checked out.*'{BASE_BRANCH}'"):
            runner.apply_run(summary.run_id)
        self.assertEqual(runner.get_run(summary.run_id)["applied"], {})
        self.assertFalse((self.memory / "topic.md").exists())
        git(self.root, "checkout", "-q", BASE_BRANCH)
        self.assertIsNone(runner.apply_run(summary.run_id).result.failed)

    def test_apply_refuses_detached_head(self):
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        git(self.root, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(runner.RunError, "detached"):
            runner.apply_run(summary.run_id)

    def test_run_refuses_detached_head_and_missing_repo(self):
        git(self.root, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(runner.PreflightError, "detached"):
            runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        git(self.root, "checkout", "-q", BASE_BRANCH)
        with mock.patch.dict(os.environ, {"TO_DREAM_MEMORY_ROOT": str(self.tmp / "nowhere")}):
            with self.assertRaisesRegex(runner.PreflightError, "memory repo not found"):
                runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)

    def test_missing_claude_is_a_preflight_error_but_not_for_dry_runs(self):
        with mock.patch.dict(os.environ, {"TO_DREAM_CLAUDE_CMD": str(self.tmp / "missing.exe")}):
            with self.assertRaisesRegex(runner.PreflightError, "claude"):
                runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
            summary = runner.run_project(self.slug, runner.RunOptions(dry_run=True), now=NOW, echo=False)
        self.assertEqual(summary.status, runner.STATUS_DRY_RUN)


class TemplateConfigTests(DreamTestCase):
    def test_branch_template_placeholders(self):
        config.set_value("branch_template", "memory-dream/{project}/{yy}{mm}{dd}-{hhmm}-{run_id}")
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        local = NOW.astimezone()
        self.assertEqual(summary.branch, f"memory-dream/{self.slug_short}/{local:%y%m%d}-{local:%H%M}-{summary.run_id}")
        self.assertIn(summary.branch, self.branches())
        self.assertEqual(dstate.load_state()["runs"][summary.run_id]["branch"], summary.branch)

    def test_worktree_dir_and_template(self):
        target = self.tmp / "elsewhere"
        config.set_value("worktree_dir", str(target))
        config.set_value("worktree_template", "wt-{project}-{hhmm}")
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(summary.worktree, target / f"wt-{self.slug_short}-{NOW.astimezone():%H%M}")
        self.assertTrue((summary.worktree / self.slug / "memory" / "topic.md").is_file())
        self.assertFalse((self.state_home / "worktrees").exists())

    def test_worktree_template_cannot_escape_its_directory(self):
        config.set_value("worktree_template", "a/b:{run_id}")
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(summary.worktree.parent, self.state_home / "worktrees")
        self.assertEqual(summary.worktree.name, f"a-b-{summary.run_id}")

    def test_template_without_run_id_still_gets_unique_branches(self):
        config.set_value("branch_template", "dream/{project}-{hhmm}")
        self.plan(**HAPPY)
        first = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.plan(**HAPPY)
        second = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertNotEqual(first.branch, second.branch)
        self.assertEqual(second.branch, first.branch + "-2")

    def test_default_templates(self):
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(summary.branch, f"dream/{summary.run_id}")
        self.assertEqual(summary.worktree.name, summary.run_id)
        self.assertEqual(summary.worktree.parent, self.state_home / "worktrees")

    def test_a_branch_name_git_rejects_is_a_preflight_error_and_leaves_nothing_behind(self):
        config.set_value("branch_template", "dream/.{run_id}")  # a component may not start with a dot
        self.plan(**HAPPY)
        with self.assertRaisesRegex(runner.PreflightError, "not a valid git branch name"):
            runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertFalse((self.state_home / "runs").exists() and any((self.state_home / "runs").iterdir()))

    def test_steering_precedence(self):
        bundled = runner.steering_path()
        self.assertEqual(bundled, runner.DEFAULT_STEERING)
        override = self.state_home / "steering.md"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("mine\n", newline="\n")
        self.assertEqual(runner.steering_path(), override)
        custom = self.tmp / "custom-steering.md"
        custom.write_text("custom\n", newline="\n")
        config.set_value("steering_path", str(custom))
        self.assertEqual(runner.steering_path(), custom)

    def test_steering_reaches_the_prompt_and_read_rules(self):
        custom = self.tmp / "custom-steering.md"
        custom.write_text("custom\n", newline="\n")
        config.set_value("steering_path", str(custom))
        self.plan(**HAPPY)
        runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        call = self.claude_call()
        self.assertIn(custom.as_posix(), call["stdin"])
        argv = call["argv"]
        allowed = argv[argv.index("--allowedTools") + 1: argv.index("--disallowedTools")]
        self.assertIn(runner.read_rule(custom), allowed)

    def test_missing_configured_steering_file_is_a_preflight_error(self):
        config.set_value("steering_path", str(self.tmp / "missing.md"))
        with self.assertRaisesRegex(runner.PreflightError, "steering_path"):
            runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)


class SnapshotBeforeRunTests(DreamTestCase):
    def test_dirty_memory_is_snapshotted_before_the_branch_is_created(self):
        (self.memory / "a.md").write_text("alpha edited by hand\n", newline="\n")
        (self.memory / "fresh.md").write_text("brand new\n", newline="\n")
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        run_id = summary.run_id
        self.assertEqual(self.subjects(2), [f"chore(to-dream): snapshot before run {run_id}", "seed memory"])
        snapshot = git(self.root, "rev-parse", BASE_BRANCH).strip()
        self.assertNotEqual(snapshot, self.seed_head)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual(dstate.load_state()["runs"][run_id]["base"], snapshot)  # the branch forks from the snapshot
        self.assertEqual(git(self.root, "merge-base", BASE_BRANCH, summary.branch).strip(), snapshot)
        prefix = f"{self.slug}/memory/"
        self.assertEqual(git(self.root, "show", f"{summary.branch}:{prefix}fresh.md"), "brand new\n")
        self.assertEqual(git(self.root, "show", f"{summary.branch}:{prefix}a.md"), "alpha edited by hand\n")
        self.assertEqual(len(gitops.list_commits(self.root, BASE_BRANCH, summary.branch)), 3)  # snapshot not in the proposal
        self.assertTrue(any("snapshot" in line for line in (summary.run_dir / "run.log").read_text().splitlines()))

    def test_clean_memory_makes_no_snapshot_commit(self):
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(git(self.root, "rev-parse", BASE_BRANCH).strip(), self.seed_head)
        self.assertFalse(any("snapshot" in s for s in self.subjects()))
        self.assertEqual(dstate.load_state()["runs"][summary.run_id]["base"], self.seed_head)

    def test_snapshot_refuses_a_secret_file_and_the_run_goes_on(self):
        (self.memory / "a.md").write_text(f"ok\n{PASSWORD_LINE}\n", newline="\n")
        (self.memory / "b.md").write_text("beta edited\n", newline="\n")
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        warning = next(w for w in summary.warnings if "not snapshotted" in w)
        self.assertIn(f"{self.slug}/memory/a.md:2 generic-secret-assignment", warning)
        self.assertNotIn("hunter2", " ".join(summary.warnings))
        snapshot_files = git(self.root, "show", "--name-only", "--format=", f"{BASE_BRANCH}").split()
        self.assertEqual(snapshot_files, [f"{self.slug}/memory/b.md"])
        self.assertIn("hunter2", (self.memory / "a.md").read_text())  # left dirty on disk
        self.assertNotIn("hunter2", git(self.root, "show", f"{summary.branch}:{self.slug}/memory/a.md"))

    def test_user_hook_rejecting_the_snapshot_does_not_abort_the_run(self):
        install_hook(self.root, "echo 'house rules' >&2\nexit 1\n")
        (self.memory / "a.md").write_text("alpha edited\n", newline="\n")
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertTrue(any("not snapshotted" in w and "house rules" in w for w in summary.warnings))
        self.assertEqual(git(self.root, "rev-parse", BASE_BRANCH).strip(), self.seed_head)
        # the user's hook also rejects every proposal commit, which is reported as today
        self.assertEqual(summary.status, runner.STATUS_REJECTED)
        self.assertEqual(sorted(name for name, _ in summary.rejected), ["MEMORY.md", "b.md", "topic.md"])
        self.assertTrue(all("house rules" in reason for _, reason in summary.rejected))


class InProcessScanTests(DreamTestCase):
    """Proposal files are scanned in-process: no hook is installed anywhere in these tests."""

    def test_secret_in_a_proposal_stays_uncommitted_and_is_reported_without_the_value(self):
        self.assertFalse((self.root / ".git" / "hooks" / "pre-commit").exists())
        plan = dict(HAPPY, write={**HAPPY["write"], "topic.md": f"intro\ntoken {SECRET}\n"})
        self.plan(**plan)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(summary.status, runner.STATUS_PENDING)
        self.assertEqual([c.subject.split("/")[-1] for c in summary.commits], ["MEMORY.md", "b.md"])
        self.assertEqual([name for name, _ in summary.rejected], ["topic.md"])
        reason = summary.rejected[0][1]
        self.assertIn(f"{self.slug}/memory/topic.md:2 github-token", reason)
        self.assertNotIn(SECRET, reason)
        self.assertNotIn(SECRET, (summary.run_dir / "run.log").read_text())
        self.assertTrue((summary.worktree / self.slug / "memory" / "topic.md").exists())
        self.assertEqual(git(summary.worktree, "diff", "--cached", "--name-only"), "")
        self.assertEqual(dstate.load_state()["runs"][summary.run_id]["rejected"][0][0], "topic.md")
        self.assertNotIn(SECRET, git(self.root, "log", "--all", "-p"))

    def test_only_rejected_files_keeps_worktree(self):
        self.plan(write={"topic.md": f"token {SECRET}\n"}, report="### topic.md\nx\n")
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.assertEqual(summary.status, runner.STATUS_REJECTED)
        self.assertTrue(summary.worktree.exists())
        self.assertIn(summary.branch, self.branches())


class FormatDriftTests(DreamTestCase):
    """With the real distiller: unreadable transcripts stop the run before Claude is called."""

    USE_STUB_DISTILL = False

    def write_transcript(self, records):
        path = self.root / self.slug / "session-1.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8", newline="\n")
        return path

    def test_unparseable_transcripts_warn_and_exit_non_zero_without_calling_claude(self):
        self.write_transcript([{"kind": "turn", "role": "user", "text": "hi"}, {"kind": "turn", "role": "assistant"}])
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=datetime.now(timezone.utc), echo=False)
        self.assertEqual(summary.status, runner.STATUS_FORMAT_DRIFT)
        self.assertEqual(summary.exit_code, 1)
        self.assertIn("transcript format may have changed; to-dream may need an update", summary.warnings)
        self.assertEqual(summary.message, runner.FORMAT_DRIFT_MESSAGE)
        self.assertFalse(self.claude_log.exists())
        self.assertEqual(self.branches(), [BASE_BRANCH])
        self.assertEqual(dstate.load_state(), {"last_dream": {}, "runs": {}})
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), self.seed_head)

    def test_dry_run_also_reports_drift(self):
        self.write_transcript([{"unexpected": True}])
        summary = runner.run_project(self.slug, runner.RunOptions(dry_run=True), now=datetime.now(timezone.utc),
                                     echo=False)
        self.assertEqual(summary.status, runner.STATUS_FORMAT_DRIFT)

    def test_a_quiet_window_is_not_drift(self):
        now = datetime.now(timezone.utc)
        self.write_transcript([{"type": "user", "message": {"content": "old prompt"},
                                "timestamp": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}])
        summary = runner.run_project(self.slug, runner.RunOptions(days=7), now=now, echo=False)
        self.assertEqual(summary.status, runner.STATUS_NO_SESSIONS)
        self.assertEqual(summary.exit_code, 0)

    def test_no_transcripts_at_all_is_not_drift(self):
        summary = runner.run_project(self.slug, runner.RunOptions(), now=datetime.now(timezone.utc), echo=False)
        self.assertEqual(summary.status, runner.STATUS_NO_SESSIONS)

    def test_readable_transcripts_run_end_to_end_with_the_real_distiller(self):
        now = datetime.now(timezone.utc)
        stamp = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.write_transcript([
            {"type": "user", "timestamp": stamp, "cwd": str(self.project_dir), "sessionId": "s1",
             "message": {"role": "user", "content": "No, use tabs instead of spaces"}},
            {"type": "assistant", "timestamp": stamp, "message": {"role": "assistant", "content": [
                {"type": "text", "text": "ok"}]}},
        ])
        self.plan(**HAPPY)
        summary = runner.run_project(self.slug, runner.RunOptions(), now=now, echo=False)
        self.assertEqual(summary.status, runner.STATUS_PENDING)
        digest = next((summary.run_dir / "digests").glob("*_session-.md"))
        self.assertIn("use tabs instead of spaces", digest.read_text(encoding="utf-8"))
        self.assertIn(str(self.project_dir.as_posix()), self.claude_call()["stdin"])  # cwd from the transcript


class ApplyDiscardE2ETests(DreamTestCase):
    def dream(self):
        self.plan(**HAPPY)
        return runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)

    def test_apply_all_then_status_applied(self):
        summary = self.dream()
        report = runner.apply_run(summary.run_id)
        self.assertIsNone(report.result.failed)
        self.assertEqual(report.already_applied, [])
        self.assertIsNone(report.snapshot.sha)  # nothing was dirty
        self.assertEqual((self.memory / "topic.md").read_text(), "prefers tabs\n")
        self.assertFalse((self.memory / "b.md").exists())
        self.assertIn("Superseded", git(self.root, "log", "-3", "--format=%B"))
        record = runner.get_run(summary.run_id)
        self.assertEqual(runner.display_status(record), "applied")
        self.assertEqual(set(record["applied"]), {c.sha for c in summary.commits})
        self.assertIn(summary.branch, self.branches())  # apply never removes branch/worktree
        self.assertTrue(summary.worktree.exists())
        again = runner.apply_run(summary.run_id)
        self.assertEqual(again.result.applied, {})
        self.assertIsNone(again.snapshot)  # nothing left to pick, so no snapshot either
        self.assertEqual(again.already_applied, [1, 2, 3])

    def test_apply_only_selected(self):
        summary = self.dream()
        runner.apply_run(summary.run_id, [3])
        self.assertEqual((self.memory / "topic.md").read_text(), "prefers tabs\n")
        self.assertTrue((self.memory / "b.md").exists())
        self.assertEqual((self.memory / "MEMORY.md").read_text(), SEED_FILES["MEMORY.md"])
        self.assertEqual(runner.display_status(runner.get_run(summary.run_id)), "partially-applied")
        runner.apply_run(summary.run_id, [1, 2])
        self.assertEqual(runner.display_status(runner.get_run(summary.run_id)), "applied")

    def test_dirty_memory_is_snapshotted_before_the_cherry_picks(self):
        summary = self.dream()
        (self.memory / "a.md").write_text("local edit\n", newline="\n")
        report = runner.apply_run(summary.run_id)
        self.assertIsNone(report.result.failed)
        self.assertEqual(report.snapshot.committed, (f"{self.slug}/memory/a.md",))
        subjects = self.subjects(5)
        self.assertEqual(subjects[3], f"chore(to-dream): snapshot before apply {summary.run_id}")
        self.assertEqual(subjects[4], "seed memory")
        self.assertEqual(subjects[:3], [s for _, s in reversed(gitops.list_commits(self.root, BASE_BRANCH, summary.branch))])
        self.assertEqual((self.memory / "a.md").read_text(), "local edit\n")  # the user's edit is kept
        self.assertEqual((self.memory / "topic.md").read_text(), "prefers tabs\n")
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_apply_with_a_secret_in_dirty_memory_snapshots_nothing_and_warns(self):
        summary = self.dream()
        (self.memory / "a.md").write_text(f"{PASSWORD_LINE}\n", newline="\n")
        report = runner.apply_run(summary.run_id, [3])
        self.assertIsNone(report.snapshot.sha)
        self.assertEqual([p for p, _ in report.snapshot.refused], [f"{self.slug}/memory/a.md"])
        self.assertIn(":1 generic-secret-assignment", report.snapshot.refused[0][1])
        self.assertNotIn("hunter2", report.snapshot.refused[0][1])
        self.assertIsNone(report.result.failed)
        self.assertIn("hunter2", (self.memory / "a.md").read_text())  # still the user's uncommitted file

    def test_apply_refuses_tracked_changes_outside_memory(self):
        summary = self.dream()
        (self.root / ".gitignore").write_text("# local tweak\n", newline="\n")
        with self.assertRaisesRegex(runner.RunError, r"outside \*/memory/.*\.gitignore"):
            runner.apply_run(summary.run_id)
        self.assertEqual(runner.get_run(summary.run_id).get("applied"), {})
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), self.seed_head)

    def test_apply_conflict_aborts_and_reports(self):
        summary = self.dream()
        (self.memory / "MEMORY.md").write_text("# Memory\nlive change\n", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "live edit")
        result = runner.apply_run(summary.run_id).result
        self.assertEqual(result.failed, summary.commits[0].sha)  # MEMORY.md update conflicts first
        self.assertEqual(result.applied, {})
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertFalse((self.root / ".git" / "CHERRY_PICK_HEAD").exists())
        self.assertEqual(runner.get_run(summary.run_id)["applied"], {})

    def test_unknown_and_discarded_runs_are_refused(self):
        with self.assertRaises(runner.RunError):
            runner.apply_run("nope")
        summary = self.dream()
        runner.discard_run(summary.run_id)
        with self.assertRaisesRegex(runner.RunError, "discarded"):
            runner.apply_run(summary.run_id)

    def test_discard_removes_worktree_and_branch(self):
        summary = self.dream()
        runner.discard_run(summary.run_id)
        self.assertFalse(summary.worktree.exists())
        self.assertEqual(self.branches(), [BASE_BRANCH])
        self.assertEqual(runner.display_status(runner.get_run(summary.run_id)), "discarded")
        self.assertEqual(git(self.root, "rev-parse", BASE_BRANCH).strip(), self.seed_head)
        runner.discard_run(summary.run_id)  # idempotent

    def test_discard_protects_uncommitted_work_unless_forced(self):
        summary = self.dream()
        (summary.worktree / self.slug / "memory" / "a.md").write_text("precious\n", newline="\n")
        with self.assertRaisesRegex(runner.RunError, "--force"):
            runner.discard_run(summary.run_id)
        self.assertTrue(summary.worktree.exists())
        runner.discard_run(summary.run_id, force=True)
        self.assertFalse(summary.worktree.exists())

    def test_discard_with_only_tool_files_dirty_needs_no_force(self):
        summary = self.dream()
        stray = summary.worktree / self.slug / "memory" / ".claude"
        stray.mkdir()
        (stray / "settings.local.json").write_text("{}", newline="\n")
        runner.discard_run(summary.run_id)
        self.assertFalse(summary.worktree.exists())


class MarkAppliedTests(DreamTestCase):
    def dream(self):
        self.plan(**HAPPY)
        return runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)

    def conflicted(self):
        """A run whose first change conflicts with a live edit, as in the real "apply by hand" scenario."""
        summary = self.dream()
        (self.memory / "MEMORY.md").write_text("# Memory\nlive change\n", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "live edit")
        self.assertEqual(runner.apply_run(summary.run_id).result.failed, summary.commits[0].sha)
        return summary

    def test_mark_applied_records_the_change_and_note(self):
        summary = self.conflicted()
        marked = runner.mark_applied(summary.run_id, [1], "merged the index line by hand", when=NOW)
        self.assertEqual([(c.n, c.file) for c in marked], [(1, "MEMORY.md")])
        record = runner.get_run(summary.run_id)
        self.assertEqual(record["marked_applied"], {
            summary.commits[0].sha: {"note": "merged the index line by hand", "at": "2026-11-02T18:30:12+00:00"}})
        change = runner.run_changes(record)[0]
        self.assertTrue(change.applied and change.marked)
        self.assertEqual(runner.display_status(record), "partially-applied")

    def test_discard_after_mark_applied_does_not_log_it_rejected(self):
        summary = self.conflicted()
        runner.mark_applied(summary.run_id, [1])
        runner.apply_run(summary.run_id, [2, 3])
        leftover = runner.discard_run(summary.run_id)
        self.assertEqual(leftover, [])
        log = (self.state_home / "rejected.md").read_text(encoding="utf-8")
        self.assertNotIn("MEMORY.md", log)
        self.assertEqual(log.count("\n## "), 0)

    def test_discard_logs_only_the_unmarked_leftovers(self):
        summary = self.conflicted()
        runner.mark_applied(summary.run_id, [1])
        leftover = runner.discard_run(summary.run_id)
        self.assertEqual([c.file for c in leftover], ["b.md", "topic.md"])
        log = (self.state_home / "rejected.md").read_text(encoding="utf-8")
        self.assertEqual(log.count("\n## "), 2)
        self.assertNotIn("| MEMORY.md |", log)

    def test_apply_skips_marked_changes(self):
        summary = self.conflicted()
        runner.mark_applied(summary.run_id, [1])
        report = runner.apply_run(summary.run_id)
        self.assertIsNone(report.result.failed)
        self.assertEqual(report.already_applied, [1])
        self.assertEqual(len(report.result.applied), 2)
        self.assertEqual(runner.display_status(runner.get_run(summary.run_id)), "applied")

    def test_mark_applied_is_idempotent_and_reject_refuses_marked_changes(self):
        summary = self.conflicted()
        self.assertEqual(len(runner.mark_applied(summary.run_id, [1])), 1)
        self.assertEqual(runner.mark_applied(summary.run_id, [1]), [])
        with self.assertRaisesRegex(runner.RunError, "already applied"):
            runner.reject_run(summary.run_id, [1])

    def test_mark_applied_errors(self):
        summary = self.dream()
        with self.assertRaises(runner.RunError):
            runner.mark_applied(summary.run_id, [9])
        runner.reject_run(summary.run_id, [2], "no")
        with self.assertRaisesRegex(runner.RunError, "rejected"):
            runner.mark_applied(summary.run_id, [2])
        with self.assertRaises(runner.RunError):
            runner.mark_applied("nope", [1])
        runner.discard_run(summary.run_id)
        with self.assertRaisesRegex(runner.RunError, "discarded"):
            runner.mark_applied(summary.run_id, [1])


class RejectSuggestionTests(DreamTestCase):
    def dream(self):
        self.plan(**HAPPY)
        return runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)

    def log_text(self):
        return (self.state_home / "rejected.md").read_text(encoding="utf-8")

    def test_entry_is_appended_under_a_global_suggestion_heading(self):
        summary = self.dream()
        self.assertTrue(runner.reject_suggestion(
            summary.run_id, "Always  use tabs\nin every project", "I prefer spaces", when=NOW))
        text = self.log_text()
        self.assertIn(f"\n## 2026-11-02 | {self.slug} | global suggestion\n", text)
        self.assertIn(f"- Run: {summary.run_id}", text)
        self.assertIn("- Suggestion: Always use tabs in every project", text)
        self.assertIn("- Reason: I prefer spaces", text)
        self.assertEqual(text.count("\n## "), 1)
        suggestions = runner.get_run(summary.run_id)["rejected_suggestions"]
        self.assertEqual([s["text"] for s in suggestions], ["Always use tabs in every project"])

    def test_default_reason_and_dedupe(self):
        summary = self.dream()
        self.assertTrue(runner.reject_suggestion(summary.run_id, "Do X"))
        self.assertFalse(runner.reject_suggestion(summary.run_id, "  Do   X "))
        self.assertIn("- Reason: no reason given", self.log_text())
        self.assertEqual(self.log_text().count("| global suggestion"), 1)

    def test_blank_text_and_unknown_run_are_errors(self):
        summary = self.dream()
        with self.assertRaisesRegex(runner.RunError, "empty"):
            runner.reject_suggestion(summary.run_id, "   ")
        with self.assertRaises(runner.RunError):
            runner.reject_suggestion("nope", "x")

    def test_next_dream_is_pointed_at_the_log(self):
        summary = self.dream()
        runner.reject_suggestion(summary.run_id, "Always use tabs", "no")
        self.plan(report="# none\n")
        runner.run_project(self.slug, runner.RunOptions(), now=NOW + timedelta(days=1), echo=False)
        self.assertIn((self.state_home / "rejected.md").as_posix(), self.claude_call()["stdin"])
        self.assertIn("Always use tabs", self.log_text())


if __name__ == "__main__":
    unittest.main()
