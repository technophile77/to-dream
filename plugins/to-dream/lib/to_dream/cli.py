"""Command line for to-dream: ``to-dream <run|list|show|apply|reject|mark-applied|reject-suggestion|discard|config|...>``.

Commands owned here: run, list, show, apply, reject, mark-applied, reject-suggestion, discard, config.
The modules ``doctor``, ``schedule``, ``update``, ``runtime`` and ``repo_setup`` contribute their own
sub-commands through ``add_parser(subparsers)`` (registered lazily, so a missing module is skipped).

Example::

    to-dream run --project=. --dry-run
    to-dream run --project=-home-sam-proj      # POSIX slugs start with '-'; use the = form (a plain space also works)
    to-dream show proj-261102-1830 --diff
    to-dream apply proj-261102-1830 --only 1 3
    to-dream reject proj-261102-1830 --only 2 --reason "too verbose"
"""

import argparse
import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from to_dream import config, gitops, runner, state as dstate

COMMAND_PREFIX = "to-dream"
VALUE_OPTIONS = ("--project",)  # options whose value may legitimately start with "-" (POSIX project slugs do)
EXTRA_COMMAND_MODULES = ("doctor", "schedule", "update", "runtime", "repo_setup")


def parse_since(text: str) -> datetime:
    """Parse ``YYYY-MM-DD`` as local midnight and return it as an aware datetime.

    >>> parse_since("2026-11-02").date().isoformat()
    '2026-11-02'

    Raises:
        argparse.ArgumentTypeError: the text is not a valid date.
    """
    try:
        return datetime.strptime(text, "%Y-%m-%d").astimezone()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from exc


def use_utf8(*streams) -> None:
    """Reconfigure text streams to UTF-8 with ``errors="replace"`` (streams without ``reconfigure`` are skipped).

    A piped Python on Windows defaults to cp1252 and would crash on characters such as a star in a report.
    """
    for stream in streams:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def print_json(data: Any) -> None:
    """Print ``data`` as indented JSON on stdout, switching stdout to UTF-8 first so non-ASCII text is safe.

    Falls back to ``\\u`` escapes if the stream still cannot encode the text.

    Example::

        print_json({"status": "ok", "note": "caf\\u00e9"})
    """
    use_utf8(sys.stdout)
    try:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except UnicodeEncodeError:
        print(json.dumps(data, indent=2, ensure_ascii=True))


# --------------------------------------------------------------------------- parser


def fold_option_values(argv: list[str], parser: argparse.ArgumentParser | None = None) -> list[str]:
    """Glue each ``VALUE_OPTIONS`` flag to the token after it (``--project X`` -> ``--project=X``).

    On macOS/Linux every Claude Code project slug starts with ``-`` (``/home/u/proj`` is
    ``-home-u-proj``), which argparse would take for another option. The next token is treated as the
    value unless it is itself one of the sub-command's known option strings.

    >>> fold_option_values(["run", "--project", "-home-u-proj", "--dry-run"])
    ['run', '--project=-home-u-proj', '--dry-run']
    >>> fold_option_values(["run", "--project", "--dry-run"])
    ['run', '--project', '--dry-run']
    """
    known = set(parser._option_string_actions) if parser else set()
    folded: list[str] = []
    tokens = iter(argv)
    for token in tokens:
        if token in VALUE_OPTIONS:
            following = next(tokens, None)
            if following is None:
                folded.append(token)
            elif following in known or following in ("-h", "--help"):
                folded += [token, following]
            else:
                folded.append(f"{token}={following}")
        else:
            folded.append(token)
    return folded


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all sub-commands (own and contributed by the other modules)."""
    parser = argparse.ArgumentParser(
        prog=COMMAND_PREFIX, description="Consolidate Claude Code memory from past sessions (\"dreaming\")."
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    add_run_parsers(sub)
    register_extra_commands(sub)
    return parser


def add_run_parsers(sub) -> None:
    """Add run, list, show, apply, reject, mark-applied, reject-suggestion, discard and config."""
    run = sub.add_parser("run", help="distil sessions and propose memory changes on a branch")
    run.add_argument("--project", required=True, help="project slug, project path, or 'all'")
    window = run.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=None,
                        help="window if the project never dreamed (default: config default_days, 7)")
    window.add_argument("--since", type=parse_since, help="YYYY-MM-DD; overrides the saved last-dream time")
    run.add_argument("--dry-run", action="store_true", help="distil only; print digest stats")
    run.add_argument("--model", default=None, help="orchestrator model (default: config model, opus)")
    run.add_argument("--analyst-model", default=None, help="analyst model (default: config analyst_model, sonnet)")
    run.add_argument("--budget-usd", type=float, default=None,
                     help="optional dollar cap passed to claude; no cap by default (on a subscription plan "
                          "--timeout-min is the runaway guard)")
    run.add_argument("--max-sessions", type=int, default=40)
    run.add_argument("--timeout-min", type=float, default=None,
                     help="kill claude after this many minutes; bounds a run (default: config timeout_min, 60)")
    run.set_defaults(func=cmd_run)

    listing = sub.add_parser("list", help="list dream runs")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show a run's report and commits")
    show.add_argument("run_id")
    form = show.add_mutually_exclusive_group()
    form.add_argument("--diff", action="store_true", help="print the full diff against the branch point")
    form.add_argument("--json", action="store_true", help="machine-readable output (changes with diffs)")
    show.set_defaults(func=cmd_show)

    apply = sub.add_parser("apply", help="snapshot, then cherry-pick dream commits onto live memory")
    apply.add_argument("run_id")
    apply.add_argument("--only", type=int, nargs="+", metavar="N", help="commit numbers to apply")
    apply.add_argument("--force", action="store_true", help="also apply changes marked rejected")
    apply.set_defaults(func=cmd_apply)

    reject = sub.add_parser("reject", help="mark changes rejected so future dreams do not re-propose them")
    reject.add_argument("run_id")
    reject.add_argument("--only", type=int, nargs="+", metavar="N", required=True, help="change numbers to reject")
    reject.add_argument("--reason", default="", help="why (recorded in rejected.md)")
    reject.set_defaults(func=cmd_reject)

    marked = sub.add_parser("mark-applied", help="record changes you applied by hand (e.g. after a conflict)")
    marked.add_argument("run_id")
    marked.add_argument("--only", type=int, nargs="+", metavar="N", required=True, help="change numbers to mark")
    marked.add_argument("--note", default="", help="what you did (kept in state)")
    marked.set_defaults(func=cmd_mark_applied)

    suggestion = sub.add_parser("reject-suggestion",
                                help="log a rejected global CLAUDE.md suggestion or permission rule")
    suggestion.add_argument("run_id")
    suggestion.add_argument("--text", required=True, help="the suggestion (or permission rule) that was turned down")
    suggestion.add_argument("--kind", choices=runner.SUGGESTION_KINDS, default=runner.KIND_GLOBAL,
                            help="global (CLAUDE.md suggestion, default) or permission (a rule)")
    suggestion.add_argument("--reason", default="", help="why (recorded in rejected.md)")
    suggestion.set_defaults(func=cmd_reject_suggestion)

    discard = sub.add_parser("discard", help="delete a run's worktree and branch")
    discard.add_argument("run_id")
    discard.add_argument("--force", action="store_true", help="discard even with uncommitted work in the worktree")
    discard.set_defaults(func=cmd_discard)

    cfg = sub.add_parser("config", help="show or change config.json")
    actions = cfg.add_subparsers(dest="config_action", required=True, metavar="action")
    shown = actions.add_parser("show", help="print every setting with its effective value")
    shown.set_defaults(func=cmd_config_show)
    got = actions.add_parser("get", help="print one setting")
    got.add_argument("key")
    got.set_defaults(func=cmd_config_get)
    put = actions.add_parser("set", help="change one setting (validated)")
    put.add_argument("key")
    put.add_argument("value")
    put.set_defaults(func=cmd_config_set)


def register_extra_commands(sub) -> None:
    """Let the sibling modules add their sub-commands; a module that does not exist yet is skipped.

    Raises:
        ImportError: a sibling module exists but fails to import (that is a bug worth seeing).
    """
    for name in EXTRA_COMMAND_MODULES:
        full = f"to_dream.{name}"
        try:
            module = importlib.import_module(full)
        except ModuleNotFoundError as exc:
            if exc.name == full:
                continue
            raise
        add_parser = getattr(module, "add_parser", None)
        if add_parser is not None:
            add_parser(sub)


# --------------------------------------------------------------------------- run


def options_from(args: argparse.Namespace) -> runner.RunOptions:
    """Translate parsed ``run`` arguments into :class:`runner.RunOptions`, filling gaps from config.

    Raises:
        config.ConfigError: ``config.json`` is invalid.
    """
    cfg = config.load()
    return runner.RunOptions(
        days=args.days if args.days is not None else cfg["default_days"], since=args.since, dry_run=args.dry_run,
        model=args.model or cfg["model"], analyst_model=args.analyst_model or cfg["analyst_model"],
        budget_usd=args.budget_usd, max_sessions=args.max_sessions,
        timeout_min=args.timeout_min if args.timeout_min is not None else float(cfg["timeout_min"]),
    )


def print_summary(summary: runner.RunSummary) -> None:
    """Print the end-of-run summary: report, branch, numbered commits, usage estimate and next commands."""
    if summary.status == runner.STATUS_NO_SESSIONS:
        print(f"{summary.slug}: no sessions in the window; nothing to dream about.")
        return
    if summary.status == runner.STATUS_FORMAT_DRIFT:
        print(f"{summary.slug}: {summary.message}", file=sys.stderr)
        return
    if summary.status == runner.STATUS_DRY_RUN:
        print(f"{summary.slug}: dry run, {summary.session_count} session(s). Index: {summary.index_path}")
        print("  " + ", ".join(f"{k}={v}" for k, v in summary.stats.items()))
        return
    print(f"\nDream {summary.run_id} ({summary.slug}): {summary.status}")
    if summary.message:
        print(f"  {summary.message}")
    if summary.report_path:
        print(f"  report:   {summary.report_path}")
    if summary.branch:
        print(f"  branch:   {summary.branch}" + (f" (from {summary.base_branch})" if summary.base_branch else ""))
        print(f"  worktree: {summary.worktree}")
    for number, commit in enumerate(summary.commits, 1):
        print(f"  {number}. {commit.subject}" + (f" -- {commit.why}" if commit.why else ""))
    if summary.superseded:
        print(f"  superseded earlier unreviewed run(s): {', '.join(summary.superseded)}")
    for name, reason in summary.rejected:
        print(f"  NOT COMMITTED {name}: {reason.splitlines()[0]}")
    if summary.cost is not None:
        print(f"  usage (API-equivalent): ${summary.cost:.2f}  (estimate of plan usage, not a charge)")
    if summary.commits or summary.rejected:
        print("Next:")
        print(f"  {COMMAND_PREFIX} show {summary.run_id} [--diff]")
        print(f"  {COMMAND_PREFIX} apply {summary.run_id} [--only N ...]")
        print(f"  {COMMAND_PREFIX} reject {summary.run_id} --only N [--reason TEXT]")
        print(f"  {COMMAND_PREFIX} discard {summary.run_id}")


def cmd_run(args: argparse.Namespace) -> int:
    """Handle ``run``: one project (slug or path) or ``all``; exit 1 if any run failed or hit a format problem."""
    options = options_from(args)
    if args.project == runner.ALL_PROJECTS:
        results = runner.run_all(options)
        if not results:
            print("No projects with transcript activity in their windows.")
        code = 0
        for result in results:
            if isinstance(result, tuple):
                print(f"error: {result[0]}: {result[1]}", file=sys.stderr)
                code = 1
            else:
                print_summary(result)
                code = code or result.exit_code
        return code
    summary = runner.run_project(runner.resolve_slug(args.project, dstate.memory_root()), options)
    print_summary(summary)
    return summary.exit_code


# --------------------------------------------------------------------------- list / show


def _by_creation(runs: dict[str, Any]) -> list[tuple[str, Any]]:
    """Order run records oldest first."""
    return sorted(runs.items(), key=lambda kv: kv[1].get("created", ""))


def cmd_list(args: argparse.Namespace) -> int:
    """Handle ``list``: one line per recorded run, oldest first (``--json``: an array of run objects)."""
    runs = dstate.load_state()["runs"]
    if args.json:
        print_json([runner.run_to_json(run_id, record) for run_id, record in _by_creation(runs)])
        return 0
    if not runs:
        print("No dream runs recorded.")
        return 0
    for run_id, record in _by_creation(runs):
        print(
            f"{run_id:<34} {runner.display_status(record):<10} files={len(record.get('files', [])):<3} "
            f"commits={len(record.get('commits', [])):<3} {record.get('created', '')[:10]} {record.get('branch', '')}"
        )
    return 0


def notice_text(notice: Any) -> str:
    """Render a state ``update_notice`` as one line.

    Accepts the ``{current, latest, ...}`` dict written by ``check-update``, a dict with a ``message``, or text.

    >>> notice_text({"current": "0.2.1", "latest": "0.3.0"})
    'to-dream 0.3.0 is out (you have 0.2.1)'
    """
    if isinstance(notice, dict):
        if notice.get("message"):
            return str(notice["message"])
        if notice.get("latest"):
            return f"to-dream {notice['latest']} is out (you have {notice.get('current', '?')})"
        return json.dumps(notice, ensure_ascii=False)
    return str(notice)


def cmd_show(args: argparse.Namespace) -> int:
    """Handle ``show``: report path, numbered commits with ``--stat``, optionally the full diff (or ``--json``)."""
    record = runner.get_run(args.run_id)
    if args.json:
        print_json(runner.run_detail_json(args.run_id, record))
        return 0
    root = dstate.memory_root()
    notice = dstate.load_state().get(runner.STATE_UPDATE_NOTICE_KEY)
    if notice:
        print(f"notice:   {notice_text(notice)}")
    report = Path(record["run_dir"]) / runner.REPORT_NAME
    print(f"run:      {args.run_id} ({runner.display_status(record)})")
    print(f"report:   {report}")
    if record.get("cost") is not None:
        print(f"usage (API-equivalent): ${record['cost']:.2f}  (estimate of plan usage, not a charge)")
    if record.get("worktree"):
        print(f"worktree: {record['worktree']}")
    if gitops.branch_exists(root, record["branch"]):
        sections = gitops.parse_report_sections(runner.report_text(record))
        for change in runner.run_changes(record):
            why = gitops.change_line(gitops.section_for(sections, change.file))
            print(f"\n{change.n}. {change.sha[:10]} {change.subject}" + (f" -- {why}" if why else ""))
            print(gitops.run_git(root, "show", "--stat", "--format=", change.sha).stdout.rstrip())
        if args.diff:
            print("\n" + gitops.run_git(root, "diff", record["base"], record["branch"]).stdout)
    else:
        print("(branch no longer exists)")
    return 0


# --------------------------------------------------------------------------- review actions


def cmd_apply(args: argparse.Namespace) -> int:
    """Handle ``apply``: snapshot, then cherry-pick all or selected commits onto live memory; exit 1 on conflict."""
    report = runner.apply_run(args.run_id, args.only, args.force)
    snapshot = report.snapshot
    if snapshot and snapshot.sha:
        print(f"snapshot {snapshot.sha[:10]}: committed {len(snapshot.committed)} uncommitted memory file(s) first")
    for path, reason in (snapshot.refused if snapshot else ()):
        print(f"warning: not snapshotted: {path} ({reason})", file=sys.stderr)
    for number in report.already_applied:
        print(f"commit {number} was already applied; skipped")
    for number in report.skipped_rejected:
        print(f"commit {number} is marked rejected; skipped (use --only {number} --force to apply it)")
    print(f"applied {len(report.result.applied)} commit(s) to {gitops.current_branch(dstate.memory_root())}")
    if report.result.failed:
        print(f"error: commit {report.result.failed[:10]} did not apply cleanly (cherry-pick aborted, live memory "
              f"unchanged by it). Review with `{COMMAND_PREFIX} show {args.run_id} --diff`; if you apply it by hand, "
              f"record it with `{COMMAND_PREFIX} mark-applied {args.run_id} --only N`.", file=sys.stderr)
        return 1
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    """Handle ``reject``: remember the chosen changes as rejected in state and ``rejected.md``."""
    rejected = runner.reject_run(args.run_id, args.only, args.reason)
    print(f"rejected {len(rejected)} change(s); recorded in {runner.rejected_log_path()}")
    return 0


def cmd_mark_applied(args: argparse.Namespace) -> int:
    """Handle ``mark-applied``: record hand-applied changes so ``discard`` does not log them as rejected."""
    marked = runner.mark_applied(args.run_id, args.only, args.note)
    print(f"marked {len(marked)} change(s) as applied")
    return 0


def cmd_reject_suggestion(args: argparse.Namespace) -> int:
    """Handle ``reject-suggestion``: log a turned-down global CLAUDE.md suggestion or permission rule (``--kind``)."""
    written = runner.reject_suggestion(args.run_id, args.text, args.reason, kind=args.kind)
    if written:
        print(f"recorded the rejected {args.kind} suggestion in {runner.rejected_log_path()}")
    else:
        print("that suggestion was already recorded for this run")
    return 0


def cmd_discard(args: argparse.Namespace) -> int:
    """Handle ``discard``: remove the run's worktree and branch, remembering unreviewed changes as rejected."""
    leftover = runner.discard_run(args.run_id, args.force)
    print(f"discarded {args.run_id}")
    if leftover:
        print(f"recorded {len(leftover)} unapplied change(s) as rejected in {runner.rejected_log_path()}")
    return 0


# --------------------------------------------------------------------------- config


def cmd_config_show(_args: argparse.Namespace) -> int:
    """Handle ``config show``: one ``key = value`` line per setting (effective values)."""
    for key, value in config.load().items():
        print(f"{key} = {config.format_value(value)}")
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    """Handle ``config get KEY``: print the effective value alone (text as is, others as JSON)."""
    print(config.format_value(config.get(args.key)))
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    """Handle ``config set KEY VALUE``: validate, store atomically and echo the stored value."""
    stored = config.set_value(args.key, args.value)
    print(f"{args.key} = {config.format_value(stored)}")
    return 0


# --------------------------------------------------------------------------- entry point

EXPECTED_ERRORS = (runner.DreamError, gitops.GitError, dstate.StateError, config.ConfigError)


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the command's handler; expected failures print ``error: ...`` and return 2.

    stdout/stderr are switched to UTF-8 first (see :func:`use_utf8`).

    Example::

        sys.exit(main(["list", "--json"]))
    """
    use_utf8(sys.stdout, sys.stderr)
    parser = build_parser()
    run_parser = parser._subparsers._group_actions[0].choices["run"]
    args = parser.parse_args(fold_option_values(list(sys.argv[1:] if argv is None else argv), run_parser))
    try:
        return args.func(args)
    except EXPECTED_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
