"""Entry-wise MEMORY.md handling: pure diff/edit logic, ``apply`` end to end and a seeded stochastic check."""

import contextlib
import io
import random
import unittest
from collections import Counter

from helpers import git, new_seed
from test_runner import HAPPY, NOW
from to_dream_helpers import DreamTestCase

from to_dream import cli, memory_index, runner, state as dstate

INDEX_REPORT = "# Dream report\n\n### MEMORY.md\nWhy.\n**Change:** tweak the index\n"


def hunk(removed=(), added=()) -> str:
    """Build a zero-context diff hunk from literal line texts (hand-written, independent of the code under test)."""
    return "@@ -1 +1 @@\n" + "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)


class EntryParsingTests(unittest.TestCase):
    def test_entry_key_reads_the_link_target(self):
        self.assertEqual(memory_index.entry_key("- [Tabs](tabs.md) — prefers tabs"), "tabs.md")
        self.assertEqual(memory_index.entry_key("* [X](sub/x.md)"), "sub/x.md")
        self.assertIsNone(memory_index.entry_key("# Memory"))
        self.assertIsNone(memory_index.entry_key("plain [text](x.md) mid-line"))

    def test_diff_lines_ignores_headers_and_context(self):
        diff = ("diff --git a/p/MEMORY.md b/p/MEMORY.md\nindex 1..2 100644\n--- a/p/MEMORY.md\n+++ b/p/MEMORY.md\n"
                "@@ -2 +2 @@\n-- [B](b.md)\n+- [C](c.md)\n context\n\\ No newline at end of file\n")
        self.assertEqual(memory_index.diff_lines(diff), (["- [B](b.md)"], ["- [C](c.md)"]))

    def test_diff_lines_strips_carriage_returns(self):
        self.assertEqual(memory_index.diff_lines("@@ -1 +1 @@\n-- [B](b.md)\r\n+- [C](c.md)\r\n"),
                         (["- [B](b.md)"], ["- [C](c.md)"]))

    def test_entry_edits_pairs_removed_and_added_by_key(self):
        edits, problems = memory_index.entry_edits(
            hunk(["- [A](a.md) old", "- [B](b.md)"], ["- [A](a.md) new", "- [N](n.md)"]))
        self.assertEqual(problems, [])
        self.assertEqual(sorted(edits, key=lambda e: e.key), [
            memory_index.EntryEdit("a.md", "- [A](a.md) old", "- [A](a.md) new"),
            memory_index.EntryEdit("b.md", "- [B](b.md)", None),
            memory_index.EntryEdit("n.md", None, "- [N](n.md)"),
        ])

    def test_entry_edits_reports_lines_it_cannot_key(self):
        _, problems = memory_index.entry_edits(hunk(["# Old heading"], ["# New heading"]))
        self.assertEqual(len(problems), 2)
        self.assertIn("not an index entry", problems[0])
        _, problems = memory_index.entry_edits(hunk([], ["- [A](a.md) one", "- [A](a.md) two"]))
        self.assertEqual(problems, ["a.md: several changed lines for one entry"])

    def test_blank_lines_are_ignored(self):
        edits, problems = memory_index.entry_edits(hunk([""], ["", "- [N](n.md)"]))
        self.assertEqual((len(edits), problems), (1, []))


class ReapplyTests(unittest.TestCase):
    LIVE = "# Memory\n- [A](a.md) old\n- [B](b.md)\n- [Z](z.md)\n"

    def test_replace_keeps_the_position(self):
        out = memory_index.reapply(self.LIVE, hunk(["- [A](a.md) old"], ["- [A](a.md) new"]))
        self.assertEqual(out.text, "# Memory\n- [A](a.md) new\n- [B](b.md)\n- [Z](z.md)\n")

    def test_add_appends_after_the_last_index_line(self):
        out = memory_index.reapply(self.LIVE + "\ntrailing note\n", hunk([], ["- [N](n.md)"]))
        self.assertEqual(out.text, "# Memory\n- [A](a.md) old\n- [B](b.md)\n- [Z](z.md)\n- [N](n.md)\n\ntrailing note\n")

    def test_add_to_a_file_without_entries_goes_before_the_final_newline(self):
        self.assertEqual(memory_index.reapply("# Memory\n", hunk([], ["- [N](n.md)"])).text, "# Memory\n- [N](n.md)\n")
        self.assertEqual(memory_index.reapply("", hunk([], ["- [N](n.md)"])).text, "- [N](n.md)\n")

    def test_remove_deletes_the_live_line(self):
        out = memory_index.reapply(self.LIVE, hunk(["- [B](b.md)"], []))
        self.assertEqual(out.text, "# Memory\n- [A](a.md) old\n- [Z](z.md)\n")

    def test_crlf_and_missing_final_newline_are_kept(self):
        live = "# Memory\r\n- [A](a.md)\r\n- [B](b.md)"
        out = memory_index.reapply(live, hunk(["- [A](a.md)"], ["- [A](a.md) hook", "- [N](n.md)"]))
        self.assertEqual(out.text, "# Memory\r\n- [A](a.md) hook\r\n- [B](b.md)\r\n- [N](n.md)")

    def test_removed_key_missing_live_is_a_conflict(self):
        out = memory_index.reapply("# Memory\n", hunk(["- [B](b.md)"], []))
        self.assertEqual((out.text, out.conflicts), (None, ("b.md: not in live MEMORY.md",)))

    def test_edited_live_line_is_a_conflict_and_nothing_is_applied(self):
        out = memory_index.reapply(self.LIVE, hunk(["- [A](a.md) other"], ["- [A](a.md) new"]))
        self.assertEqual((out.text, out.conflicts), (None, ("a.md: the live line was edited since the run",)))

    def test_one_conflict_blocks_every_entry(self):
        diff = hunk(["- [B](b.md)", "- [A](a.md) edited"], ["- [N](n.md)"])
        out = memory_index.reapply(self.LIVE, diff)
        self.assertIsNone(out.text)
        self.assertEqual(out.conflicts, ("a.md: the live line was edited since the run",))

    def test_entries_already_in_the_wanted_state_are_not_conflicts(self):
        out = memory_index.reapply(self.LIVE, hunk(["- [A](a.md) older"], ["- [A](a.md) old"]))
        self.assertEqual(out.text, self.LIVE)
        out = memory_index.reapply(self.LIVE, hunk([], ["- [Z](z.md)"]))
        self.assertEqual(out.text, self.LIVE)

    def test_add_over_a_different_live_entry_is_a_conflict(self):
        out = memory_index.reapply(self.LIVE, hunk([], ["- [Z](z.md) different"]))
        self.assertEqual(out.conflicts, ("z.md: already in live MEMORY.md with different text",))


class EntrywiseApplyTests(DreamTestCase):
    def commit_live(self, text: str, message: str = "live index edit") -> None:
        (self.memory / "MEMORY.md").write_text(text, encoding="utf-8", newline="")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def dream_index(self, new_index: str):
        self.plan(write={"MEMORY.md": new_index}, report=INDEX_REPORT)
        return runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)

    def live_text(self) -> str:
        return (self.memory / "MEMORY.md").read_bytes().decode("utf-8")

    def test_replace_and_add_are_reapplied_when_cherry_pick_conflicts(self):
        summary = self.dream_index("# Memory\n- [A](a.md) — new hook\n- [B](b.md)\n- [N](n.md)\n")
        self.commit_live("# Memory\n- [A](a.md)\n- [B](b.md)\n- [Z](z.md)\n")
        report = runner.apply_run(summary.run_id)
        self.assertIsNone(report.result.failed)
        self.assertEqual((report.entrywise, report.entry_conflicts), ([1], []))
        self.assertEqual(self.live_text(), "# Memory\n- [A](a.md) — new hook\n- [B](b.md)\n- [Z](z.md)\n- [N](n.md)\n")
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        record = runner.get_run(summary.run_id)
        self.assertEqual(runner.display_status(record), "applied")
        self.assertEqual(record["applied"][summary.commits[0].sha], git(self.root, "rev-parse", "HEAD").strip())
        self.assertEqual(record["applied_entrywise"], {summary.commits[0].sha: memory_index.ENTRYWISE_NOTE})
        message = git(self.root, "log", "-1", "--format=%B")
        self.assertTrue(message.startswith(f"chore(to-dream): update {self.slug_short}/MEMORY.md"), message)
        self.assertIn(f"(applied entry-wise from commit {summary.commits[0].sha})", message)

    def test_remove_is_reapplied(self):
        summary = self.dream_index("# Memory\n- [A](a.md)\n")  # drops B
        self.commit_live("# Memory\n- [A](a.md)\n- [B](b.md)\n- [Z](z.md)\n")
        report = runner.apply_run(summary.run_id)
        self.assertEqual((report.result.failed, report.entrywise), (None, [1]))
        self.assertEqual(self.live_text(), "# Memory\n- [A](a.md)\n- [Z](z.md)\n")

    def test_crlf_live_file_keeps_its_line_endings(self):
        summary = self.dream_index("# Memory\n- [A](a.md)\n- [B](b.md)\n- [N](n.md)\n")
        self.commit_live("# Memory\r\n- [A](a.md)\r\n- [B](b.md)\r\n- [Z](z.md)\r\n")
        self.assertEqual(runner.apply_run(summary.run_id).entrywise, [1])
        self.assertEqual(self.live_text(), "# Memory\r\n- [A](a.md)\r\n- [B](b.md)\r\n- [Z](z.md)\r\n- [N](n.md)\r\n")

    def test_edited_entry_is_reported_and_the_change_stays_pending(self):
        summary = self.dream_index("# Memory\n- [A](a.md)\n")  # drops B
        edited = "# Memory\n- [A](a.md)\n- [B](b.md) — edited later\n- [Z](z.md)\n"
        self.commit_live(edited)
        head = git(self.root, "rev-parse", "HEAD").strip()
        report = runner.apply_run(summary.run_id)
        self.assertEqual(report.result.failed, summary.commits[0].sha)
        self.assertEqual((report.entrywise, report.entry_conflicts), ([], ["b.md: the live line was edited since the run"]))
        self.assertEqual(self.live_text(), edited)
        self.assertEqual(git(self.root, "rev-parse", "HEAD").strip(), head)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        record = runner.get_run(summary.run_id)
        self.assertEqual(record["applied"], {})
        self.assertNotIn("applied_entrywise", record)
        self.assertEqual(runner.display_status(record), "pending")

    def test_the_rest_of_the_run_continues_after_an_entrywise_apply(self):
        self.plan(**HAPPY)  # MEMORY.md swaps B for Topic, b.md is deleted, topic.md is added
        summary = runner.run_project(self.slug, runner.RunOptions(), now=NOW, echo=False)
        self.commit_live("# Memory\n- [A](a.md)\n- [B](b.md)\n- [Z](z.md)\n")
        report = runner.apply_run(summary.run_id)
        self.assertEqual((report.result.failed, report.entrywise, len(report.result.applied)), (None, [1], 3))
        self.assertEqual(self.live_text(), "# Memory\n- [A](a.md)\n- [Z](z.md)\n- [Topic](topic.md)\n")
        self.assertFalse((self.memory / "b.md").exists())
        self.assertEqual((self.memory / "topic.md").read_text(), "prefers tabs\n")

    def test_uncommitted_index_edits_are_never_committed_entrywise(self):
        summary = self.dream_index("# Memory\n- [A](a.md)\n- [B](b.md)\n- [N](n.md)\n")
        self.commit_live("# Memory\n- [A](a.md)\n- [B](b.md)\n- [Z](z.md)\n")
        (self.memory / "MEMORY.md").write_text("# Memory\n- [Q](q.md)\n", newline="\n")
        outcome = memory_index.apply_entrywise(self.root, summary.commits[0].sha)
        self.assertIsNone(outcome.sha)
        self.assertIn("uncommitted", outcome.message)
        self.assertEqual(self.live_text(), "# Memory\n- [Q](q.md)\n")

    def test_cli_reports_entrywise_and_conflicts(self):
        summary = self.dream_index("# Memory\n- [A](a.md)\n")
        self.commit_live("# Memory\n- [A](a.md)\n- [B](b.md) — edited\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["apply", summary.run_id])
        self.assertEqual(code, 1)
        self.assertIn("MEMORY.md entry conflict: b.md: the live line was edited since the run", err.getvalue())
        self.commit_live("# Memory\n- [A](a.md)\n- [B](b.md)\n- [Z](z.md)\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["apply", summary.run_id])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("commit 1 (MEMORY.md) conflicted; re-applied entry by entry", out.getvalue())
        self.assertEqual(dstate.load_state()["runs"][summary.run_id]["applied_entrywise"].keys(),
                         {summary.commits[0].sha})


class StochasticEntrywiseTests(unittest.TestCase):
    """Random index edits re-applied to a randomly drifted live index must give the expected set of lines."""

    ROUNDS = 300

    @staticmethod
    def line(key: int, rev: int) -> str:
        return f"- [T{key}](k{key}.md) — v{rev}"

    def test_random_edits_match_the_expected_lines(self):
        seed = new_seed()
        print(f"seed={seed}")
        rng = random.Random(seed)
        for round_number in range(self.ROUNDS):
            base_keys = rng.sample(range(40), rng.randint(0, 12))
            base = {k: self.line(k, 0) for k in base_keys}
            fresh = [k for k in range(40, 60) if rng.random() < 0.3]
            touched = [k for k in base_keys if rng.random() < 0.4]
            removed_keys = [k for k in touched if rng.random() < 0.4]
            replaced_keys = [k for k in touched if k not in removed_keys]
            removed = [base[k] for k in touched]
            added = [self.line(k, 1) for k in replaced_keys] + [self.line(k, 0) for k in fresh]
            drift_add = [k for k in range(60, 70) if rng.random() < 0.3]
            drift_drop = [k for k in base_keys if rng.random() < 0.3]
            conflict_key = rng.choice(touched) if touched and rng.random() < 0.25 else None
            live_lines = ["# Memory"] + [
                f"{base[k]} edited" if k == conflict_key else base[k] for k in base_keys if k not in drift_drop
            ] + [self.line(k, 0) for k in drift_add]
            live = "\n".join(live_lines) + "\n"

            outcome = memory_index.reapply(live, hunk(removed, added))

            context = f"seed={seed} round={round_number}"
            missing = [k for k in touched if k in drift_drop]  # live lost a line the change removes/replaces
            if conflict_key is not None and conflict_key not in drift_drop:
                self.assertIsNone(outcome.text, context)
                self.assertGreaterEqual(len(outcome.conflicts), 1, context)
                continue
            if missing:
                self.assertIsNone(outcome.text, context)
                self.assertEqual(len(outcome.conflicts), len(missing), context)
                continue
            expected = (Counter(live_lines) - Counter(removed)) + Counter(added)
            self.assertIsNotNone(outcome.text, context)
            self.assertEqual(Counter(outcome.text.splitlines()), expected, context)
            self.assertTrue(outcome.text.endswith("\n"), context)
            self.assertEqual(outcome.text.splitlines()[0], "# Memory", context)


if __name__ == "__main__":
    unittest.main()
