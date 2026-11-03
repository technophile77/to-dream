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
POSIX_ROOT = "/home/sam/.claude/projects"
POSIX_ROOT_PARTS = ["home", "sam", ".claude", "projects"]


class WindowsStyleRootTests(unittest.TestCase):
    """Windows-form roots behave the same on every OS: separators, drive spellings and case are normalised."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, FAKE_HOME_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_is_memory_path_table(self):
        cases = [
            (r"C:\Users\sam\.claude\projects\x\memory\a.md", True),
            ("C:/Users/sam/.claude/projects/x/memory/a.md", True),
            ("/c/Users/sam/.claude/projects/x/memory/a.md", True),
            ("/C/Users/sam/.claude/projects/x/memory/a.md", True),
            ("/cygdrive/c/Users/sam/.claude/projects/x/memory/a.md", True),
            ("c:/USERS/sam/.CLAUDE/Projects/X/MEMORY/a.md", True),
            ("~/.claude/projects/x/memory/a.md", True),
            ("$HOME/.claude/projects/x/memory/a.md", True),
            ("${HOME}/.claude/projects/x/memory/a.md", True),
            ("C:/Users/sam/.claude/projects/x/memory/", True),
            ("C:/Users/sam/.claude/projects/x/memory", True),
            ("C:/Users/sam/.claude/projects/x/memory/sub/deep/b.md", True),
            ("C:/Users/sam/.claude/projects/x/foo/../memory/a.md", True),
            ("C:/Users/sam/.claude/projects/y/../x/memory/a.md", True),
            ("C:/Users/sam/.claude/projects/-home-sam-proj/memory/a.md", True),
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

    def test_root_may_be_given_in_any_form(self):
        for root in ["~/.claude/projects", "C:/Users/sam/.claude/projects/", "/c/Users/sam/.claude/projects", R]:
            with self.subTest(root=root):
                self.assertTrue(paths.is_memory_path(r"C:\Users\sam\.claude\projects\x\memory\a.md", root))

    def test_slug_of_table(self):
        cases = [
            ("C:/Users/sam/.claude/projects/C--code-proj/memory/MEMORY.md", "C--code-proj"),
            ("~/.claude/projects/Abc/memory/", "Abc"),
            ("/c/Users/sam/.claude/projects/p/memory/x/y.md", "p"),
            ("C:/Users/sam/.claude/projects/p/other/y.md", None),
            ("/etc/passwd", None),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(paths.slug_of(path, R), expected)

    def test_normalize_is_os_independent(self):
        self.assertEqual(paths.normalize("C:\\Users\\sam\\.claude\\..\\x\\"), "C:/Users/sam/x")
        self.assertEqual(paths.normalize("/home/sam/./proj/"), "/home/sam/proj")


class PosixRootTests(unittest.TestCase):
    def test_dash_leading_slugs(self):
        for slug in ("-home-sam-proj", "--hidden", "-srv-x"):
            with self.subTest(slug=slug):
                path = f"{POSIX_ROOT}/{slug}/memory/a.md"
                self.assertTrue(paths.is_memory_path(path, POSIX_ROOT))
                self.assertEqual(paths.slug_of(path, POSIX_ROOT), slug)
                self.assertFalse(paths.is_memory_path(f"{POSIX_ROOT}/{slug}/notes/a.md", POSIX_ROOT))

    def test_posix_matching_is_case_sensitive(self):
        self.assertFalse(paths.is_memory_path(f"{POSIX_ROOT}/-home-sam-proj/Memory/a.md", POSIX_ROOT))
        self.assertFalse(paths.is_memory_path("/home/sam/.Claude/projects/-p/memory/a.md", POSIX_ROOT))

    def test_posix_root_does_not_treat_slash_c_as_a_drive(self):
        self.assertFalse(paths.is_memory_path("/c/Users/sam/.claude/projects/x/memory/a.md", POSIX_ROOT))
        self.assertTrue(paths.is_memory_path("/c/data/projects/x/memory/a.md", "/c/data/projects"))

    def test_trailing_separators_and_detours(self):
        self.assertTrue(paths.is_memory_path(f"{POSIX_ROOT}/./-p/../-q/memory/", POSIX_ROOT + "/"))


class PathsStochasticTests(unittest.TestCase):
    """Random spellings of paths must agree with a ground truth built from the structure alone."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, FAKE_HOME_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def random_structure(rng, root_parts, slugs, outsiders):
        """Return (parts, expected_slug_or_None); ground truth comes from construction."""
        slug = rng.choice(slugs)
        rest = [rng.choice(["a.md", "MEMORY.md", "sub"]) for _ in range(rng.randint(0, 2))]
        kind = rng.choice(["mem", "mem", "other_dir", "slug_only", "outside", "memlike", "nested"])
        if kind == "mem":
            return root_parts + [slug, "memory"] + rest, slug
        if kind == "other_dir":
            return root_parts + [slug, "notes"] + rest, None
        if kind == "slug_only":
            return root_parts + [slug], None
        if kind == "memlike":
            return root_parts + [slug, "memory2"] + rest, None
        if kind == "nested":
            return root_parts + [slug, "inner", "memory"] + rest, None
        return rng.choice(outsiders) + rest, None

    def test_windows_style_spellings_agree_with_ground_truth(self):
        seed = new_seed()
        print(f"[test_windows_style_spellings] seed={seed}")
        rng = random.Random(seed)
        outsiders = [["Users", "bob", "x", "memory"], ["Users", "sam", ".claude", "x", "memory"]]
        for _ in range(1500):
            parts, slug = self.random_structure(rng, ROOT_PARTS, ["C--code-x", "proj", "A1", "p.q", "-home-sam-p"], outsiders)
            text = render_path(rng, parts)
            with self.subTest(seed=seed, path=text):
                self.assertEqual(paths.is_memory_path(text, R), slug is not None)
                found = paths.slug_of(text, R)
                self.assertEqual(found is not None, slug is not None)
                if slug is not None:
                    self.assertEqual(found.lower(), slug.lower())  # random case may have been applied to the path

    def test_posix_paths_with_dash_slugs_agree_with_ground_truth(self):
        seed = new_seed()
        print(f"[test_posix_paths_with_dash_slugs] seed={seed}")
        rng = random.Random(seed)
        slugs = ["-home-sam-proj", "-srv-x", "--hidden", "-tmp-tmpab12-code-st-timelines", "proj", "A1"]
        outsiders = [["home", "bob", "x", "memory"], ["home", "sam", ".claude", "x", "memory"],
                     ["home", "sam", ".Claude", "projects", "p", "memory"]]
        for _ in range(1500):
            parts, slug = self.random_structure(rng, POSIX_ROOT_PARTS, slugs, outsiders)
            detours = []
            for _ in range(rng.randint(0, 2)):
                detours.append(rng.choice([["junk", ".."], ["."]]))
            body = list(parts)
            for extra in detours:
                pos = rng.randint(0, len(body))
                body[pos:pos] = extra
            text = "/" + "/".join(body) + ("/" if rng.random() < 0.2 else "")
            with self.subTest(seed=seed, path=text):
                self.assertEqual(paths.is_memory_path(text, POSIX_ROOT), slug is not None)
                self.assertEqual(paths.slug_of(text, POSIX_ROOT), slug)

    def test_slug_preserves_case_when_unmangled(self):
        seed = new_seed()
        rng = random.Random(seed)
        for _ in range(300):
            slug = rng.choice(["Abc-Def", "x1"])
            text = rng.choice(["C:\\", "C:/", "/c/"]) + "Users/sam/.claude/projects/" + slug + "/memory/a.md"
            with self.subTest(seed=seed, path=text):
                self.assertEqual(paths.slug_of(text, R), slug)


if __name__ == "__main__":
    unittest.main()
