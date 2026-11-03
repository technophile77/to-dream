import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import GIT_IDENTITY, git, install_hook, make_memory_repo, new_seed

from to_dream import gitops

SLUG = "proj"
PREFIX = f"{SLUG}/memory/"
BASE = "trunk"  # not "memory": nothing may assume a branch name
# Assembled at runtime so no key-shaped literal exists in this file.
SECRET = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"
PASSWORD_LINE = "password: " + "hunter2" + "hunter2"


class NamingTests(unittest.TestCase):
    def test_slugify(self):
        cases = [
            ("My_Project/Claude #Memory", "my-project-claude-memory"),
            ("--a__b--", "a-b"),
            ("ÀÉ", "x"),
            ("", "x"),
            ("UPPER.case", "upper-case"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(gitops.slugify(text), expected)

    def test_slugify_cap_trims_trailing_dash(self):
        self.assertEqual(gitops.slugify("abcdefghij-klmnop", cap=11), "abcdefghij")
        self.assertEqual(gitops.slugify("a" * 30, cap=15), "a" * 15)

    def test_unique_path_appends_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            first = gitops.unique_path(parent, "wt")
            self.assertEqual(first.name, "wt")
            first.mkdir()
            second = gitops.unique_path(parent, "wt")
            self.assertEqual(second.name, "wt_2")
            second.mkdir()
            self.assertEqual(gitops.unique_path(parent, "wt").name, "wt_3")

    def test_in_memory_scope(self):
        cases = [("proj/memory/a.md", True), ("proj/memory/sub/b.md", True), ("proj/memory", False),
                 ("proj/notes/a.md", False), (".gitignore", False), ("a/b/memory/c.md", False)]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(gitops.in_memory_scope(path), expected)


class ReportTests(unittest.TestCase):
    def test_sections_and_lookup(self):
        report = (
            "# Dream report\nintro\n## Changes\n### MEMORY.md\nAdded index line.\n- evidence 1\n"
            "### `topics/x y.md`\nWhy x\n\n### b.md\nWhy b\n## Footer\nignored\n"
        )
        sections = gitops.parse_report_sections(report)
        self.assertEqual(sections["MEMORY.md"], "Added index line.\n- evidence 1")
        self.assertEqual(sections["topics/x y.md"], "Why x")
        self.assertEqual(sections["b.md"], "Why b")
        self.assertEqual(gitops.section_for(sections, "topics/x y.md"), "Why x")
        self.assertEqual(gitops.section_for(sections, "sub/b.md"), "Why b")  # base-name fallback
        self.assertEqual(gitops.section_for(sections, "nope.md"), "")

    def test_level2_sections(self):
        report = "# R\n## Stale candidates\n- x\n### f.md\nq\n## Not acted on!\nnone\n# Tail\nout\n"
        self.assertEqual(
            gitops.level2_sections(report),
            {"stale_candidates": "- x\n### f.md\nq", "not_acted_on": "none"},
        )

    def test_change_line(self):
        cases = [
            ("**Change:** add tabs\nmore", "add tabs"),
            ("Why\n- **Change**: drop b  \n", "drop b"),
            ("no marker line\nsecond", "no marker line"),
            ("", ""),
        ]
        for section, expected in cases:
            with self.subTest(section=section):
                self.assertEqual(gitops.change_line(section), expected)

    def test_parse_subject(self):
        self.assertEqual(gitops.parse_subject("chore(to-dream): delete st/sub/b c.md"), ("delete", "sub/b c.md"))
        self.assertIsNone(gitops.parse_subject("feat: something"))
        self.assertIsNone(gitops.parse_subject("chore(to-dream): snapshot before run x"))
        self.assertIsNone(gitops.parse_subject("chore(dream): add st/a.md"))

    def test_cap_text(self):
        self.assertEqual(gitops.cap_text("  short  "), "short")
        capped = gitops.cap_text("x" * 5000)
        self.assertEqual(len(capped), gitops.BODY_CAP)
        self.assertTrue(capped.endswith("[truncated]"))

    def test_first_line(self):
        self.assertEqual(gitops.first_line("\n\n- **Why** it matters\nmore"), "**Why** it matters")
        self.assertEqual(len(gitops.first_line("y" * 300)), 120)
        self.assertEqual(gitops.first_line(""), "")

    def test_commit_message_shape(self):
        change = gitops.FileChange(PREFIX + "a.md", gitops.ACTION_ADD)
        message = gitops.commit_message("st", change, "a.md", "Because.", "st-261102-1830")
        self.assertEqual(message, "chore(to-dream): add st/a.md\n\nBecause.\n\nTo-Dream-Run: st-261102-1830\n")
        bare = gitops.commit_message("st", change, "a.md", "", "r1")
        self.assertEqual(bare, "chore(to-dream): add st/a.md\n\nTo-Dream-Run: r1\n")


class PorcelainTests(unittest.TestCase):
    def test_parse_porcelain_z(self):
        raw = " M a.md\0?? sp ace.md\0R  new.md\0old.md\0D  gone.md\0"
        self.assertEqual(
            gitops.parse_porcelain_z(raw),
            [(" M", "a.md"), ("??", "sp ace.md"), ("R ", "new.md"), ("D ", "gone.md")],
        )

    def test_split_scope(self):
        inside, outside = gitops.split_scope([("??", "proj/memory/a.md"), (" M", ".gitignore")], PREFIX)
        self.assertEqual(inside, [("??", "proj/memory/a.md")])
        self.assertEqual(outside, [(" M", ".gitignore")])


class RepoTestCase(unittest.TestCase):
    """A temp memory repo (branch ``trunk``) with project ``proj`` (a.md, b.md) and a dream worktree on a branch."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, GIT_IDENTITY)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.root = self.tmp / "projects"
        self.root.mkdir()
        make_memory_repo(self.root, BASE)
        mem = self.root / SLUG / "memory"
        mem.mkdir(parents=True)
        (mem / "a.md").write_text("alpha\n", newline="\n")
        (mem / "b.md").write_text("beta\n", newline="\n")
        (self.root / "other" / "memory").mkdir(parents=True)
        (self.root / "other" / "memory" / "o.md").write_text("other\n", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "seed")
        self.branch = "dream/proj-261102-1830"
        self.wt = self.tmp / "worktrees" / "wt"
        gitops.create_worktree(self.root, self.wt, self.branch, BASE)
        self.mem = self.wt / SLUG / "memory"

    def commit_all(self, changes):
        outcomes = [
            gitops.commit_change(self.wt, c, gitops.commit_message("p", c, c.path[len(PREFIX):], "why", "r"))
            for c in changes
        ]
        for o in outcomes:
            self.assertIsNotNone(o.sha, o.error)
        return outcomes


class WorktreeTests(RepoTestCase):
    def test_branch_created_from_base_and_worktree_checked_out(self):
        self.assertTrue(gitops.branch_exists(self.root, self.branch))
        self.assertEqual(gitops.current_branch(self.wt), self.branch)
        self.assertEqual(gitops.current_branch(self.root), BASE)
        self.assertEqual(gitops.rev_parse(self.root, self.branch), gitops.rev_parse(self.root, BASE))
        self.assertTrue((self.mem / "a.md").is_file())

    def test_existing_branch_is_an_error(self):
        with self.assertRaises(gitops.GitError):
            gitops.create_worktree(self.root, self.tmp / "wt2", self.branch, BASE)

    def test_valid_branch_name(self):
        for good in ("dream/x-1", "a", "feat_26-11-02_x"):
            self.assertTrue(gitops.valid_branch_name(self.root, good), good)
        for bad in ("has space", "a..b", "x.lock", "/lead", "trail/", "q?", "a~b", "@{x}"):
            self.assertFalse(gitops.valid_branch_name(self.root, bad), bad)


class CommitTests(RepoTestCase):
    def test_one_commit_per_file_including_delete(self):
        (self.mem / "a.md").write_text("alpha v2\n", newline="\n")
        (self.mem / "new topic.md").write_text("fresh\n", newline="\n")
        (self.mem / "b.md").write_text("  \n\t\n", newline="\n")  # blank => delete
        (self.mem / "ghost.md").write_text("", newline="\n")  # new + blank => dropped
        changes, outside = gitops.collect_changes(self.wt, PREFIX)
        self.assertEqual(outside, [])
        self.assertEqual(
            [(c.path, c.action) for c in changes],
            [(PREFIX + "a.md", "update"), (PREFIX + "b.md", "delete"),
             (PREFIX + "ghost.md", "drop"), (PREFIX + "new topic.md", "add")],
        )
        gitops.remove_dropped(self.wt, changes)
        self.assertFalse((self.mem / "ghost.md").exists())
        self.commit_all([c for c in changes if c.action != "drop"])
        commits = gitops.list_commits(self.root, BASE, self.branch)
        self.assertEqual(
            [s for _, s in commits],
            ["chore(to-dream): update p/a.md", "chore(to-dream): delete p/b.md", "chore(to-dream): add p/new topic.md"],
        )
        for (sha, _), path in zip(commits, ["a.md", "b.md", "new topic.md"]):
            self.assertEqual(gitops.changed_paths(self.root, sha), [PREFIX + path])
        self.assertEqual(git(self.root, "show", f"{self.branch}:{PREFIX}a.md"), "alpha v2\n")
        self.assertEqual(git(self.root, "ls-tree", "-r", "--name-only", self.branch, PREFIX).split("\n")[:-1],
                         [PREFIX + "a.md", PREFIX + "new topic.md"])
        body = git(self.root, "log", "-1", "--format=%B", commits[0][0])
        self.assertIn("why", body)
        self.assertIn("To-Dream-Run: r", body)
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")

    def test_commit_diff_and_cap(self):
        (self.mem / "a.md").write_text("alpha\n" + "y" * 100 + "\n", newline="\n")
        (change,) = gitops.collect_changes(self.wt, PREFIX)[0]
        sha = self.commit_all([change])[0].sha
        diff, truncated = gitops.commit_diff(self.root, sha)
        self.assertIn("+" + "y" * 100, diff)
        self.assertFalse(truncated)
        short, truncated = gitops.commit_diff(self.root, sha, cap=50)
        self.assertEqual((len(short), truncated), (50, True))

    def test_out_of_scope_changes_are_reverted(self):
        (self.mem / "a.md").write_text("alpha v2\n", newline="\n")
        (self.wt / ".gitignore").write_text("# tampered\n", newline="\n")  # tracked file
        (self.wt / "other" / "memory" / "o.md").write_text("tampered\n", newline="\n")  # tracked, other project
        (self.wt / "other" / "memory" / "new.md").write_text("sneaky\n", newline="\n")  # untracked, other project
        changes, outside = gitops.collect_changes(self.wt, PREFIX)
        self.assertEqual([c.path for c in changes], [PREFIX + "a.md"])
        self.assertEqual(
            sorted(p for _, p in outside), [".gitignore", "other/memory/new.md", "other/memory/o.md"]
        )
        reverted = gitops.revert_entries(self.wt, outside)
        self.assertEqual(len(reverted), 3)
        self.assertEqual((self.wt / "other" / "memory" / "o.md").read_text(), "other\n")
        self.assertFalse((self.wt / "other" / "memory" / "new.md").exists())
        self.assertNotIn("tampered", (self.wt / ".gitignore").read_text())
        _, outside_after = gitops.collect_changes(self.wt, PREFIX)
        self.assertEqual(outside_after, [])

    def test_user_hook_rejection_leaves_file_uncommitted_and_unstaged(self):
        install_hook(self.root, "echo 'nope' >&2\nexit 1\n")
        (self.mem / "a.md").write_text("alpha v2\n", newline="\n")
        change = gitops.FileChange(PREFIX + "a.md", "update")
        outcome = gitops.commit_change(self.wt, change, "chore(to-dream): update p/a.md\n")
        self.assertIsNone(outcome.sha)
        self.assertIn("nope", outcome.error)
        self.assertEqual(git(self.wt, "diff", "--cached", "--name-only"), "")
        self.assertEqual((self.mem / "a.md").read_text(), "alpha v2\n")
        self.assertEqual(gitops.list_commits(self.root, BASE, self.branch), [])


class InProcessSecretScanTests(RepoTestCase):
    """Every proposal commit is scanned in-process; no git hook is installed in these tests."""

    def test_secret_in_a_proposal_blocks_that_file_and_reports_path_line_rule(self):
        (self.mem / "a.md").write_text(f"fine\ntoken {SECRET}\n", newline="\n")
        (self.mem / "c.md").write_text("clean\n", newline="\n")
        changes, _ = gitops.collect_changes(self.wt, PREFIX)
        outcomes = {o.change.path: o for o in (gitops.commit_change(self.wt, c, "chore(to-dream): x\n") for c in changes)}
        blocked = outcomes[PREFIX + "a.md"]
        self.assertIsNone(blocked.sha)
        self.assertIn(f"{PREFIX}a.md:2 github-token", blocked.error)
        self.assertNotIn(SECRET, blocked.error)
        self.assertNotIn("token", blocked.error.replace("github-token", ""))
        self.assertIsNotNone(outcomes[PREFIX + "c.md"].sha)
        self.assertEqual(git(self.wt, "diff", "--cached", "--name-only"), "")
        self.assertIn(SECRET, (self.mem / "a.md").read_text())  # left on disk, uncommitted
        self.assertEqual(len(gitops.list_commits(self.root, BASE, self.branch)), 1)

    def test_several_findings_are_all_listed(self):
        (self.mem / "a.md").write_text(f"{PASSWORD_LINE}\nok\ntoken {SECRET}\n", newline="\n")
        (change,) = gitops.collect_changes(self.wt, PREFIX)[0]
        outcome = gitops.commit_change(self.wt, change, "chore(to-dream): x\n")
        self.assertIn(f"{PREFIX}a.md:1 generic-secret-assignment", outcome.error)
        self.assertIn(f"{PREFIX}a.md:3 github-token", outcome.error)

    def test_allow_marker_lets_a_line_through(self):
        (self.mem / "a.md").write_text(f"token {SECRET}  secret-scan: allow\n", newline="\n")
        (change,) = gitops.collect_changes(self.wt, PREFIX)[0]
        self.assertIsNotNone(gitops.commit_change(self.wt, change, "chore(to-dream): x\n").sha)

    def test_only_added_lines_count(self):
        (self.mem / "a.md").write_text(f"token {SECRET}  secret-scan: allow\n", newline="\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "baseline with allowed secret")
        (self.mem / "a.md").write_text("clean replacement\n", newline="\n")
        (change,) = gitops.collect_changes(self.wt, PREFIX)[0]
        self.assertIsNotNone(gitops.commit_change(self.wt, change, "chore(to-dream): x\n").sha)

    def test_deleting_a_file_is_never_blocked(self):
        (self.mem / "a.md").write_text(f"token {SECRET}  secret-scan: allow\n", newline="\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "baseline")
        (self.mem / "a.md").write_text("", newline="\n")
        (change,) = gitops.collect_changes(self.wt, PREFIX)[0]
        self.assertEqual(change.action, gitops.ACTION_DELETE)
        self.assertIsNotNone(gitops.commit_change(self.wt, change, "chore(to-dream): x\n").sha)


class SnapshotTests(RepoTestCase):
    def live(self, name):
        return self.root / SLUG / "memory" / name

    def test_clean_repo_makes_no_commit(self):
        before = gitops.rev_parse(self.root, "HEAD")
        result = gitops.snapshot_memory(self.root, "run x")
        self.assertEqual(result, gitops.SnapshotResult())
        self.assertEqual(gitops.rev_parse(self.root, "HEAD"), before)

    def test_dirty_memory_is_committed_with_the_snapshot_subject(self):
        self.live("a.md").write_text("alpha edited\n", newline="\n")
        self.live("new topic.md").write_text("fresh\n", newline="\n")
        self.live("b.md").unlink()
        result = gitops.snapshot_memory(self.root, "run st-261102-1830")
        self.assertEqual(result.sha, gitops.rev_parse(self.root, "HEAD"))
        self.assertEqual(result.committed, (PREFIX + "a.md", PREFIX + "b.md", PREFIX + "new topic.md"))
        self.assertEqual(result.refused, ())
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "chore(to-dream): snapshot before run st-261102-1830\n")
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual(git(self.root, "show", f"HEAD:{PREFIX}a.md"), "alpha edited\n")
        self.assertEqual(sorted(gitops.changed_paths(self.root, result.sha)),
                         [PREFIX + "a.md", PREFIX + "b.md", PREFIX + "new topic.md"])

    def test_files_outside_the_memory_scope_are_left_alone(self):
        self.live("a.md").write_text("alpha edited\n", newline="\n")
        (self.root / ".gitignore").write_text("# user edit\n", newline="\n")
        (self.root / "other" / "notes").mkdir()
        (self.root / "other" / "notes" / "n.md").write_text("not memory\n", newline="\n")
        result = gitops.snapshot_memory(self.root, "apply x")
        self.assertEqual(result.committed, (PREFIX + "a.md",))
        self.assertEqual(gitops.changed_paths(self.root, result.sha), [PREFIX + "a.md"])
        self.assertEqual(gitops.dirty_outside_memory(self.root), [".gitignore"])

    def test_staged_changes_elsewhere_stay_staged_and_out_of_the_snapshot(self):
        (self.root / ".gitignore").write_text("# staged by the user\n", newline="\n")
        git(self.root, "add", ".gitignore")
        self.live("a.md").write_text("alpha edited\n", newline="\n")
        result = gitops.snapshot_memory(self.root, "run x")
        self.assertEqual(gitops.changed_paths(self.root, result.sha), [PREFIX + "a.md"])
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only").split(), [".gitignore"])

    def test_secret_file_is_refused_with_path_line_rule_and_others_are_committed(self):
        self.live("a.md").write_text(f"ok\n{PASSWORD_LINE}\n", newline="\n")
        self.live("b.md").write_text("beta edited\n", newline="\n")
        result = gitops.snapshot_memory(self.root, "run x")
        self.assertEqual(result.committed, (PREFIX + "b.md",))
        (refused,) = result.refused
        self.assertEqual(refused[0], PREFIX + "a.md")
        self.assertIn(f"{PREFIX}a.md:2 generic-secret-assignment", refused[1])
        self.assertNotIn("hunter2", refused[1])
        self.assertEqual(gitops.changed_paths(self.root, result.sha), [PREFIX + "b.md"])
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertIn("hunter2", self.live("a.md").read_text())  # still dirty on disk
        self.assertEqual(git(self.root, "status", "--porcelain").split()[1], PREFIX + "a.md")

    def test_only_secret_files_means_no_commit(self):
        before = gitops.rev_parse(self.root, "HEAD")
        self.live("a.md").write_text(f"token {SECRET}\n", newline="\n")
        result = gitops.snapshot_memory(self.root, "run x")
        self.assertIsNone(result.sha)
        self.assertEqual(len(result.refused), 1)
        self.assertEqual(gitops.rev_parse(self.root, "HEAD"), before)

    def test_user_hook_rejection_unstages_everything(self):
        install_hook(self.root, "echo 'blocked by house rules' >&2\nexit 1\n")
        before = gitops.rev_parse(self.root, "HEAD")
        self.live("a.md").write_text("alpha edited\n", newline="\n")
        result = gitops.snapshot_memory(self.root, "run x")
        self.assertIsNone(result.sha)
        self.assertEqual([p for p, _ in result.refused], [PREFIX + "a.md"])
        self.assertIn("house rules", result.refused[0][1])
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "")
        self.assertEqual(gitops.rev_parse(self.root, "HEAD"), before)

    def test_awkward_file_names(self):
        names = ["sp ace.md", "br[1].md", "it's.md", "café.md", "br{a,b}.md", "sub dir/deep [x].md"]
        for name in names:
            target = self.live(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"content of {name}\n", encoding="utf-8", newline="\n")
        result = gitops.snapshot_memory(self.root, "run x")
        self.assertEqual(set(result.committed), {PREFIX + n for n in names})
        self.assertEqual(git(self.root, "status", "--porcelain"), "")


class DirtyOutsideMemoryTests(RepoTestCase):
    def test_lists_only_tracked_non_memory_changes(self):
        self.assertEqual(gitops.dirty_outside_memory(self.root), [])
        (self.root / "other" / "memory" / "o.md").write_text("edited\n", newline="\n")  # memory scope: ignored
        (self.root / "untracked.txt").write_text("x\n", newline="\n")  # untracked: ignored
        self.assertEqual(gitops.dirty_outside_memory(self.root), [])
        (self.root / ".gitignore").write_text("# edited\n", newline="\n")
        self.assertEqual(gitops.dirty_outside_memory(self.root), [".gitignore"])


class ApplyDiscardTests(RepoTestCase):
    def make_three_commits(self):
        (self.mem / "a.md").write_text("alpha v2\n", newline="\n")
        (self.mem / "c.md").write_text("gamma\n", newline="\n")
        (self.mem / "b.md").write_text("", newline="\n")
        changes, _ = gitops.collect_changes(self.wt, PREFIX)
        self.commit_all(changes)
        return gitops.list_commits(self.root, BASE, self.branch)

    def test_apply_all(self):
        commits = self.make_three_commits()
        result = gitops.cherry_pick_all(self.root, [sha for sha, _ in commits])
        self.assertIsNone(result.failed)
        self.assertEqual(list(result.applied), [sha for sha, _ in commits])
        live = self.root / SLUG / "memory"
        self.assertEqual((live / "a.md").read_text(), "alpha v2\n")
        self.assertEqual((live / "c.md").read_text(), "gamma\n")
        self.assertFalse((live / "b.md").exists())
        subjects = git(self.root, "log", "-3", "--format=%s").split("\n")[:3]
        self.assertEqual(subjects, [s for _, s in reversed(commits)])
        self.assertIn("cherry picked from commit", git(self.root, "log", "-1", "--format=%B"))

    def test_apply_selected_only(self):
        commits = self.make_three_commits()
        result = gitops.cherry_pick_all(self.root, [commits[2][0]])
        self.assertEqual(list(result.applied), [commits[2][0]])
        live = self.root / SLUG / "memory"
        self.assertEqual((live / "a.md").read_text(), "alpha\n")
        self.assertTrue((live / "b.md").exists())
        self.assertEqual((live / "c.md").read_text(), "gamma\n")

    def test_conflict_aborts_and_leaves_clean_state(self):
        commits = self.make_three_commits()
        live = self.root / SLUG / "memory"
        (live / "a.md").write_text("alpha LIVE edit\n", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "live edit")
        head_before = gitops.rev_parse(self.root, "HEAD")
        result = gitops.cherry_pick_all(self.root, [sha for sha, _ in commits])
        self.assertEqual(result.failed, commits[0][0])
        self.assertEqual(result.applied, {})
        self.assertEqual(gitops.rev_parse(self.root, "HEAD"), head_before)
        self.assertFalse((self.root / ".git" / "CHERRY_PICK_HEAD").exists())
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual((live / "a.md").read_text(), "alpha LIVE edit\n")

    def test_later_conflict_keeps_earlier_picks(self):
        commits = self.make_three_commits()  # order: update a, delete b, add c
        live = self.root / SLUG / "memory"
        (live / "c.md").write_text("live gamma\n", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "live c")
        result = gitops.cherry_pick_all(self.root, [sha for sha, _ in commits])
        self.assertEqual(result.failed, commits[2][0])
        self.assertEqual(list(result.applied), [commits[0][0], commits[1][0]])
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_discard_pieces(self):
        self.make_three_commits()
        with self.assertRaises(gitops.GitError):
            gitops.delete_branch(self.root, self.branch)  # checked out in the worktree
        gitops.remove_worktree(self.root, self.wt)
        gitops.delete_branch(self.root, self.branch)
        self.assertFalse(self.wt.exists())
        self.assertFalse(gitops.branch_exists(self.root, self.branch))

    def test_dirty_worktree_needs_force(self):
        (self.mem / "a.md").write_text("unsaved work\n", newline="\n")
        with self.assertRaises(gitops.GitError):
            gitops.remove_worktree(self.root, self.wt)
        self.assertFalse(gitops.only_tool_files_dirty(self.wt))
        (self.mem / "a.md").write_text("alpha\n", newline="\n")
        (self.mem / ".claude").mkdir()
        (self.mem / ".claude" / "settings.local.json").write_text("{}", newline="\n")
        self.assertTrue(gitops.only_tool_files_dirty(self.wt))
        gitops.remove_worktree(self.root, self.wt, force=True)
        self.assertFalse(self.wt.exists())


ODD_NAMES = [
    "plain.md", "with space.md", "br[1].md", "hash#tag.md", "pct%20.md", "it's.md", "semi;colon.md",
    "amp&er.md", "café.md", "bang!.md", "a,b.md", "(paren).md", "plus+.md", "eq=.md", "at@.md",
    "tilde~.md", "dollar$.md", "caret^.md", "{brace}.md", "sub dir/deep [x].md", "UPPER.MD", "dot.in.name.md",
]


class StochasticCommitTests(RepoTestCase):
    """Random add/modify/delete sets over awkward file names: one commit per file, tree == expectation."""

    def test_random_change_sets(self):
        seed = new_seed()
        print(f"[test_random_change_sets] seed={seed}")
        rng = random.Random(seed)
        for trial in range(12):
            with self.subTest(seed=seed, trial=trial):
                self.run_trial(rng, trial)

    def run_trial(self, rng: random.Random, trial: int) -> None:
        branch = f"dream/trial-{trial}"
        wt = self.tmp / "worktrees" / f"trial{trial}"
        gitops.create_worktree(self.root, wt, branch, BASE)
        mem = wt / SLUG / "memory"
        expected = {p.relative_to(mem).as_posix(): p.read_text() for p in mem.rglob("*") if p.is_file()}
        names = rng.sample(ODD_NAMES, rng.randint(3, 12))
        # seed some of the names into the base state of this trial's branch (committed in the worktree)
        for name in names[: len(names) // 2]:
            target = mem / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"original {name}\n", encoding="utf-8", newline="\n")
            expected[name] = f"original {name}\n"
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "trial base")
        base = gitops.rev_parse(wt, "HEAD")

        touched = 0
        for name in names:
            target = mem / name
            exists = name in expected
            op = rng.choice(["modify", "delete", "blank"] if exists else ["add", "add", "blank"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if op in ("modify", "add"):
                text = f"new content {rng.random()} for {name}\n"
                target.write_text(text, encoding="utf-8", newline="\n")
                expected[name] = text
                touched += 1
            elif op == "delete":  # never used by Claude (no shell), kept for the git-level path
                target.unlink()
                expected.pop(name)
                touched += 1
            else:  # blank
                target.write_text(rng.choice(["", "\n", " \t\n"]), newline="\n")
                if exists:
                    expected.pop(name)
                    touched += 1

        changes, outside = gitops.collect_changes(wt, PREFIX)
        self.assertEqual(outside, [])
        gitops.remove_dropped(wt, changes)
        real = [c for c in changes if c.action != gitops.ACTION_DROP]
        self.assertEqual(len(real), touched)
        for change in real:
            outcome = gitops.commit_change(
                wt, change, gitops.commit_message("p", change, change.path[len(PREFIX):], "", "r"))
            self.assertIsNotNone(outcome.sha, outcome.error)

        commits = gitops.list_commits(self.root, base, branch)
        self.assertEqual(len(commits), touched)
        seen = set()
        for sha, _subject in commits:
            paths = gitops.changed_paths(self.root, sha)
            self.assertEqual(len(paths), 1, paths)
            seen.add(paths[0])
        self.assertEqual(seen, {c.path for c in real})
        tree = git(self.root, "ls-tree", "-r", "-z", "--name-only", branch, PREFIX).split("\0")
        in_branch = {p[len(PREFIX):]: git(self.root, "show", f"{branch}:{p}") for p in tree if p}
        self.assertEqual(in_branch, expected)
        on_disk = {p.relative_to(mem).as_posix(): p.read_text(encoding="utf-8") for p in mem.rglob("*") if p.is_file()}
        self.assertEqual(on_disk, expected)
        self.assertEqual(git(wt, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
