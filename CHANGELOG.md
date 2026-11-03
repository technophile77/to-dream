# Changelog

All notable changes to to-dream are recorded here. Versions follow [semver](https://semver.org/); the source of truth is `version` in `plugins/to-dream/.claude-plugin/plugin.json`.

## 0.1.3 - 2026-11-03

Fix: an older Claude Code session could overwrite the runtime with older code.

- `to-dream sync-runtime` no longer downgrades `$TO_DREAM_HOME/runtime`. When two sessions run different plugin versions, the one on the older version leaves the newer runtime alone and prints a one-line note to stderr (stdout is still only the path, exit code 0). Same-version content differences, older runtimes and unparseable versions still refresh.
- `to-dream sync-runtime --force` allows a deliberate downgrade, for example a rollback.
- `to-dream doctor` reports a runtime newer than the plugin as a warning with the update hint, rather than "out of date".

## 0.1.2 - 2026-11-03

Fix: auto-update instructions only worked in the terminal.

- The `/plugin` → Marketplaces → Enable auto-update toggle exists only in the terminal version of Claude Code, not the VS Code extension. The README, `/to-dream:setup` and the update reminder now also describe the client-independent route: `extraKnownMarketplaces.to-dream.autoUpdate` in `~/.claude/settings.json`.
- `/to-dream:setup` offers to add that settings entry for you.
- The one-line update reminder now points to the README's Updating section instead of naming the terminal toggle.

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
