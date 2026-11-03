"""Tests for ``to_dream.repo_setup`` using real git in temp directories."""
import shutil
import subprocess
import unittest

from td_testing import IsolatedHomeTestCase

from to_dream import repo_setup

GIT = shutil.which("git")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


@unittest.skipUnless(GIT, "git not installed")
class RepoSetupTests(IsolatedHomeTestCase):
    def populate(self):
        """One project with memory + transcripts, one with only a transcript."""
        (self.memory_root / "proj-a" / "memory").mkdir(parents=True)
        (self.memory_root / "proj-a" / "memory" / "MEMORY.md").write_text("- note\n")
        (self.memory_root / "proj-a" / "memory" / "topic.md").write_bytes(b"topic\r\nwith crlf\r\n")
        (self.memory_root / "proj-a" / "session1.jsonl").write_text('{"x": 1}\n')
        (self.memory_root / "proj-a" / "subagents").mkdir()
        (self.memory_root / "proj-a" / "subagents" / "agent.jsonl").write_text("{}\n")
        (self.memory_root / "proj-b").mkdir()
        (self.memory_root / "proj-b" / "session2.jsonl").write_text("{}\n")
        (self.memory_root / "stray.txt").write_text("not memory\n")

    def tracked(self):
        return sorted(git(self.memory_root, "ls-files").stdout.split())

    def test_new_repo_tracks_only_memory_folders(self):
        self.populate()
        code, lines = repo_setup.init_repo(self.memory_root)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.tracked(), [".gitattributes", ".gitignore", "proj-a/memory/MEMORY.md",
                                          "proj-a/memory/topic.md"])
        status = git(self.memory_root, "status", "--porcelain", "--untracked-files=all").stdout
        self.assertEqual(status.strip(), "", "transcripts must be ignored, not untracked-visible")

    def test_default_branch_is_memory_and_custom_branch_honoured(self):
        repo_setup.init_repo(self.memory_root)
        self.assertEqual(git(self.memory_root, "symbolic-ref", "--short", "HEAD").stdout.strip(), "memory")
        other = self.tmp / "other"
        other.mkdir()
        self.assertEqual(repo_setup.init_repo(other, "trunk-x")[0], 0)
        self.assertEqual(git(other, "symbolic-ref", "--short", "HEAD").stdout.strip(), "trunk-x")

    def test_gitignore_and_attributes_content(self):
        repo_setup.init_repo(self.memory_root)
        lines = (self.memory_root / ".gitignore").read_text().splitlines()
        for expected in ("/*", "!/.gitignore", "!/.gitattributes", "!/*/", "/*/*", "!/*/memory/"):
            self.assertIn(expected, lines)
        self.assertEqual((self.memory_root / ".gitattributes").read_text(), "* -text\n")

    def test_line_endings_are_preserved_byte_for_byte(self):
        self.populate()
        repo_setup.init_repo(self.memory_root)
        blob = subprocess.run(["git", "show", "HEAD:proj-a/memory/topic.md"], cwd=str(self.memory_root),
                              capture_output=True).stdout
        self.assertEqual(blob, b"topic\r\nwith crlf\r\n")

    def test_new_memory_files_are_picked_up_and_new_transcripts_are_not(self):
        repo_setup.init_repo(self.memory_root)
        (self.memory_root / "p" / "memory").mkdir(parents=True)
        (self.memory_root / "p" / "memory" / "a.md").write_text("a")
        (self.memory_root / "p" / "t.jsonl").write_text("{}")
        git(self.memory_root, "add", "-A")
        self.assertEqual(self.tracked()[-1], "p/memory/a.md")
        self.assertNotIn("p/t.jsonl", self.tracked())

    def test_creates_missing_memory_root(self):
        missing = self.tmp / "no" / "such" / "projects"
        self.assertEqual(repo_setup.init_repo(missing)[0], 0)
        self.assertTrue((missing / ".git").exists())

    def test_invalid_branch_name_fails_without_creating_a_repo(self):
        code, lines = repo_setup.init_repo(self.memory_root, "bad..name")
        self.assertEqual(code, 2)
        self.assertIn("invalid branch name", lines[0])
        self.assertFalse((self.memory_root / ".git").exists())

    def test_existing_good_repo_is_untouched_and_verified(self):
        repo_setup.init_repo(self.memory_root, "mem")
        head = git(self.memory_root, "rev-parse", "HEAD").stdout
        before = (self.memory_root / ".gitignore").read_bytes()
        code, lines = repo_setup.init_repo(self.memory_root, "different")
        self.assertEqual(code, 0)
        self.assertIn("branch: mem", lines[0])
        self.assertEqual(git(self.memory_root, "rev-parse", "HEAD").stdout, head)
        self.assertEqual((self.memory_root / ".gitignore").read_bytes(), before)
        self.assertEqual(git(self.memory_root, "symbolic-ref", "--short", "HEAD").stdout.strip(), "mem")

    def test_existing_bad_repo_is_reported_and_never_rewritten(self):
        git(self.memory_root, "init")
        (self.memory_root / "notes.txt").write_text("hi")
        git(self.memory_root, "add", "notes.txt")
        code, lines = repo_setup.init_repo(self.memory_root)
        self.assertEqual(code, 1)
        report = "\n".join(lines)
        self.assertIn(".gitignore is missing", report)
        self.assertIn("'* -text'", report)
        self.assertIn("tracked files outside", report)
        self.assertIn("notes.txt", report)
        self.assertFalse((self.memory_root / ".gitignore").exists())
        self.assertFalse((self.memory_root / ".gitattributes").exists())

    def test_parent_repo_does_not_count_as_existing(self):
        git(self.tmp, "init")
        sub = self.tmp / "nested-projects"
        sub.mkdir()
        self.assertEqual(repo_setup.init_repo(sub)[0], 0)
        self.assertTrue((sub / ".git").exists())

    def test_commit_works_without_a_configured_identity(self):
        env_home = self.tmp / "emptyhome"
        env_home.mkdir()
        import os
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        os.environ.update(HOME=str(env_home), USERPROFILE=str(env_home), GIT_CONFIG_NOSYSTEM="1",
                          GIT_CONFIG_GLOBAL=str(env_home / "nonexistent"))
        self.assertEqual(repo_setup.init_repo(self.memory_root)[0], 0)
        self.assertEqual(git(self.memory_root, "log", "--format=%an").stdout.strip(), "to-dream")

@unittest.skipUnless(GIT, "git not installed")
class InitRepoSecretScanTests(IsolatedHomeTestCase):
    # Assembled at runtime so no key-shaped literal exists in this file.
    SECRET = "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"

    def memory_file(self, project, name, text):
        target = self.memory_root / project / "memory" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    def test_secret_in_existing_memory_blocks_the_first_commit(self):
        self.memory_file("proj-a", "MEMORY.md", "- fine\n")
        self.memory_file("proj-a", "keys.md", "ok\ntoken " + self.SECRET + "\n")
        code, lines = repo_setup.init_repo(self.memory_root)
        text = "\n".join(lines)
        self.assertEqual(code, 1, text)
        self.assertIn("proj-a/memory/keys.md:2 github-token", text)
        self.assertNotIn(self.SECRET, text)
        self.assertIn("secret-scan: allow", text)
        self.assertIn("Nothing was staged", text)
        self.assertFalse((self.memory_root / ".git").exists())  # no repo, nothing staged or committed

    def test_allow_marker_and_clean_files_pass(self):
        self.memory_file("proj-a", "MEMORY.md", "- fine\n")
        self.memory_file("proj-a", "doc.md", "token " + self.SECRET + "  secret-scan: allow\n")
        self.assertEqual(repo_setup.init_repo(self.memory_root)[0], 0)
        self.assertIn("proj-a/memory/doc.md", git(self.memory_root, "ls-files").stdout)

    def test_removing_the_secret_lets_a_retry_succeed(self):
        self.memory_file("proj-a", "keys.md", "token " + self.SECRET + "\n")
        self.assertEqual(repo_setup.init_repo(self.memory_root)[0], 1)
        self.memory_file("proj-a", "keys.md", "token lives in the vault\n")
        self.assertEqual(repo_setup.init_repo(self.memory_root)[0], 0)

    def test_secrets_outside_memory_folders_are_not_scanned(self):
        (self.memory_root / "proj-a").mkdir(parents=True)
        (self.memory_root / "proj-a" / "session.jsonl").write_text(self.SECRET + "\n")
        self.assertEqual(repo_setup.init_repo(self.memory_root)[0], 0)


if __name__ == "__main__":
    unittest.main()
