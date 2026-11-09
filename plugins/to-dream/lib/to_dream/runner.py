"""The dream pipeline: snapshot, distil transcripts, run a headless Claude in an isolated worktree, commit per file.

Live memory is only ever changed by a snapshot commit (uncommitted memory edits, committed before a run
branches or an apply picks) and by ``apply_run``. Claude edits a worktree on a proposal branch; this
module turns its edits into one commit per file (with evidence from ``DREAM_REPORT.md``). ``apply_run``
and ``discard_run`` are the only operations that cherry-pick onto the live branch or delete a dream
branch on request; the one other deletion is a successful roll-up run superseding the project's older
unreviewed runs (``supersede_run``). Nothing is ever pushed.

The branch checked out in the memory root when a run starts is its *base branch*; it is recorded in the
run and ``apply`` refuses to run while a different branch is checked out.

Example::

    summary = run_project("-home-sam-proj", RunOptions(days=7))
    print(summary.branch, [c.subject for c in summary.commits])
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath
from typing import Any, Callable, Iterable

from to_dream import config, gitops, permissions, state as dstate

MEMORY_DIRNAME = "memory"
REPORT_NAME = "DREAM_REPORT.md"
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
ORCHESTRATOR_PROMPT = PROMPT_DIR / "orchestrator.md"
DEFAULT_STEERING = PROMPT_DIR / "steering.md"
REJECTED_HEADER = (
    "# Rejected dream changes\n\n"
    "Changes the user reviewed and rejected, plus suggestions for the global CLAUDE.md that they turned down "
    "(headed \"global suggestion\") and permission rules they declined (headed \"permission suggestion\"). "
    "Do not propose them again unless new evidence contradicts the reason given.\n"
)
KIND_GLOBAL = "global"
KIND_PERMISSION = "permission"
SUGGESTION_KINDS = (KIND_GLOBAL, KIND_PERMISSION)
SUGGESTION_HEADINGS = {KIND_GLOBAL: "global suggestion", KIND_PERMISSION: "permission suggestion"}
NO_ROLLUP_NOTE = "This is not a roll-up."
REASON_DISCARDED = "discarded with run"
REASON_NONE = "no reason given"
ENTRY_CAP = 600
ENV_CLAUDE_CMD = "TO_DREAM_CLAUDE_CMD"
CLAUDE_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
STATE_UPDATE_NOTICE_KEY = "update_notice"  # state.json key (written by check-update) shown by show --json
FORMAT_DRIFT_MESSAGE = "transcript format may have changed; to-dream may need an update"
ALL_PROJECTS = "all"

PLACEHOLDERS = frozenset(
    {
        "slug", "memory_dir", "live_memory_dir", "digest_dir", "digest_index", "report_path",
        "project_dir", "global_claude_md", "steering_path", "analyst_model", "window_since",
        "window_until", "session_count", "rejected_log", "rollup_note", "permissions_path",
    }
)
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_PERMISSIONS_START = r"<!-- permissions-review:start -->"
_PERMISSIONS_END = r"<!-- permissions-review:end -->"
_PERMISSIONS_MARKER = re.compile(f"{_PERMISSIONS_START}|{_PERMISSIONS_END}")
_PERMISSIONS_MARKER_LINE = re.compile(rf"^(?:{_PERMISSIONS_START}|{_PERMISSIONS_END})[ \t]*\n", re.MULTILINE)
_PERMISSIONS_LINE_BLOCK = re.compile(rf"^{_PERMISSIONS_START}[ \t]*\n.*?^{_PERMISSIONS_END}[ \t]*\n", re.DOTALL | re.MULTILINE)
_PERMISSIONS_INLINE_BLOCK = re.compile(f"{_PERMISSIONS_START}.*?{_PERMISSIONS_END}", re.DOTALL)
_DRIVE_PREFIX = re.compile(r"^[a-z]--", re.IGNORECASE)
_UNSAFE_DIRNAME = re.compile(r'[\\/:*?"<>|\s]+')

DENIED_TOOLS = ("Bash", "PowerShell", "WebFetch", "WebSearch")
ALLOWED_TOOLS = ("Read", "Glob", "Grep", "Agent")

STATUS_PENDING = "pending"
STATUS_PARTIAL = "partially-applied"
STATUS_APPLIED = "applied"
STATUS_DISCARDED = "discarded"
STATUS_FAILED = "failed"
STATUS_EMPTY = "no-changes"
STATUS_REJECTED = "needs-attention"  # no commits, but files the secret scan refused remain
STATUS_RUNNING = "running"
STATUS_DRY_RUN = "dry-run"
STATUS_NO_SESSIONS = "no-sessions"
STATUS_FORMAT_DRIFT = "format-drift"
STATUS_SUPERSEDED = "superseded"  # replaced by a later roll-up run (``superseded_by`` names it)
FAILING_STATUSES = frozenset({STATUS_FAILED, STATUS_FORMAT_DRIFT})
INACTIVE_STATUSES = frozenset(
    {STATUS_DISCARDED, STATUS_FAILED, STATUS_EMPTY, STATUS_RUNNING, STATUS_SUPERSEDED}
)
UNREVIEWED_STATUSES = frozenset({STATUS_PENDING, STATUS_PARTIAL, STATUS_REJECTED})  # as shown by display_status
ROLLUP_STATUSES = frozenset({STATUS_PENDING, STATUS_EMPTY, STATUS_REJECTED})  # outcomes that supersede older runs


class DreamError(RuntimeError):
    """Base class for errors the CLI reports as a one-line message (exit code 2)."""


class PreflightError(DreamError):
    """Raised when the environment cannot support a run (no memory repo, detached HEAD, no claude)."""


class TemplateError(DreamError):
    """Raised when a prompt template has an unknown placeholder or a placeholder without a value."""


class RunError(DreamError):
    """Raised for bad run ids, unusable arguments and refused apply/discard operations."""


@dataclass(frozen=True)
class RunOptions:
    """User-selectable settings of ``run``.

    ``since`` (an aware datetime) overrides the saved state; ``days`` is only the fallback window
    for a project that has never dreamed successfully. ``budget_usd`` is None unless the user asked
    for a cap, and only then is ``--max-budget-usd`` passed to claude.
    """

    days: int = 7
    since: datetime | None = None
    dry_run: bool = False
    model: str = "opus"
    analyst_model: str = "sonnet"
    budget_usd: float | None = None
    max_sessions: int = 40
    timeout_min: float = 60.0


@dataclass(frozen=True)
class CommitInfo:
    """A dream commit: its sha, subject line, memory file (relative) and the one-line reason from the report."""

    sha: str
    subject: str
    why: str = ""
    file: str = ""


@dataclass(frozen=True)
class ClaudeOutcome:
    """Interpretation of one headless Claude invocation."""

    ok: bool
    cost: float | None = None
    error: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class RunSummary:
    """Everything the CLI prints after a run; ``status`` is one of the ``STATUS_*`` constants."""

    run_id: str
    slug: str
    status: str
    run_dir: Path
    report_path: Path | None = None
    index_path: Path | None = None
    branch: str | None = None
    worktree: Path | None = None
    commits: tuple[CommitInfo, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    cost: float | None = None
    session_count: int = 0
    stats: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    message: str = ""
    base_branch: str | None = None
    superseded: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        """Return 1 for a failed run or a transcript-format problem, 0 otherwise."""
        return 1 if self.status in FAILING_STATUSES else 0


# --------------------------------------------------------------------------- naming and windows


def slug_from_path(path: str) -> str:
    """Map a project path to Claude Code's directory slug (every non-alphanumeric becomes ``-``).

    >>> slug_from_path("/home/sam/my-proj")
    '-home-sam-my-proj'
    >>> slug_from_path("C:\\\\Users\\\\sam\\\\my proj\\\\")
    'C--Users-sam-my-proj'
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path.strip().rstrip("\\/"))


def slug_short(slug: str, home: str | None = None) -> str:
    """Shorten a project slug for ids and branches: drop the drive and home-directory prefix, then slugify.

    ``home`` defaults to the user's home directory; a slug equal to it is kept whole.

    >>> slug_short("-home-sam-code-my-proj", "/home/sam")
    'code-my-proj'
    >>> slug_short("C--Users-sam-Proj", "C:\\\\Users\\\\sam")
    'proj'
    >>> slug_short("E--Other-Place-proj", "/home/sam")
    'other-place-proj'
    """
    stripped = slug
    own_home = slug_from_path(home if home is not None else str(Path.home())).lower()
    if own_home and slug.lower().startswith(own_home + "-"):
        stripped = slug[len(own_home) + 1:]
    else:
        stripped = _DRIVE_PREFIX.sub("", slug, count=1)
    return gitops.slugify(stripped or slug)


def project_slugs(root: Path) -> dict[str, str]:
    """Map lower-cased names to real names for the project directories under ``root``."""
    return {p.name.lower(): p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


def resolve_slug(project: str, root: Path) -> str:
    """Resolve a slug or a project path to the existing project directory name (case-insensitive).

    ``project`` may be the slug itself, a path (the slug rule: every non-alphanumeric becomes ``-``) or
    an existing directory such as ``.`` (resolved to an absolute path first).

    Raises:
        RunError: no project directory under ``root`` matches.
    """
    known = project_slugs(root)
    candidates = [project, slug_from_path(project)]
    if Path(project).exists():
        candidates.append(slug_from_path(str(Path(project).resolve())))
    for candidate in candidates:
        if candidate.lower() in known:
            return known[candidate.lower()]
    raise RunError(f"no Claude project directory for {project!r} under {root}")


def worktrees_base(cfg: dict[str, Any] | None = None) -> Path:
    """Return the directory that holds dream worktrees: config ``worktree_dir``, else ``$TO_DREAM_HOME/worktrees``."""
    configured = (cfg or config.load())["worktree_dir"]
    return Path(configured).expanduser() if configured else dstate.worktrees_dir()


def is_dream_slug(slug: str) -> bool:
    """Say whether a project slug belongs to to-dream's own runs (worktrees or state dir)."""
    own = [slug_from_path(str(worktrees_base())), slug_from_path(str(dstate.home()))]
    return any(slug.lower().startswith(prefix.lower()) for prefix in own)


def has_activity(project_dir: Path, since: datetime) -> bool:
    """Say whether any transcript (``*.jsonl``) under ``project_dir`` was modified at or after ``since``."""
    cutoff = since.timestamp()
    return any(f.stat().st_mtime >= cutoff for f in project_dir.rglob("*.jsonl"))


def resolve_window(
    last: datetime | None, since: datetime | None, days: int, now: datetime
) -> tuple[datetime, datetime]:
    """Pick the transcript window: explicit ``since``, else the last successful dream, else ``days`` back.

    >>> now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    >>> resolve_window(None, None, 7, now)[0].day
    26
    """
    return (since or last or now - timedelta(days=days)), now


def unreviewed_runs(state: dict[str, Any], slug: str) -> list[tuple[str, dict[str, Any]]]:
    """List a project's unreviewed runs (pending, partially-applied or needs-attention), oldest first.

    "Unreviewed" follows :func:`display_status`: a pending run whose commits are all applied counts as
    reviewed, a partly applied one does not. Failed, discarded, superseded and empty runs never count.

    >>> unreviewed_runs({"runs": {"a": {"slug": "p", "status": "pending", "commits": [{}]}}}, "p")[0][0]
    'a'
    """
    items = [(run_id, record) for run_id, record in state.get("runs", {}).items()
             if record.get("slug") == slug and display_status(record) in UNREVIEWED_STATUSES]
    return sorted(items, key=lambda item: item[1].get("created", ""))


def _window_start(record: dict[str, Any]) -> datetime | None:
    """Return a run record's ``window_since`` as an aware datetime, or None if it is absent or malformed."""
    try:
        return dstate.parse_ts(record["window_since"])
    except (KeyError, TypeError, ValueError):
        return None


def rollup_since(since: datetime, unreviewed: list[tuple[str, dict[str, Any]]]) -> datetime:
    """Return the window start of a roll-up: the earliest of ``since`` and the unreviewed runs' window starts.

    >>> rollup_since(datetime(2026, 11, 5, tzinfo=timezone.utc),
    ...              [("a", {"window_since": "2026-11-03T00:00:00+00:00"})]).day
    3
    """
    starts = [start for start in (_window_start(record) for _, record in unreviewed) if start]
    return min([since, *starts])


def rollup_note(unreviewed: list[tuple[str, dict[str, Any]]]) -> str:
    """Describe a roll-up for the orchestrator prompt (``NO_ROLLUP_NOTE`` when there is nothing to replace).

    Example::

        rollup_note([("p-261105-0330", {"window_since": "2026-11-04T09:00:00+00:00",
                                         "window_until": "2026-11-05T09:00:00+00:00"})])
        # 'This run replaces earlier unreviewed runs: p-261105-0330 (window 2026-11-04T09:00:00+00:00 .. ...'
    """
    if not unreviewed:
        return NO_ROLLUP_NOTE
    described = ", ".join(
        f"{run_id} (window {record.get('window_since', '?')} .. {record.get('window_until', '?')})"
        for run_id, record in unreviewed
    )
    return (
        f"This run replaces earlier unreviewed runs: {described}. Changes the user already accepted are in "
        "live memory and rejected ones are in the rejected log, so don't re-propose either; re-propose "
        "anything still warranted."
    )


def name_fields(short: str, run_id: str, local_now: datetime) -> dict[str, str]:
    """Return the values of the branch/worktree template placeholders (``config.TEMPLATE_FIELDS``).

    >>> name_fields("st", "st-261102-1830", datetime(2026, 11, 2, 18, 30))["hhmm"]
    '1830'
    """
    return {
        "run_id": run_id, "project": short, "yy": f"{local_now:%y}", "mm": f"{local_now:%m}",
        "dd": f"{local_now:%d}", "hhmm": f"{local_now:%H%M}",
    }


def unique_run_id(short: str, local_now: datetime, root: Path, branch_template: str) -> tuple[str, str]:
    """Return a ``(run_id, branch)`` pair unused by any run dir or branch, e.g. ``st-261102-1830``.

    The branch comes from ``branch_template``; a repeat in the same minute gets ``-2``, ``-3``... on both.

    Example::

        unique_run_id("st", datetime(2026, 11, 2, 18, 30), root, "dream/{run_id}")
        # ('st-261102-1830', 'dream/st-261102-1830')
    """
    suffix = 1
    while True:
        tail = "" if suffix == 1 else f"-{suffix}"
        run_id = f"{short}-{local_now:%y%m%d-%H%M}{tail}"
        branch = config.render_name(branch_template, name_fields(short, run_id, local_now), "branch_template")
        if suffix > 1 and "{run_id}" not in branch_template:
            branch += tail
        if not dstate.run_dir(run_id).exists() and not gitops.branch_exists(root, branch):
            return run_id, branch
        suffix += 1


def worktree_path(cfg: dict[str, Any], short: str, run_id: str, local_now: datetime) -> Path:
    """Pick an unused worktree directory from config ``worktree_dir`` and ``worktree_template``."""
    name = config.render_name(cfg["worktree_template"], name_fields(short, run_id, local_now), "worktree_template")
    safe = _UNSAFE_DIRNAME.sub("-", name).strip("-.") or run_id
    return gitops.unique_path(worktrees_base(cfg), safe)


# --------------------------------------------------------------------------- templates


def render_template(template: str, values: dict[str, str]) -> str:
    """Replace ``{{name}}`` placeholders, failing loudly on unknown names or names without a value.

    >>> render_template("hi {{slug}}", {"slug": "x"})
    'hi x'

    Raises:
        TemplateError: a placeholder is not in ``PLACEHOLDERS`` (unknown) or has no entry in ``values``.
    """
    names = set(_PLACEHOLDER.findall(template))
    unknown = sorted(names - PLACEHOLDERS)
    if unknown:
        raise TemplateError(f"unknown placeholder(s) in prompt template: {', '.join(unknown)}")
    missing = sorted(n for n in names if n not in values)
    if missing:
        raise TemplateError(f"no value for placeholder(s): {', '.join(missing)}")
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def apply_permissions_review(template: str, enabled: bool) -> str:
    """Resolve the ``permissions-review`` marker comments of a prompt template.

    Passages wrapped in ``<!-- permissions-review:start -->`` / ``<!-- permissions-review:end -->`` are
    kept (markers removed) when ``enabled`` and dropped entirely (markers included) otherwise. A pair on
    their own lines drops those whole lines; a pair inside a line drops only the text between them.

    >>> apply_permissions_review("a\\n<!-- permissions-review:start -->\\nb\\n<!-- permissions-review:end -->\\nc\\n", False)
    'a\\nc\\n'
    >>> apply_permissions_review("x<!-- permissions-review:start --> y<!-- permissions-review:end --> z", True)
    'x y z'
    """
    if enabled:
        return _PERMISSIONS_MARKER.sub("", _PERMISSIONS_MARKER_LINE.sub("", template))
    return _PERMISSIONS_INLINE_BLOCK.sub("", _PERMISSIONS_LINE_BLOCK.sub("", template))


def render_prompt(template: str, values: dict[str, str], permissions_review: bool) -> str:
    """Resolve the permissions-review markers (:func:`apply_permissions_review`), then fill the placeholders.

    Unknown placeholders fail in both modes, even inside a passage that is dropped. In the dropped
    mode ``permissions_path`` needs no value.

    >>> render_prompt("<!-- permissions-review:start -->{{permissions_path}}<!-- permissions-review:end -->{{slug}}",
    ...               {"slug": "p"}, False)
    'p'

    Raises:
        TemplateError: see :func:`render_template`.
    """
    unknown = sorted(set(_PLACEHOLDER.findall(template)) - PLACEHOLDERS)
    if unknown:
        raise TemplateError(f"unknown placeholder(s) in prompt template: {', '.join(unknown)}")
    return render_template(apply_permissions_review(template, permissions_review), values)


def steering_path(cfg: dict[str, Any] | None = None) -> Path:
    """Pick the steering file: config ``steering_path``, else ``$TO_DREAM_HOME/steering.md``, else the bundled one.

    Raises:
        PreflightError: ``steering_path`` is set but is not a file.
    """
    configured = (cfg or config.load())["steering_path"]
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise PreflightError(f"config steering_path {configured!r} is not a file")
        return path
    custom = dstate.steering_override_path()
    return custom if custom.is_file() else DEFAULT_STEERING


def _posix(path: Path | str) -> str:
    """Render a path with forward slashes (accepted by Claude's tools on Windows)."""
    return Path(path).as_posix()


def rejected_log_path() -> Path:
    """Return the path of ``rejected.md``, the memory of changes the user rejected."""
    return dstate.rejected_log_path()


def ensure_rejected_log() -> Path:
    """Create the rejection log with its header if it is missing; return its path."""
    path = rejected_log_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(REJECTED_HEADER, encoding="utf-8", newline="\n")
    return path


def build_prompt_values(
    slug: str, memory_dir: Path, run_dir: Path, index: Path, project_dir: str | None,
    options: RunOptions, since: datetime, until: datetime, session_count: int, steering: Path,
    permissions_path: Path | None, note: str = NO_ROLLUP_NOTE,
) -> dict[str, str]:
    """Collect the value of every placeholder of the orchestrator prompt.

    ``permissions_path`` is the run's ``permissions.md``, or None when the permissions review is off
    (config ``permissions_review``; the placeholder is then empty and unused); ``note`` is the
    :func:`rollup_note` text.
    """
    return {
        "slug": slug,
        "memory_dir": _posix(memory_dir),
        "live_memory_dir": _posix(dstate.memory_root() / slug / MEMORY_DIRNAME),
        "digest_dir": _posix(run_dir / "digests"),
        "digest_index": _posix(index),
        "report_path": _posix(memory_dir / REPORT_NAME),
        "project_dir": _posix(project_dir) if project_dir else "unknown",
        "global_claude_md": _posix(dstate.claude_home() / "CLAUDE.md"),
        "steering_path": _posix(steering),
        "analyst_model": options.analyst_model,
        "window_since": dstate.format_ts(since),
        "window_until": dstate.format_ts(until),
        "session_count": str(session_count),
        "rejected_log": _posix(ensure_rejected_log()),
        "rollup_note": note,
        "permissions_path": _posix(permissions_path) if permissions_path else "",
    }


# --------------------------------------------------------------------------- claude invocation


def read_rule(path: Path | str, recursive: bool = False) -> str:
    """Build a Claude ``Read(...)`` permission rule for an absolute path, whatever OS it comes from.

    POSIX paths become ``Read(//home/u/p)``; Windows drive paths (either separator) become
    ``Read(//c/Users/u/p)``. ``recursive`` appends ``/**``.

    >>> read_rule("/home/u/proj", recursive=True)
    'Read(//home/u/proj/**)'
    >>> read_rule("C:/Users/a/proj", recursive=True)
    'Read(//c/Users/a/proj/**)'
    """
    text = path.as_posix() if isinstance(path, PurePath) else str(path)
    if re.match(r"^[A-Za-z]:([\\/]|$)", text):
        text = "/" + text[0].lower() + text[2:].replace("\\", "/")
    return f"Read(/{text}{'/**' if recursive else ''})"


def claude_executable() -> str:
    """Return ``$TO_DREAM_CLAUDE_CMD`` if set, else ``claude`` as found on PATH (or plain ``claude``)."""
    return os.environ.get(ENV_CLAUDE_CMD) or shutil.which("claude") or "claude"


def budget_args(budget_usd: float | None) -> list[str]:
    """Return ``--max-budget-usd N`` when a budget was requested, else nothing (no cap by default).

    >>> budget_args(None), budget_args(2.5)
    ([], ['--max-budget-usd', '2.5'])
    """
    return [] if budget_usd is None else ["--max-budget-usd", f"{budget_usd:g}"]


def settings_deny_rules(project_dir: str | Path | None) -> list[str]:
    """Build ``Read(...)`` deny rules for every settings file that could hold secrets.

    Covers the user ``settings.json`` plus ``settings.json`` and ``settings.local.json`` under ``.claude`` of
    the project directory and of its git root (``env`` blocks may hold credentials; the dream gets the
    redacted ``permissions.md`` instead). Deny beats the recursive ``Read(<project_dir>/**)`` allow.

    >>> settings_deny_rules("/nonexistent/proj")[-2:]
    ['Read(//nonexistent/proj/.claude/settings.json)', 'Read(//nonexistent/proj/.claude/settings.local.json)']
    """
    roots: list[Path] = []
    if project_dir:
        start = Path(project_dir)
        git_root = permissions.find_git_root(start)
        roots = [start] + ([git_root] if git_root is not None and git_root != start else [])
    paths = [dstate.claude_home() / permissions.SETTINGS_FILE]
    for root in roots:
        paths += [root / ".claude" / permissions.SETTINGS_FILE, root / ".claude" / permissions.LOCAL_SETTINGS_FILE]
    return [read_rule(path) for path in paths]


def build_claude_command(
    exe: str, model: str, budget_usd: float | None, run_dir: Path, read_rules: Iterable[str],
    deny_rules: Iterable[str] = (),
) -> list[str]:
    """Build the headless Claude argv (prompt goes on stdin).

    Edit/Write are deliberately *not* in ``--allowedTools``: a blanket allow lets Claude write anywhere
    (verified), whereas ``acceptEdits`` alone confines edits to the working dir (the worktree's memory
    dir) and ``--add-dir`` (the run dir). Read access outside those is granted per path via ``Read(...)``
    rules, which grant no write access. ``deny_rules`` (e.g. :func:`settings_deny_rules`) are appended to
    ``--disallowedTools`` after the denied tools and win over any allow rule.
    """
    return [
        exe, "-p", "--output-format", "json", "--no-session-persistence",
        "--permission-mode", "acceptEdits", "--permission-prompts", "none",
        "--model", model, *budget_args(budget_usd),
        "--allowedTools", *ALLOWED_TOOLS, *read_rules,
        "--disallowedTools", *DENIED_TOOLS, *deny_rules,
        "--add-dir", str(run_dir),
    ]


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and its children (a ``.cmd`` shim spawns a grandchild on Windows; POSIX uses a process group)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.kill()


def run_claude(command: list[str], prompt: str, cwd: Path, timeout_s: float) -> tuple[int, str, str, bool]:
    """Run the command with ``prompt`` on stdin; return ``(returncode, stdout, stderr, timed_out)``."""
    proc = subprocess.Popen(
        command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, **CLAUDE_ENV}, start_new_session=os.name != "nt",
    )
    try:
        out, err = proc.communicate(prompt.encode("utf-8"), timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        timed_out = True
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out


def interpret_output(returncode: int, stdout: str, stderr: str, timed_out: bool, timeout_min: float) -> ClaudeOutcome:
    """Decide success from the exit code and the ``--output-format json`` payload (cost, ``is_error``).

    Success needs exit code 0 and a JSON object with ``is_error`` false; anything else is a failure.
    """
    cost = None
    payload: Any = None
    try:
        payload = json.loads(stdout)
    except ValueError:
        pass
    if isinstance(payload, dict):
        raw_cost = payload.get("total_cost_usd", payload.get("cost_usd"))
        cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    if timed_out:
        return ClaudeOutcome(False, cost, f"timed out after {timeout_min:g} min", True)
    if returncode != 0:
        return ClaudeOutcome(False, cost, f"claude exited with code {returncode}: {stderr.strip()[:300]}")
    if not isinstance(payload, dict):
        return ClaudeOutcome(False, cost, "claude produced no JSON result")
    if payload.get("is_error"):
        return ClaudeOutcome(False, cost, f"claude reported an error ({payload.get('subtype', 'unknown')})")
    return ClaudeOutcome(True, cost)


# --------------------------------------------------------------------------- logging


class RunLogger:
    """Append timestamped lines to ``run.log`` and echo them (warnings to stderr)."""

    def __init__(self, path: Path | None, echo: bool = True) -> None:
        self.path = path
        self.echo = echo
        self.warnings: list[str] = []

    def _write(self, level: str, message: str) -> None:
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now():%H:%M:%S} {level} {message}\n")

    def info(self, message: str) -> None:
        """Log an informational line."""
        self._write("INFO", message)
        if self.echo:
            print(message, flush=True)

    def warn(self, message: str) -> None:
        """Log a warning (also collected into the run summary)."""
        self.warnings.append(message)
        self._write("WARN", message)
        if self.echo:
            print(f"warning: {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- pipeline


def load_distill():
    """Import and return the ``to_dream.distill`` module (late, so other commands work without it)."""
    from to_dream import distill

    return distill


def _count(value: Any) -> int:
    """Count a digest statistic that may be an int or a collection."""
    return value if isinstance(value, int) else len(value) if hasattr(value, "__len__") else 0


def digest_stats(digests: list) -> dict[str, int]:
    """Total the per-session counters of digests, for the dry-run printout."""
    names = ("human_prompts", "corrections", "tool_errors", "denials", "memory_writes", "chars")
    stats = {"sessions": len(digests)}
    stats.update({n: sum(_count(getattr(d, n, 0)) for d in digests) for n in names})
    return stats


def preflight(root: Path, need_claude: bool) -> str:
    """Check the environment and return the base branch (the branch checked out in ``root``).

    Raises:
        PreflightError: memory repo missing, detached HEAD, or ``claude`` not found.
    """
    if not (root / ".git").exists():
        raise PreflightError(
            f"memory repo not found at {root} (expected a git repo; `to-dream init-repo` can create it)"
        )
    branch = gitops.current_branch(root)
    if not branch:
        raise PreflightError(f"{root} has a detached HEAD; check out a branch first")
    if need_claude:
        exe = claude_executable()
        if not (Path(exe).is_file() or shutil.which(exe)):
            raise PreflightError(f"`claude` was not found on PATH (set {ENV_CLAUDE_CMD} to override)")
    return branch


def log_snapshot(snapshot: gitops.SnapshotResult, log: RunLogger) -> None:
    """Log a snapshot's outcome: the commit made, and a warning per file the secret scan refused."""
    if snapshot.sha:
        log.info(f"snapshot {snapshot.sha[:10]}: committed {len(snapshot.committed)} uncommitted memory file(s)")
    for path, reason in snapshot.refused:
        log.warn(f"not snapshotted: {path} ({reason.splitlines()[0]})")


def collect_commits(
    worktree: Path, prefix: str, slug_short_name: str, run_id: str, report: str, log: RunLogger
) -> tuple[list[CommitInfo], list[tuple[str, str]]]:
    """Revert out-of-scope edits, then commit each in-scope change on its own.

    Returns ``(commits, rejected)`` where ``rejected`` lists ``(file, reason)`` for files the secret scan
    (or a user hook) refused, left uncommitted. Blank files count as deletions; blank new files are removed.
    """
    changes, outside = gitops.collect_changes(worktree, prefix)
    for path in gitops.revert_entries(worktree, outside):
        log.warn(f"reverted change outside {prefix}: {path}")
    gitops.remove_dropped(worktree, changes)
    sections = gitops.parse_report_sections(report)
    commits: list[CommitInfo] = []
    rejected: list[tuple[str, str]] = []
    for change in (c for c in changes if c.action != gitops.ACTION_DROP):
        relative = change.path[len(prefix):]
        section = gitops.section_for(sections, relative)
        if not section:
            log.warn(f"no report section for {relative}")
        message = gitops.commit_message(slug_short_name, change, relative, section, run_id)
        outcome = gitops.commit_change(worktree, change, message)
        if outcome.sha:
            commits.append(CommitInfo(outcome.sha, message.splitlines()[0], gitops.change_line(section), relative))
        else:
            reason = outcome.error or "rejected"
            rejected.append((relative, reason))
            log.warn(f"not committed: {relative} ({reason.splitlines()[0]})")
    return commits, rejected


def _stash_report(memory_dir: Path, run_dir: Path, log: RunLogger) -> str:
    """Move the report Claude wrote in the worktree into the run dir; return its text ('' if absent).

    Claude cannot write under ``~/.claude`` (protected path), so it writes inside the worktree's memory
    dir; moving it out first keeps it out of the change scan.
    """
    written = memory_dir / REPORT_NAME
    target = run_dir / REPORT_NAME
    if written.is_file():
        shutil.move(str(written), str(target))
    if not target.is_file():
        log.warn(f"Claude wrote no {REPORT_NAME}")
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


def _record_commits(commits: list[CommitInfo]) -> list[dict[str, str]]:
    """Serialise commits for the state file."""
    return [{"sha": c.sha, "subject": c.subject, "why": c.why, "file": c.file} for c in commits]


def _prepare_branch(
    root: Path, run_id: str, run_dir: Path, base_branch: str, branch: str, worktree: Path, log: RunLogger
) -> str:
    """Snapshot uncommitted memory, then create the proposal worktree; return the base commit sha.

    On any failure the half-made run directory is removed so a retry starts clean.
    """
    try:
        log_snapshot(gitops.snapshot_memory(root, f"run {run_id}"), log)
        base = gitops.rev_parse(root, base_branch)
        gitops.create_worktree(root, worktree, branch, base_branch)
    except (DreamError, gitops.GitError):
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return base


def run_project(slug: str, options: RunOptions, now: datetime | None = None, echo: bool = True) -> RunSummary:
    """Dream one project end to end and return what happened.

    Order: preflight, distil, (dry-run stops here), snapshot uncommitted memory, create the worktree on a
    new branch off the checked-out base branch, run claude, commit each changed file. If transcripts in the
    window exist but none of their records parse, the run stops with ``STATUS_FORMAT_DRIFT`` (exit code 1)
    before calling Claude.

    Roll-up: with config ``rollup`` on (default) and unreviewed runs of the project present, the window starts
    at the earliest of their ``window_since`` and the normal start; once the new run succeeds (pending,
    no-changes or needs-attention) those runs are superseded (``RunSummary.superseded``). A failed run
    leaves them untouched. With config ``permissions_review`` on (default off), ``permissions.md`` (the
    current permission rules) is written into the run dir, the prompt asks for permission suggestions and the
    digests carry the permission-event sections; off, none of that happens and ``permission_targets`` is
    ``{}``. Either way Claude is denied reading the settings files themselves (:func:`settings_deny_rules`).

    Raises:
        PreflightError: environment unusable (see :func:`preflight`).
        TemplateError: the orchestrator prompt has unknown/unfilled placeholders.
        config.ConfigError: ``branch_template`` / ``worktree_template`` is invalid.
        GitError: a git step that must succeed failed (e.g. worktree creation).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = dstate.memory_root()
    base_branch = preflight(root, need_claude=not options.dry_run)
    cfg = config.load()
    steering = steering_path(cfg)
    short = slug_short(slug)
    local_now = now.astimezone()
    run_id, branch = unique_run_id(short, local_now, root, cfg["branch_template"])
    if not gitops.valid_branch_name(root, branch):
        raise PreflightError(f"config branch_template produced {branch!r}, which is not a valid git branch name")
    run_dir = dstate.run_dir(run_id)
    (run_dir / "digests").mkdir(parents=True)
    log = RunLogger(run_dir / "run.log", echo)
    state = dstate.load_state()
    since, until = resolve_window(dstate.get_last_dream(state, slug), options.since, options.days, now)
    unreviewed = unreviewed_runs(state, slug) if cfg["rollup"] else []
    since = rollup_since(since, unreviewed)
    log.info(f"dream {run_id}: window {dstate.format_ts(since)} .. {dstate.format_ts(until)}")
    if unreviewed:
        log.info(f"roll-up: window extended over unreviewed run(s) {', '.join(r for r, _ in unreviewed)}")

    distill = load_distill()
    distill_stats = distill.DistillStats() if hasattr(distill, "DistillStats") else None
    digests = distill.distill_project(
        root / slug, run_dir / "digests", since=since, until=until, max_sessions=options.max_sessions,
        stats=distill_stats, permissions_review=cfg["permissions_review"],
    )
    if not digests:
        drifted = bool(distill_stats and distill_stats.format_changed)
        if drifted:
            log.warn(FORMAT_DRIFT_MESSAGE)
        shutil.rmtree(run_dir)
        if drifted:
            return RunSummary(run_id, slug, STATUS_FORMAT_DRIFT, run_dir, warnings=tuple(log.warnings),
                              message=FORMAT_DRIFT_MESSAGE, base_branch=base_branch)
        return RunSummary(run_id, slug, STATUS_NO_SESSIONS, run_dir, message="no sessions in window",
                          base_branch=base_branch)
    index = distill.write_index(digests, run_dir / "digests", permissions_review=cfg["permissions_review"])
    stats = digest_stats(digests)
    log.info(f"distilled {len(digests)} session(s) -> {index}")
    if options.dry_run:
        return RunSummary(run_id, slug, STATUS_DRY_RUN, run_dir, index_path=index, stats=stats,
                          session_count=len(digests), warnings=tuple(log.warnings), base_branch=base_branch)

    project_dir = distill.project_cwd(digests)
    permissions_file: Path | None = None
    targets: dict[str, Any] = {}
    if cfg["permissions_review"]:
        permissions_file, targets = permissions.write_permissions(run_dir, slug, project_dir)
    worktree = worktree_path(cfg, short, run_id, local_now)
    base = _prepare_branch(root, run_id, run_dir, base_branch, branch, worktree, log)
    memory_dir = worktree / slug / MEMORY_DIRNAME
    memory_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"worktree {worktree} on {branch} (base branch {base_branch})")
    dstate.update_state(lambda s: dstate.set_run(
        s, run_id, slug=slug, slug_short=short, branch=branch, base_branch=base_branch, worktree=str(worktree),
        run_dir=str(run_dir), base=base, created=dstate.format_ts(now), window_since=dstate.format_ts(since),
        window_until=dstate.format_ts(until), status=STATUS_RUNNING, commits=[], applied={},
        permission_targets=targets,
    ))

    prompt = render_prompt(
        ORCHESTRATOR_PROMPT.read_text(encoding="utf-8"),
        build_prompt_values(slug, memory_dir, run_dir, index, project_dir, options, since, until, len(digests),
                            steering, permissions_file, rollup_note(unreviewed)),
        cfg["permissions_review"],
    )
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    rules = [read_rule(dstate.claude_home() / "CLAUDE.md"), read_rule(steering), read_rule(rejected_log_path()),
             *([read_rule(permissions_file)] if permissions_file else []),
             read_rule(root / slug / MEMORY_DIRNAME, recursive=True)]
    rules += [read_rule(project_dir, recursive=True)] if project_dir else []
    command = build_claude_command(claude_executable(), options.model, options.budget_usd, run_dir, rules,
                                   settings_deny_rules(project_dir))
    budget = "no budget cap" if options.budget_usd is None else f"budget cap ${options.budget_usd:g}"
    log.info(f"running claude ({options.model}, {budget}, timeout {options.timeout_min:g} min)")
    code, out, err, timed_out = run_claude(command, prompt, memory_dir, options.timeout_min * 60)
    (run_dir / "claude-output.json").write_text(out, encoding="utf-8")
    outcome = interpret_output(code, out, err, timed_out, options.timeout_min)
    report = _stash_report(memory_dir, run_dir, log)
    report_path = run_dir / REPORT_NAME if (run_dir / REPORT_NAME).is_file() else None

    if not outcome.ok:
        log.warn(outcome.error)
        dstate.update_state(lambda s: dstate.set_run(s, run_id, status=STATUS_FAILED, error=outcome.error))
        return RunSummary(run_id, slug, STATUS_FAILED, run_dir, report_path, index, branch, worktree,
                          cost=outcome.cost, session_count=len(digests), stats=stats,
                          warnings=tuple(log.warnings), message=outcome.error, base_branch=base_branch)

    commits, rejected = collect_commits(worktree, f"{slug}/{MEMORY_DIRNAME}/", short, run_id, report, log)
    if commits or rejected:
        status = STATUS_PENDING if commits else STATUS_REJECTED
        kept: tuple[str | None, Path | None] = (branch, worktree)
    else:
        gitops.remove_worktree(root, worktree, force=True)
        gitops.delete_branch(root, branch)
        status, kept = STATUS_EMPTY, (None, None)
        log.info("no memory changes proposed; worktree and branch removed")

    def finish(s: dict) -> None:
        dstate.set_run(s, run_id, status=status, commits=_record_commits(commits), cost=outcome.cost,
                       rejected=[list(r) for r in rejected], files=sorted({c.file for c in commits}))
        dstate.set_last_dream(s, slug, until)

    dstate.update_state(finish)
    superseded = supersede_runs([r for r, _ in unreviewed], run_id, log) if status in ROLLUP_STATUSES else []
    return RunSummary(run_id, slug, status, run_dir, report_path, index, kept[0], kept[1], tuple(commits),
                      tuple(rejected), outcome.cost, len(digests), stats, tuple(log.warnings),
                      base_branch=base_branch, superseded=tuple(superseded))


def run_all(options: RunOptions, now: datetime | None = None, echo: bool = True) -> list[RunSummary | tuple[str, str]]:
    """Dream every project with transcript activity in its window; per-project errors do not stop the rest.

    Returns summaries, or ``(slug, error message)`` for projects whose run raised a :class:`DreamError`.

    Raises:
        PreflightError: the environment is unusable for every project (checked once, up front).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = dstate.memory_root()
    preflight(root, need_claude=not options.dry_run)
    state = dstate.load_state()
    results: list[RunSummary | tuple[str, str]] = []
    for slug in sorted(project_slugs(root).values()):
        since, _ = resolve_window(dstate.get_last_dream(state, slug), options.since, options.days, now)
        if is_dream_slug(slug) or not has_activity(root / slug, since):
            continue
        try:
            results.append(run_project(slug, options, now, echo))
        except (DreamError, gitops.GitError, config.ConfigError) as exc:
            results.append((slug, str(exc)))
    return results


# --------------------------------------------------------------------------- run records


def get_run(run_id: str) -> dict[str, Any]:
    """Return the state record of ``run_id``.

    Raises:
        RunError: no such run.
    """
    record = dstate.load_state()["runs"].get(run_id)
    if record is None:
        raise RunError(f"unknown run {run_id!r} (see `to-dream list`)")
    return record


def display_status(record: dict[str, Any]) -> str:
    """Return the user-facing status; applied/partially-applied count applied and marked-applied commits."""
    status = record.get("status", "")
    if status != STATUS_PENDING:
        return status
    done = len(set(record.get("applied", {})) | set(record.get("marked_applied", {})))
    total = len(record.get("commits", []))
    return STATUS_APPLIED if total and done >= total else STATUS_PARTIAL if done else STATUS_PENDING


def run_commits(record: dict[str, Any]) -> list[tuple[str, str]]:
    """List a run's ``(sha, subject)`` commits in order, read from git so history is the source of truth."""
    return gitops.list_commits(dstate.memory_root(), record["base"], record["branch"])


def select_commits(commits: list[tuple[str, str]], only: list[int] | None) -> list[tuple[int, str, str]]:
    """Pick commits by 1-based number (all if ``only`` is empty), keeping run order.

    >>> select_commits([("a", "x"), ("b", "y")], [2])
    [(2, 'b', 'y')]

    Raises:
        RunError: a number is out of range.
    """
    numbered = [(i, sha, subject) for i, (sha, subject) in enumerate(commits, 1)]
    if not only:
        return numbered
    bad = [n for n in only if not 1 <= n <= len(commits)]
    if bad:
        raise RunError(f"no such commit number(s) {bad}; this run has {len(commits)}")
    return [item for item in numbered if item[0] in set(only)]


@dataclass(frozen=True)
class RunChange:
    """One numbered change (a dream commit) of a run with its review state.

    ``applied`` is true both for cherry-picked changes and for ones the user applied by hand and recorded
    with ``mark-applied`` (``marked`` tells the two apart).
    """

    n: int
    sha: str
    subject: str
    file: str
    action: str
    applied: bool
    rejected: bool
    marked: bool = False


@dataclass(frozen=True)
class ApplyReport:
    """Outcome of :func:`apply_run`: the cherry-pick result, skipped change numbers and the snapshot made first."""

    result: gitops.ApplyResult
    already_applied: list[int] = field(default_factory=list)
    skipped_rejected: list[int] = field(default_factory=list)
    snapshot: gitops.SnapshotResult | None = None


def run_changes(record: dict[str, Any]) -> list[RunChange]:
    """Number a run's changes (from git while the branch exists, else from the state record).

    ``action`` and ``file`` come from the commit subject (``chore(to-dream): <action> <slug-short>/<file>``).
    """
    root = dstate.memory_root()
    if record.get("branch") and gitops.branch_exists(root, record["branch"]):
        commits = run_commits(record)
    else:
        commits = [(c["sha"], c["subject"]) for c in record.get("commits", [])]
    done, rejected = record.get("applied", {}), record.get("rejected_commits", {})
    marked = record.get("marked_applied", {})
    changes = []
    for number, (sha, subject) in enumerate(commits, 1):
        action, file = gitops.parse_subject(subject) or ("update", subject)
        changes.append(RunChange(number, sha, subject, file, action, sha in done or sha in marked,
                                 sha in rejected, sha in marked))
    return changes


def report_text(record: dict[str, Any]) -> str:
    """Return the run's DREAM_REPORT.md text, or '' if it is missing."""
    path = Path(record.get("run_dir", "")) / REPORT_NAME
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _one_line(text: str, cap: int = ENTRY_CAP) -> str:
    """Collapse whitespace in ``text`` and cap it for a one-line log entry."""
    return gitops.cap_text(" ".join(text.split()), cap)


def _record_rejections(
    run_id: str, record: dict[str, Any], changes: list[RunChange], reason: str, when: datetime
) -> None:
    """Mark ``changes`` rejected in state and append one entry each to the rejection log."""
    sections = gitops.parse_report_sections(report_text(record))
    log = ensure_rejected_log()
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        for change in changes:
            section = gitops.section_for(sections, change.file)
            what = gitops.cap_text(gitops.change_line(section, ENTRY_CAP), ENTRY_CAP) or "(not recorded)"
            why = _one_line(reason) or REASON_NONE
            handle.write(
                f"\n## {when:%Y-%m-%d} | {record['slug']} | {change.file} | {change.action}\n"
                f"- Run: {run_id}\n- Change: {what}\n- Reason: {why}\n"
            )

    def save(s: dict) -> None:
        marks = s["runs"][run_id].setdefault("rejected_commits", {})
        for change in changes:
            marks[change.sha] = {"reason": reason or REASON_NONE, "at": dstate.format_ts(when)}

    dstate.update_state(save)


def reject_run(
    run_id: str, only: list[int], reason: str = "", when: datetime | None = None
) -> list[RunChange]:
    """Mark the numbered changes rejected so future dreams do not re-propose them; return the newly rejected.

    Appends one entry per change (date, slug, file, action, the report's ``**Change:**`` line, reason) to
    ``rejected.md``. Changes already rejected are left alone.

    Example::

        reject_run("proj-261102-1830", [2], "memory should stay terse")

    Raises:
        RunError: unknown run, bad change number, or a selected change is already applied.
    """
    record = get_run(run_id)
    changes = run_changes(record)
    wanted = {n for n, _, _ in select_commits([(c.sha, c.subject) for c in changes], only or None)}
    chosen = [c for c in changes if c.n in wanted]
    applied = [c.n for c in chosen if c.applied]
    if applied:
        raise RunError(f"change(s) {applied} already applied; revert them in live memory instead")
    fresh = [c for c in chosen if not c.rejected]
    if fresh:
        _record_rejections(run_id, record, fresh, reason.strip(), when or datetime.now(timezone.utc))
    return fresh


def reject_suggestion(
    run_id: str, text: str, reason: str = "", when: datetime | None = None, kind: str = KIND_GLOBAL
) -> bool:
    """Log a rejected suggestion in ``rejected.md`` so future dreams stop proposing it.

    ``kind`` is ``"global"`` (a global CLAUDE.md suggestion, the default) or ``"permission"`` (a permission
    rule, ``text`` being the rule). The entry sits under a ``## <date> | <slug> | <kind> suggestion``
    heading. Repeating the same text for the same run and kind is a no-op. Returns True when an entry
    was written.

    Example::

        reject_suggestion("proj-261102-1830", "Always use tabs", "I prefer spaces")
        reject_suggestion("proj-261102-1830", "Bash(npm test *)", "too broad", kind="permission")

    Raises:
        RunError: unknown run, unknown ``kind``, or ``text`` is blank.
    """
    if kind not in SUGGESTION_KINDS:
        raise RunError(f"unknown suggestion kind {kind!r}; use one of: {', '.join(SUGGESTION_KINDS)}")
    record = get_run(run_id)
    suggestion = _one_line(text)
    if not suggestion:
        raise RunError("suggestion text is empty")
    when = when or datetime.now(timezone.utc)
    why = _one_line(reason) or REASON_NONE
    if any(item.get("text") == suggestion and item.get("kind", KIND_GLOBAL) == kind
           for item in record.get("rejected_suggestions", [])):
        return False
    label = "Rule" if kind == KIND_PERMISSION else "Suggestion"
    log = ensure_rejected_log()
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            f"\n## {when:%Y-%m-%d} | {record['slug']} | {SUGGESTION_HEADINGS[kind]}\n"
            f"- Run: {run_id}\n- {label}: {suggestion}\n- Reason: {why}\n"
        )

    def save(s: dict) -> None:
        entry = {"text": suggestion, "reason": reason.strip() or REASON_NONE, "at": dstate.format_ts(when)}
        if kind != KIND_GLOBAL:
            entry["kind"] = kind
        s["runs"][run_id].setdefault("rejected_suggestions", []).append(entry)

    dstate.update_state(save)
    return True


def mark_applied(
    run_id: str, only: list[int], note: str = "", when: datetime | None = None
) -> list[RunChange]:
    """Record changes the user applied by hand (after a cherry-pick conflict); return the newly marked ones.

    Marked changes count as applied: ``apply`` skips them, ``discard`` does not log them as rejected and
    ``reject`` refuses them. Already-applied or already-marked changes are left alone.

    Example::

        mark_applied("proj-261102-1830", [2], "merged by hand")

    Raises:
        RunError: unknown or finished run, bad change number, or a selected change was rejected.
    """
    record = get_run(run_id)
    if record.get("status") in INACTIVE_STATUSES:
        raise RunError(f"run {run_id} is {record['status']}; nothing to mark")
    changes = run_changes(record)
    wanted = {n for n, _, _ in select_commits([(c.sha, c.subject) for c in changes], only)}
    chosen = [c for c in changes if c.n in wanted]
    rejected = [c.n for c in chosen if c.rejected and not c.applied]
    if rejected:
        raise RunError(f"change(s) {rejected} were rejected; they cannot also be marked applied")
    fresh = [c for c in chosen if not c.applied]
    when = when or datetime.now(timezone.utc)

    def save(s: dict) -> None:
        marks = s["runs"][run_id].setdefault("marked_applied", {})
        for change in fresh:
            marks[change.sha] = {"note": note.strip(), "at": dstate.format_ts(when)}

    if fresh:
        dstate.update_state(save)
    return fresh


def apply_run(run_id: str, only: list[int] | None = None, force: bool = False) -> ApplyReport:
    """Snapshot uncommitted memory, then cherry-pick a run's commits onto the live base branch.

    Already-applied (or marked-applied) changes are skipped. Rejected changes are skipped when applying
    everything, but naming one in ``only`` is refused unless ``force`` is set (which applies rejected ones
    too). The snapshot (``chore(to-dream): snapshot before apply <run-id>``) is only made when there is
    something to pick. A conflict aborts the pick and is reported in ``result.failed``.

    Raises:
        RunError: unknown/discarded run, a different branch than the run's base is checked out, tracked
            files outside ``*/memory/`` have uncommitted changes, the branch is gone, or an explicitly
            selected change is rejected (and ``force`` is false).
        GitError: the snapshot could not be made.
    """
    record = get_run(run_id)
    root = dstate.memory_root()
    if record.get("status") in INACTIVE_STATUSES:
        raise RunError(f"run {run_id} is {record['status']}; nothing to apply")
    current = gitops.current_branch(root)
    base_branch = record.get("base_branch") or current
    if current != base_branch:
        raise RunError(
            f"{root} has {current or '(detached HEAD)'!r} checked out, but run {run_id} was based on "
            f"{base_branch!r}; check out {base_branch!r} first"
        )
    dirty = gitops.dirty_outside_memory(root)
    if dirty:
        raise RunError(f"live repo has uncommitted changes outside */memory/ (e.g. {dirty[0]}); commit or stash them first")
    if not gitops.branch_exists(root, record["branch"]):
        raise RunError(f"branch {record['branch']} no longer exists")
    changes = run_changes(record)
    wanted = {n for n, _, _ in select_commits([(c.sha, c.subject) for c in changes], only)}
    chosen = [c for c in changes if c.n in wanted]
    rejected = [c.n for c in chosen if c.rejected and not c.applied]
    if rejected and only and not force:
        raise RunError(f"change(s) {rejected} were rejected; pass --force to apply them anyway")
    skipped_rejected = [] if force or only else rejected
    todo = [c for c in chosen if not c.applied and c.n not in skipped_rejected]
    snapshot = gitops.snapshot_memory(root, f"apply {run_id}") if todo else None
    result = gitops.cherry_pick_all(root, [c.sha for c in todo])

    def save(s: dict) -> None:
        s["runs"][run_id].setdefault("applied", {}).update(result.applied)

    dstate.update_state(save)
    return ApplyReport(result, [c.n for c in chosen if c.applied], skipped_rejected, snapshot)


def discard_run(run_id: str, force: bool = False) -> list[RunChange]:
    """Remove a run's worktree and delete its branch; with :func:`supersede_run` the only place dream branches are deleted.

    The worktree is removed with ``--force`` only if it is clean apart from the tool's own files (or
    ``force`` is given); otherwise the uncommitted work is protected. Changes that were neither applied
    (including marked-applied) nor rejected are then recorded as rejected ("discarded with run"); they
    are returned.

    Raises:
        RunError: unknown run, or the worktree holds uncommitted work and ``force`` is false.
    """
    record = get_run(run_id)
    leftover = [] if record.get("status") in (STATUS_DISCARDED, STATUS_SUPERSEDED) else [
        c for c in run_changes(record) if not c.applied and not c.rejected
    ]
    remove_checkout(record, force)
    if leftover:
        _record_rejections(run_id, record, leftover, REASON_DISCARDED, datetime.now(timezone.utc))
    dstate.update_state(lambda s: dstate.set_run(s, run_id, status=STATUS_DISCARDED))
    return leftover


def remove_checkout(record: dict[str, Any], force: bool = False) -> None:
    """Remove a run's worktree and delete its branch (shared by discard and supersede; changes no state).

    The worktree is removed with ``--force`` only if it is clean apart from the tool's own files, or
    ``force`` is given; otherwise the uncommitted work is protected.

    Raises:
        RunError: the worktree holds uncommitted work and ``force`` is false (nothing has been deleted).
    """
    root = dstate.memory_root()
    worktree = Path(record["worktree"]) if record.get("worktree") else None
    if worktree and worktree.exists():
        try:
            gitops.remove_worktree(root, worktree)
        except gitops.GitError as exc:
            if not (force or gitops.only_tool_files_dirty(worktree)):
                raise RunError(f"worktree {worktree} has uncommitted work; re-run with --force to discard it") from exc
            gitops.remove_worktree(root, worktree, force=True)
    gitops.run_git(root, "worktree", "prune")
    if gitops.branch_exists(root, record["branch"]):
        gitops.delete_branch(root, record["branch"])


def supersede_run(run_id: str, by: str, log: RunLogger) -> bool:
    """Retire an unreviewed run that the run ``by`` replaced; return False (with a warning) if it is left alone.

    Removes its worktree and branch like ``discard`` does, marks it ``STATUS_SUPERSEDED`` with
    ``superseded_by: by``, and does NOT log its leftover changes as rejected (accepted ones are live,
    rejected ones are already logged, the rest are covered by the new run). A worktree with real
    uncommitted work is never forced: the run stays as it was. A run that is no longer unreviewed
    (applied or discarded meanwhile) is skipped silently.

    Example::

        supersede_run("proj-261105-0330", "proj-261106-0330", log)  # True
    """
    record = get_run(run_id)
    if display_status(record) not in UNREVIEWED_STATUSES:
        return False
    try:
        remove_checkout(record)
    except (RunError, gitops.GitError) as exc:
        log.warn(f"{run_id} not superseded by {by}: {exc}")
        return False
    dstate.update_state(lambda s: dstate.set_run(s, run_id, status=STATUS_SUPERSEDED, superseded_by=by))
    log.info(f"superseded {run_id} (worktree and branch removed)")
    return True


def supersede_runs(run_ids: list[str], by: str, log: RunLogger) -> list[str]:
    """Supersede each run in ``run_ids`` (see :func:`supersede_run`); return the ids that were superseded."""
    return [run_id for run_id in run_ids if supersede_run(run_id, by, log)]


# --------------------------------------------------------------------------- JSON views

EXTRA_SECTIONS = ("global_suggestions", "stale_candidates", "not_acted_on", "permission_suggestions")
_EXTRA_MATCHERS: dict[str, Callable[[str], bool]] = {
    "global_suggestions": lambda title: "global" in title and "permission" not in title,
    "stale_candidates": lambda title: title.startswith("stale"),
    "not_acted_on": lambda title: "not_acted_on" in title,
    "permission_suggestions": lambda title: "permission" in title and "global" not in title,
}


def pick_extras(sections: dict[str, str]) -> dict[str, str]:
    """Map normalised ``## `` titles to the report extras by keyword (headings vary in wording).

    ``global_suggestions`` <- a title containing ``global`` (but not ``permission``); ``stale_candidates``
    <- one starting with ``stale``; ``not_acted_on`` <- one containing ``not_acted_on``;
    ``permission_suggestions`` <- one containing ``permission`` (but not ``global``). The first match
    wins; missing -> ''.

    >>> pick_extras({"suggestions_for_global_claude_md": "g", "permission_suggestions": "p"})
    {'global_suggestions': 'g', 'stale_candidates': '', 'not_acted_on': '', 'permission_suggestions': 'p'}
    """
    return {
        name: next((body for title, body in sections.items() if _EXTRA_MATCHERS[name](title)), "")
        for name in EXTRA_SECTIONS
    }


def run_to_json(run_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Describe a run for ``list --json`` (status is one of the ``STATUS_*`` values)."""
    return {
        "run_id": run_id,
        "slug": record.get("slug"),
        "status": display_status(record),
        "created": record.get("created"),
        "window_since": record.get("window_since"),
        "window_until": record.get("window_until"),
        "branch": record.get("branch"),
        "base_branch": record.get("base_branch"),
        "worktree": record.get("worktree"),
        "report_path": str(Path(record.get("run_dir", "")) / REPORT_NAME),
        "changes": len(record.get("commits", [])),
        "cost_usd": record.get("cost"),
        **({"superseded_by": record["superseded_by"]} if record.get("superseded_by") else {}),
    }


def run_detail_json(run_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Describe a run and each change (report section, capped diff, review flags) for ``show --json``.

    ``report_extras`` holds the raw text of each extra section plus ``permission_suggestions_parsed``
    (see :func:`permissions.parse_permission_suggestions`); ``permission_targets`` maps each suggestion
    scope (``project-local``, ``project``, ``user``) to the settings file recorded when the run was made
    (``{}`` for runs from before permissions existed); ``superseded_by`` appears for superseded runs.
    When ``state.json`` holds an ``update_notice`` (see ``STATE_UPDATE_NOTICE_KEY``) it is included at the
    top level so the review can mention that a newer to-dream is out.
    """
    root = dstate.memory_root()
    report = report_text(record)
    sections = gitops.parse_report_sections(report)
    extras = gitops.level2_sections(report)
    summary = run_to_json(run_id, record)
    branch_alive = bool(record.get("branch")) and gitops.branch_exists(root, record["branch"])
    changes = []
    for change in run_changes(record):
        diff, truncated = gitops.commit_diff(root, change.sha) if branch_alive else ("", False)
        changes.append({
            "n": change.n, "file": change.file, "action": change.action, "commit": change.sha,
            "subject": change.subject, "report_section": gitops.section_for(sections, change.file),
            "diff": diff, "diff_truncated": truncated, "applied": change.applied, "rejected": change.rejected,
            "marked_applied": change.marked,
        })
    picked = pick_extras(extras)
    detail = {
        **{key: summary[key] for key in ("run_id", "slug", "status", "report_path", "worktree", "branch",
                                         "base_branch")},
        **({"superseded_by": summary["superseded_by"]} if "superseded_by" in summary else {}),
        "live_memory_dir": str(root / str(record.get("slug", "")) / MEMORY_DIRNAME),
        "changes": changes,
        "report_extras": {
            **picked, "permission_suggestions_parsed": permissions.parse_permission_suggestions(
                picked["permission_suggestions"]),
        },
        "permission_targets": record.get("permission_targets", {}),
    }
    notice = dstate.load_state().get(STATE_UPDATE_NOTICE_KEY)
    if notice:
        detail[STATE_UPDATE_NOTICE_KEY] = notice
    return detail
