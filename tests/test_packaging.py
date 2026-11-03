"""Packaging tests: manifests, skills, the bin shim, README header and absence of personal paths."""
import json
import re
import unittest

from td_testing import PLUGIN_ROOT, REPO_ROOT

# Built from pieces so this file does not trip its own scan.
FORBIDDEN = ["E:" + "/MyData", "E:" + "\\MyData", "E:" + "\\\\MyData", "C:" + "/Users/Alex",
             "C:" + "\\Users\\Alex", "technophile77/" + "claude-memory"]
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules"}

README_HEADER = (
    "# \U0001F30C In Memory of Sir Patrick Stewart (1940–2026)\n"
    "\n"
    "> *\"To die, to sleep— / To sleep, perchance to dream—ay, there's the rub, / "
    "For in that sleep of death what dreams may come...\"*  \n"
    "> — **William Shakespeare**, *Hamlet* (Act III, Scene 1)\n"
    "\n"
    "---\n"
    "\n"
    "# to-dream\n")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.marketplace = load(REPO_ROOT / ".claude-plugin" / "marketplace.json")
        self.plugin = load(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")

    def test_names_match(self):
        self.assertEqual(self.plugin["name"], "to-dream")
        self.assertEqual(self.marketplace["name"], "to-dream")
        self.assertEqual([p["name"] for p in self.marketplace["plugins"]], [self.plugin["name"]])

    def test_marketplace_source_points_at_the_plugin_dir(self):
        entry = self.marketplace["plugins"][0]
        self.assertEqual(entry["source"], "./plugins/to-dream")
        self.assertTrue((REPO_ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file())

    def test_version_lives_only_in_plugin_json(self):
        self.assertNotIn("version", self.marketplace)
        for entry in self.marketplace["plugins"]:
            self.assertNotIn("version", entry)
        self.assertRegex(self.plugin["version"], r"^\d+\.\d+\.\d+$")

    def test_plugin_metadata(self):
        self.assertEqual(self.plugin["license"], "MIT")
        self.assertEqual(self.plugin["repository"], "https://github.com/technophile77/to-dream")
        self.assertEqual(self.plugin["author"], {"name": "Alex Cresswell"})
        self.assertTrue(self.plugin["description"])

    def test_changelog_has_current_version(self):
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(changelog, r"(?m)^## %s\b" % re.escape(self.plugin["version"]))

    def test_license_names_the_author(self):
        text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) 2026 Alex Cresswell", text)


class SkillTests(unittest.TestCase):
    def test_expected_skills_exist(self):
        names = sorted(p.parent.name for p in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(names, ["review", "run", "setup"])

    def test_each_skill_has_name_and_description_frontmatter(self):
        for skill in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
            text = skill.read_text(encoding="utf-8")
            match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
            self.assertIsNotNone(match, skill)
            front = match.group(1)
            self.assertRegex(front, r"(?m)^name: %s$" % skill.parent.name, skill)
            self.assertRegex(front, r"(?m)^description: \S.{20,}", skill)

    def test_each_skill_runs_sync_runtime_and_check_update_first(self):
        for skill in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
            text = skill.read_text(encoding="utf-8")
            self.assertLess(text.index("to-dream sync-runtime"), text.index("to-dream check-update --json"), skill)
            self.assertIn("/plugin marketplace update to-dream", text)

    def test_skills_do_not_reference_the_old_tool(self):
        for skill in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
            self.assertNotIn("dreamctl", skill.read_text(encoding="utf-8"), skill)


class ShimTests(unittest.TestCase):
    def setUp(self):
        self.shim = PLUGIN_ROOT / "bin" / "to-dream"

    def test_shim_has_lf_endings_and_shebang(self):
        data = self.shim.read_bytes()
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.startswith(b"#!/usr/bin/env bash\n"))

    def test_entry_script_exists_and_has_no_crlf_surprises(self):
        self.assertTrue((PLUGIN_ROOT / "scripts" / "to_dream.py").is_file())

    def test_repo_declares_lf_for_the_shim(self):
        attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertRegex(attributes, r"(?m)^plugins/to-dream/bin/\*\s+text eol=lf")


class ReadmeTests(unittest.TestCase):
    def test_readme_starts_with_the_memorial_block_exactly(self):
        data = (REPO_ROOT / "README.md").read_bytes()
        self.assertTrue(data.decode("utf-8").startswith(README_HEADER))

    def test_readme_credits_and_disclaims(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Learning while you sleep: Beyond memory to dreaming", text)
        self.assertIn("AI Native DevCon", text)
        self.assertIn("not affiliated with Anthropic", text)
        for step in ("/plugin marketplace add technophile77/to-dream", "/plugin install to-dream@to-dream",
                     "/to-dream:setup"):
            self.assertIn(step, text)


class NoPersonalPathsTests(unittest.TestCase):
    def test_no_file_contains_personal_paths_or_private_repo_names(self):
        offenders = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            offenders += ["%s: %s" % (path.relative_to(REPO_ROOT), bad) for bad in FORBIDDEN if bad in text]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
