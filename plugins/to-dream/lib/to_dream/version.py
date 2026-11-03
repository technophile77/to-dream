"""Version and repository lookup for the running to-dream copy.

The same code runs from two layouts:

* the installed plugin: ``<plugin>/.claude-plugin/plugin.json`` next to ``lib/``
* the runtime copy:     ``$TO_DREAM_HOME/runtime/{VERSION,plugin.json}`` next to ``lib/``
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

PLUGIN_NAME = "to-dream"
DEFAULT_REPOSITORY = "https://github.com/technophile77/to-dream"

_SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")

# lib/to_dream/version.py -> lib/to_dream -> lib -> <plugin root or runtime dir>
ROOT = Path(__file__).resolve().parents[2]


def parse_semver(text: str) -> Optional[tuple]:
    """Parse ``MAJOR.MINOR.PATCH[-pre][+build]`` (optional leading ``v``); None if invalid."""
    match = _SEMVER.match(text.strip()) if isinstance(text, str) else None
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), pre.split(".") if pre else []


def _compare_identifiers(a: list[str], b: list[str]) -> int:
    """Compare pre-release identifier lists per semver 2.0 section 11."""
    if not a and not b:
        return 0
    if not a:
        return 1  # a release outranks any of its pre-releases
    if not b:
        return -1
    for left, right in zip(a, b):
        if left == right:
            continue
        left_num, right_num = left.isdigit(), right.isdigit()
        if left_num and right_num:
            return -1 if int(left) < int(right) else 1
        if left_num != right_num:
            return -1 if left_num else 1  # numeric identifiers sort before alphanumeric ones
        return -1 if left < right else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0 or 1 as ``a`` is older than, equal to, or newer than ``b`` (semver).

    Raises:
        ValueError: if either string is not a valid semantic version.

    Example:
        >>> compare_versions("0.2.0", "0.10.0")
        -1
        >>> compare_versions("1.0.0-rc.1", "1.0.0")
        -1
    """
    left, right = parse_semver(a), parse_semver(b)
    if left is None or right is None:
        raise ValueError("not a semantic version: %r" % (a if left is None else b))
    if left[:3] != right[:3]:
        return -1 if left[:3] < right[:3] else 1
    return _compare_identifiers(left[3], right[3])


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
