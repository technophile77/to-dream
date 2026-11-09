"""Test fixtures for the dream pipeline: temp state + memory repo, a stub distiller and a fake claude."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from helpers import GIT_IDENTITY, git, make_memory_repo

from to_dream import runner

FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"
BASE_BRANCH = "main"  # deliberately not "memory": no branch name may be hardcoded
SEED_FILES = {
    "MEMORY.md": "# Memory\n- [A](a.md)\n- [B](b.md)\n",
    "a.md": "alpha fact\n",
    "b.md": "beta fact\n",
}


@dataclass(frozen=True)
class StubDigest:
    """Minimal stand-in for ``to_dream.distill.SessionDigest``."""

    session_id: str
    chars: int = 1000
    human_prompts: int = 3
    corrections: int = 1
    tool_errors: int = 0
    denials: int = 0
    memory_writes: int = 0


class StubDistill:
    """Replacement for the ``to_dream.distill`` module: returns canned digests and records its calls."""

    def __init__(self, project_dir: str, count: int = 2) -> None:
        self.project_dir = project_dir
        self.count = count
        self.calls: list[dict] = []

    def distill_project(self, project_dir, out_dir, since=None, until=None, max_chars=20000, max_sessions=None,
                        stats=None, permissions_review=True):
        """Record the call, write one digest file per session and return the digests."""
        self.calls.append({"project_dir": project_dir, "out_dir": out_dir, "since": since, "until": until,
                           "max_sessions": max_sessions, "permissions_review": permissions_review})
        digests = [StubDigest(f"s{i}") for i in range(self.count)]
        for d in digests:
            (Path(out_dir) / f"{d.session_id}.md").write_text("digest\n", encoding="utf-8")
        return digests

    def write_index(self, digests, out_dir, permissions_review=True):
        """Write and return a trivial INDEX.md."""
        self.index_permissions_review = permissions_review
        path = Path(out_dir) / "INDEX.md"
        path.write_text("\n".join(d.session_id for d in digests), encoding="utf-8")
        return path

    def project_cwd(self, digests):
        """Return the fake project source dir."""
        return self.project_dir


def make_fake_claude_shim(directory: Path) -> Path:
    """Create an executable shim that runs ``fake_claude.py`` with the current Python; return its path."""
    if os.name == "nt":
        shim = directory / "fake-claude.cmd"
        shim.write_text(f'@"{sys.executable}" "{FAKE_CLAUDE}" %*\r\n', encoding="utf-8")
    else:
        shim = directory / "fake-claude.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n', encoding="utf-8", newline="\n")
        shim.chmod(0o755)
    return shim


class DreamTestCase(unittest.TestCase):
    """Base class: temp state dir plus a memory repo seeded with one project, env wired to temp dirs.

    Attributes set up for tests: ``root`` (memory repo, branch ``BASE_BRANCH``), ``state_home``
    (``TO_DREAM_HOME``), ``project_dir`` / ``slug`` / ``slug_short`` (a project below the fake home),
    ``memory`` (its live memory dir), ``seed_head`` (sha of the seed commit).
    """

    USE_STUB_DISTILL = True

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.root = self.home / ".claude" / "projects"
        self.state_home = self.tmp / "state"
        self.root.mkdir(parents=True)
        make_memory_repo(self.root, BASE_BRANCH)
        self.project_dir = self.home / "code" / "st-timelines"
        self.slug = runner.slug_from_path(str(self.project_dir))
        self.slug_short = "code-st-timelines"
        self.memory = self.root / self.slug / "memory"
        self.memory.mkdir(parents=True)
        for name, text in SEED_FILES.items():
            (self.memory / name).write_text(text, encoding="utf-8", newline="\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "seed memory")
        self.seed_head = git(self.root, "rev-parse", "HEAD").strip()

        self.shim = make_fake_claude_shim(self.tmp)
        self.plan_path = self.tmp / "plan.json"
        self.claude_log = self.tmp / "claude-call.json"
        patcher = mock.patch.dict(os.environ, {
            **GIT_IDENTITY, "HOME": str(self.home), "USERPROFILE": str(self.home),
            "TO_DREAM_HOME": str(self.state_home), "TO_DREAM_MEMORY_ROOT": str(self.root),
            "TO_DREAM_CLAUDE_CMD": str(self.shim), "FAKE_CLAUDE_PLAN": str(self.plan_path),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.distill = StubDistill(str(self.project_dir))
        if self.USE_STUB_DISTILL:
            distill_patch = mock.patch.object(runner, "load_distill", lambda: self.distill)
            distill_patch.start()
            self.addCleanup(distill_patch.stop)

    def plan(self, **plan) -> None:
        """Write the fake claude's plan (and have it log its invocation)."""
        plan.setdefault("log", str(self.claude_log))
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def claude_call(self) -> dict:
        """Return what the fake claude recorded about its last invocation."""
        return json.loads(self.claude_log.read_text(encoding="utf-8"))

    def branches(self) -> list[str]:
        """List local branch names of the live repo."""
        return git(self.root, "branch", "--format=%(refname:short)").split()

    def subjects(self, count: int = 10) -> list[str]:
        """Return the subjects of the newest ``count`` commits of the live repo, newest first."""
        return git(self.root, "log", f"-{count}", "--format=%s").splitlines()
