"""Permission rules for the dream: read the current ones safely, parse the suggestions it writes.

The dream proposes ``allow`` / ``ask`` rules, so it must see what is already configured. Settings files
can also hold secrets (``env`` blocks, API-key helpers), therefore only the ``permissions`` object is ever
read, and only the keys in ``PERMISSION_KEYS`` of it; every string is passed through :func:`redact.redact`.

Example::

    path, targets = write_permissions(run_dir, "-home-sam-proj", "/home/sam/proj")
    targets["project-local"]  # '/home/sam/proj/.claude/settings.local.json'

    parse_permission_suggestions("- **Rule:** `Bash(npm test *)` · **Kind:** allow · **Scope:** project-local")
    # [{'rule': 'Bash(npm test *)', 'kind': 'allow', 'scope': 'project-local', ...}]
"""

import re
from pathlib import Path
from typing import Any

from to_dream import redact, state as dstate

PERMISSIONS_NAME = "permissions.md"
PERMISSION_KEYS = ("allow", "ask", "deny", "defaultMode", "additionalDirectories")
LIST_KEYS = ("allow", "ask", "deny", "additionalDirectories")
SCOPE_USER = "user"
SCOPE_PROJECT = "project"
SCOPE_LOCAL = "project-local"
SCOPES = (SCOPE_LOCAL, SCOPE_PROJECT, SCOPE_USER)
SCOPE_TITLES = {
    SCOPE_USER: "User settings",
    SCOPE_PROJECT: "Project shared settings",
    SCOPE_LOCAL: "Project local settings",
}
SETTINGS_FILE = "settings.json"
LOCAL_SETTINGS_FILE = "settings.local.json"
KINDS = ("allow", "ask")
SUGGESTION_FIELDS = ("rule", "kind", "scope", "evidence", "risk", "why")

PermissionTargets = dict[str, str | None]

_BLOCK_START = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])?[ \t]*\*\*[ \t]*Rule\b", re.IGNORECASE | re.MULTILINE)
_LABEL = re.compile(
    r"\*\*[ \t]*(Rule|Kind|Scope|Evidence|Risk|Why)[ \t]*:?[ \t]*\*\*[ \t]*:?[ \t]*", re.IGNORECASE
)
_CODE_SPAN = re.compile(r"^(`+)[ \t]*(.+?)[ \t]*\1")
_TRAILING_SEPARATORS = " \t\r\n·|;,•*+-"  # field separators and the bullet marker of the next line


# --------------------------------------------------------------------------- locating settings files


def find_git_root(start: Path) -> Path | None:
    """Return the nearest directory at or above ``start`` that contains ``.git`` (a directory or a worktree file).

    Returns None if ``start`` is not an existing directory or no ancestor has ``.git``.

    Example::

        find_git_root(Path("/home/sam/proj/src"))  # PosixPath('/home/sam/proj')
    """
    if not start.is_dir():
        return None
    return next((d for d in (start, *start.parents) if (d / ".git").exists()), None)


def project_roots(project_dir: str | Path | None) -> list[Path]:
    """List the directories whose ``.claude`` folder holds project settings: ``project_dir``, plus the git root.

    The git root (nearest ancestor with ``.git``, e.g. when ``project_dir`` is a subdirectory or a worktree)
    is only added when ``project_dir/.claude`` does not exist. Empty when ``project_dir`` is unknown.
    """
    if not project_dir:
        return []
    start = Path(project_dir)
    roots = [start]
    if not (start / ".claude").exists():
        git_root = find_git_root(start)
        if git_root is not None and git_root != start:
            roots.append(git_root)
    return roots


def permission_targets(project_dir: str | Path | None) -> PermissionTargets:
    """Return the absolute settings file a review should edit for each suggestion scope.

    Project scopes point into the last of :func:`project_roots` (the git root when ``project_dir`` has no
    ``.claude`` of its own, else ``project_dir``) and are None when the project directory is unknown.
    The files need not exist.

    >>> permission_targets(None)["project"] is None
    True
    """
    roots = project_roots(project_dir)
    targets: PermissionTargets = {
        SCOPE_LOCAL: None, SCOPE_PROJECT: None, SCOPE_USER: (dstate.claude_home() / SETTINGS_FILE).as_posix(),
    }
    if roots:
        targets[SCOPE_LOCAL] = (roots[-1] / ".claude" / LOCAL_SETTINGS_FILE).as_posix()
        targets[SCOPE_PROJECT] = (roots[-1] / ".claude" / SETTINGS_FILE).as_posix()
    return targets


def settings_files(project_dir: str | Path | None) -> list[tuple[str, Path]]:
    """List ``(scope, path)`` of every settings file to report on: user, then shared and local per project root.

    Paths that resolve to the same file are listed once (the first scope wins).
    """
    candidates = [(SCOPE_USER, dstate.claude_home() / SETTINGS_FILE)]
    for root in project_roots(project_dir):
        candidates.append((SCOPE_PROJECT, root / ".claude" / SETTINGS_FILE))
        candidates.append((SCOPE_LOCAL, root / ".claude" / LOCAL_SETTINGS_FILE))
    seen: set[str] = set()
    files = []
    for scope, path in candidates:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            files.append((scope, path))
    return files


# --------------------------------------------------------------------------- reading the permissions object


def read_permissions(path: Path) -> tuple[str, dict[str, Any]]:
    """Read only the whitelisted keys of a settings file's ``permissions`` object.

    Returns ``(status, permissions)``; ``status`` is ``"ok"``, ``"missing"``, ``"invalid JSON"``, ``"unreadable"``,
    ``"not an object"`` or ``"no permissions object"``, and ``permissions`` is empty unless ``ok``. Nothing
    outside ``permissions`` (``env``, ``apiKeyHelper``, hooks...) is ever returned. Never raises.

    Example::

        read_permissions(Path("~/.claude/settings.json").expanduser())
        # ('ok', {'allow': ['Bash(git status)']})
    """
    try:
        data = dstate.read_json_file(path)
    except FileNotFoundError:
        return "missing", {}
    except ValueError:
        return "invalid JSON", {}
    except OSError:
        return "unreadable", {}
    if not isinstance(data, dict):
        return "not an object", {}
    block = data.get("permissions")
    if not isinstance(block, dict):
        return "no permissions object", {}
    return "ok", {key: block[key] for key in PERMISSION_KEYS if key in block}


def _code(text: str) -> str:
    """Render redacted text as a markdown code span on one line (longer fence if it holds backticks)."""
    flat = " ".join(redact.redact(text).split())
    if "`" not in flat:
        return f"`{flat}`"
    return f"`` {flat} ``"


def render_rules(permissions: dict[str, Any]) -> list[str]:
    """Render a permissions dict as markdown bullets (strings redacted, non-string list items dropped).

    >>> render_rules({"allow": ["Bash(ls)"]})[0]
    '- allow:'
    """
    lines = []
    for key in LIST_KEYS:
        if key not in permissions:
            continue
        items = [item for item in permissions[key] if isinstance(item, str)] if isinstance(permissions[key], list) else []
        lines.append(f"- {key}:" + ("" if items else " (none)"))
        lines += [f"  - {_code(item)}" for item in items]
    mode = permissions.get("defaultMode")
    if isinstance(mode, str):
        lines.append(f"- defaultMode: {_code(mode)}")
    return lines or ["(no rules set)"]


def render_permissions(slug: str, files: list[tuple[str, Path]], targets: PermissionTargets) -> str:
    """Build the text of ``permissions.md``: edit targets per scope, then one section per settings file."""
    lines = [
        f"# Current permission rules: {slug}", "",
        "Only the `permissions` object of each settings file is shown here; nothing else is read.", "",
        "## Edit targets (absolute paths; a file may not exist yet)", "",
    ]
    for scope in SCOPES:
        target = targets.get(scope)
        lines.append(f"- {scope}: " + (_code(target) if target else "(unknown: project directory not known)"))
    for scope, path in files:
        status, permissions = read_permissions(path)
        lines += ["", f"## {SCOPE_TITLES[scope]} (scope: {scope})", "", f"File: {_code(path.as_posix())}", ""]
        lines += render_rules(permissions) if status == "ok" else [f"({'file missing' if status == 'missing' else status})"]
    return "\n".join(lines) + "\n"


def write_permissions(run_dir: Path, slug: str, project_dir: str | Path | None) -> tuple[Path, PermissionTargets]:
    """Write ``run_dir/permissions.md`` for the dream; return its path and the edit target of each scope.

    Example::

        path, targets = write_permissions(run_dir, "-home-sam-proj", "/home/sam/proj")
    """
    targets = permission_targets(project_dir)
    path = run_dir / PERMISSIONS_NAME
    path.write_text(render_permissions(slug, settings_files(project_dir), targets), encoding="utf-8", newline="\n")
    return path, targets


# --------------------------------------------------------------------------- parsing the suggestions


def _field_value(raw: str, unwrap_code: bool = False) -> str:
    """Clean one labelled value: trim trailing separators and whitespace, collapse spaces.

    With ``unwrap_code`` a leading code span is the whole value (a rule may itself contain ``·`` or ``|``).
    """
    text = raw.strip()
    span = _CODE_SPAN.match(text) if unwrap_code else None
    if span:
        return span[2].strip()
    return " ".join(text.rstrip(_TRAILING_SEPARATORS).split())


def normalise_scope(text: str) -> str:
    """Map scope wording to ``project-local``, ``project`` or ``user`` (unknown text is lower-cased and hyphenated).

    >>> normalise_scope("Project Local"), normalise_scope("project-shared"), normalise_scope("User")
    ('project-local', 'project', 'user')
    """
    words = re.sub(r"[^a-z]+", "-", text.lower()).strip("-")
    if "local" in words:
        return SCOPE_LOCAL
    if words.startswith(SCOPE_PROJECT):
        return SCOPE_PROJECT
    if words.startswith(SCOPE_USER):
        return SCOPE_USER
    return words


def normalise_kind(text: str) -> str:
    """Return ``allow`` or ``ask`` if the text mentions one (case-insensitive), else the lower-cased text."""
    lowered = text.lower()
    return next((kind for kind in KINDS if re.search(rf"\b{kind}\b", lowered)), lowered.strip())


def _parse_block(block: str) -> dict[str, str] | None:
    """Parse one suggestion block into the six fields, or None if it names no rule."""
    marks = list(_LABEL.finditer(block))
    fields = dict.fromkeys(SUGGESTION_FIELDS, "")
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(block)
        name = mark[1].lower()
        if not fields[name]:
            fields[name] = _field_value(block[mark.end():end], unwrap_code=name == "rule")
    if not fields["rule"]:
        return None
    fields["kind"] = normalise_kind(fields["kind"])
    fields["scope"] = normalise_scope(fields["scope"])
    return fields


def parse_permission_suggestions(text: str) -> list[dict[str, str]]:
    """Parse the ``## Permission suggestions`` section body into a list of suggestion dicts.

    Each entry starts at a ``**Rule:**`` bullet; the labels Rule, Kind, Scope, Evidence, Risk and Why
    (bold, colon inside or outside the bold, any order, bullets or none) delimit the values. Missing fields
    are ``""``; ``kind`` becomes ``allow``/``ask`` and ``scope`` ``project-local``/``project``/``user`` when
    recognisable. Blocks without a rule (and text such as "None.") yield nothing.

    Example::

        parse_permission_suggestions(
            "- **Rule:** `Bash(npm test *)` · **Kind:** allow · **Scope:** project-local\\n"
            "  - **Evidence:** a.md L1 · blocked 4×\\n"
            "  - **Risk:** runs project tests\\n"
            "  - **Why:** prompted every session"
        )
        # [{'rule': 'Bash(npm test *)', 'kind': 'allow', 'scope': 'project-local',
        #   'evidence': 'a.md L1 · blocked 4×', 'risk': 'runs project tests', 'why': 'prompted every session'}]
    """
    starts = [m.start() for m in _BLOCK_START.finditer(text)]
    blocks = [text[begin:end] for begin, end in zip(starts, starts[1:] + [len(text)])]
    return [entry for entry in map(_parse_block, blocks) if entry]
