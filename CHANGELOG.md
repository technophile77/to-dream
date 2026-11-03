# Changelog

All notable changes to to-dream are recorded here. Versions follow [semver](https://semver.org/); the source of truth is `version` in `plugins/to-dream/.claude-plugin/plugin.json`.

## 0.1.0 - 2026-11-02

Initial release.

- Out-of-band memory consolidation ("dreaming") for Claude Code auto memory: distil transcripts, dream headless, propose per-file commits on a worktree branch, review with accept / reject / edit.
- Skills: `/to-dream:setup`, `/to-dream:run`, `/to-dream:review`.
- `to-dream` command: `doctor`, `init-repo`, `run`, `list`, `show`, `apply`, `reject`, `mark-applied`, `reject-suggestion`, `discard`, `schedule`, `check-update`, `sync-runtime`, `config`.
- Nightly scheduling on Windows (Task Scheduler), macOS (launchd) and Linux (systemd user timer, or cron).
- Update reminders for marketplaces without auto-update.
