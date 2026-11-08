import json
import random
import tempfile
import unittest
from pathlib import Path

from helpers import new_seed
from test_runner import HAPPY, NOW, SECRET
from to_dream_helpers import DreamTestCase

from to_dream import permissions, runner, state as dstate

# Never read by to-dream; assembled at runtime so no key-shaped literal sits in the file.
ENV_TOKEN = "ghp_" + "Zz9yX8wV7uT6sR5qP4oN3mL2kJ1iH0gF9eD8"

EXACT_BLOCK = (
    "- **Rule:** `Bash(npm test *)` · **Kind:** allow · **Scope:** project-local\n"
    "  - **Evidence:** 20261107-1436_7aca5a6b.md L1576, L1608 · blocked 4×, approved after block 3× in 2 sessions\n"
    "  - **Risk:** runs the project's own test script\n"
    "  - **Why:** prompted in every session\n"
)
EXACT_ENTRY = {
    "rule": "Bash(npm test *)", "kind": "allow", "scope": "project-local",
    "evidence": "20261107-1436_7aca5a6b.md L1576, L1608 · blocked 4×, approved after block 3× in 2 sessions",
    "risk": "runs the project's own test script", "why": "prompted in every session",
}


class ParserTests(unittest.TestCase):
    def test_exact_format_round_trips(self):
        second = (
            "- **Rule:** `Bash(git push *)` · **Kind:** ask · **Scope:** user\n"
            "  - **Evidence:** a.md L3 · blocked 2×\n  - **Risk:** publishes commits\n  - **Why:** always confirmed\n"
        )
        parsed = permissions.parse_permission_suggestions(EXACT_BLOCK + "\n" + second)
        self.assertEqual(parsed, [EXACT_ENTRY, {
            "rule": "Bash(git push *)", "kind": "ask", "scope": "user", "evidence": "a.md L3 · blocked 2×",
            "risk": "publishes commits", "why": "always confirmed",
        }])

    def test_nothing_to_parse(self):
        for text in ("", "None.", "No suggestions.\n", "### file.md\n- **Action:** add\n"):
            with self.subTest(text=text):
                self.assertEqual(permissions.parse_permission_suggestions(text), [])

    def test_tolerates_formatting_drift(self):
        cases = {
            "colon outside the bold": "- **Rule**: `Bash(ls)` · **Kind**: allow · **Scope**: project\n",
            "no backticks, pipes": "- **Rule:** Bash(ls) | **Kind:** Allow | **Scope:** Project\n",
            "star bullet, lowercase labels": "* **rule:** `Bash(ls)` · **kind:** ALLOW · **scope:** project\n",
            "numbered, one field per line": "1. **Rule:** `Bash(ls)`\n   **Kind:** allow\n   **Scope:** project shared\n",
            "no bullet": "**Rule:** `Bash(ls)` · **Kind:** allow · **Scope:** Project (shared)\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                (entry,) = permissions.parse_permission_suggestions(text)
                self.assertEqual((entry["rule"], entry["kind"], entry["scope"]), ("Bash(ls)", "allow", "project"))

    def test_scope_and_kind_wording(self):
        for text, scope in (("Project Local", "project-local"), ("project-local", "project-local"),
                            ("local", "project-local"), ("project", "project"), ("User", "user"),
                            ("user (~/.claude/settings.json)", "user"), ("", ""), ("elsewhere", "elsewhere")):
            with self.subTest(text=text):
                self.assertEqual(permissions.normalise_scope(text), scope)
        self.assertEqual([permissions.normalise_kind(t) for t in ("Allow", "ask (prompt)", "deny")],
                         ["allow", "ask", "deny"])

    def test_missing_fields_are_empty_strings_and_ruleless_blocks_are_skipped(self):
        text = "- **Kind:** allow\n- **Rule:** `Read(./docs/**)` · **Kind:** allow\n"
        self.assertEqual(permissions.parse_permission_suggestions(text), [{
            "rule": "Read(./docs/**)", "kind": "allow", "scope": "", "evidence": "", "risk": "", "why": "",
        }])

    def test_a_rule_may_contain_separators_and_a_value_may_start_with_code(self):
        text = ("- **Rule:** `Bash(echo a · b | c)` · **Kind:** ask · **Scope:** user\n"
                "  - **Why:** `npm test` is run 9 times a day\n")
        (entry,) = permissions.parse_permission_suggestions(text)
        self.assertEqual(entry["rule"], "Bash(echo a · b | c)")
        self.assertEqual(entry["why"], "`npm test` is run 9 times a day")

    def test_random_entries_round_trip(self):
        seed = new_seed()
        print(f"[test_random_entries_round_trip] seed={seed}")
        rng = random.Random(seed)
        words = ["blocked", "approved", "npm", "test", "L12", "session", "x.md", "(sic)", "3×", "a/b", "·", "—"]
        rule_chars = "abcXYZ019 *():./-_·|"

        def phrase() -> str:
            text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 8)))
            return text if text[-1] not in " ·-—" else text + "end"

        for trial in range(60):
            entries = [{
                "rule": "Bash(" + "".join(rng.choice(rule_chars) for _ in range(rng.randint(1, 20))).strip() + "x)",
                "kind": rng.choice(["allow", "ask"]),
                "scope": rng.choice(["project-local", "project", "user"]),
                "evidence": phrase(), "risk": phrase(), "why": phrase(),
            } for _ in range(rng.randint(0, 5))]
            bullet = rng.choice(["-", "*"])
            sep = rng.choice([" · ", " | ", "  ·  "])
            text = rng.choice(["", "Preamble line.\n\n"])
            for e in entries:
                text += (f"{bullet} **Rule:** `{e['rule']}`{sep}**Kind:** {e['kind']}{sep}**Scope:** {e['scope']}\n"
                         f"  {bullet} **Evidence:** {e['evidence']}\n  {bullet} **Risk:** {e['risk']}\n"
                         f"  {bullet} **Why:** {e['why']}\n" + rng.choice(["", "\n"]))
            with self.subTest(seed=seed, trial=trial):
                self.assertEqual(permissions.parse_permission_suggestions(text), entries)


class SettingsReadingTests(unittest.TestCase):
    def test_read_permissions_returns_only_the_whitelisted_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "env": {"K": ENV_TOKEN}, "hooks": {"a": 1},
                "permissions": {"allow": ["A"], "ask": ["B"], "deny": ["C"], "defaultMode": "plan",
                                "additionalDirectories": ["/d"], "other": "no"},
            }), encoding="utf-8")
            status, perms = permissions.read_permissions(path)
        self.assertEqual(status, "ok")
        self.assertEqual(perms, {"allow": ["A"], "ask": ["B"], "deny": ["C"], "defaultMode": "plan",
                                 "additionalDirectories": ["/d"]})

    def test_statuses_for_bad_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cases = {"missing.json": None, "bad.json": "{nope", "list.json": "[1]", "noperms.json": '{"env": {}}'}
            for name, text in cases.items():
                if text is not None:
                    (tmp / name).write_text(text, encoding="utf-8")
            got = {name: permissions.read_permissions(tmp / name) for name in cases}
        self.assertEqual(got, {
            "missing.json": ("missing", {}), "bad.json": ("invalid JSON", {}), "list.json": ("not an object", {}),
            "noperms.json": ("no permissions object", {}),
        })


class PermissionsFileTests(DreamTestCase):
    def setUp(self):
        super().setUp()
        self.project_dir.mkdir(parents=True)
        self.user_file = self.root.parent / "settings.json"
        self.shared = self.project_dir / ".claude" / "settings.json"
        self.local = self.project_dir / ".claude" / "settings.local.json"

    def write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def dream(self):
        self.plan(**HAPPY)
        return runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)

    def md(self, summary) -> str:
        return (summary.run_dir / "permissions.md").read_text(encoding="utf-8")

    def test_only_permission_rules_are_copied_and_secrets_are_redacted(self):
        self.write(self.user_file, {
            "env": {"ANTHROPIC_API_KEY": ENV_TOKEN}, "apiKeyHelper": "key-helper-sentinel",
            "permissions": {"allow": ["Bash(git status)", f"Bash(curl -H 'Authorization: token {SECRET}')"],
                            "deny": ["Read(./.env)"], "defaultMode": "acceptEdits", "unknownKey": "unknown-sentinel"},
        })
        self.write(self.shared, {"hooks": {"Stop": "hook-sentinel"}, "env": {"T": ENV_TOKEN},
                                 "permissions": {"ask": ["Bash(git push *)"], "additionalDirectories": ["../shared"]}})
        text = self.md(self.dream())
        for expected in ("`Bash(git status)`", "`Read(./.env)`", "`Bash(git push *)`", "`../shared`",
                         "defaultMode: `acceptEdits`", "[REDACTED:"):
            self.assertIn(expected, text)
        for forbidden in (ENV_TOKEN, SECRET, "key-helper-sentinel", "unknown-sentinel", "hook-sentinel",
                          "ANTHROPIC_API_KEY", "apiKeyHelper", "hooks"):
            self.assertNotIn(forbidden, text)

    def test_layout_names_every_scope_with_its_absolute_file_and_marks_missing_files(self):
        self.write(self.local, '{"permissions": {"allow": ["Bash(ls)"]}}')
        self.write(self.shared, "{not json")
        text = self.md(self.dream())
        for expected in (
            f"- project-local: `{self.local.as_posix()}`", f"- project: `{self.shared.as_posix()}`",
            f"- user: `{self.user_file.as_posix()}`",
            "## User settings (scope: user)", "## Project shared settings (scope: project)",
            "## Project local settings (scope: project-local)",
            f"File: `{self.user_file.as_posix()}`\n\n(file missing)", f"File: `{self.shared.as_posix()}`\n\n(invalid JSON)",
            f"File: `{self.local.as_posix()}`\n\n- allow:\n  - `Bash(ls)`",
        ):
            self.assertIn(expected, text)

    def test_targets_are_recorded_in_the_run_and_the_claude_gets_only_the_permissions_file(self):
        summary = self.dream()
        targets = runner.get_run(summary.run_id)["permission_targets"]
        self.assertEqual(targets, {"project-local": self.local.as_posix(), "project": self.shared.as_posix(),
                                   "user": self.user_file.as_posix()})
        call = self.claude_call()
        allowed = call["argv"][call["argv"].index("--allowedTools") + 1: call["argv"].index("--disallowedTools")]
        self.assertIn(runner.read_rule(summary.run_dir / "permissions.md"), allowed)
        self.assertEqual([a for a in allowed if "settings" in a], [])
        self.assertIn((summary.run_dir / "permissions.md").as_posix(), call["stdin"])

    def test_settings_files_are_denied_to_the_dream_so_the_recursive_project_read_cannot_reach_them(self):
        self.dream()
        argv = self.claude_call()["argv"]
        denied = argv[argv.index("--disallowedTools") + 1: argv.index("--add-dir")]

        def expected(path: Path) -> str:  # written out by hand: drive letters become //<lowercase drive>/...
            text = path.as_posix()
            return f"Read(//{text[0].lower()}{text[2:]})" if text[1:2] == ":" else f"Read(/{text})"

        for settings in (self.shared, self.local, self.user_file):
            with self.subTest(settings=settings.name):
                self.assertIn(expected(settings), denied)
        self.assertEqual(denied[:4], ["Bash", "PowerShell", "WebFetch", "WebSearch"])

    def test_git_root_settings_are_denied_too(self):
        repo = self.tmp / "repo-deny"
        sub = repo / "app"
        (repo / ".git").mkdir(parents=True)
        (sub / ".claude").mkdir(parents=True)  # own .claude dir: the git root is not an edit target, but is still denied
        rules = runner.settings_deny_rules(sub)
        for path in (repo / ".claude" / "settings.json", repo / ".claude" / "settings.local.json",
                     sub / ".claude" / "settings.json", sub / ".claude" / "settings.local.json",
                     dstate.claude_home() / "settings.json"):
            with self.subTest(path=path):
                self.assertIn(runner.read_rule(path), rules)
        self.assertEqual(runner.settings_deny_rules(None), [runner.read_rule(dstate.claude_home() / "settings.json")])

    def test_nearest_git_root_is_used_when_the_project_dir_has_no_claude_dir(self):
        for dot_git in ("dir", "file"):
            with self.subTest(dot_git=dot_git):
                repo = self.tmp / f"repo-{dot_git}"
                sub = repo / "pkg" / "app"
                sub.mkdir(parents=True)
                if dot_git == "dir":
                    (repo / ".git").mkdir()
                else:
                    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
                self.write(repo / ".claude" / "settings.json", {"permissions": {"allow": [f"Bash({dot_git})"]}})
                self.distill.project_dir = str(sub)
                text = self.md(self.dream_again())
                self.assertIn(f"`Bash({dot_git})`", text)
                self.assertIn(f"- project: `{(repo / '.claude' / 'settings.json').as_posix()}`", text)
                self.assertIn(f"- project-local: `{(repo / '.claude' / 'settings.local.json').as_posix()}`", text)

    def dream_again(self):
        self.plan(report="# none\n")
        return runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)  # same minute: id gets -2

    def test_project_dir_with_its_own_claude_dir_ignores_the_git_root(self):
        repo = self.tmp / "repo"
        sub = repo / "app"
        (repo / ".git").mkdir(parents=True)
        self.write(repo / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(from-root)"]}})
        self.write(sub / ".claude" / "settings.json", {"permissions": {"allow": ["Bash(from-sub)"]}})
        self.assertEqual([p for _, p in permissions.settings_files(sub)][1:],
                         [sub / ".claude" / "settings.json", sub / ".claude" / "settings.local.json"])
        self.assertEqual(permissions.permission_targets(sub)["project"], (sub / ".claude" / "settings.json").as_posix())

    def test_files_are_listed_once_when_scopes_resolve_to_the_same_path(self):
        files = permissions.settings_files(self.home)  # project root == home, so project settings == user settings
        self.assertEqual([scope for scope, _ in files], ["user", "project-local"])

    def test_unknown_project_dir_has_no_project_targets(self):
        self.distill.project_dir = None
        summary = self.dream()
        self.assertEqual(runner.get_run(summary.run_id)["permission_targets"],
                         {"project-local": None, "project": None, "user": self.user_file.as_posix()})
        text = self.md(summary)
        self.assertIn("- project: (unknown: project directory not known)", text)
        self.assertNotIn("Project shared settings", text)

    def test_show_json_exposes_the_recorded_targets_not_a_recomputation(self):
        summary = self.dream()
        recorded = {"project-local": "/x/local.json", "project": "/x/shared.json", "user": "/x/user.json"}
        dstate.update_state(lambda s: dstate.set_run(s, summary.run_id, permission_targets=recorded))
        data = runner.run_detail_json(summary.run_id, runner.get_run(summary.run_id))
        self.assertEqual(data["permission_targets"], recorded)


if __name__ == "__main__":
    unittest.main()
