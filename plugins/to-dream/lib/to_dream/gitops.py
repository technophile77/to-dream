"""Git plumbing for dreaming: snapshots, worktrees, per-file commits, scope enforcement, apply and discard.

All git calls run with ``cwd=`` set (never ``git -C`` / ``git -c``) and with
``GIT_LITERAL_PATHSPECS=1`` so file names such as ``a[1].md`` or ``*.md`` are never globbed.
Commit identity comes from the usual git config or ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` env vars.

No git hook is needed: every commit this module makes (proposal or snapshot) is scanned in-process
for secrets (:mod:`to_dream.redact`) before it is created. A hook the user installed on their own
still runs, and its rejection is handled like a failed scan.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from to_dream import redact

LITERAL_PATHSPECS_ENV = {"GIT_LITERAL_PATHSPECS": "1"}
FALLBACK_SLUG = "x"
BODY_CAP = 4000
TRUNCATION_MARK = "\n[truncated]"
COMMIT_SCOPE = "to-dream"
RUN_TRAILER = "To-Dream-Run"

ACTION_ADD = "add"
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"
ACTION_DROP = "drop"  # new file left blank: nothing to commit, just remove it

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_BULLET = re.compile(r"^(?:[-*>]\s+)+")
_SUBJECT = re.compile(rf"^chore\({COMMIT_SCOPE}\): (add|update|delete) [^/\s]+/(.+)$")
_CHANGE_LINE = re.compile(r"^[ 	>*-]*\*\*Change:?\*\*:?[ 	]*(.+?)[ 	]*$", re.MULTILINE)
_LEVEL2_HEADING = re.compile(r"^##[ 	]+(.+?)[ 	]*$", re.MULTILINE)
_MEMORY_SCOPE = re.compile(r"^[^/]+/memory/")
DIFF_CAP = 8000
WHY_CAP = 100
_REPORT_HEADING = re.compile(r"^###[ \t]+(.+?)[ \t]*$", re.MULTILINE)


class GitError(RuntimeError):
    """Raised when a git command that must succeed fails; the message carries git's stderr."""


@dataclass(frozen=True)
class FileChange:
    """One changed file inside the memory scope; ``path`` is repo-relative with ``/`` separators."""

    path: str
    action: str


@dataclass(frozen=True)
class CommitOutcome:
    """Result of committing one file: ``sha`` on success, else ``error`` (a secret-scan refusal or hook output)."""

    change: FileChange
    sha: str | None
    error: str | None = None


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of cherry-picking: ``applied`` maps source sha to new sha; ``failed`` is the sha that stopped it."""

    applied: dict[str, str]
    failed: str | None = None
    message: str = ""


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of :func:`snapshot_memory`.

    ``sha`` is the snapshot commit (None when nothing was committed), ``committed`` the files it holds and
    ``refused`` the ``(path, reason)`` pairs left uncommitted because the secret scan (or a hook) objected.
    """

    sha: str | None = None
    committed: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()


def run_git(
    cwd: Path | str,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd`` and return the completed process (text, UTF-8, errors replaced).

    Raises:
        GitError: git exited non-zero and ``check`` is true.

    Example::

        run_git(".", "rev-parse", "--is-inside-work-tree").stdout.strip()  # 'true'
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=None if input_text is None else input_text.encode("utf-8"),
        capture_output=True,
        env={**os.environ, **LITERAL_PATHSPECS_ENV},
    )
    done = subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )
    if check and done.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done


# --------------------------------------------------------------------------- naming


def slugify(text: str, cap: int | None = None) -> str:
    """Lowercase ``text`` to ``[a-z0-9-]``: other characters become ``-``, runs collapse, ends trim.

    The optional ``cap`` truncates first and then trims a trailing ``-``; an empty result is ``x``.

    >>> slugify("My_Project/Claude #Memory")
    'my-project-claude-memory'
    >>> slugify("abcdefghij-klmnop", cap=11)
    'abcdefghij'
    """
    slug = _NON_SLUG.sub("-", text.lower()).strip("-")
    if cap is not None:
        slug = slug[:cap].rstrip("-")
    return slug or FALLBACK_SLUG


def unique_path(parent: Path, name: str) -> Path:
    """Return ``parent/name``, or ``parent/name_2``, ``_3``... for the first one that does not exist."""
    candidate = parent / name
    counter = 2
    while candidate.exists():
        candidate = parent / f"{name}_{counter}"
        counter += 1
    return candidate


# --------------------------------------------------------------------------- repo queries


def current_branch(repo: Path) -> str:
    """Return the checked-out branch name of ``repo`` (empty string on a detached HEAD)."""
    return run_git(repo, "branch", "--show-current").stdout.strip()


def branch_exists(repo: Path, branch: str) -> bool:
    """Say whether local branch ``branch`` exists."""
    return run_git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def valid_branch_name(repo: Path, branch: str) -> bool:
    """Say whether ``branch`` is acceptable as a local branch name (``git check-ref-format``)."""
    return run_git(repo, "check-ref-format", f"refs/heads/{branch}", check=False).returncode == 0


def rev_parse(repo: Path, rev: str) -> str:
    """Resolve ``rev`` to a full commit sha."""
    return run_git(repo, "rev-parse", f"{rev}^{{commit}}").stdout.strip()


def has_uncommitted_changes(repo: Path) -> bool:
    """Say whether the working tree or index of ``repo`` has tracked or untracked (non-ignored) changes."""
    return bool(run_git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip())


def in_memory_scope(path: str) -> bool:
    """Say whether a repo-relative path is under ``<project>/memory/`` (one project level).

    >>> in_memory_scope("proj/memory/a.md"), in_memory_scope("proj/notes/a.md"), in_memory_scope(".gitignore")
    (True, False, False)
    """
    return bool(_MEMORY_SCOPE.match(path))


def dirty_outside_memory(repo: Path) -> list[str]:
    """List tracked files with uncommitted changes that are NOT under ``*/memory/`` (untracked files ignored).

    These cannot be snapshotted, and they can make a cherry-pick fail, so ``apply`` refuses to run while any exist.
    """
    raw = run_git(repo, "status", "--porcelain", "-z", "--untracked-files=no").stdout
    return [path for _code, path in parse_porcelain_z(raw) if not in_memory_scope(path)]


# --------------------------------------------------------------------------- secret scan and snapshot


def refusal_reason(findings: Iterable[redact.Finding]) -> str:
    """Describe scan findings as ``path:line rule`` pairs (never the matched values).

    >>> refusal_reason([redact.Finding("p/memory/a.md", 3, "jwt")])
    'possible secret refused (values not shown): p/memory/a.md:3 jwt'
    """
    items = [redact.format_finding(f) for f in findings]
    noun = "secret" if len(items) == 1 else "secrets"
    return f"possible {noun} refused (values not shown): " + ", ".join(items)


def scan_staged(repo: Path, path: str) -> list[redact.Finding]:
    """Scan the staged added lines of one file; findings are labelled with the repo-relative ``path``.

    Raises:
        GitError: the staged diff cannot be read (callers must fail closed).
    """
    diff = run_git(repo, "diff", "--cached", "--no-renames", "-U0", "--no-color", "--no-ext-diff", "--", path).stdout
    return [redact.Finding(path, f.line, f.rule) for f in redact.scan_diff(diff)]


def _unstage(repo: Path, paths: list[str]) -> None:
    """Remove ``paths`` from the index, leaving the working tree alone (best effort)."""
    if paths:
        run_git(repo, "reset", "-q", "--pathspec-from-file=-", "--pathspec-file-nul",
                input_text="\0".join(paths) + "\0", check=False)


def snapshot_memory(repo: Path, label: str) -> SnapshotResult:
    """Commit uncommitted changes under ``*/memory/**`` as ``chore(to-dream): snapshot before <label>``.

    Each changed file is staged and scanned in-process; a file with findings is unstaged, left dirty and
    reported in ``refused`` as ``path:line rule`` text. Files outside the memory scope are never touched.
    If a user-installed hook rejects the commit, nothing stays staged and every file is reported refused.

    Example::

        snapshot_memory(Path.home() / ".claude/projects", "run proj-261102-1830").sha  # None if already clean

    Raises:
        GitError: staging or scanning failed for a reason other than a refusal.
    """
    raw = run_git(repo, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    paths = sorted({path for _code, path in parse_porcelain_z(raw) if in_memory_scope(path)})
    if not paths:
        return SnapshotResult()
    run_git(repo, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input_text="\0".join(paths) + "\0")
    refused: list[tuple[str, str]] = []
    for path in paths:
        findings = scan_staged(repo, path)
        if findings:
            refused.append((path, refusal_reason(findings)))
    blocked = {path for path, _ in refused}
    _unstage(repo, sorted(blocked))
    keep = [p for p in paths if p not in blocked]
    if not keep:
        return SnapshotResult(refused=tuple(refused))
    result = run_git(
        repo, "commit", "-q", "-m", f"chore({COMMIT_SCOPE}): snapshot before {label}",
        "--pathspec-from-file=-", "--pathspec-file-nul", input_text="\0".join(keep) + "\0", check=False,
    )
    if result.returncode != 0:
        _unstage(repo, keep)
        detail = (result.stderr.strip() or result.stdout.strip())[:500] or "commit rejected"
        return SnapshotResult(refused=tuple(refused) + tuple((p, detail) for p in keep))
    return SnapshotResult(rev_parse(repo, "HEAD"), tuple(keep), tuple(refused))


# --------------------------------------------------------------------------- worktree


def create_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    """Create ``branch`` from ``base`` checked out in a new worktree at ``path`` (cwd = ``repo``).

    Raises:
        GitError: the branch exists, ``base`` is unknown or the path is unusable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(repo, "worktree", "add", str(path), "-b", branch, base)


def remove_worktree(repo: Path, path: Path, force: bool = False) -> None:
    """Remove the worktree at ``path`` (``--force`` also drops local modifications) and prune stale entries.

    Raises:
        GitError: git refuses (e.g. the worktree has changes and ``force`` is false).
    """
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(path)]
    run_git(repo, *args)
    run_git(repo, "worktree", "prune")


def delete_branch(repo: Path, branch: str) -> None:
    """Force-delete local ``branch`` (``git branch -D``); only dream cleanup/discard may call this."""
    run_git(repo, "branch", "-D", branch)


# --------------------------------------------------------------------------- change detection


def parse_porcelain_z(raw: str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain -z`` output into ``(XY, path)`` pairs (rename origins are skipped).

    >>> parse_porcelain_z(" M a.md\\0?? b c.md\\0")
    [(' M', 'a.md'), ('??', 'b c.md')]
    """
    entries = [e for e in raw.split("\0") if e]
    pairs: list[tuple[str, str]] = []
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False
            continue
        code, path = entry[:2], entry[3:]
        skip_next = "R" in code or "C" in code
        pairs.append((code, path))
    return pairs


def is_blank_file(path: Path) -> bool:
    """Say whether ``path`` is a file that is empty or whitespace-only (the "delete me" convention)."""
    try:
        return path.is_file() and not path.read_bytes().strip()
    except OSError:
        return False


def classify(code: str, path: Path) -> str:
    """Map a porcelain ``XY`` code (and the on-disk file) to an ``ACTION_*``.

    A file that is blank on disk counts as deleted (or dropped, if it was never committed), because
    the headless Claude has no shell to delete with.
    """
    if "D" in code:
        return ACTION_DELETE
    is_new = code == "??" or code[0] == "A"
    if is_blank_file(path):
        return ACTION_DROP if is_new else ACTION_DELETE
    return ACTION_ADD if is_new else ACTION_UPDATE


def split_scope(entries: Iterable[tuple[str, str]], prefix: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Partition porcelain entries into those under ``prefix`` (e.g. ``"slug/memory/"``) and the rest."""
    inside, outside = [], []
    for entry in entries:
        (inside if entry[1].startswith(prefix) else outside).append(entry)
    return inside, outside


def collect_changes(worktree: Path, prefix: str) -> tuple[list[FileChange], list[tuple[str, str]]]:
    """Return ``(in-scope changes sorted by path, out-of-scope porcelain entries)`` for a worktree.

    Example::

        collect_changes(Path("wt"), "slug/memory/")
        # ([FileChange(path='slug/memory/a.md', action='add')], [])
    """
    raw = run_git(worktree, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    inside, outside = split_scope(parse_porcelain_z(raw), prefix)
    changes = [FileChange(path, classify(code, worktree / path)) for code, path in inside]
    return sorted(changes, key=lambda c: c.path), outside


def _remove_with_empty_parents(stop: Path, file: Path) -> None:
    """Delete ``file`` and then any now-empty parent directories below ``stop``."""
    file.unlink(missing_ok=True)
    parent = file.parent
    while parent != stop and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def revert_entries(worktree: Path, entries: Iterable[tuple[str, str]]) -> list[str]:
    """Undo out-of-scope changes: restore files known to HEAD, remove files that are new; return their paths."""
    reverted = []
    for _code, path in entries:
        in_head = run_git(worktree, "cat-file", "-e", f"HEAD:{path}", check=False).returncode == 0
        run_git(worktree, "reset", "-q", "HEAD", "--", path, check=False)
        if in_head:
            run_git(worktree, "checkout", "HEAD", "--", path)
        else:
            _remove_with_empty_parents(worktree, worktree / path)
        reverted.append(path)
    return reverted


def remove_dropped(worktree: Path, changes: Iterable[FileChange]) -> None:
    """Delete the blank, never-committed files marked ``ACTION_DROP`` from the worktree."""
    for change in changes:
        if change.action == ACTION_DROP:
            (worktree / change.path).unlink(missing_ok=True)


# --------------------------------------------------------------------------- report sections


def parse_report_sections(report: str) -> dict[str, str]:
    """Split a DREAM_REPORT.md into ``{filename: body}`` for each ``### <filename>`` heading.

    Backticks around the heading text are ignored; the body runs to the next ``###`` (or ``##``/``#``)
    heading.

    >>> parse_report_sections("# R\\n### `a.md`\\nWhy a\\n### b.md\\nWhy b\\n")
    {'a.md': 'Why a', 'b.md': 'Why b'}
    """
    sections: dict[str, str] = {}
    matches = list(_REPORT_HEADING.finditer(report))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(report)
        body = re.split(r"^#{1,2}[ \t]", report[match.end():end], maxsplit=1, flags=re.MULTILINE)[0]
        sections[match[1].strip().strip("`").strip()] = body.strip()
    return sections


def level2_sections(report: str) -> dict[str, str]:
    """Split a report into ``{normalised title: body}`` for each ``## Title`` section.

    Titles are lower-cased with runs of non-alphanumerics turned into ``_``; a body runs to the next
    ``#`` / ``##`` heading, so ``###`` per-file subsections stay inside their parent.

    >>> level2_sections("# R\\n## Stale candidates\\n- x\\n## Not acted on\\nnone\\n")
    {'stale_candidates': '- x', 'not_acted_on': 'none'}
    """
    sections: dict[str, str] = {}
    matches = list(_LEVEL2_HEADING.finditer(report))
    for match in matches:
        body = re.split(r"^#{1,2}[ 	]", report[match.end():], maxsplit=1, flags=re.MULTILINE)[0]
        sections[re.sub(r"[^a-z0-9]+", "_", match[1].lower()).strip("_")] = body.strip()
    return sections


def change_line(section: str, limit: int = WHY_CAP) -> str:
    """Return the text of a section's ``**Change:**`` line (label stripped), else its first line ('' if empty).

    The result is cut to ``limit`` characters.

    >>> change_line("**Change:** add tabs preference\\nmore")
    'add tabs preference'
    """
    match = _CHANGE_LINE.search(section)
    if not match:
        return first_line(section, limit)
    text = match[1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_subject(subject: str) -> tuple[str, str] | None:
    """Split a ``chore(to-dream): <action> <slug-short>/<file>`` subject into ``(action, file)``, or None.

    >>> parse_subject("chore(to-dream): delete st/sub/b.md")
    ('delete', 'sub/b.md')
    """
    match = _SUBJECT.match(subject)
    return (match[1], match[2]) if match else None


def commit_diff(repo: Path, sha: str, cap: int = DIFF_CAP) -> tuple[str, bool]:
    """Return ``(unified diff of commit sha, truncated)``, the diff cut to ``cap`` characters."""
    text = run_git(repo, "show", "--format=", "--patch", sha).stdout
    return (text[:cap], True) if len(text) > cap else (text, False)


def section_for(sections: dict[str, str], relative_file: str) -> str:
    """Pick the report section for a memory file: exact relative name first, then its base name, else ''."""
    if relative_file in sections:
        return sections[relative_file]
    base = relative_file.rsplit("/", 1)[-1]
    return sections.get(base, "")


def cap_text(text: str, limit: int = BODY_CAP) -> str:
    """Trim ``text`` to at most ``limit`` characters, marking truncation."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARK)].rstrip() + TRUNCATION_MARK


def first_line(text: str, limit: int = 120) -> str:
    """Return the first non-blank line of ``text`` (markdown bullet marks stripped), cut to ``limit`` chars."""
    for line in text.splitlines():
        stripped = _BULLET.sub("", line.strip())
        if stripped:
            return stripped if len(stripped) <= limit else stripped[: limit - 1].rstrip() + "…"
    return ""


# --------------------------------------------------------------------------- committing


def commit_message(slug_short: str, change: FileChange, relative_file: str, section: str, run_id: str) -> str:
    """Compose ``chore(to-dream): <action> <slug-short>/<file>`` plus the report section and a run trailer.

    >>> commit_message("st", FileChange("p/memory/a.md", "add"), "a.md", "New fact.", "st-261102-1830")
    'chore(to-dream): add st/a.md\\n\\nNew fact.\\n\\nTo-Dream-Run: st-261102-1830\\n'
    """
    parts = [f"chore({COMMIT_SCOPE}): {change.action} {slug_short}/{relative_file}"]
    if section.strip():
        parts.append(cap_text(section))
    parts.append(f"{RUN_TRAILER}: {run_id}")
    return "\n\n".join(parts) + "\n"


def commit_change(worktree: Path, change: FileChange, message: str) -> CommitOutcome:
    """Stage exactly one file, scan it for secrets in-process, then commit it.

    A file whose added lines trip the scanner is unstaged and refused with a ``path:line rule`` reason
    (never the value). A commit rejected by a user-installed hook is unstaged and reports the hook's
    output. In both cases the file is left as it is on disk (uncommitted).

    Example::

        commit_change(wt, FileChange("proj/memory/a.md", "update"), "chore(to-dream): update p/a.md\\n").sha
    """
    path = change.path
    if change.action == ACTION_DELETE:
        staged = run_git(worktree, "rm", "-q", "-f", "--ignore-unmatch", "--", path, check=False)
    else:
        staged = run_git(worktree, "add", "--", path, check=False)
    if staged.returncode != 0:
        return CommitOutcome(change, None, staged.stderr.strip())
    if change.action != ACTION_DELETE:
        try:
            findings = scan_staged(worktree, path)
        except GitError as exc:
            run_git(worktree, "reset", "-q", "HEAD", "--", path, check=False)
            return CommitOutcome(change, None, f"secret scan could not run: {exc}"[:500])
        if findings:
            run_git(worktree, "reset", "-q", "HEAD", "--", path, check=False)
            return CommitOutcome(change, None, refusal_reason(findings))
    result = run_git(worktree, "commit", "-q", "-F", "-", input_text=message, check=False)
    if result.returncode != 0:
        run_git(worktree, "reset", "-q", "HEAD", "--", path, check=False)
        detail = (result.stderr.strip() or result.stdout.strip())[:500]
        return CommitOutcome(change, None, detail or "commit rejected")
    return CommitOutcome(change, rev_parse(worktree, "HEAD"))


# --------------------------------------------------------------------------- inspecting / applying


def list_commits(repo: Path, base: str, branch: str) -> list[tuple[str, str]]:
    """Return ``(sha, subject)`` for commits on ``branch`` since ``base``, oldest first."""
    out = run_git(repo, "log", "--reverse", "--format=%H%x00%s", f"{base}..{branch}").stdout
    return [tuple(line.split("\0", 1)) for line in out.splitlines() if line]  # type: ignore[misc]


def changed_paths(repo: Path, sha: str) -> list[str]:
    """Return the paths touched by commit ``sha``."""
    out = run_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", sha).stdout
    return [p for p in out.split("\0") if p]


def merge_base(repo: Path, left: str, right: str) -> str:
    """Return the merge-base commit of two revisions."""
    return run_git(repo, "merge-base", left, right).stdout.strip()


def cherry_pick_one(repo: Path, sha: str) -> ApplyResult:
    """Cherry-pick ``sha`` onto the branch checked out in ``repo`` (``-x`` records the origin).

    On failure the pick is aborted (``git cherry-pick --abort``) so the repo is left clean and the
    result has ``failed`` set to ``sha``; on success ``applied`` maps ``sha`` to the new commit.
    """
    result = run_git(repo, "cherry-pick", "-x", sha, check=False)
    if result.returncode != 0:
        run_git(repo, "cherry-pick", "--abort", check=False)
        return ApplyResult({}, sha, (result.stderr.strip() or result.stdout.strip())[:500])
    return ApplyResult({sha: rev_parse(repo, "HEAD")})


def cherry_pick_all(repo: Path, shas: Iterable[str]) -> ApplyResult:
    """Cherry-pick ``shas`` in order onto the branch checked out in ``repo`` (``-x`` records the origin).

    On the first failure the pick is aborted (``git cherry-pick --abort``) so the repo is left clean,
    earlier successful picks are kept, and the failing sha is reported.
    """
    applied: dict[str, str] = {}
    for sha in shas:
        picked = cherry_pick_one(repo, sha)
        if picked.failed:
            return ApplyResult(applied, sha, picked.message)
        applied.update(picked.applied)
    return ApplyResult(applied)


def tool_owned(code: str, path: str) -> bool:
    """Say whether an untracked porcelain entry is something Claude/the tool leaves behind (``.claude/`` dirs)."""
    return code == "??" and (path.startswith(".claude/") or "/.claude/" in path)


def only_tool_files_dirty(worktree: Path) -> bool:
    """Say whether the worktree is clean apart from the tool's own untracked files (so ``--force`` is safe)."""
    raw = run_git(worktree, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    return all(tool_owned(code, path) for code, path in parse_porcelain_z(raw))
