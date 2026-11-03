import os
import random
import unittest
from unittest import mock

from helpers import new_seed, render_path

from to_dream import paths

FAKE_HOME = r"C:\Users\sam"
FAKE_HOME_ENV = {"HOME": FAKE_HOME, "USERPROFILE": FAKE_HOME}
ROOT_PARTS = ["Users", "sam", ".claude", "projects"]
R = r"C:\Users\sam\.claude\projects"


class PathsTableTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, FAKE_HOME_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_is_memory_path_table(self):
        cases = [
            (r"C:\Users\sam\.claude\projects\x\memory\a.md", True),
            ("C:/Users/sam/.claude/projects/x/memory/a.md", True),
            ("~/.claude/projects/x/memory/a.md", True),
            ("$HOME/.claude/projects/x/memory/a.md", True),
            ("${HOME}/.claude/projects/x/memory/a.md", True),
            ("C:/Users/sam/.claude/projects/x/memory/", True),
            ("C:/Users/sam/.claude/projects/x/memory", True),
            ("C:/Users/sam/.claude/projects/x/memory/sub/deep/b.md", True),
            ("C:/Users/sam/.claude/projects/x/foo/../memory/a.md", True),
            ("C:/Users/sam/.claude/projects/y/../x/memory/a.md", True),
            ("C:/Users/sam/.claude/projects/x/memory/../MEMORY.md", False),
            ("C:/Users/sam/.claude/projects/x/other/a.md", False),
            ("C:/Users/sam/.claude/projects/x/memory2/a.md", False),
            ("C:/Users/sam/.claude/projects/x", False),
            ("C:/Users/sam/.claude/projects/memory/a.md", False),
            ("C:/Users/sam/.claude/projects/x/y/memory/a.md", False),
            ("C:/Users/sam/.claude/projects/../x/memory/a.md", False),
            ("C:/Users/sam/.claude/settings.json", False),
            ("C:/Users/bob/.claude/projects/x/memory/a.md", False),
            ("/tmp/x/memory/a.md", False),
            ("memory/a.md", False),
            ("", False),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(paths.is_memory_path(path, R), expected)

    @unittest.skipUnless(paths.IS_WINDOWS, "MSYS drive spellings and case-insensitivity are Windows-only")
    def test_msys_spelling_and_case_insensitive_on_windows(self):
        self.assertTrue(paths.is_memory_path("/c/Users/sam/.claude/projects/x/memory/a.md", R))
        self.assertTrue(paths.is_memory_path("/C/Users/sam/.claude/projects/x/memory/a.md", R))
        self.assertTrue(paths.is_memory_path("c:/USERS/sam/.CLAUDE/Projects/X/MEMORY/a.md", R))

    def test_root_may_be_given_in_any_form(self):
        roots = ["~/.claude/projects", "C:/Users/sam/.claude/projects/"]
        for root in roots + (["/c/Users/sam/.claude/projects"] if paths.IS_WINDOWS else []):
            with self.subTest(root=root):
                self.assertTrue(paths.is_memory_path(r"C:\Users\sam\.claude\projects\x\memory\a.md", root))

    def test_slug_of_table(self):
        cases = [
            ("C:/Users/sam/.claude/projects/C--code-proj/memory/MEMORY.md", "C--code-proj"),
            ("~/.claude/projects/Abc/memory/", "Abc"),
            ("C:/Users/sam/.claude/projects/p/other/y.md", None),
            ("/etc/passwd", None),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(paths.slug_of(path, R), expected)


class PosixPathsTests(unittest.TestCase):
    def test_posix_root_and_paths(self):
        root = "/home/sam/.claude/projects"
        self.assertTrue(paths.is_memory_path("/home/sam/.claude/projects/-home-sam-proj/memory/a.md", root))
        self.assertFalse(paths.is_memory_path("/home/sam/.claude/projects/-home-sam-proj/notes/a.md", root))
        self.assertEqual(paths.slug_of("/home/sam/.claude/projects/-home-sam-proj/memory", root), "-home-sam-proj")


class PathsStochasticTests(unittest.TestCase):
    """Random spellings of paths must agree with a ground truth built from the structure alone."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, FAKE_HOME_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def random_structure(rng):
        """Return (parts, expected_slug_or_None); ground truth comes from construction."""
        slug = rng.choice(["C--code-x", "proj", "A1", "p.q"])
        rest = [rng.choice(["a.md", "MEMORY.md", "sub"]) for _ in range(rng.randint(0, 2))]
        kind = rng.choice(["mem", "mem", "other_dir", "slug_only", "outside", "memlike", "nested"])
        if kind == "mem":
            return ROOT_PARTS + [slug, "memory"] + rest, slug
        if kind == "other_dir":
            return ROOT_PARTS + [slug, "notes"] + rest, None
        if kind == "slug_only":
            return ROOT_PARTS + [slug], None
        if kind == "memlike":
            return ROOT_PARTS + [slug, "memory2"] + rest, None
        if kind == "nested":
            return ROOT_PARTS + [slug, "inner", "memory"] + rest, None
        return rng.choice([["Users", "bob", "x", "memory"], ["Users", "sam", ".claude", "x", "memory"]]) + rest, None

    def test_is_memory_path_agrees_with_ground_truth(self):
        seed = new_seed()
        rng = random.Random(seed)
        for _ in range(1500):
            parts, slug = self.random_structure(rng)
            text = render_path(rng, parts, paths.IS_WINDOWS)
            with self.subTest(seed=seed, path=text):
                self.assertEqual(paths.is_memory_path(text, R), slug is not None)
                self.assertEqual(paths.slug_of(text, R) is not None, slug is not None)
                if slug is not None and not paths.IS_WINDOWS:
                    self.assertEqual(paths.slug_of(text, R), slug)

    def test_slug_preserves_case_when_unmangled(self):
        seed = new_seed()
        rng = random.Random(seed)
        for _ in range(300):
            slug = rng.choice(["Abc-Def", "x1"])
            text = rng.choice(["C:\\", "C:/"] + (["/c/"] if paths.IS_WINDOWS else [])) + "Users/sam/.claude/projects/" + slug + "/memory/a.md"
            with self.subTest(seed=seed, path=text):
                self.assertEqual(paths.slug_of(text, R), slug)


if __name__ == "__main__":
    unittest.main()
