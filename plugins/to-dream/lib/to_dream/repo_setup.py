"""``to-dream init-repo``: make the memory root a git repo that tracks only ``*/memory/``.

Transcripts live next to the memory folders in ``~/.claude/projects``; the ``.gitignore``
written here ignores everything except ``<project>/memory/``. An existing repo is verified
and reported on, never rewritten.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from . import redact, state

DEFAULT_BRANCH = "memory"
GITIGNORE_LINES = ("/*", "!/.gitignore", "!/.gitattributes", "!/*/", "/*/*", "!/*/memory/")
GITIGNORE_TEXT = "# Managed by to-dream: track only <project>/memory/, never transcripts.\n" \
    + "\n".join(GITIGNORE_LINES) + "\n"
GITATTRIBUTES_TEXT = "* -text\n"
COMMIT_MESSAGE = "chore: initial snapshot of auto memory (to-dream init-repo)"
_TRACKED_OK = re.compile(r"^(\.gitignore|\.gitattributes|[^/]+/memory/.+)$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def run_git(args: list[str], cwd: Path, env: Optional[dict] = None) -> "subprocess.CompletedProcess[str]":
    """Run ``git <args>`` in ``cwd`` capturing text output; a missing git yields returncode 127."""
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env)
    except FileNotFoundError:
        return subprocess.CompletedProcess(["git", *args], 127, "", "git not found")


def has_repo(root: Path) -> bool:
    """True when ``root`` itself holds a ``.git`` (a parent directory's repo doesn't count)."""
    return (root / ".git").exists()


def current_branch(root: Path, git: Runner = run_git) -> str:
    """Return the branch HEAD points at in ``root`` (works before the first commit); '' if unknown."""
    result = git(["symbolic-ref", "--short", "HEAD"], root)
    return result.stdout.strip() if result.returncode == 0 else ""


def layout_problems(root: Path, git: Runner = run_git) -> list[str]:
    """Return human-readable problems with an existing memory repo ([] means the layout is right)."""
    problems: list[str] = []
    try:
        ignore_lines = {ln.strip() for ln in (root / ".gitignore").read_text(encoding="utf-8").splitlines()}
    except OSError:
        ignore_lines = set()
    missing = [ln for ln in GITIGNORE_LINES if ln not in ignore_lines]
    if missing:
        problems.append(".gitignore is missing or lacks: " + " ".join(missing))
    try:
        attributes = (root / ".gitattributes").read_text(encoding="utf-8")
    except OSError:
        attributes = ""
    if not re.search(r"^\*\s+-text\s*$", attributes, re.MULTILINE):
        problems.append(".gitattributes is missing or lacks '* -text'")
    tracked = git(["ls-files", "-z"], root)
    if tracked.returncode == 0:
        stray = [p for p in tracked.stdout.split("\0") if p and not _TRACKED_OK.match(p)]
        if stray:
            problems.append("tracked files outside <project>/memory/: %s%s" % (
                ", ".join(stray[:5]), " ..." if len(stray) > 5 else ""))
    return problems


def _identity_env(root: Path, git: Runner) -> Optional[dict]:
    """Return an environment supplying a commit identity only when git has none configured."""
    if git(["config", "user.email"], root).stdout.strip() and git(["config", "user.name"], root).stdout.strip():
        return None
    env = dict(os.environ)
    for who in ("AUTHOR", "COMMITTER"):
        env.setdefault("GIT_%s_NAME" % who, "to-dream")
        env.setdefault("GIT_%s_EMAIL" % who, "to-dream@localhost")
    return env


def _check_branch_name(branch: str, root: Path, git: Runner) -> None:
    if git(["check-ref-format", "--branch", branch], root).returncode != 0:
        raise ValueError("invalid branch name: %r" % branch)


def memory_files(root: Path) -> list[str]:
    """List the files the first commit would hold: everything under ``<project>/memory/`` (``/`` separators)."""
    found: list[str] = []
    if not root.is_dir():
        return found
    for project in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        memory = project / "memory"
        if memory.is_dir():
            found += [f.relative_to(root).as_posix() for f in sorted(memory.rglob("*")) if f.is_file()]
    return found


class SecretsFound(RuntimeError):
    """Raised before anything is staged when existing memory files look like they contain secrets."""

    def __init__(self, findings: list) -> None:
        super().__init__("possible secrets in existing memory files")
        self.findings = findings


def create_repo(root: Path, branch: str = DEFAULT_BRANCH, git: Runner = run_git) -> None:
    """Initialise ``root`` as a memory repo on ``branch`` with the standard files and one commit.

    Raises:
        ValueError: if ``branch`` is not a valid branch name.
        SecretsFound: if existing memory files trip the secret scan (nothing is initialised or staged).
        RuntimeError: if a git command fails (the message includes git's error output).
    """
    _check_branch_name(branch, root, git)
    findings = redact.scan_files(root, memory_files(root))
    if findings:
        raise SecretsFound(findings)

    def must(args: list[str], env: Optional[dict] = None) -> None:
        result = git(args, root, env) if env is not None else git(args, root)
        if result.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (args[0], (result.stderr or result.stdout).strip()))

    must(["init"])
    must(["symbolic-ref", "HEAD", "refs/heads/" + branch])
    (root / ".gitignore").write_text(GITIGNORE_TEXT, encoding="utf-8", newline="\n")
    (root / ".gitattributes").write_text(GITATTRIBUTES_TEXT, encoding="utf-8", newline="\n")
    must(["add", "-A"])
    must(["commit", "-m", COMMIT_MESSAGE], _identity_env(root, git))


def init_repo(root: Path, branch: str = DEFAULT_BRANCH, git: Runner = run_git) -> tuple[int, list[str]]:
    """Create the memory repo, or verify an existing one; returns ``(exit_code, report_lines)``.

    An existing repo is never modified. Exit code is 0 when created or the layout is right,
    1 when an existing repo has layout problems or the first commit would contain secrets, 2 when creation fails.

    Example:
        >>> code, lines = init_repo(Path.home() / ".claude" / "projects")
        >>> code
        0
    """
    if has_repo(root):
        current = current_branch(root, git)
        lines = ["Memory repo already exists at %s (branch: %s). Left untouched." % (root, current or "unknown")]
        problems = layout_problems(root, git)
        lines += ["  problem: " + p for p in problems]
        if problems:
            lines.append("Fix these by hand; to-dream never rewrites an existing repo.")
        return (1 if problems else 0), lines
    try:
        root.mkdir(parents=True, exist_ok=True)
        create_repo(root, branch, git)
    except SecretsFound as exc:
        return 1, ["init-repo refused: possible secrets in files it would commit (values not shown):"]             + ["  %s:%d %s" % (f.path, f.line, f.rule) for f in exc.findings]             + ["Nothing was staged or committed. Remove the secret, or append '%s' to a line that is a "
               "false positive, then run init-repo again." % redact.ALLOW_MARKER]
    except (ValueError, RuntimeError, OSError) as exc:
        return 2, ["init-repo failed: %s" % exc]
    return 0, ["Created memory repo at %s on branch %s." % (root, branch),
               "It tracks only <project>/memory/ folders; transcripts stay untracked. Nothing is pushed."]


def _handle(args) -> int:
    code, lines = init_repo(Path(state.memory_root()), args.branch)
    print("\n".join(lines), file=sys.stderr if code == 2 else sys.stdout)
    return code


def add_parser(subparsers) -> None:
    """Register the ``init-repo`` subcommand."""
    parser = subparsers.add_parser(
        "init-repo", help="make ~/.claude/projects a git repo that tracks only */memory/ (or verify one)")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="branch name for a new repo (default: memory)")
    parser.set_defaults(func=_handle)
