"""Version and repository lookup for the running to-dream copy.

The same code runs from two layouts:

* the installed plugin: ``<plugin>/.claude-plugin/plugin.json`` next to ``lib/``
* the runtime copy:     ``$TO_DREAM_HOME/runtime/{VERSION,plugin.json}`` next to ``lib/``
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

PLUGIN_NAME = "to-dream"
DEFAULT_REPOSITORY = "https://github.com/technophile77/to-dream"

# lib/to_dream/version.py -> lib/to_dream -> lib -> <plugin root or runtime dir>
ROOT = Path(__file__).resolve().parents[2]


def manifest_paths(root: Path = ROOT) -> list[Path]:
    """Return the places a plugin.json may live relative to ``root``, in lookup order."""
    return [root / ".claude-plugin" / "plugin.json", root / "plugin.json"]


def read_manifest(root: Path = ROOT) -> dict:
    """Load the plugin.json next to ``root``, or ``{}`` if none is readable.

    Example:
        >>> read_manifest().get("name")
        'to-dream'
    """
    for path in manifest_paths(root):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def current_version(root: Path = ROOT) -> str:
    """Return the running version, preferring ``root/VERSION`` (runtime copy) over plugin.json.

    Returns ``"0.0.0"`` if neither can be read, so callers never have to handle ``None``.

    Example:
        >>> current_version()
        '0.1.0'
    """
    try:
        stamped = (root / "VERSION").read_text(encoding="utf-8").strip()
        if stamped:
            return stamped
    except OSError:
        pass
    version = read_manifest(root).get("version")
    return version if isinstance(version, str) and version else "0.0.0"


def repository_url(root: Path = ROOT) -> str:
    """Return the repository URL from plugin.json (no trailing slash or ``.git``).

    Falls back to the public repository when the manifest has no ``repository`` string.

    Example:
        >>> repository_url()
        'https://github.com/technophile77/to-dream'
    """
    repo: Optional[object] = read_manifest(root).get("repository")
    if isinstance(repo, dict):
        repo = repo.get("url")
    if not isinstance(repo, str) or not repo:
        return DEFAULT_REPOSITORY
    repo = repo.strip().rstrip("/")
    return repo[:-4] if repo.endswith(".git") else repo
