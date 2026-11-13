"""The review summary behind ``to-dream plan``: what each pending change does and whether it still fits live memory.

For every pending change of the given runs (oldest run first) it reports the file, action, the report's
``**Change:**`` line, the added/removed line counts, whether the live file moved since the run was made
(``live_changed``), whether the change would apply to live memory now (``applies_cleanly``) and whether an
earlier change of the same runs already made the same edit (``duplicate_of``). It reads git and state
only; nothing is changed except unreachable tree objects that ``git merge-tree`` may write.

Example::

    plan = build_plan(["proj-261102-1830", "proj-261109-0330"])
    print(format_plan(plan))
"""

import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from to_dream import gitops, memory_index, runner, state as dstate

CHANGE_TEXT_CAP = 400
WRAP_WIDTH = 100
STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
_CONFLICT_EXIT = 1  # ``git merge-tree --write-tree`` exit status for "merged with conflicts"


@dataclass(frozen=True)
class PlanChange:
    """One pending change with the facts a reviewer needs.

    ``live_changed`` is None when the diff could not be read (branch gone). ``applies_cleanly`` is judged
    against the committed ``HEAD`` of live memory (uncommitted edits are snapshotted by ``apply`` first).
    ``entrywise_ok`` is only set for a ``MEMORY.md`` update that does not cherry-pick cleanly: whether
    ``apply`` can still re-apply it entry by entry. ``duplicate_of`` is ``{"run_id", "n", "status"}`` of
    the earlier change with the same file and the same added/removed lines.
    """

    run_id: str
    n: int
    file: str
    action: str
    change: str
    added: int
    removed: int
    live_changed: bool | None
    applies_cleanly: bool | None
    entrywise_ok: bool | None
    duplicate_of: dict[str, Any] | None


@dataclass(frozen=True)
class Plan:
    """The pending changes of the requested runs, plus how many applied/rejected ones were left out."""

    changes: list[PlanChange]
    skipped: int = 0


def change_status(change: runner.RunChange) -> str:
    """Return ``applied`` (also for marked-applied), ``rejected`` or ``pending`` for a run change."""
    if change.applied:
        return STATUS_APPLIED
    return STATUS_REJECTED if change.rejected else STATUS_PENDING


def read_live(repo: Path, path: str) -> str | None:
    """Return the live working-tree text of ``path`` (decoded like git output), or None if the file is absent."""
    target = repo / path
    return target.read_bytes().decode("utf-8", "replace") if target.is_file() else None


def read_base(repo: Path, sha: str, path: str) -> str | None:
    """Return ``path`` as it was in the parent of commit ``sha`` (what the run's change was based on), or None if new."""
    shown = gitops.run_git(repo, "show", f"{sha}^:{path}", check=False)
    return shown.stdout if shown.returncode == 0 else None


def live_changed(live: str | None, base: str | None) -> bool:
    """Say whether live content differs from the run's base content (a new file counts as changed once it exists).

    >>> live_changed("a\\n", "a\\n"), live_changed("b\\n", "a\\n"), live_changed(None, None), live_changed("x", None)
    (False, True, False, True)
    """
    return live != base


def applies_cleanly(repo: Path, sha: str, path: str) -> bool:
    """Say whether commit ``sha`` would cherry-pick onto the live ``HEAD`` without a conflict.

    Uses ``git merge-tree --write-tree`` (git 2.38+), which merges exactly as a cherry-pick does; on an
    older git it falls back to the stricter ``git apply --check`` of the commit's patch.
    """
    merged = gitops.run_git(repo, "merge-tree", "--write-tree", f"--merge-base={sha}^", "HEAD", sha, check=False)
    if merged.returncode in (0, _CONFLICT_EXIT):
        return merged.returncode == 0
    patch = gitops.run_git(repo, "show", "--format=", "--patch", "--no-color", "--no-ext-diff", sha, "--", path).stdout
    return gitops.run_git(repo, "apply", "--check", input_text=patch, check=False).returncode == 0


def effect_of(removed: list[str], added: list[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Reduce a change to the sets of removed and added lines, the definition of "same effect".

    >>> effect_of(["a", "b"], ["c"]) == effect_of(["b", "a", "a"], ["c"])
    True
    """
    return frozenset(removed), frozenset(added)


def format_change_text(section: str) -> str:
    """Return a report section's ``**Change:**`` line (else its first line), cut to ``CHANGE_TEXT_CAP``."""
    return gitops.change_line(section, CHANGE_TEXT_CAP)


def change_diff(repo: Path, sha: str) -> str:
    """Return commit ``sha``'s zero-context diff (the added and removed lines are all the plan needs)."""
    return gitops.run_git(repo, "show", "--format=", "--patch", "-U0", "--no-color", "--no-ext-diff", sha).stdout


def _plan_one(root: Path, run_id: str, change: runner.RunChange, section: str, diff: str,
              duplicate: dict[str, Any] | None) -> PlanChange:
    """Build the :class:`PlanChange` for one pending change from its ``diff`` and report ``section``."""
    path = memory_index.repo_path_of(root, change.sha)
    removed, added = memory_index.diff_lines(diff)
    live = read_live(root, path) if path else None
    changed = live_changed(live, read_base(root, change.sha, path)) if path else None
    clean = applies_cleanly(root, change.sha, path or change.file)
    entrywise = None
    if not clean and runner.is_index_update(change) and path:
        entrywise = memory_index.reapply(live or "", diff).text is not None
    return PlanChange(run_id, change.n, change.file, change.action, format_change_text(section),
                      len(added), len(removed), changed, clean, entrywise, duplicate)


def build_plan(run_ids: list[str]) -> Plan:
    """Summarise the pending changes of ``run_ids`` (oldest run first, whatever the argument order).

    Applied (including marked-applied) and rejected changes are left out of ``changes`` and counted in
    ``skipped``, but they still count as earlier changes for ``duplicate_of``.

    Example::

        plan = build_plan(["proj-261102-1830"])
        [(c.n, c.live_changed, c.applies_cleanly) for c in plan.changes]   # [(1, False, True), (3, False, True)]

    Raises:
        runner.RunError: an unknown run, or one that is finished (discarded, superseded, ...) or whose branch is gone.
    """
    root = dstate.memory_root()
    records = {run_id: runner.get_run(run_id) for run_id in dict.fromkeys(run_ids)}
    ordered = sorted(records.items(), key=lambda item: item[1].get("created", ""))
    seen: dict[tuple, dict[str, Any]] = {}
    planned: list[PlanChange] = []
    skipped = 0
    for run_id, record in ordered:
        if record.get("status") in runner.INACTIVE_STATUSES:
            raise runner.RunError(f"run {run_id} is {record['status']}; nothing to plan")
        if not gitops.branch_exists(root, record["branch"]):
            raise runner.RunError(f"branch {record['branch']} no longer exists")
        sections = gitops.parse_report_sections(runner.report_text(record))
        for change in runner.run_changes(record):
            diff = change_diff(root, change.sha)
            removed, added = memory_index.diff_lines(diff)
            key = (record.get("slug"), change.file, effect_of(removed, added))
            status = change_status(change)
            if status == STATUS_PENDING:
                duplicate = seen.get(key) if (removed or added) else None
                planned.append(_plan_one(root, run_id, change, gitops.section_for(sections, change.file), diff,
                                         duplicate))
            else:
                skipped += 1
            seen.setdefault(key, {"run_id": run_id, "n": change.n, "status": status})
    return Plan(planned, skipped)


def plan_to_json(plan: Plan) -> dict[str, Any]:
    """Return the ``plan --json`` object: ``{"changes": [...], "skipped": <count>}``."""
    return {"changes": [asdict(change) for change in plan.changes], "skipped": plan.skipped}


def _flags(change: PlanChange) -> str:
    """Render the live/apply/duplicate facts of a change as short words (upper case marks a warning)."""
    live = {None: "live=?", False: "live=same", True: "LIVE-CHANGED"}[change.live_changed]
    if change.applies_cleanly:
        apply = "clean"
    else:
        apply = "CONFLICTS" + {None: "", True: " (entry-wise ok)", False: " (entry-wise conflict)"}[change.entrywise_ok]
    flags = [live, apply]
    if change.duplicate_of:
        dup = change.duplicate_of
        flags.append(f"DUP-OF {dup['run_id']}#{dup['n']} ({dup['status']})")
    return " ".join(flags)


def format_change(change: PlanChange) -> str:
    """Render one change as a compact line plus its wrapped ``Change:`` text."""
    head = (f"{change.run_id} #{change.n} {change.action} {change.file} "
            f"+{change.added}/-{change.removed} {_flags(change)}")
    if not change.change:
        return head
    return head + "\n" + textwrap.fill(f"Change: {change.change}", WRAP_WIDTH, initial_indent="    ",
                                       subsequent_indent="    ")


def format_plan(plan: Plan) -> str:
    """Render the plan as text: one block per pending change, then a note of how many changes were skipped."""
    if not plan.changes:
        return "No pending changes." + (f" ({plan.skipped} applied or rejected skipped)" if plan.skipped else "")
    blocks = [format_change(change) for change in plan.changes]
    if plan.skipped:
        blocks.append(f"({plan.skipped} applied or rejected change(s) skipped)")
    return "\n".join(blocks)
