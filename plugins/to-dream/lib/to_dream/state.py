"""Locations and persistent state of to-dream (``$TO_DREAM_HOME/state.json``).

Two roots drive everything:

* ``home()``: to-dream's own directory (``$TO_DREAM_HOME``, default ``~/.claude/to-dream``) with
  ``state.json``, ``runs/<run-id>/``, ``rejected.md``, ``config.json``, ``logs/`` and ``worktrees/``.
* ``memory_root()``: the git repo that tracks ``<project>/memory/**``
  (``$TO_DREAM_MEMORY_ROOT``, default ``~/.claude/projects``).

State layout::

    {"last_dream": {"<slug>": "<ISO-8601 UTC>"},
     "runs": {"<run-id>": {"slug": ..., "branch": ..., "base_branch": ..., "status": ..., ...}}}

Other top-level keys (for example ``update_notice``) are preserved untouched.
"""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ENV_HOME = "TO_DREAM_HOME"
ENV_MEMORY_ROOT = "TO_DREAM_MEMORY_ROOT"
STATE_FILE = "state.json"
CONFIG_FILE = "config.json"
REJECTED_FILE = "rejected.md"
STEERING_FILE = "steering.md"
RUNS_DIRNAME = "runs"
WORKTREES_DIRNAME = "worktrees"
LOGS_DIRNAME = "logs"
REPLACE_ATTEMPTS = 20
REPLACE_DELAY_S = 0.01

State = dict[str, Any]


class StateError(RuntimeError):
    """Raised when ``state.json`` exists but cannot be parsed (it is never silently overwritten)."""


def home() -> Path:
    """Return to-dream's state directory: ``$TO_DREAM_HOME`` if set, else ``~/.claude/to-dream``.

    Example::

        os.environ["TO_DREAM_HOME"] = "/tmp/td"
        home()  # PosixPath('/tmp/td')
    """
    custom = os.environ.get(ENV_HOME)
    return Path(custom).expanduser() if custom else Path.home() / ".claude" / "to-dream"


def memory_root() -> Path:
    """Return the memory root / git repo: ``$TO_DREAM_MEMORY_ROOT`` if set, else ``~/.claude/projects``."""
    custom = os.environ.get(ENV_MEMORY_ROOT)
    return Path(custom).expanduser() if custom else Path.home() / ".claude" / "projects"


def claude_home() -> Path:
    """Return the Claude home directory: the parent of the memory root (``~/.claude``)."""
    return memory_root().parent


def runs_dir() -> Path:
    """Return the directory holding one sub-directory per run."""
    return home() / RUNS_DIRNAME


def run_dir(run_id: str) -> Path:
    """Return the directory of run ``run_id`` (digests, prompt, output, log, report)."""
    return runs_dir() / run_id


def worktrees_dir() -> Path:
    """Return the default directory for dream worktrees (``$TO_DREAM_HOME/worktrees``)."""
    return home() / WORKTREES_DIRNAME


def logs_dir() -> Path:
    """Return the directory for scheduled-job logs (``$TO_DREAM_HOME/logs``)."""
    return home() / LOGS_DIRNAME


def state_path() -> Path:
    """Return the path of ``state.json``."""
    return home() / STATE_FILE


def config_path() -> Path:
    """Return the path of ``config.json``."""
    return home() / CONFIG_FILE


def rejected_log_path() -> Path:
    """Return the path of ``rejected.md``, the log of proposals the user turned down."""
    return home() / REJECTED_FILE


def steering_override_path() -> Path:
    """Return ``$TO_DREAM_HOME/steering.md``, the optional override of the bundled steering file."""
    return home() / STEERING_FILE


def format_ts(moment: datetime) -> str:
    """Render an aware datetime as UTC ISO-8601 with second precision.

    >>> format_ts(datetime(2026, 11, 2, 18, 30, tzinfo=timezone.utc))
    '2026-11-02T18:30:00+00:00'
    """
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(text: str) -> datetime:
    """Parse an ISO-8601 string written by :func:`format_ts` (naive input is taken as UTC)."""
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _read_with_retry(target: Path) -> str:
    """Read a text file, retrying briefly on PermissionError (Windows, while a writer is replacing it)."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            return target.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_DELAY_S * (attempt + 1))
    raise AssertionError("unreachable")


def _replace_with_retry(source: str, target: Path) -> None:
    """``os.replace`` that retries briefly: on Windows a concurrent reader can make it raise PermissionError."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_DELAY_S * (attempt + 1))


def write_json_atomic(data: Any, target: Path, prefix: str = ".tmp-") -> None:
    """Write ``data`` as pretty JSON to ``target`` atomically (temp file, fsync, ``os.replace``).

    A crash leaves either the old or the new file, never a torn one; a failed write leaves no temp file.

    Example::

        write_json_atomic({"a": 1}, home() / "x.json")
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_json_file(target: Path) -> Any:
    """Read and parse a JSON file with the Windows retry; a missing file raises ``FileNotFoundError``.

    Raises:
        ValueError: the file is not valid JSON (``json.JSONDecodeError`` is a subclass).
    """
    return json.loads(_read_with_retry(target))


def load_state(path: Path | None = None) -> State:
    """Read the state file; a missing file yields an empty state.

    Raises:
        StateError: the file exists but is not a JSON object.
    """
    target = path or state_path()
    try:
        raw = _read_with_retry(target)
    except FileNotFoundError:
        return {"last_dream": {}, "runs": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(f"{target} is corrupt ({exc}); fix or delete it") from exc
    if not isinstance(data, dict):
        raise StateError(f"{target} is corrupt (not a JSON object); fix or delete it")
    data.setdefault("last_dream", {})
    data.setdefault("runs", {})
    return data


def save_state(state: State, path: Path | None = None) -> None:
    """Write the state atomically (see :func:`write_json_atomic`)."""
    write_json_atomic(state, path or state_path(), prefix=".state-")


def update_state(change: Callable[[State], None], path: Path | None = None) -> State:
    """Reload the state, apply ``change`` in place, save atomically and return the result.

    Reloading right before writing keeps the lost-update window to a few milliseconds when a
    nightly run and a manual ``apply`` overlap.

    Example::

        update_state(lambda s: set_last_dream(s, "my-slug", datetime.now(timezone.utc)))
    """
    state = load_state(path)
    change(state)
    save_state(state, path)
    return state


def get_last_dream(state: State, slug: str) -> datetime | None:
    """Return when ``slug`` last dreamed successfully, or None if it never has."""
    value = state.get("last_dream", {}).get(slug)
    return parse_ts(value) if value else None


def set_last_dream(state: State, slug: str, moment: datetime) -> None:
    """Record ``moment`` (the run's window end) as ``slug``'s last successful dream."""
    state.setdefault("last_dream", {})[slug] = format_ts(moment)


def set_run(state: State, run_id: str, **fields: Any) -> None:
    """Create or update the record of ``run_id`` with ``fields``."""
    state.setdefault("runs", {}).setdefault(run_id, {}).update(fields)
