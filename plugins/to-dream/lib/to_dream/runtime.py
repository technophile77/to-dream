"""``to-dream sync-runtime``: keep a stable copy of the code for scheduled jobs.

``${CLAUDE_PLUGIN_ROOT}`` changes on every plugin update, so the nightly job never points
at it. Instead the code is copied to ``$TO_DREAM_HOME/runtime/`` and stamped with the
plugin version (``VERSION``). The copy is refreshed whenever its version or file contents differ
from the source, so any command run after an update (or a local edit) repairs it.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from . import state, version

RUNTIME_DIRNAME = "runtime"
CODE_DIRS = ("scripts", "lib")
MANIFEST_NAME = "plugin.json"
IGNORED_NAMES = shutil.ignore_patterns("__pycache__", "*.pyc")


def runtime_dir(home: Optional[Path] = None) -> Path:
    """Return ``<home>/runtime`` (``home`` defaults to the to-dream state directory)."""
    return Path(home if home is not None else state.home()) / RUNTIME_DIRNAME


def _source_files(root: Path) -> list[Path]:
    """List the files that make up the runtime copy, sorted for a stable hash."""
    files: list[Path] = []
    for name in CODE_DIRS:
        base = root / name
        if base.is_dir():
            files.extend(p for p in base.rglob("*") if p.is_file()
                         and "__pycache__" not in p.parts and p.suffix != ".pyc")
    manifest = next((p for p in version.manifest_paths(root) if p.is_file()), None)
    if manifest:
        files.append(manifest)
    return sorted(files)


def content_hash(root: Path) -> str:
    """Hash the relative names and bytes of every runtime file under ``root``.

    The manifest counts as ``plugin.json`` wherever it lives, so a source tree and its runtime
    copy hash the same.
    """
    digest = hashlib.sha256()
    named = [(MANIFEST_NAME if path.name == MANIFEST_NAME else path.relative_to(root).as_posix(), path)
             for path in _source_files(root)]
    for name, path in sorted(named):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_current(root: Path, target: Path) -> bool:
    """True when ``target`` holds the same version and content as the code under ``root``."""
    return (_read(target / "VERSION") == version.current_version(root)
            and all((target / name).is_dir() for name in CODE_DIRS)
            and content_hash(root) == content_hash(target))


def _replace_dir(source: Path, destination: Path) -> None:
    """Copy ``source`` over ``destination`` via a sibling temp dir, so readers see old or new."""
    staging = destination.with_name(destination.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(source, staging, ignore=IGNORED_NAMES)
    if destination.exists():
        shutil.rmtree(destination)
    os.replace(staging, destination)


def sync(root: Optional[Path] = None, home: Optional[Path] = None) -> tuple[Path, bool]:
    """Copy scripts/, lib/ and plugin.json under ``root`` into ``<home>/runtime``.

    Returns ``(runtime_path, changed)``; ``changed`` is False when the copy was already
    current, or when ``root`` already is the runtime copy.

    Example:
        >>> path, changed = sync()   # first call after install  -> changed is True
        >>> sync()[1]                # nothing new                -> False
        False
    """
    root = Path(root if root is not None else version.ROOT).resolve()
    target = runtime_dir(home)
    if root == target.resolve() or not (root / "scripts").is_dir():
        return target, False
    if is_current(root, target):
        return target, False

    target.mkdir(parents=True, exist_ok=True)
    for name in CODE_DIRS:
        if (root / name).is_dir():
            _replace_dir(root / name, target / name)
    manifest = next((p for p in version.manifest_paths(root) if p.is_file()), None)
    if manifest:
        shutil.copyfile(manifest, target / MANIFEST_NAME)
    (target / "VERSION").write_text(version.current_version(root) + "\n", encoding="utf-8", newline="\n")
    return target, True


def _handle(args) -> int:
    try:
        path, _changed = sync()
    except OSError as exc:
        print("to-dream: could not sync runtime: %s" % exc, file=sys.stderr)
        return 1
    print(path)
    return 0


def add_parser(subparsers) -> None:
    """Register the ``sync-runtime`` subcommand."""
    parser = subparsers.add_parser(
        "sync-runtime",
        help="copy the code into $TO_DREAM_HOME/runtime (used by scheduled jobs) and print its path")
    parser.set_defaults(func=_handle)
