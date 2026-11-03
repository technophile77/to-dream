"""Secret detection and masking, run in-process (no git hook needed).

Two uses: :func:`redact` masks secret-looking spans before text reaches a digest or a model, and
:func:`scan_diff` / :func:`scan_text` find secrets in lines about to be committed so the commit can
be refused. Findings carry ``path:line`` and a rule name, never the matched value.

Add ``secret-scan: allow`` to a line to exempt it from scanning (redaction ignores the marker).

Example::

    >>> redact("password: " + "hunter2" + "hunter2")
    'password: [REDACTED:generic-secret-assignment]'
"""

import re
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

ALLOW_MARKER = "secret-scan: allow"
BLOCK_BANNER = "secret-scan: blocked - possible secrets (values not shown)"

GENERIC_RULE = "generic-secret-assignment"

TOKEN_RULES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("private-key-header", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        ("aws-access-key-id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"),
        ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
        ("anthropic-api-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
        ("openai-api-key", r"\bsk-(?!ant-)(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}"),
        ("stripe-live-key", r"\b[sr]k_live_[A-Za-z0-9]{16,}"),
        ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
    )
)

_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b[\w.-]*(?:password|passwd|secret|api[_-]?key|token)
    (?:_?(?:access_)?(?:key|value))?
    [\"'*`]*\s*[:=]\s*[\"']?
    (?P<value>[^\s\"',;`]{8,})
    """
)
_PLACEHOLDER = re.compile(
    r"x{3,}|\*{3,}|changeme|change[-_]me|example|placeholder|redacted|dummy|your[-_]|\.\.\.",
    re.IGNORECASE,
)
_PLACEHOLDER_START = ("<", "$", "{", "%", "[", "*")
_ENV_REF = re.compile(r"^(?:process\.env|os\.environ|os\.getenv|ENV\[)")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class Finding(NamedTuple):
    """One suspected secret: file, 1-based line number in the new file, and rule name."""

    path: str
    line: int
    rule: str


def is_placeholder(value: str) -> bool:
    """Say whether an assigned value is an obvious non-secret (template, env reference, code).

    >>> is_placeholder("<your-token>"), is_placeholder("${DB_PASS}"), is_placeholder("hunter2hunter2")
    (True, True, False)
    """
    return bool(
        value.startswith(_PLACEHOLDER_START)
        or "(" in value
        or ")" in value
        or _PLACEHOLDER.search(value)
        or _ENV_REF.match(value)
        or _ENV_NAME.match(value)
    )


def scan_line(line: str) -> list[str]:
    """Return the names of every rule the line trips (empty if clean or exempted).

    >>> scan_line("password: " + "hunter2" + "hunter2")
    ['generic-secret-assignment']
    >>> scan_line("the password is stored in SSM")
    []
    """
    if ALLOW_MARKER in line:
        return []
    rules = [name for name, pattern in TOKEN_RULES if pattern.search(line)]
    if any(not is_placeholder(m.group("value")) for m in _ASSIGNMENT.finditer(line)):
        rules.append(GENERIC_RULE)
    return rules


def redact(text: str) -> str:
    """Replace every secret-looking span in text with ``[REDACTED:<rule>]`` so it is safe to store.

    Token-shaped matches (``TOKEN_RULES``) are replaced whole; for generic assignments only the
    assigned value is replaced, so ``password: hunter2hunter2`` keeps its key. Placeholders are left
    alone and ``secret-scan: allow`` is NOT honoured (redaction is for output, not commits).

    >>> redact("password: " + "hunter2" + "hunter2")
    'password: [REDACTED:generic-secret-assignment]'
    >>> redact("the password is stored in SSM")
    'the password is stored in SSM'
    """
    for name, pattern in TOKEN_RULES:
        text = pattern.sub(f"[REDACTED:{name}]", text)

    def _value(match: re.Match) -> str:
        if is_placeholder(match.group("value")):
            return match.group(0)
        start = match.start("value") - match.start()
        return match.group(0)[:start] + f"[REDACTED:{GENERIC_RULE}]"

    return _ASSIGNMENT.sub(_value, text)


def scan_text(path: str, text: str) -> list[Finding]:
    """Scan every line of ``text`` (1-based numbering) and return findings labelled with ``path``.

    >>> scan_text("a.md", "ok\\npassword: " + "hunter2" + "hunter2")
    [Finding(path='a.md', line=2, rule='generic-secret-assignment')]
    """
    return [
        Finding(path, number, rule)
        for number, line in enumerate(text.splitlines(), 1)
        for rule in scan_line(line)
    ]


def scan_files(root: Path, rel_paths: Iterable[str]) -> list[Finding]:
    """Scan whole files (paths relative to ``root``, ``/`` separated) and return findings as ``path:line rule``.

    Used where there is no diff yet, e.g. the very first commit of existing memory files. Unreadable or
    missing files are skipped; text is decoded as UTF-8 with replacement. Values are never returned.

    Example::

        scan_files(Path("~/.claude/projects").expanduser(), ["proj/memory/MEMORY.md"])  # [] when clean
    """
    findings: list[Finding] = []
    for rel in rel_paths:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(scan_text(rel, text))
    return findings


def _diff_path(header: str) -> str | None:
    """Extract the new-file path from the text after ``+++ ``; None for /dev/null."""
    text = header.split("\t")[0].strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    if text == "/dev/null":
        return None
    return text[2:] if text[:2] in ("a/", "b/") else text


def iter_added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    """Yield ``(path, new_line_number, content)`` for each added line of a ``-U0`` unified diff.

    >>> list(iter_added_lines("diff --git a/f b/f\\n--- a/f\\n+++ b/f\\n@@ -0,0 +1,2 @@\\n+a\\n+b\\n"))
    [('f', 1, 'a'), ('f', 2, 'b')]
    """
    path: str | None = None
    lineno = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            in_hunk, path = False, None
        elif in_hunk and raw.startswith("+"):
            if path is not None:
                yield path, lineno, raw[1:]
            lineno += 1
        elif raw.startswith("@@"):
            match = _HUNK.match(raw)
            if match:
                in_hunk, lineno = True, int(match.group(1))
        elif not in_hunk and raw.startswith("+++ "):
            path = _diff_path(raw[4:])


def scan_diff(diff: str) -> list[Finding]:
    """Scan a staged ``-U0`` diff and return unique findings in file/line order.

    Only added lines are considered, so a secret that is merely being removed does not block.
    """
    found: dict[Finding, None] = {}
    for path, lineno, content in iter_added_lines(diff):
        for rule in scan_line(content):
            found[Finding(path, lineno, rule)] = None
    return list(found)


def format_finding(finding: Finding) -> str:
    """Render one finding as ``path:line rule`` (never the matched value).

    >>> format_finding(Finding("p/memory/a.md", 3, "jwt"))
    'p/memory/a.md:3 jwt'
    """
    return f"{finding.path}:{finding.line} {finding.rule}"


def format_findings(findings: list[Finding]) -> str:
    """Render findings as the banner plus one ``path:line rule`` line each (no values)."""
    lines = [BLOCK_BANNER, *(format_finding(f) for f in findings)]
    lines.append(f"Remove the secret, or append '{ALLOW_MARKER}' to a line that is a false positive.")
    return "\n".join(lines) + "\n"
