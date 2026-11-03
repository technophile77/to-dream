"""``to-dream sync-runtime``: keep a stable copy of the code for scheduled jobs.

``${CLAUDE_PLUGIN_ROOT}`` changes on every plugin update, so the nightly job never points
at it. Instead the code is copied to ``$TO_DREAM_HOME/runtime/`` and stamped with the
plugin version (``VERSION``). The copy is refreshed whenever its version or file contents differ
from the source, so any command run after an update (or a local edit) repairs it.

The copy is never downgraded by default. Two Claude Code sessions can run different plugin
versions at once; if the runtime's ``VERSION`` is valid semver and strictly newer than this
plugin's, ``sync`` leaves it alone and the CLI prints a one-line note to stderr (stdout stays
exactly the runtime path, exit code 0)::

    $ to-dream sync-runtime
    to-dream: runtime is 0.1.2, newer than this plugin (0.1.1); left as is. Update this ...
    /home/u/.to-dream/runtime

``to-dream sync-runtime --force`` (``sync(force=True)``) downgrades on purpose, e.g. a rollback.
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


def newer_runtime(root: Path, target: Path) -> Optional[str]:
    """Return the runtime's stamped version when it is strictly newer than the code under ``root``.

    Returns None when either version is not valid semver, the runtime has no ``VERSION``
    stamp, or the runtime is the same age or older, so only a provable downgrade is reported.

    Example:
        >>> newer_runtime(plugin_0_1_1, runtime_0_1_2)
        '0.1.2'
        >>> newer_runtime(plugin_0_1_2, runtime_0_1_1) is None
        True
    """
    stamped = _read(target / "VERSION")
    source = version.current_version(root)
    if version.parse_semver(stamped) is None or version.parse_semver(source) is None:
        return None
    return stamped if version.compare_versions(stamped, source) > 0 else None


def sync(root: Optional[Path] = None, home: Optional[Path] = None, force: bool = False) -> tuple[Path, bool]:
    """Copy scripts/, lib/ and plugin.json under ``root`` into ``<home>/runtime``.

    Returns ``(runtime_path, changed)``; ``changed`` is False when the copy was already
    current, when ``root`` already is the runtime copy, or when the copy was left alone
    because it is newer than ``root``.

    Never downgrades by default: a session still running an old plugin version must not
    overwrite the runtime that a newer session installed for the nightly job. Pass
    ``force=True`` to downgrade on purpose (a rollback). A same-version content difference,
    an older runtime, or an unparseable version on either side still refreshes. Use
    :func:`newer_runtime` to find out whether a call was skipped for that reason.

    Example:
        >>> path, changed = sync()   # first call after install  -> changed is True
        >>> sync()[1]                # nothing new                -> False
        False
        >>> # runtime holds 0.1.2, this plugin is 0.1.1:
        >>> sync()[1]                # skipped, runtime untouched -> False
        False
        >>> sync(force=True)[1]      # deliberate rollback        -> True
        True
    """
    root = Path(root if root is not None else version.ROOT).resolve()
    target = runtime_dir(home)
    if root == target.resolve() or not (root / "scripts").is_dir():
        return target, False
    if is_current(root, target):
        return target, False
    if not force and newer_runtime(root, target) is not None:
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


def skip_note(newer: str, own: str) -> str:
    """Return the one-line stderr message for a skipped downgrade."""
    return ("to-dream: runtime is %s, newer than this plugin (%s); left as is. Update this session with "
            "/plugin marketplace update to-dream then /reload-plugins, or pass --force to downgrade."
            % (newer, own))


def _handle(args) -> int:
    force = bool(getattr(args, "force", False))
    try:
        path, changed = sync(force=force)
    except OSError as exc:
        print("to-dream: could not sync runtime: %s" % exc, file=sys.stderr)
        return 1
    if not changed and not force:
        source = version.ROOT.resolve()
        newer = newer_runtime(source, path)
        if newer is not None:
            print(skip_note(newer, version.current_version(source)), file=sys.stderr)
    print(path)  # stdout is exactly the path: skills capture it
    return 0


def add_parser(subparsers) -> None:
    """Register the ``sync-runtime`` subcommand."""
    parser = subparsers.add_parser(
        "sync-runtime",
        help="copy the code into $TO_DREAM_HOME/runtime (used by scheduled jobs) and print its path")
    parser.add_argument("--force", action="store_true",
                        help="overwrite the runtime even if it is newer than this plugin (rollback)")
    parser.set_defaults(func=_handle)
