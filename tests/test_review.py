"""``to-dream plan``: the compact review summary (live drift, apply conflicts, duplicates across runs)."""

import contextlib
import io
import json
import unittest
from datetime import timedelta

from helpers import git
from test_runner import HAPPY, NOW
from to_dream_helpers import DreamTestCase

from to_dream import cli, config, review, runner

DAY = timedelta(days=1)


class PlanCase(DreamTestCase):
    """Two or more unreviewed runs may coexist (roll-up off); ``HAPPY`` makes changes 1 MEMORY.md, 2 b.md, 3 topic.md."""

    def setUp(self):
        super().setUp()
        config.set_value("rollup", "false")

    def dream(self, days=0, **plan):
        self.plan(**(plan or HAPPY))
        return runner.run_project(self.slug, runner.RunOptions(), now=NOW + days * DAY, echo=False)

    def commit_live(self, files: dict[str, str], message: str = "live edit") -> None:
        for name, text in files.items():
            (self.memory / name).write_text(text, encoding="utf-8", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def by_n(self, plan, run_id=None):
        return {c.n: c for c in plan.changes if run_id in (None, c.run_id)}


class PlanFieldsTests(PlanCase):
    def test_untouched_live_memory(self):
        summary = self.dream()
        plan = review.build_plan([summary.run_id])
        self.assertEqual(plan.skipped, 0)
        self.assertEqual([(c.n, c.file, c.action) for c in plan.changes],
                         [(1, "MEMORY.md", "update"), (2, "b.md", "delete"), (3, "topic.md", "add")])
        changes = self.by_n(plan)
        # HAPPY: the index swaps one line, b.md's single line is deleted, topic.md is one new line.
        self.assertEqual([(changes[n].added, changes[n].removed) for n in (1, 2, 3)], [(1, 1), (0, 1), (1, 0)])
        self.assertEqual(changes[1].change, "link the new topic from the index")
        self.assertEqual(changes[2].change, "remove the stale beta fact")
        self.assertEqual(changes[3].change, 'add a "prefers tabs" preference')
        for change in plan.changes:
            self.assertEqual((change.run_id, change.live_changed, change.applies_cleanly, change.entrywise_ok,
                              change.duplicate_of), (summary.run_id, False, True, None, None))

    def test_live_drift_and_conflicts(self):
        summary = self.dream()
        # b.md is edited live (the run deletes it), topic.md appears live with other text (the run adds it),
        # MEMORY.md gains an entry right below the line the run rewrites.
        self.commit_live({"b.md": "beta fact, edited\n", "topic.md": "something else\n",
                          "MEMORY.md": "# Memory\n- [A](a.md)\n- [B](b.md)\n- [Z](z.md)\n"})
        changes = self.by_n(review.build_plan([summary.run_id]))
        for n in (1, 2, 3):
            self.assertTrue(changes[n].live_changed, n)
            self.assertFalse(changes[n].applies_cleanly, n)
        self.assertTrue(changes[1].entrywise_ok)  # the index can still be re-applied entry by entry
        self.assertIsNone(changes[2].entrywise_ok)

    def test_live_changed_without_conflict(self):
        summary = self.dream()
        self.commit_live({"a.md": "alpha fact, edited\n", "extra.md": "unrelated\n"})
        changes = self.by_n(review.build_plan([summary.run_id]))
        self.assertEqual([(changes[n].live_changed, changes[n].applies_cleanly) for n in (1, 2, 3)],
                         [(False, True)] * 3)  # a.md and extra.md are not files the run touches

    def test_an_index_edit_that_does_not_overlap_applies_cleanly_but_is_live_changed(self):
        summary = self.dream()
        self.commit_live({"MEMORY.md": "# Memory (live)\n- [A](a.md)\n- [B](b.md)\n"})
        change = self.by_n(review.build_plan([summary.run_id]))[1]
        self.assertTrue(change.live_changed)
        self.assertTrue(change.applies_cleanly)
        self.assertIsNone(change.entrywise_ok)

    def test_uncommitted_live_edit_counts_as_live_changed(self):
        summary = self.dream()
        (self.memory / "b.md").write_text("beta fact, edited\n", newline="\n")
        self.assertTrue(self.by_n(review.build_plan([summary.run_id]))[2].live_changed)

    def test_applied_and_rejected_changes_are_skipped_and_counted(self):
        summary = self.dream()
        runner.apply_run(summary.run_id, [3])
        runner.reject_run(summary.run_id, [2], "keep b.md")
        plan = review.build_plan([summary.run_id])
        self.assertEqual(([c.n for c in plan.changes], plan.skipped), ([1], 2))

    def test_unusable_runs_are_errors(self):
        with self.assertRaises(runner.RunError):
            review.build_plan(["nope"])
        summary = self.dream()
        runner.discard_run(summary.run_id)
        with self.assertRaisesRegex(runner.RunError, "discarded"):
            review.build_plan([summary.run_id])


class PlanDuplicateTests(PlanCase):
    def test_same_effect_in_a_later_run_points_at_the_earlier_one(self):
        first = self.dream()
        second = self.dream(days=1)
        plan = review.build_plan([second.run_id, first.run_id])  # argument order does not matter
        self.assertEqual([c.run_id for c in plan.changes], [first.run_id] * 3 + [second.run_id] * 3)
        for change in self.by_n(plan, first.run_id).values():
            self.assertIsNone(change.duplicate_of)
        for n, change in self.by_n(plan, second.run_id).items():
            self.assertEqual(change.duplicate_of, {"run_id": first.run_id, "n": n, "status": "pending"})

    def test_a_different_effect_on_the_same_file_is_not_a_duplicate(self):
        first = self.dream()
        second = self.dream(days=1, write={"topic.md": "prefers spaces\n"}, report=HAPPY["report"])
        later = self.by_n(review.build_plan([first.run_id, second.run_id]), second.run_id)
        self.assertEqual([c.file for c in later.values()], ["topic.md"])
        self.assertIsNone(later[1].duplicate_of)

    def test_duplicate_status_follows_the_earlier_change(self):
        first = self.dream()
        second = self.dream(days=1)
        runner.reject_run(first.run_id, [2], "keep b.md")
        runner.apply_run(first.run_id, [3])
        plan = review.build_plan([first.run_id, second.run_id])
        self.assertEqual(plan.skipped, 2)
        later = self.by_n(plan, second.run_id)
        self.assertEqual(later[1].duplicate_of["status"], "pending")
        self.assertEqual(later[2].duplicate_of, {"run_id": first.run_id, "n": 2, "status": "rejected"})
        self.assertEqual(later[3].duplicate_of, {"run_id": first.run_id, "n": 3, "status": "applied"})


class PlanCommandTests(PlanCase):
    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_output(self):
        first = self.dream()
        second = self.dream(days=1)
        code, out, err = self.call("plan", first.run_id, second.run_id, "--json")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["skipped"], 0)
        self.assertEqual(len(data["changes"]), 6)
        row = data["changes"][4]
        self.assertEqual(
            {k: row[k] for k in ("run_id", "n", "file", "action", "added", "removed", "live_changed", "applies_cleanly")},
            {"run_id": second.run_id, "n": 2, "file": "b.md", "action": "delete", "added": 0, "removed": 1,
             "live_changed": False, "applies_cleanly": True})
        self.assertEqual(row["duplicate_of"], {"run_id": first.run_id, "n": 2, "status": "pending"})

    def test_text_output_is_one_compact_line_per_change_plus_the_change_text(self):
        summary = self.dream()
        self.commit_live({"b.md": "beta fact, edited\n"})
        code, out, _ = self.call("plan", summary.run_id)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"{summary.run_id} #1 update MEMORY.md +1/-1 live=same clean")
        self.assertEqual(lines[1], "    Change: link the new topic from the index")
        self.assertEqual(lines[2], f"{summary.run_id} #2 delete b.md +0/-1 LIVE-CHANGED CONFLICTS")
        self.assertEqual(lines[4], f"{summary.run_id} #3 add topic.md +1/-0 live=same clean")

    def test_long_change_text_is_wrapped(self):
        long_change = " ".join(["word"] * 60)
        report = f"# R\n\n### topic.md\nWhy.\n**Change:** {long_change}\n"
        summary = self.dream(write={"topic.md": "x\n"}, report=report)
        _, out, _ = self.call("plan", summary.run_id)
        body = out.splitlines()[1:]
        self.assertGreater(len(body), 1)
        self.assertTrue(all(len(line) <= review.WRAP_WIDTH and line.startswith("    ") for line in body))
        self.assertEqual(" ".join(" ".join(body).split()), f"Change: {long_change}")

    def test_duplicates_and_skips_show_in_text(self):
        first = self.dream()
        second = self.dream(days=1)
        runner.apply_run(first.run_id, [3])
        _, out, _ = self.call("plan", first.run_id, second.run_id)
        self.assertIn(f"{second.run_id} #3 add topic.md +1/-0 LIVE-CHANGED clean DUP-OF {first.run_id}#3 (applied)", out)
        self.assertIn(f"{second.run_id} #1 update MEMORY.md +1/-1 live=same clean DUP-OF {first.run_id}#1 (pending)", out)
        self.assertTrue(out.rstrip().endswith("(1 applied or rejected change(s) skipped)"))

    def test_nothing_pending(self):
        summary = self.dream()
        runner.apply_run(summary.run_id)
        code, out, _ = self.call("plan", summary.run_id)
        self.assertEqual((code, out.strip()), (0, "No pending changes. (3 applied or rejected skipped)"))

    def test_unknown_run_is_a_clean_error(self):
        code, _, err = self.call("plan", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown run", err)


class PurePlanHelperTests(unittest.TestCase):
    def test_live_changed(self):
        self.assertFalse(review.live_changed("a\n", "a\n"))
        self.assertTrue(review.live_changed("b\n", "a\n"))
        self.assertFalse(review.live_changed(None, None))
        self.assertTrue(review.live_changed("new\n", None))  # a file the run adds now exists live
        self.assertTrue(review.live_changed(None, "old\n"))

    def test_effect_ignores_order_and_repeats(self):
        self.assertEqual(review.effect_of(["a", "b", "a"], ["c"]), review.effect_of(["b", "a"], ["c", "c"]))
        self.assertNotEqual(review.effect_of(["a"], ["c"]), review.effect_of(["a"], ["d"]))


if __name__ == "__main__":
    unittest.main()
