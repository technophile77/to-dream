"""Entry-wise handling of ``MEMORY.md``, the index of lines like ``- [Title](file.md) — hook``.

A cherry-pick of an index change conflicts as soon as live ``MEMORY.md`` gained or lost any line near
it. The index is a set of independent lines keyed by their link target (``file.md``), so a change can
be re-applied one entry at a time: replace the live line for a key, append a new one, or delete one.
An entry whose live line is missing or was edited since the run is a conflict and nothing is applied.

Example::

    outcome = reapply(live_text, diff_text)
    if outcome.text is not None:
        path.write_text(outcome.text)   # every entry applied
    else:
        print(outcome.conflicts)        # e.g. ['b.md: the live line was edited since the run']
"""

import re
from dataclasses import dataclass
from pathlib import Path

from to_dream import gitops

INDEX_NAME = "MEMORY.md"
_ENTRY = re.compile(r"^[ \t]*[-*][ \t]+\[[^\]]*\]\(([^)\s]+)\)")
_HUNK = re.compile(r"^@@ ", re.MULTILINE)
ENTRYWISE_NOTE = "applied entry-wise after a cherry-pick conflict"


@dataclass(frozen=True)
class EntryEdit:
    """One index entry changed by a diff: ``removed`` and/or ``added`` line text (None when absent), keyed by ``key``."""

    key: str
    removed: str | None = None
    added: str | None = None


@dataclass(frozen=True)
class Reapplied:
    """Result of :func:`reapply`: the new file ``text`` (None if anything conflicted) and the ``conflicts`` found."""

    text: str | None
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntrywiseOutcome:
    """Result of :func:`apply_entrywise`: the commit ``sha`` on success, else ``conflicts`` / ``message`` say why."""

    sha: str | None
    conflicts: tuple[str, ...] = ()
    message: str = ""


def entry_key(line: str) -> str | None:
    """Return the link target of an index line (``- [Title](file.md) — hook``), or None if it is not an entry.

    >>> entry_key("- [Tabs](tabs.md) — prefers tabs")
    'tabs.md'
    >>> entry_key("# Memory") is None
    True
    """
    match = _ENTRY.match(line)
    return match[1] if match else None


def diff_lines(diff: str) -> tuple[list[str], list[str]]:
    """Return the ``(removed, added)`` line texts of a unified diff (file headers and context are ignored).

    >>> diff_lines("--- a/M\\n+++ b/M\\n@@ -1 +1 @@\\n-old\\n+new\\n")
    (['old'], ['new'])
    """
    start = _HUNK.search(diff)
    removed: list[str] = []
    added: list[str] = []
    for line in (diff[start.start():] if start else "").splitlines():
        if line.startswith("-"):
            removed.append(line[1:].rstrip("\r"))
        elif line.startswith("+"):
            added.append(line[1:].rstrip("\r"))
    return removed, added


def entry_edits(diff: str) -> tuple[list[EntryEdit], list[str]]:
    """Group a diff's changed lines into one :class:`EntryEdit` per key; return them with the problems found.

    Blank lines are ignored. A changed line without a link target, or several removed/added lines for
    one key, cannot be applied entry by entry and is reported as a problem instead.

    >>> entry_edits("@@ -1 +1,2 @@\\n-- [A](a.md) old\\n+- [A](a.md) new\\n+- [B](b.md) x\\n")
    ([EntryEdit(key='a.md', removed='- [A](a.md) old', added='- [A](a.md) new'), EntryEdit(key='b.md', removed=None, added='- [B](b.md) x')], [])
    """
    removed, added = diff_lines(diff)
    problems: list[str] = []
    by_key: dict[str, dict[str, list[str]]] = {}
    for side, lines in (("removed", removed), ("added", added)):
        for line in lines:
            if not line.strip():
                continue
            key = entry_key(line)
            if key is None:
                problems.append(f"not an index entry (no link target): {line.strip()[:80]}")
                continue
            by_key.setdefault(key, {"removed": [], "added": []})[side].append(line)
    edits = []
    for key, sides in by_key.items():
        if len(sides["removed"]) > 1 or len(sides["added"]) > 1:
            problems.append(f"{key}: several changed lines for one entry")
            continue
        edits.append(EntryEdit(key, (sides["removed"] or [None])[0], (sides["added"] or [None])[0]))
    return edits, problems


def _live_line_index(lines: list[str], key: str) -> list[int]:
    """Return the positions of the index lines whose link target is ``key``."""
    return [i for i, line in enumerate(lines) if entry_key(line) == key]


def _plan_edit(lines: list[str], edit: EntryEdit) -> tuple[str, int | None, str | None]:
    """Decide one edit against the live ``lines``: ``(problem, None, None)``, or ``("", position, new line)``.

    ``position`` is the live line to rewrite (``new line`` None deletes it); a ``None`` position with a
    new line means "append"; ``("", None, None)`` means the live file already has the wanted state.
    """
    found = _live_line_index(lines, edit.key)
    live = lines[found[0]] if found else None
    if len(found) > 1:
        return f"{edit.key}: several live lines for this entry", None, None
    if edit.removed is None:  # added only
        if live is None:
            return "", None, edit.added
        return ("", None, None) if live == edit.added else (f"{edit.key}: already in live MEMORY.md with different text", None, None)
    if live is None:
        return f"{edit.key}: not in live MEMORY.md", None, None
    if live == edit.removed:
        return "", found[0], edit.added
    if edit.added is not None and live == edit.added:
        return "", None, None  # already has the new text
    return f"{edit.key}: the live line was edited since the run", None, None


def reapply(live_text: str, diff: str) -> Reapplied:
    """Re-apply an index diff to ``live_text`` entry by entry; ``text`` is None when any entry conflicts.

    A key removed and added replaces the live line, added only appends after the last index line,
    removed only deletes the live line. A removed line whose key is not live, or whose live text differs
    from the diff's removed line, is a conflict and nothing is applied. The file's line endings (LF or
    CRLF) and its trailing newline are kept.

    >>> reapply("# Memory\\n- [A](a.md) old\\n", "@@ -2 +2,2 @@\\n-- [A](a.md) old\\n+- [A](a.md) new\\n+- [B](b.md) x\\n").text
    '# Memory\\n- [A](a.md) new\\n- [B](b.md) x\\n'
    >>> reapply("# Memory\\n", "@@ -2 +1 @@\\n-- [A](a.md) old\\n").conflicts
    ('a.md: not in live MEMORY.md',)
    """
    edits, problems = entry_edits(diff)
    crlf = "\r\n" in live_text
    lines = live_text.replace("\r\n", "\n").split("\n")
    replaced: dict[int, str | None] = {}
    appended: list[str] = []
    for edit in edits:
        problem, position, new_line = _plan_edit(lines, edit)
        if problem:
            problems.append(problem)
        elif position is not None:
            replaced[position] = new_line
        elif new_line is not None:
            appended.append(new_line)
    if problems:
        return Reapplied(None, tuple(problems))
    result = [replaced.get(i, line) for i, line in enumerate(lines)]
    last_entry = max((i for i, line in enumerate(lines) if entry_key(line) is not None), default=None)
    at = len(result) if last_entry is None else last_entry + 1
    if last_entry is None and result and result[-1] == "":
        at = len(result) - 1  # keep the trailing newline after the appended lines
    result[at:at] = appended
    text = "\n".join(line for line in result if line is not None)
    return Reapplied(text.replace("\n", "\r\n") if crlf else text)


def repo_path_of(repo: Path, sha: str) -> str | None:
    """Return the single repo-relative path commit ``sha`` touches, or None if it touches several."""
    paths = gitops.changed_paths(repo, sha)
    return paths[0] if len(paths) == 1 else None


def index_diff(repo: Path, sha: str, path: str) -> str:
    """Return commit ``sha``'s zero-context diff of ``path`` (what :func:`reapply` reads)."""
    return gitops.run_git(repo, "show", "--format=", "--patch", "-U0", "--no-color", "--no-ext-diff",
                          sha, "--", path).stdout


def apply_entrywise(repo: Path, sha: str) -> EntrywiseOutcome:
    """Re-apply the ``MEMORY.md`` change in commit ``sha`` onto the live file entry by entry and commit it.

    The commit message is the original one plus a note naming ``sha``; the file goes through the same
    secret scan as every other commit. Nothing is written when an entry conflicts, the file has uncommitted
    edits (``apply`` snapshots first, so this only happens when the snapshot refused it) or the commit
    touches more than one path; in those cases ``sha`` is None and ``conflicts`` / ``message`` say why.

    Example::

        outcome = apply_entrywise(root, change.sha)
        if outcome.sha is None:
            print("; ".join(outcome.conflicts) or outcome.message)
    """
    path = repo_path_of(repo, sha)
    if path is None:
        return EntrywiseOutcome(None, message="the commit does not change exactly one file")
    target = repo / path
    if gitops.run_git(repo, "status", "--porcelain", "--", path).stdout.strip():
        return EntrywiseOutcome(None, message=f"{path} has uncommitted changes")
    live = target.read_bytes().decode("utf-8", "replace") if target.is_file() else ""
    outcome = reapply(live, index_diff(repo, sha, path))
    if outcome.text is None:
        return EntrywiseOutcome(None, outcome.conflicts)
    target.write_bytes(outcome.text.encode("utf-8"))
    original = gitops.run_git(repo, "log", "-1", "--format=%B", sha).stdout.rstrip()
    message = f"{original}\n\n(applied entry-wise from commit {sha})\n"
    committed = gitops.commit_change(repo, gitops.FileChange(path, gitops.ACTION_UPDATE), message)
    if committed.sha is None:
        gitops.run_git(repo, "checkout", "HEAD", "--", path)
        return EntrywiseOutcome(None, message=committed.error or "commit failed")
    return EntrywiseOutcome(committed.sha)
