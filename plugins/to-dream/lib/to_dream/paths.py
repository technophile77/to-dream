"""Pure path helpers: decide whether a path points into one of Claude Code's auto-memory directories.

Memory files live in ``<root>/<slug>/memory/``, where ``<root>`` is the memory root
(``~/.claude/projects`` by default). Paths reach the digester in many spellings (``C:\\...``,
``C:/...``, ``/c/...``, ``~/...``, ``$HOME/...``); everything here normalises them before comparing.
Comparison is case-insensitive on Windows and case-sensitive elsewhere.
"""

import os
import posixpath
import re

IS_WINDOWS = os.name == "nt"

MEMORY_DIR = "memory"

_HOME_PREFIX = re.compile(
    r"^(?:~|\$\{?HOME\}?|\$\{?USERPROFILE\}?|%USERPROFILE%|\$env:USERPROFILE|\$env:HOME)(?=[\\/]|$)",
    re.IGNORECASE,
)
_MSYS_DRIVE = re.compile(r"^/(?:cygdrive/)?([A-Za-z])(?=/|$)")


def _home() -> str:
    """Return the user's home directory, preferring ``HOME`` (what ``~`` means in Git Bash)."""
    return os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")


def normalize(path: str) -> str:
    """Turn any supported spelling of a path into a canonical forward-slash form.

    Expands ``~`` / ``$HOME``, converts MSYS ``/c/x`` to ``c:/x`` (Windows only), unifies separators
    and resolves ``.`` / ``..``. Case is preserved; comparisons go through :func:`_key`.

    >>> normalize(r"C:\\Users\\sam\\.claude\\..\\x\\")
    'C:/Users/sam/x'
    """
    text = path.strip().strip("\"'")
    text = _HOME_PREFIX.sub(lambda _m: _home(), text, count=1)
    text = text.replace("\\", "/")
    if IS_WINDOWS:
        text = _MSYS_DRIVE.sub(lambda m: m.group(1) + ":", text, count=1)
    return posixpath.normpath(text)


def _key(text: str) -> str:
    """Return the comparison key for a path component: case-folded on Windows."""
    return text.casefold() if IS_WINDOWS else text


def _split_memory(path: str, root: str) -> list[str] | None:
    """Return ``[slug, "memory", *rest]`` (original case) if path is in a memory dir, else None."""
    root_parts = normalize(root).split("/")
    path_parts = normalize(path).split("/")
    depth = len(root_parts)
    if len(path_parts) < depth + 2:
        return None
    if [_key(p) for p in path_parts[:depth]] != [_key(p) for p in root_parts]:
        return None
    slug, memory = path_parts[depth], path_parts[depth + 1]
    if not slug or slug == ".." or _key(memory) != MEMORY_DIR:
        return None
    return path_parts[depth:]


def is_memory_path(path: str, root: str) -> bool:
    """Say whether path is ``<root>/<slug>/memory`` or anything beneath it (one slug level).

    ``path`` may use any spelling; ``..`` segments are resolved first.

    >>> is_memory_path("/home/sam/.claude/projects/x/memory/a.md", "/home/sam/.claude/projects")
    True
    >>> is_memory_path("/home/sam/.claude/projects/x/notes/a.md", "/home/sam/.claude/projects")
    False
    """
    return _split_memory(path, root) is not None


def slug_of(path: str, root: str) -> str | None:
    """Return the project slug owning a memory path, or None if path is not a memory path.

    >>> slug_of("/home/sam/.claude/projects/-home-sam-x/memory/MEMORY.md", "/home/sam/.claude/projects")
    '-home-sam-x'
    """
    parts = _split_memory(path, root)
    return parts[0] if parts else None
