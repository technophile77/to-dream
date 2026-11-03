"""Shared test utilities: import path setup, hermetic git environment, temp memory repos."""

import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT = TESTS_DIR.parent
LIB_DIR = PROJECT / "plugins" / "to-dream" / "lib"
for _entry in (LIB_DIR, TESTS_DIR):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

# An empty file stands in for the user's global git config so their settings (signing, autocrlf, hooks
# path...) cannot change test outcomes. Nothing here ever runs `git config`.
_EMPTY_CONFIG = Path(tempfile.mkdtemp(prefix="to-dream-gitcfg-")) / "gitconfig"
_EMPTY_CONFIG.write_text("", encoding="utf-8")

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": str(_EMPTY_CONFIG),
    "GIT_CONFIG_NOSYSTEM": "1",
}
GITIGNORE = "/*\n!/.gitignore\n!/*/\n/*/*\n!/*/memory/\n"


def new_seed() -> int:
    """Pick a fresh random seed (printed by tests so failures can be reproduced)."""
    return int(time.time() * 1000) % 2**31


def clean_env(**extra: str) -> dict:
    """Return a subprocess environment with git identity and any extra variables."""
    env = {**os.environ, **GIT_IDENTITY, **extra}
    for var in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        env.pop(var, None)
    return env


def git(root, *args: str, env=None) -> str:
    """Run git in ``root`` and return stdout text (raises on failure)."""
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, env=env or clean_env())
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8", "replace")


def make_memory_repo(root, branch: str = "memory") -> None:
    """Create a git repo at ``root`` laid out like ``~/.claude/projects`` (tracks only ``*/memory/``) with a first commit."""
    git(root, "init", "-q", "-b", branch)
    (Path(root) / ".gitignore").write_text(GITIGNORE)
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "chore: init")


def install_hook(root, body: str, name: str = "pre-commit") -> Path:
    """Install a user-style git hook script (``body`` is POSIX sh) in the repo at ``root``; return its path."""
    hook = Path(root) / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\n" + body, newline="\n")
    hook.chmod(0o755)
    return hook


def render_path(rng: random.Random, parts: list[str], is_windows: bool) -> str:
    """Render ``parts`` (below the drive) as a random Windows-ish spelling of an absolute path.

    Forms: ``C:\\``, ``C:/``, mixed separators, MSYS ``/c/``, ``~``, ``$HOME``, with ``.``/``..`` detours,
    trailing separators and (on Windows) random case. Built independently of ``paths.normalize``.
    """
    body = list(parts)
    uses_home = body[:2] == ["Users", "sam"]
    form = rng.choice(["back", "fwd", "mixed"] + (["msys"] if is_windows else []) + (["tilde", "homevar"] if uses_home else []))
    if form in ("tilde", "homevar"):
        body = body[2:]
    for _ in range(rng.randint(0, 2)):
        pos = rng.randint(0, len(body))
        body[pos:pos] = rng.choice([["junk", ".."], ["."], ["a", "b", "..", ".."]])
    if is_windows and rng.random() < 0.3:
        body = [rng.choice([s.upper(), s.lower(), s.swapcase()]) for s in body]
    seps = {"back": "\\", "fwd": "/", "msys": "/", "tilde": "/", "homevar": "/"}

    def sep() -> str:
        return rng.choice(["\\", "/"]) if form == "mixed" else seps[form]

    drive = rng.choice(["C", "c"])
    head = {
        "back": f"{drive}:\\", "fwd": f"{drive}:/", "mixed": f"{drive}:{sep()}",
        "msys": f"/{drive.lower()}/", "tilde": "~/", "homevar": "$HOME/",
    }[form]
    text = head + body[0]
    for part in body[1:]:
        text += sep() + part
    if rng.random() < 0.2:
        text += sep()
    return text
