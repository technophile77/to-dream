"""Pure path helpers: decide whether a path points into one of Claude Code's auto-memory directories.

Memory files live in ``<root>/<slug>/memory/``, where ``<root>`` is the memory root
(``~/.claude/projects`` by default). The logic is plain string work and behaves identically on every OS:
separators are unified, ``~``/``$HOME`` are expanded, and the *root* decides the comparison rules. A
Windows-style root or path (``C:/...``) matches case-insensitively and also accepts MSYS spellings (``/c/...``)
of the same location; a POSIX root (``/home/u/...``) matches case-sensitively.
"""

import os
import posixpath
import re

MEMORY_DIR = "memory"

_HOME_PREFIX = re.compile(
    r"^(?:~|\$\{?HOME\}?|\$\{?USERPROFILE\}?|%USERPROFILE%|\$env:USERPROFILE|\$env:HOME)(?=[\\/]|$)",
    re.IGNORECASE,
)
_DRIVE = re.compile(r"^([A-Za-z]):(?=/|$)")
_MSYS_DRIVE = re.compile(r"^/(?:cygdrive/)?([A-Za-z])(?=/|$)")


def _home() -> str:
    """Return the user's home directory, preferring ``HOME`` (what ``~`` means in Git Bash)."""
    return os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")


def normalize(path: str) -> str:
    """Turn any supported spelling of a path into a canonical forward-slash form (OS independent).

    Expands ``~`` / ``$HOME``, unifies separators and resolves ``.`` / ``..``. Case is preserved.

    >>> normalize("C:/Users/sam/.claude/../x/")
    'C:/Users/sam/x'
    >>> normalize("/home/sam/./proj/")
    '/home/sam/proj'
    """
    text = path.strip().strip("\"'")
    text = _HOME_PREFIX.sub(lambda _m: _home(), text, count=1)
    return posixpath.normpath(text.replace("\\", "/"))


def _parts(path: str, root_is_drive: bool) -> list[str]:
    """Split a normalised path into comparison components (case-folded when the root is a drive path).

    With a drive root, MSYS spellings (``/c/x``) are first rewritten to ``c:/x``.
    """
    if root_is_drive:
        path = _MSYS_DRIVE.sub(lambda m: m.group(1) + ":", path, count=1)
    return path.split("/")


def _key(text: str, fold: bool) -> str:
    """Return the comparison key for a path component."""
    return text.casefold() if fold else text


def _split_memory(path: str, root: str) -> list[str] | None:
    """Return ``[slug, "memory", *rest]`` (original case) if path is in a memory dir, else None."""
    norm_root, norm_path = normalize(root), normalize(path)
    fold = bool(_DRIVE.match(norm_root) or _DRIVE.match(norm_path))  # a drive path on either side: Windows rules
    root_parts = _parts(norm_root, fold)
    path_parts = _parts(norm_path, fold)
    depth = len(root_parts)
    if len(path_parts) < depth + 2:
        return None
    if [_key(p, fold) for p in path_parts[:depth]] != [_key(p, fold) for p in root_parts]:
        return None
    slug, memory = path_parts[depth], path_parts[depth + 1]
    if not slug or slug == ".." or _key(memory, fold) != MEMORY_DIR:
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
