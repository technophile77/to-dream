"""``to-dream doctor``: check that everything to-dream needs is in place.

Each check is ``ok``, ``warn`` or ``fail`` and carries a fix hint when it isn't ok.
Exit code is 1 only when a check fails; warnings do not fail.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from . import repo_setup, runtime, schedule, state, update, version

OK, WARN, FAIL = "ok", "warn", "fail"
MIN_PYTHON = (3, 10)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class Check:
    """One doctor result: ``status`` is ok/warn/fail; ``fix`` is empty when status is ok."""
    name: str
    status: str
    detail: str
    fix: str = ""


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def check_python(info: tuple = tuple(sys.version_info[:3])) -> Check:
    """Python must be at least 3.10."""
    text = ".".join(str(n) for n in info)
    if tuple(info[:2]) >= MIN_PYTHON:
        return Check("python", OK, "Python %s" % text)
    return Check("python", FAIL, "Python %s is too old" % text, "Install Python 3.10 or newer.")


def check_git(run: Runner = _run) -> Check:
    """git must be on PATH."""
    result = run(["git", "--version"])
    if result.returncode == 0:
        return Check("git", OK, result.stdout.strip())
    return Check("git", FAIL, "git not found on PATH", "Install git (https://git-scm.com/downloads).")


def check_claude(run: Runner = _run, which: Callable[[str], Optional[str]] = shutil.which) -> Check:
    """The ``claude`` CLI must be on PATH (dreams run it headless)."""
    if not which("claude"):
        return Check("claude", FAIL, "claude not found on PATH",
                     "Install Claude Code (https://code.claude.com) and make sure `claude` is on PATH.")
    result = run(["claude", "--version"])
    if result.returncode != 0:
        return Check("claude", WARN, "claude is on PATH but `claude --version` failed",
                     "Run `claude --version` in a terminal to see the error.")
    return Check("claude", OK, result.stdout.strip() or "claude found")


def check_memory_root(root: Path) -> Check:
    """The memory root (``~/.claude/projects``) must exist."""
    if root.is_dir():
        count = sum(1 for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        return Check("memory-root", OK, "%s (%d project folders)" % (root, count))
    return Check("memory-root", FAIL, "%s does not exist" % root,
                 "Use Claude Code in a project first so it creates ~/.claude/projects, "
                 "or set TO_DREAM_MEMORY_ROOT.")


def check_memory_repo(root: Path, git: Runner = repo_setup.run_git) -> Check:
    """The memory root should be a git repo with the tracked-memory-only layout."""
    if not root.is_dir():
        return Check("memory-repo", WARN, "memory root missing", "Fix the memory-root check first.")
    if not repo_setup.has_repo(root):
        return Check("memory-repo", WARN, "not a git repo", "Run `to-dream init-repo` (or /to-dream:setup).")
    branch = repo_setup.current_branch(root, git) or "unknown"
    problems = repo_setup.layout_problems(root, git)
    if problems:
        return Check("memory-repo", WARN, "branch %s; %s" % (branch, "; ".join(problems)),
                     "Fix the layout by hand; to-dream never rewrites an existing repo.")
    return Check("memory-repo", OK, "branch %s, layout ok" % branch)


def check_runtime() -> Check:
    """The runtime copy used by scheduled jobs should match the installed plugin."""
    target = runtime.runtime_dir()
    if not target.is_dir():
        return Check("runtime", WARN, "no runtime copy yet", "Run `to-dream sync-runtime`.")
    if version.ROOT.resolve() == target.resolve() or runtime.is_current(version.ROOT, target):
        return Check("runtime", OK, "%s (version %s)" % (target, version.current_version(target)))
    newer = runtime.newer_runtime(version.ROOT, target)
    if newer is not None:
        return Check("runtime", WARN,
                     "runtime copy is %s, newer than this plugin (%s)" % (newer, version.current_version(version.ROOT)),
                     "Run `/plugin marketplace update to-dream`, then `/reload-plugins`. "
                     "`to-dream sync-runtime --force` downgrades the runtime on purpose.")
    return Check("runtime", WARN, "runtime copy is out of date", "Run `to-dream sync-runtime`.")


def check_update(info: Optional[dict] = None) -> Check:
    """Report whether a newer release is published (uses the cached lookup)."""
    info = info if info is not None else update.check_update()
    if info["status"] == "disabled":
        return Check("update", OK, "version %s (update check disabled)" % info["current"])
    if info["status"] == "unknown":
        return Check("update", OK, "version %s (latest unknown: offline?)" % info["current"])
    if info["update_available"]:
        return Check("update", WARN, "version %s; %s is out" % (info["current"], info["latest"]),
                     "Run `/plugin marketplace update to-dream`, then `/reload-plugins`.")
    return Check("update", OK, "version %s is up to date" % info["current"])


def check_schedule(info: Optional[dict] = None) -> Check:
    """The nightly schedule is optional; warn if absent so users know it is not running."""
    info = info if info is not None else schedule.status(schedule.Context())
    if info["installed"]:
        return Check("schedule", OK, "%s at %s" % (info["detail"], info["time"] or "?"))
    return Check("schedule", WARN, "no nightly schedule installed (optional)",
                 "Run `to-dream schedule install --time 03:30`.")


def run_checks() -> list[Check]:
    """Run every check against the real environment."""
    root = Path(state.memory_root())
    return [check_python(), check_git(), check_claude(), check_memory_root(root),
            check_memory_repo(root), check_runtime(), check_update(), check_schedule()]


def overall_exit_code(checks: list[Check]) -> int:
    """1 if any check failed, else 0."""
    return 1 if any(c.status == FAIL for c in checks) else 0


def render(checks: list[Check]) -> str:
    """Format checks as ``[status] name: detail`` lines with indented fix hints."""
    lines = []
    for check in checks:
        lines.append("[%s] %s: %s" % (check.status, check.name, check.detail))
        if check.fix:
            lines.append("      fix: " + check.fix)
    return "\n".join(lines)


def _handle(args) -> int:
    checks = run_checks()
    code = overall_exit_code(checks)
    if args.json:
        print(json.dumps({"ok": code == 0, "home": str(state.home()), "version": version.current_version(),
                          "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        print("to-dream %s  (state: %s)" % (version.current_version(), state.home()))
        print(render(checks))
    return code


def add_parser(subparsers) -> None:
    """Register the ``doctor`` subcommand."""
    parser = subparsers.add_parser("doctor", help="check Python, git, claude, memory repo, updates and schedule")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.set_defaults(func=_handle)
