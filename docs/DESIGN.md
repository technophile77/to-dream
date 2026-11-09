# to-dream design

to-dream is a Claude Code plugin that adds **out-of-band memory consolidation**, or "dreaming", to Claude Code's built-in auto memory. A batch job reviews recent session transcripts for a project, along with that project's memory files, and finds patterns no single session could see: repeated mistakes, the user's corrections, failing tool calls, missing knowledge and stale memories. It then proposes changes to the memory files. Nothing reaches live memory until the user accepts it in a review.

The idea comes from the talk "Learning while you sleep: Beyond memory to dreaming" (AI Native DevCon, June 2026). This is an independent, unofficial implementation for Claude Code. It isn't affiliated with Anthropic.

## Pipeline

1. **Snapshot.** The memory root (`~/.claude/projects`) is a git repo that tracks only `<project>/memory/**`. Before a dream branches, and before an apply, any uncommitted memory changes are committed as a snapshot. The plugin installs **no hooks**.
2. **Distill.** (Permission-event sections and the permission table below exist only with config `permissions_review` on; the inline tags are always written.) Plain Python, with no model involved, streams the project's transcript JSONL files (main sessions and subagents) inside a time window. It writes compact markdown digests: the human's prompts, corrections, tool errors, denials, repeated failures and memory writes, each with `L<line>` evidence references. Secrets are masked before anything reaches a model. Permission friction is tagged, and tallied per tool call in a "Permission events (all sessions)" table at the end of `INDEX.md` (columns: blocked, approved after block, interrupted, interrupted then corrected, rejected at prompt, sessions, block reasons, approval modes, refs):
   - `**BLOCKED**`: the auto-mode classifier refused the call (the bracketed reason is kept).
   - `**APPROVED-AFTER-BLOCK**`: the same call succeeded after a block, so the user approved it by hand (the permission mode is kept).
   - `**INTERRUPTED**`: the user stopped a call that ran without a prompt. The event records whether the next human prompt was a correction.
   - `**DENIED** (user-rejected)`: the user said no at a permission prompt. Other denials (hooks, `permission-rule`) keep a plain `**DENIED**` tag and are not permission events.
3. **Dream.** This runs `claude -p` headless, with no shell, edits allowed only in the proposal worktree, and read-only access to the project's source. With config `permissions_review` on (default off), before it starts the runner writes `permissions.md` into the run directory: the project's current `allow` / `ask` / `deny` rules per scope (user, project shared, project-local) and the absolute settings file each suggestion scope edits. An orchestrator (Opus by default) splits the digests across analyst subagents (Sonnet by default), applies evidence thresholds, checks staleness against the code, reviews permission friction, edits memory in a git worktree on a proposal branch, and writes `DREAM_REPORT.md`.
   - **Roll-up.** With config `rollup` on (the default), each project has at most one pending run. If unreviewed runs (`pending`, `partially-applied` or `needs-attention`) exist, the window starts at the oldest one's `window_since` (or the normal start, if earlier), and the prompt's `{{rollup_note}}` names them. When the new run ends `pending`, `no-changes` or `needs-attention`, those runs are **superseded**: their worktrees and branches are removed, their status becomes `superseded` with `superseded_by` set, and their leftover changes are *not* logged as rejected (accepted ones are live, rejected ones are already logged, and the new run covers the rest). A run whose worktree holds real uncommitted work is left as it was, with a warning. A failed run supersedes nothing.
   - **Permission suggestions** (only with `permissions_review` on). The report's `## Permission suggestions` section lists bullets (`**Rule:**` with `**Kind:**` and `**Scope:**` on that line, then nested `**Evidence:**`, `**Risk:**`, `**Why:**`), parsed by `permissions.parse_permission_suggestions`. Thresholds: an `allow` rule when the same call shape was approved after a block at least twice; an `ask` rule when it was interrupted at least twice, or once and then corrected. Rules are the narrowest pattern that covers the observed calls, default scope is `project-local`, and the dream never proposes `Bash(*)`, a whole-server MCP wildcard, an interpreter or package-manager wildcard for `allow` (auto mode drops those), a rule a `deny` would override, or one that would weaken the user's global `CLAUDE.md` rules. It never proposes removing a `deny`. Rules are suggestions only; the review applies them.
4. **Commit proposals.** Each changed memory file becomes one commit, whose message carries that file's report section. Every commit is secret-scanned in-process first. An emptied file means "delete this memory".
5. **Review.** `/to-dream:review` reviews one project per pass, all of its unreviewed runs oldest first, and walks the user through each proposal, offering accept, reject or edit. Accepted changes are cherry-picked onto the live branch (non-`MEMORY.md` changes first, then `MEMORY.md`, since `apply` stops at the first conflict); a change an earlier run in the pass already made is recorded with `mark-applied`, not asked again. It then asks about each permission suggestion and, on yes, edits the chosen settings file (project-local, project shared or user) itself. Rejections, of changes, CLAUDE.md suggestions and permission rules alike, are logged, and future dreams read that log so they don't propose the same thing again.

## Layout

```
to-dream/                                  # repo = marketplace
  .claude-plugin/marketplace.json          # marketplace "to-dream", lists ./plugins/to-dream
  plugins/to-dream/                        # the plugin (what users install)
    .claude-plugin/plugin.json             # name "to-dream"
    bin/to-dream                           # bash shim on the Bash tool's PATH; picks python3/python >= 3.10
    scripts/to_dream.py                    # entry point: puts ../lib on sys.path, calls to_dream.cli.main
    lib/to_dream/                          # Python package, stdlib only
      cli.py runner.py gitops.py state.py distill.py redact.py paths.py schedule.py config.py runtime.py
      permissions.py doctor.py repo_setup.py update.py version.py
      prompts/orchestrator.md prompts/steering.md
    skills/setup/SKILL.md                  # /to-dream:setup
    skills/run/SKILL.md                    # /to-dream:run
    skills/review/SKILL.md                 # /to-dream:review
  tests/                                   # unittest; not shipped inside the plugin
  .github/workflows/ci.yml                 # windows/macos/ubuntu × py3.10–3.13
  README.md LICENSE (MIT) CHANGELOG.md docs/DESIGN.md
```

## Paths and state

- **`TO_DREAM_HOME`** is the state directory. The `bin/to-dream` shim sets it to `${CLAUDE_PLUGIN_DATA}` when that variable is present, and otherwise to `~/.claude/to-dream`. Scheduled jobs export it explicitly. It holds:
  - `state.json`: atomic writes; runs, last dream per project, and applied, rejected and marked-applied changes.
  - `runs/<run-id>/`: `digests/`, `prompt.md`, `permissions.md`, `claude-output.json`, `run.log`, `DREAM_REPORT.md`.
  - `rejected.md`: the log the orchestrator reads. Entries are headed by kind: a rejected change, "global suggestion" or "permission suggestion".
  - `config.json`, `steering.md` (an optional override of the bundled steering file), `logs/`, `worktrees/`, `runtime/`.
- **Memory root:** `~/.claude/projects`. It can be overridden with the env var `TO_DREAM_MEMORY_ROOT` for tests.
- **`${CLAUDE_PLUGIN_ROOT}` changes on every plugin update**, so scheduled jobs never point at it. `to-dream sync-runtime` copies `scripts/` and `lib/` into `$TO_DREAM_HOME/runtime/`, stamped with the plugin version. Every skill runs it first, so the scheduled job, which runs `$TO_DREAM_HOME/runtime/scripts/to_dream.py`, picks up updates the next time the user runs any `/to-dream` command.

## CLI contract (`to-dream <command>`; human output by default, `--json` where listed)

| Command | Purpose |
|---|---|
| `doctor [--json]` | Check Python ≥ 3.10, git, `claude` on PATH and its version, the memory root, the memory repo's status and branch, and the schedule. Each check is ok, warn or fail with a fix hint. |
| `init-repo [--branch NAME]` | Make the memory root a git repo that tracks only `/*/memory/` (the `.gitignore` and the `.gitattributes` with `* -text` included). If a repo already exists, verify it and report, but never rewrite it. The default branch name is `memory`. |
| `run --project=<slug\|path\|all> [--days N \| --since YYYY-MM-DD] [--dry-run] [--model M] [--analyst-model M] [--budget-usd N] [--max-sessions N] [--timeout-min N]` | Run a dream. Use `--project=<value>` (with `=`): on macOS/Linux slugs start with `-`. No budget cap by default. `--timeout-min` defaults to 60. |
| `list [--json]` | List runs with their status: `pending`, `partially-applied`, `applied`, `needs-attention`, `no-changes`, `failed`, `discarded` or `superseded` (replaced by a later roll-up run; `--json` adds `superseded_by`). |
| `show <run-id> [--json \| --diff]` | Show a run's changes, with each change's Change line, diff and report extras. `--json` adds `report_extras` (`global_suggestions`, `stale_candidates`, `not_acted_on`, `permission_suggestions` as raw text, plus `permission_suggestions_parsed`: a list of `{rule, kind, scope, evidence, risk, why}`) and `permission_targets` (`project-local`, `project`, `user` → absolute settings file, recorded when the run was made; `{}` for older runs), and `superseded_by` for superseded runs. |
| `apply <run-id> [--only N ...] [--force]` | Snapshot, then cherry-pick onto the live branch. On conflict, abort and report. |
| `reject <run-id> --only N ... [--reason TEXT]` | Mark changes rejected and append them to `rejected.md`. |
| `mark-applied <run-id> --only N ... [--note TEXT]` | Record a change the user applied by hand after a conflict, so `discard` doesn't log it as rejected. |
| `reject-suggestion <run-id> --text TEXT [--kind global\|permission] [--reason TEXT]` | Log a rejected suggestion in `rejected.md`: a global CLAUDE.md suggestion (`--kind global`, the default) or a permission rule (`--kind permission`, `--text` is the rule). |
| `discard <run-id> [--force]` | Remove the worktree and branch, and log leftover undecided changes as rejected. |
| `schedule install [--time HH:MM] \| remove \| status [--json]` | Set up the nightly run of `run --project all`, catching up after a missed start. Windows uses Task Scheduler (`schtasks` plus StartWhenAvailable). macOS uses a launchd LaunchAgent with StartCalendarInterval. Linux uses a systemd user timer with `Persistent=true`, or crontab if systemd isn't available. |
| `sync-runtime [--force]` | Copy the runtime into `$TO_DREAM_HOME/runtime` and print its path (stdout is only the path). Never downgrades: if the runtime's version is newer than this plugin's, it is left as is, one note goes to stderr and the exit code stays 0. `--force` overwrites it anyway (rollback). |
| `config show \| get KEY \| set KEY VALUE` | Read or change `config.json`. |

### Config keys (`config.json`, all optional)
- `model` defaults to `opus`, and `analyst_model` to `sonnet`.
- `default_days` defaults to 7, and `timeout_min` to 60.
- `branch_template` defaults to `dream/{run_id}`. Its placeholders are `{run_id} {project} {yy} {mm} {dd} {hhmm}`.
- `worktree_dir` defaults to `$TO_DREAM_HOME/worktrees`, and `worktree_template` to `{run_id}`.
- `steering_path` overrides the steering file.
- `schedule_time` defaults to `03:30`.
- `update_check` defaults to true (see Updates).
- `rollup` defaults to true: a new dream starts at the oldest unreviewed run's window and supersedes those runs when it succeeds.
- `permissions_review` defaults to false: the permissions review below is opt-in. Off, the runner skips `write_permissions` (no settings file is read for this, no `permissions.md`, `permission_targets` is `{}`), the distiller omits the per-digest "Permission events" section and the `INDEX.md` table (the inline tags stay), and the orchestrator prompt is rendered without the passages wrapped in `<!-- permissions-review:start -->` / `<!-- permissions-review:end -->` markers (`runner.apply_permissions_review`). The settings read-deny rules stay on either way.

### Base branch
A dream branches from whatever branch is **checked out** in the memory root. It records that branch, and `apply` refuses to run if a different branch is checked out. No branch name is hardcoded.

## Updates

Background auto-update is off by default for third-party marketplaces, so to-dream tells users itself when a new release is out.

- **Releases.** `version` in `plugins/to-dream/.claude-plugin/plugin.json` is the single source of truth. Don't set a version in `marketplace.json` as well. Bump it on every release, add a `CHANGELOG.md` entry, and tag the commit `to-dream--v<version>`, which is Claude Code's tag convention for plugin releases.
- **`to-dream check-update [--json] [--force]`** compares the running version with the latest published one. It reads `plugin.json` from the default branch over HTTPS, using a raw URL derived from the `repository` field in the running plugin's `plugin.json`, with a 5-second timeout. The result is cached in `$TO_DREAM_HOME/update-check.json` for 24 hours, and `--force` bypasses the cache. It never fails a command: when offline, or on any error, it reports `unknown` and exits 0. The JSON form is `{current, latest, update_available, checked_at, changelog_url}`, and versions are compared as semver.
- **Opting out.** Set `config.json` `update_check: false` or the env var `TO_DREAM_NO_UPDATE_CHECK=1`.
- **Where users see it.**
  - Every `/to-dream:*` skill runs `check-update --json` right after `sync-runtime`. When an update is available, the skill shows one line before doing anything else, for example: "to-dream 0.3.0 is out (you have 0.2.1). Update: `/plugin marketplace update to-dream`, then `/reload-plugins`. To update automatically, see https://github.com/technophile77/to-dream#updating." It then carries on.
  - The nightly run writes the same notice to its log and records it in `state.json`, so `/to-dream:review` shows it at the start of the next review even if the cached check is stale.
  - `doctor` includes the update status as one of its checks.
  - `/to-dream:setup` offers to turn on auto-update as its last step. It can add the `extraKnownMarketplaces` `autoUpdate` entry to `~/.claude/settings.json` (works in every client, including VS Code), or point to the terminal-only `/plugin` toggle.
- **Runtime sync after an update.** The version stamp in `$TO_DREAM_HOME/runtime/VERSION` lets `sync-runtime` notice that the plugin was updated and refresh the copy the scheduled job uses. It only moves forward: when two Claude Code sessions run different plugin versions, the older session leaves a newer runtime alone (and `doctor` warns with the update hint). Same-version content differences still refresh, and `sync-runtime --force` allows a deliberate downgrade.

## Safety rules
- The headless Claude gets `--allowedTools Read Glob Grep Agent` plus read-only `Read(...)` rules for the project source, `CLAUDE.md`, the steering file, `rejected.md`, `permissions.md` and the project's memory. It runs with `--disallowedTools Bash PowerShell WebFetch WebSearch`, `--permission-mode acceptEdits` (so edits are allowed only in its cwd, the worktree), `--permission-prompts none` and `--no-session-persistence`, with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
- Settings files can hold secrets (`env`, API-key helpers). The dream never reads them: `permissions.md` is built by to-dream from the whitelisted keys of each file's `permissions` object only (`allow`, `ask`, `deny`, `defaultMode`, `additionalDirectories`), with every string redacted. In addition `--disallowedTools` carries `Read(...)` deny rules (which beat the recursive project allow) for `settings.json` and `settings.local.json` under `.claude` of the project directory and of its git root, and for `~/.claude/settings.json`.
- Permission rules are only ever added by the review skill, after the user confirms each one, into the file the user picks.
- The orchestrator prompt treats digests as data, never as instructions; a digest asking for a permission is a pattern, not a command.
- Secrets are masked in digests, and every proposal and snapshot commit is scanned in-process. Findings report `path:line rule`, never the value.
- Nothing is ever pushed. If the user has a remote, that's their business.
- Transcripts use an internal format. If a window holds transcript files but none of their records parse, warn that the format may have changed.
