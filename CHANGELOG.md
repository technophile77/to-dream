# Changelog

All notable changes to to-dream are recorded here. Versions follow [semver](https://semver.org/); the source of truth is `version` in `plugins/to-dream/.claude-plugin/plugin.json`.

## 0.1.1 - 2026-11-03

Fix macOS/Linux: project ids starting with "-".

- `run --project -home-u-proj` no longer fails with "expected one argument": the CLI treats the token after `--project` as its value. Skills and docs now use `--project=<value>`.
- Git calls and tests no longer let a slug starting with "-" be read as a switch (`ls-tree` and friends); end-to-end tests cover a dash-leading slug through run, apply and discard.
- `paths.is_memory_path` / `slug_of` are pure string logic and behave the same on every OS (Windows-form roots match case-insensitively and accept `/c/...`; POSIX roots are case-sensitive).
- `Read(//...)` permission rules are built correctly for POSIX and Windows paths on any OS.

## 0.1.0 - 2026-11-02

Initial release.

- Out-of-band memory consolidation ("dreaming") for Claude Code auto memory: distil transcripts, dream headless, propose per-file commits on a worktree branch, review with accept / reject / edit.
- Skills: `/to-dream:setup`, `/to-dream:run`, `/to-dream:review`.
- `to-dream` command: `doctor`, `init-repo`, `run`, `list`, `show`, `apply`, `reject`, `mark-applied`, `reject-suggestion`, `discard`, `schedule`, `check-update`, `sync-runtime`, `config`.
- Nightly scheduling on Windows (Task Scheduler), macOS (launchd) and Linux (systemd user timer, or cron).
- Update reminders for marketplaces without auto-update.
