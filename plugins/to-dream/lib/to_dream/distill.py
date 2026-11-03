"""Distil Claude Code session transcripts into compact, redacted, evidence-rich markdown digests.

Transcripts (``~/.claude/projects/<slug>/<session>.jsonl`` plus
``<session>/subagents/agent-<id>.jsonl``) can be hundreds of MB, so each is streamed line by
line and reduced to the events an analyst needs: human prompts, user corrections, tool errors,
denials, repeated failures and in-band memory writes. Every entry cites ``L<n>``, the 1-based
line of the source JSONL, and every string written passes through
:func:`to_dream.redact.redact`. Times are shown in UTC, as stored.

Example::

    digests = distill_project(Path.home() / ".claude/projects/-home-sam-proj", Path("out"))
    write_index(digests, Path("out"))
"""

import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple

from to_dream import paths, redact

PROMPT_CHARS = 600
CORRECTION_CONTEXT_CHARS = 300
ERROR_CHARS = 300
COMMAND_CHARS = 200
TITLE_CHARS = 120
RAW_CAP = 4000  # raw chars redacted before clipping, so a secret cannot straddle a clip point
HARNESS_CAP = 100_000  # longest prompt scanned for harness blocks
CORRECTION_SCAN_CHARS = 800
RESULT_SCAN_CHARS = 2000
TOOL_MAP_LIMIT = 2000
REPEAT_ROWS = 20
MAX_BRANCHES = 5
NOTE_RESERVE = 160  # room kept for the "dropped for size" header line
MEMORY_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Read"})
NON_HUMAN_PROMPT_SOURCES = frozenset({"system", "sdk"})
UNKNOWN_TOOL = "unknown-tool"

_RELEVANT_TYPES = (b'"user"', b'"assistant"', b'"ai-title"')  # loose: tolerates spaced JSON
_TYPE_PREFIX = b'{"type":"'
_PARSED_TYPES = frozenset({b"user", b"assistant", b"ai-title"})
_HARNESS_TAGS = (
    "system-reminder|task-notification|command-name|command-message|local-command-stdout|"
    "local-command-stderr|local-command-caveat|ide_opened_file|ide_selection|ide_diagnostics|"
    "user-prompt-submit-hook|hook_additional_context"
)
_HARNESS_BLOCK = re.compile(rf"<({_HARNESS_TAGS})\b[^>]*>.*?(?:</\1>|\Z)", re.DOTALL)
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_SYSTEM_NOTICE = re.compile(r"\[SYSTEM NOTIFICATION.*?(?:\n\s*\n|\Z)", re.DOTALL)
_CONTINUATION = "This session is being continued from a previous conversation"
_CORRECTION = re.compile(
    r"""(?ix)
    ^\s*no\b | \bdon['’]?t\b | \bdo\ not\b | \bstop\b | \bwrong\b | \bnot\ what\ i\b
    | \bi\ said\b | \bi\ asked\b | \binstead\b | \bactually\b | \bwhy\ did\ you\b
    | \brevert\b | \bundo\b | \bthat['’]?s\ not\b | \byou\ forgot\b | \bagain\s*\?
    """
)
_DENY_STRONG = re.compile(
    r"doesn.t want to proceed|tool use was rejected|\[Request interrupted by user for tool use\]",
    re.IGNORECASE,
)
_DENY_WORDS = re.compile(r"was rejected|\bdenied\b|hook error|\bblocked\b", re.IGNORECASE)
_ENTRY = re.compile(r"^- \[(?P<hm>\d\d:\d\d|--:--)\] L(?P<line>\d+) (?P<tag>\S+)")

PRIORITY = {"PROMPT": 0, "MEMORY-WRITE": 1, "ERROR": 2, "**DENIED**": 3, "**CORRECTION**": 3}


@dataclass(frozen=True)
class SessionDigest:
    """Summary of one written digest file (counts are totals in the window, before size trimming)."""

    session_id: str
    digest_path: Path
    source: Path
    is_subagent: bool
    title: str | None
    cwd: str | None
    first_ts: str | None
    last_ts: str | None
    human_prompts: int
    corrections: int
    tool_errors: int
    denials: int
    memory_writes: int
    chars: int


@dataclass
class DistillStats:
    """Counters that let a caller notice a transcript format it no longer understands.

    ``files`` counts non-empty transcripts that were read (not skipped as older than ``since``);
    ``records`` counts the user/assistant records among them that parsed, in or out of the window.
    ``files > 0`` with ``records == 0`` means the format probably changed.

    Example::

        stats = DistillStats()
        distill_project(project, out, stats=stats)
        stats.format_changed  # False for healthy transcripts
    """

    files: int = 0
    records: int = 0

    @property
    def format_changed(self) -> bool:
        """Say whether transcripts were read but none of their records could be understood."""
        return self.files > 0 and self.records == 0


class Entry(NamedTuple):
    """One timeline item: source line, timestamp, tag (a key of ``PRIORITY``) and rendered text."""

    line: int
    ts: str | None
    tag: str
    text: str

    def render(self) -> str:
        """Render as a single markdown bullet: ``- [HH:MM] L<line> <tag> <text>``."""
        return f"- [{hhmm(self.ts)}] L{self.line} {self.tag} {self.text}"


def hhmm(ts: str | None) -> str:
    """Return the ``HH:MM`` part of an ISO timestamp, or ``--:--`` if absent.

    >>> hhmm("2026-11-02T14:05:09.123Z")
    '14:05'
    """
    return ts[11:16] if ts and len(ts) >= 16 else "--:--"


def parse_ts(ts: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` allowed; naive means UTC); None if absent or malformed.

    >>> parse_ts("2026-11-02T14:05:09.123Z").isoformat()
    '2026-11-02T14:05:09.123000+00:00'
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _aware(moment: datetime | None) -> datetime | None:
    """Treat a naive datetime as UTC."""
    return moment.replace(tzinfo=timezone.utc) if moment and moment.tzinfo is None else moment


def clip(text: str, limit: int, total: int | None = None) -> str:
    """Collapse whitespace and cut to limit chars, noting how many were cut as ``…[+N chars]``.

    ``total`` is the length of the text before any earlier capping, so the count stays honest.

    >>> clip("a  b\\nc", 10)
    'a b c'
    >>> clip("x" * 12, 5)
    'xxxxx…[+7 chars]'
    """
    flat = " ".join(text.split())
    full = max(total if total is not None else len(flat), len(flat))
    return flat if full <= limit else f"{flat[:limit]}…[+{full - limit} chars]"


def safe(text: Any, limit: int) -> str:
    """Redact then clip an arbitrary value for inclusion in a digest (redaction comes first)."""
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    return clip(redact.redact(raw[:RAW_CAP]), limit, len(raw))


def strip_harness(text: str) -> str:
    """Remove harness-injected blocks (system reminders, notifications, IDE context) from text.

    >>> strip_harness("<system-reminder>x</system-reminder>fix the bug")
    'fix the bug'
    """
    text = _COMMAND_ARGS.sub(r"\1", text[:HARNESS_CAP])
    text = _HARNESS_BLOCK.sub("", text)
    return _SYSTEM_NOTICE.sub("", text).strip()


def is_correction(text: str) -> bool:
    """Say whether a human prompt pushes back on the assistant.

    >>> is_correction("No, use the other file"), is_correction("stop adding logging")
    (True, True)
    >>> is_correction("please add a test")
    False
    """
    return bool(_CORRECTION.search(text[:CORRECTION_SCAN_CHARS]))


def _blocks(content: Any) -> list[dict]:
    """Normalise message content (string or block list) into a list of block dicts."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text_of(blocks: Iterable[dict]) -> str:
    """Join the ``text`` blocks of a block list with newlines."""
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str))


def human_prompt(record: dict) -> tuple[str, int] | None:
    """Return ``(text, original_length)`` if a user record is a genuine human prompt, else None.

    Tool results, compaction records, non-human origins and anything that is empty after
    :func:`strip_harness` are rejected. ``origin.kind == "human"`` is authoritative (even with
    ``promptSource == "sdk"``, as the VS Code extension reports); ``isMeta`` and
    ``promptSource`` system/sdk are only consulted when origin is absent.

    >>> human_prompt({"message": {"content": "<system-reminder>r</system-reminder>hi"}})
    ('hi', 38)
    """
    if record.get("isCompactSummary"):
        return None
    origin = record.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind not in (None, "human"):
        return None
    if kind is None and (record.get("isMeta") or record.get("promptSource") in NON_HUMAN_PROMPT_SOURCES):
        return None  # heuristics apply only when origin is absent: the VS Code extension reports human prompts as "sdk"
    message = record.get("message")
    blocks = _blocks(message.get("content") if isinstance(message, dict) else None)
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    raw = _text_of(blocks)
    text = strip_harness(raw)
    if not text or text.startswith(_CONTINUATION):
        return None
    return text, len(raw)


def tool_summary(name: str, tool_input: Any) -> str:
    """Compactly describe a tool call: Bash command, file path, or the first few key=value pairs.

    >>> tool_summary("Edit", {"file_path": "/a/b.py", "old_string": "x"})
    '/a/b.py'
    >>> tool_summary("Grep", {"pattern": "foo", "path": "src"})
    'pattern=foo, path=src'
    """
    if not isinstance(tool_input, dict):
        return ""
    if name == "Bash" and isinstance(tool_input.get("command"), str):
        return safe(tool_input["command"], COMMAND_CHARS)
    if name in FILE_TOOLS and isinstance(tool_input.get("file_path"), str):
        return safe(tool_input["file_path"], COMMAND_CHARS)
    return safe(", ".join(f"{k}={safe(v, 40)}" for k, v in list(tool_input.items())[:3]), COMMAND_CHARS)


def _result_text(block: dict) -> str:
    """Extract (up to ``RESULT_SCAN_CHARS`` of) the text of a ``tool_result`` block."""
    content = block.get("content")
    text = content if isinstance(content, str) else _text_of(_blocks(content))
    return text[:RESULT_SCAN_CHARS]


def is_denial(text: str, is_error: bool, denial_kind: Any = None) -> bool:
    """Say whether a tool result reports a user rejection or hook block.

    Strong phrasing counts even without ``is_error``; looser words (``denied``, ``blocked``)
    only count on errors, so a successful read of a file mentioning them is not flagged.

    >>> is_denial("The user doesn't want to proceed with this tool use.", True)
    True
    >>> is_denial("this file mentions denied", False)
    False
    """
    head = text[:500]
    if _DENY_STRONG.search(head):
        return True
    return bool((is_error or denial_kind) and _DENY_WORDS.search(head))


class _Collector:
    """Accumulates the events of one transcript window as records are fed in file order."""

    def __init__(self, is_subagent: bool, memory_root: Path, since: datetime | None, until: datetime | None):
        self.is_subagent = is_subagent
        self.memory_root = str(memory_root)
        self.since, self.until = since, until
        self.entries: list[Entry] = []
        self.tools: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self.failures: Counter = Counter()
        self.counts: Counter = Counter()
        self.cwds: Counter = Counter()
        self.branches: dict[str, None] = {}
        self.title: str | None = None
        self.session_id: str | None = None
        self.first_ts: str | None = None
        self.last_ts: str | None = None
        self.records = 0
        self.parsed = 0  # user/assistant records understood, whatever their timestamp
        self._ts: str | None = None
        self._assistant_tail = ""

    def in_window(self, ts: str | None) -> bool:
        """Say whether a timestamp lies inside ``[since, until]`` (unknown times fail a set bound)."""
        if self.since is None and self.until is None:
            return True
        moment = parse_ts(ts)
        if moment is None:
            return False
        return (self.since is None or moment >= self.since) and (self.until is None or moment <= self.until)

    def feed(self, lineno: int, record: dict) -> None:
        """Process one decoded transcript record (line number is 1-based)."""
        kind = record.get("type")
        if kind == "ai-title":
            if isinstance(record.get("aiTitle"), str):
                self.title = safe(record["aiTitle"], TITLE_CHARS)
            return
        message = record.get("message")
        if kind not in ("user", "assistant") or not isinstance(message, dict):
            return
        self.parsed += 1
        ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else self._ts
        self._ts = ts
        blocks = _blocks(message.get("content"))
        windowed = self.in_window(ts)
        if kind == "assistant":
            self._assistant(lineno, ts, blocks, windowed)
        elif windowed:
            self._user(lineno, ts, record, blocks)
        if windowed:
            self._note(record, ts)

    def _note(self, record: dict, ts: str | None) -> None:
        """Record per-record metadata (window bounds, cwd, branch, session id)."""
        self.records += 1
        self.first_ts = self.first_ts or ts
        self.last_ts = ts or self.last_ts
        if isinstance(record.get("cwd"), str):
            self.cwds[record["cwd"]] += 1
        branch = record.get("gitBranch")
        if isinstance(branch, str) and branch and len(self.branches) < MAX_BRANCHES:
            self.branches[branch] = None
        if self.session_id is None and isinstance(record.get("sessionId"), str):
            self.session_id = record["sessionId"]

    def _assistant(self, lineno: int, ts: str | None, blocks: list[dict], windowed: bool) -> None:
        """Remember tool calls (always) and last assistant text; log memory writes (in window)."""
        text = _text_of(blocks)
        if text.strip():
            self._assistant_tail = text[-CORRECTION_CONTEXT_CHARS * 2:]
        for block in blocks:
            if block.get("type") != "tool_use" or not isinstance(block.get("name"), str):
                continue
            name, tool_input = block["name"], block.get("input")
            if isinstance(block.get("id"), str):
                self.tools[block["id"]] = (name, tool_summary(name, tool_input))
                if len(self.tools) > TOOL_MAP_LIMIT:
                    self.tools.popitem(last=False)
            if windowed and name in MEMORY_TOOLS and isinstance(tool_input, dict):
                self._memory_write(lineno, ts, name, tool_input.get("file_path"))

    def _memory_write(self, lineno: int, ts: str | None, tool: str, file_path: Any) -> None:
        """Log an edit/write whose target is inside an auto-memory directory."""
        if isinstance(file_path, str) and paths.is_memory_path(file_path, self.memory_root):
            self.counts["memory_writes"] += 1
            filename = file_path.replace("\\", "/").rsplit("/", 1)[-1]
            self.entries.append(Entry(lineno, ts, "MEMORY-WRITE", f"{tool} {safe(filename, COMMAND_CHARS)}"))

    def _user(self, lineno: int, ts: str | None, record: dict, blocks: list[dict]) -> None:
        """Handle a user record: tool results become failures, otherwise maybe a human prompt."""
        results = [b for b in blocks if b.get("type") == "tool_result"]
        for block in results:
            self._tool_result(lineno, ts, record, block)
        if results or self.is_subagent:
            return
        prompt = human_prompt(record)
        if prompt is None:
            return
        text, total = prompt
        self.counts["human_prompts"] += 1
        shown = safe(text, PROMPT_CHARS)
        if is_correction(text):
            self.counts["corrections"] += 1
            context = safe(self._assistant_tail[-CORRECTION_CONTEXT_CHARS:], CORRECTION_CONTEXT_CHARS)
            suffix = f' (assistant had said: "{context}")' if context else ""
            self.entries.append(Entry(lineno, ts, "**CORRECTION**", f"{shown}{suffix}"))
        else:
            self.entries.append(Entry(lineno, ts, "PROMPT", shown))

    def _tool_result(self, lineno: int, ts: str | None, record: dict, block: dict) -> None:
        """Log an erroring or denied tool result, resolving the tool name from earlier tool_use."""
        text = _result_text(block)
        is_error = block.get("is_error") is True
        denied = is_denial(text, is_error, record.get("toolDenialKind"))
        if not (is_error or denied):
            return
        name, summary = self.tools.get(block.get("tool_use_id"), (UNKNOWN_TOOL, ""))
        self.failures[(name, summary)] += 1
        self.counts["denials" if denied else "tool_errors"] += 1
        tag = "**DENIED**" if denied else "ERROR"
        self.entries.append(Entry(lineno, ts, tag, f"{name} `{summary}`: {safe(text, ERROR_CHARS)}"))


def _stream(path: Path) -> Iterable[tuple[int, dict]]:
    """Yield ``(line_number, record)`` for user/assistant/ai-title lines, skipping the rest cheaply."""
    with open(path, "rb") as handle:
        for lineno, line in enumerate(handle, 1):
            if line.startswith(_TYPE_PREFIX):
                if line[len(_TYPE_PREFIX):len(_TYPE_PREFIX) + 12].split(b'"', 1)[0] not in _PARSED_TYPES:
                    continue
            elif not any(marker in line for marker in _RELEVANT_TYPES):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield lineno, record


def is_subagent_path(path: Path) -> bool:
    """Say whether a transcript path is a subagent file (``.../subagents/agent-<id>.jsonl``)."""
    return path.parent.name == "subagents"


def _identity(path: Path) -> tuple[str, str]:
    """Return ``(session_id, name_suffix)`` derived from the path (parent session for subagents)."""
    if is_subagent_path(path):
        agent = path.stem.removeprefix("agent-")
        return path.parent.parent.name, f"_agent-{agent[:8]}"
    return path.stem, ""


def _read_meta(path: Path) -> dict:
    """Load the ``agent-<id>.meta.json`` sibling of a subagent transcript ({} if absent/bad)."""
    try:
        meta = json.loads(path.with_name(path.stem + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def find_transcripts(project_dir: Path) -> list[Path]:
    """List main-session and subagent transcripts of a project dir (memory/ and tool-results/ ignored).

    >>> [p.name for p in find_transcripts(Path("proj"))]  # doctest: +SKIP
    ['aaaa.jsonl', 'agent-1.jsonl']
    """
    mains = [p for p in project_dir.glob("*.jsonl") if p.is_file()]
    subs = [p for p in project_dir.glob("*/subagents/agent-*.jsonl") if p.is_file()]
    return sorted(mains) + sorted(subs)


def _header(
    ident: str, source: Path, is_sub: bool, col: _Collector, meta: dict, title: str | None, dropped: Counter
) -> list[str]:
    """Build the digest header lines (counts are totals; ``dropped`` lists size-trimmed entries)."""
    c = col.counts
    cwd = col.cwds.most_common(1)[0][0] if col.cwds else None
    lines = [
        f"# Session digest {ident[:8]}{' (subagent)' if is_sub else ''}",
        f"- session: {safe(ident, 80)}",
        f"- source: {safe(str(source), 300)}",
        f"- title: {title or '(none)'}",
    ]
    if is_sub:
        lines.append(
            f"- agent: type={safe(meta.get('agentType'), 60)} model={safe(meta.get('model'), 60)} "
            f"description={safe(meta.get('description'), 200)}"
        )
    lines += [
        f"- cwd: {safe(cwd, 200) if cwd else '(unknown)'}",
        f"- branches: {safe(', '.join(col.branches), 200) or '(unknown)'}",
        f"- window: {col.first_ts or '?'} .. {col.last_ts or '?'} (UTC; refs L<n> = line in source jsonl)",
        "- counts: " + " ".join(
            f"{k}={c[k]}" for k in ("human_prompts", "corrections", "tool_errors", "denials", "memory_writes")
        ),
    ]
    if dropped:
        lines.append("- dropped for size: " + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
    return lines


def _repeat_lines(failures: Counter) -> list[str]:
    """Render the ``(tool, input)`` pairs that failed at least twice, most frequent first."""
    repeated = [(k, n) for k, n in failures.most_common() if n >= 2][:REPEAT_ROWS]
    return [f"- {name} `{summary}` x{n}" for (name, summary), n in repeated]


def _compose(header: list[str], timeline: list[str], repeats: list[str]) -> str:
    """Assemble header, timeline and repeated-failure sections into the digest body."""
    parts = [*header, "", "## Timeline", *(timeline or ["(no events)"]), "", "## Repeated failures"]
    parts += repeats or ["(none)"]
    return "\n".join(parts) + "\n"


def trim_entries(entries: list[Entry], budget: int) -> tuple[list[Entry], Counter]:
    """Drop lowest-priority entries (latest first within a priority) until rendering fits budget.

    Returns the surviving entries in original order and a Counter of dropped tags.

    >>> kept, dropped = trim_entries([Entry(1, None, "PROMPT", "x" * 50), Entry(2, None, "ERROR", "y")], 40)
    >>> [e.tag for e in kept], dict(dropped)
    (['ERROR'], {'PROMPT': 1})
    """
    sizes = [len(e.render()) + 1 for e in entries]
    total = sum(sizes)
    drop: set[int] = set()
    for i in sorted(range(len(entries)), key=lambda i: (PRIORITY[entries[i].tag], -i)):
        if total <= budget:
            break
        total -= sizes[i]
        drop.add(i)
    return [e for i, e in enumerate(entries) if i not in drop], Counter(entries[i].tag for i in drop)


def _digest_name(first_ts: str | None, ident: str, suffix: str) -> str:
    """Name a digest ``<yyyymmdd-hhmm>_<session8>[_agent-<id8>].md`` from the window start."""
    moment = parse_ts(first_ts)
    stamp = moment.strftime("%Y%m%d-%H%M") if moment else "00000000-0000"
    return f"{stamp}_{ident[:8]}{suffix}.md"


def distill_transcript(
    path: Path,
    out_dir: Path,
    since: datetime | None,
    until: datetime | None,
    memory_root: Path,
    max_chars: int = 20000,
    stats: DistillStats | None = None,
) -> SessionDigest | None:
    """Stream one transcript and write its digest into out_dir; None if there is nothing to report.

    Nothing to report means no user/assistant record inside ``[since, until]`` (inclusive; records
    without any timestamp fail a bound), or - for subagents - no errors, denials or memory writes.
    A file last modified before ``since`` is skipped without being read. The body never exceeds
    ``max_chars``: lowest-priority entries (plain prompts, then memory writes, errors, then
    corrections/denials) are dropped first and the header notes how many.

    Args:
        path: main-session or subagent ``.jsonl`` transcript.
        out_dir: directory for the digest (created if missing).
        since, until: inclusive window bounds (timezone-aware; naive is taken as UTC) or None.
        memory_root: the projects root (``~/.claude/projects``) used to recognise memory writes.
        max_chars: upper bound for the digest body, in characters.
        stats: optional counters, updated with this file's contribution (see :class:`DistillStats`).

    Example::

        d = distill_transcript(Path("s.jsonl"), Path("out"), None, None, Path.home() / ".claude/projects")
        d.digest_path.name  # '20261102-1405_8650bccb.md'

    Raises:
        OSError: if the transcript cannot be opened or the digest cannot be written.
    """
    since, until = _aware(since), _aware(until)
    status = path.stat()
    if since is not None and datetime.fromtimestamp(status.st_mtime, timezone.utc) < since:
        return None
    is_sub = is_subagent_path(path)
    ident, suffix = _identity(path)
    col = _Collector(is_sub, memory_root, since, until)
    for lineno, record in _stream(path):
        col.feed(lineno, record)
    if stats is not None and status.st_size > 0:
        stats.files += 1
        stats.records += col.parsed
    if col.records == 0 or (is_sub and not col.entries):
        return None
    meta = _read_meta(path) if is_sub else {}
    repeats = _repeat_lines(col.failures)
    fixed = len(_compose(_header(ident, path, is_sub, col, meta, col.title, Counter()), [], repeats))
    kept, dropped = trim_entries(col.entries, max_chars - fixed - NOTE_RESERVE)
    header = _header(ident, path, is_sub, col, meta, col.title, dropped)
    body = _compose(header, [e.render() for e in kept], repeats)
    if len(body) > max_chars:
        body = body[: max(max_chars - 16, 0)] + "\n…[truncated]\n"
        body = body[:max_chars]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / _digest_name(col.first_ts, ident, suffix)
    target.write_text(body, encoding="utf-8", newline="\n")
    cwd = col.cwds.most_common(1)[0][0] if col.cwds else None
    c = col.counts
    return SessionDigest(
        session_id=ident, digest_path=target, source=path, is_subagent=is_sub, title=col.title,
        cwd=cwd, first_ts=col.first_ts, last_ts=col.last_ts, human_prompts=c["human_prompts"],
        corrections=c["corrections"], tool_errors=c["tool_errors"], denials=c["denials"],
        memory_writes=c["memory_writes"], chars=len(body),
    )


def _sort_key(digest: SessionDigest) -> datetime:
    """Sort key placing digests by last activity (undated ones oldest)."""
    return parse_ts(digest.last_ts) or datetime.min.replace(tzinfo=timezone.utc)


def distill_project(
    project_dir: Path,
    out_dir: Path,
    since: datetime | None = None,
    until: datetime | None = None,
    max_chars: int = 20000,
    max_sessions: int | None = None,
    stats: DistillStats | None = None,
) -> list[SessionDigest]:
    """Distil every transcript of a project into out_dir and return digests, newest first.

    With ``max_sessions`` only the most recent N (by last timestamp) are kept; files are read in
    modification-time order and reading stops once no remaining file can be newer than the Nth,
    and digest files of the others are not left behind. Pass ``stats`` to learn whether any record
    of the transcripts read could be parsed at all (:class:`DistillStats`).

    Example::

        digests = distill_project(proj, Path("out"), since=datetime.now(timezone.utc) - timedelta(days=7))
        [d.digest_path.name for d in digests][:1]  # newest first
    """
    memory_root = project_dir.parent
    files = sorted(find_transcripts(project_dir), key=lambda p: p.stat().st_mtime, reverse=True)
    digests: list[SessionDigest] = []
    for path in files:
        if max_sessions is not None and len(digests) >= max_sessions:
            nth = sorted(map(_sort_key, digests), reverse=True)[max_sessions - 1]
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < nth:
                break
        digest = distill_transcript(path, out_dir, since, until, memory_root, max_chars, stats)
        if digest is not None:
            digests.append(digest)
    digests.sort(key=_sort_key, reverse=True)
    if max_sessions is not None:
        for extra in digests[max_sessions:]:
            extra.digest_path.unlink(missing_ok=True)
        digests = digests[:max_sessions]
    return digests


def _cell(text: Any) -> str:
    """Make a value safe for a markdown table cell."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def write_index(digests: list[SessionDigest], out_dir: Path) -> Path:
    """Write ``INDEX.md`` (newest first table of digests with counts and totals) and return its path.

    Example::

        write_index(distill_project(proj, out), out).name  # 'INDEX.md'
    """
    ordered = sorted(digests, key=_sort_key, reverse=True)
    rows = [
        "| digest | session | title | window (UTC) | prompts | corrections | errors | denials | memory writes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for d in ordered:
        kind = "subagent " if d.is_subagent else ""
        rows.append(
            f"| [{_cell(d.digest_path.name)}]({_cell(d.digest_path.name)}) | {kind}{_cell(d.session_id[:8])} "
            f"| {_cell(d.title)} | {_cell(d.first_ts)} .. {_cell(d.last_ts)} | {d.human_prompts} "
            f"| {d.corrections} | {d.tool_errors} | {d.denials} | {d.memory_writes} |"
        )
    totals = {
        "prompts": sum(d.human_prompts for d in ordered), "corrections": sum(d.corrections for d in ordered),
        "errors": sum(d.tool_errors for d in ordered), "denials": sum(d.denials for d in ordered),
        "memory writes": sum(d.memory_writes for d in ordered),
    }
    lines = [
        "# Digest index", "",
        f"{len(ordered)} digests ({sum(d.is_subagent for d in ordered)} subagent).", "", *rows, "",
        "**Totals:** " + ", ".join(f"{k}={v}" for k, v in totals.items()),
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "INDEX.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return target


def project_cwd(digests: Iterable[SessionDigest]) -> str | None:
    """Return the most common cwd across digests (the project's source dir), or None if unknown.

    >>> project_cwd([])  # doctest: +SKIP
    """
    counts = Counter(d.cwd for d in digests if d.cwd)
    return counts.most_common(1)[0][0] if counts else None
