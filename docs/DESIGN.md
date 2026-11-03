# to-dream design

to-dream is a Claude Code plugin that adds **out-of-band memory consolidation**, or "dreaming", to Claude Code's built-in auto memory. A batch job reviews recent session transcripts for a project, along with that project's memory files, and finds patterns no single session could see: repeated mistakes, the user's corrections, failing tool calls, missing knowledge and stale memories. It then proposes changes to the memory files. Nothing reaches live memory until the user accepts it in a review.

The idea comes from the talk "Learning while you sleep: Beyond memory to dreaming" (AI Native DevCon, June 2026). This is an independent, unofficial implementation for Claude Code. It isn't affiliated with Anthropic.

## Pipeline

1. **Snapshot.** The memory root (`~/.claude/projects`) is a git repo that tracks only `<project>/memory/**`. Before a dream branches, and before an apply, any uncommitted memory changes are committed as a snapshot. The plugin installs **no hooks**.
2. **Distill.** Plain Python, with no model involved, streams the project's transcript JSONL files (main sessions and subagents) inside a time window. It writes compact markdown digests: the human's prompts, corrections, tool errors, denials, repeated failures and memory writes, each with `L<line>` evidence references. Secrets are masked before anything reaches a model.
3. **Dream.** This runs `claude -p` headless, with no shell, edits allowed only in the proposal worktree, and read-only access to the project's source. An orchestrator (Opus by default) splits the digests across analyst subagents (Sonnet by default), applies evidence thresholds, checks staleness against the code, edits memory in a git worktree on a proposal branch, and writes `DREAM_REPORT.md`.
4. **Commit proposals.** Each changed memory file becomes one commit, whose message carries that file's report section. Every commit is secret-scanned in-process first. An emptied file means "delete this memory".
5. **Review.** `/to-dream:review` walks the user through each proposal, offering accept, reject or edit. Accepted changes are cherry-picked onto the live branch. Rejections are logged, and future dreams read that log so they don't propose the same thing again.

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
  - `runs/<run-id>/`: `digests/`, `prompt.md`, `claude-output.json`, `run.log`, `DREAM_REPORT.md`.
  - `rejected.md`: the log the orchestrator reads.
  - `config.json`, `steering.md` (an optional override of the bundled steering file), `logs/`, `worktrees/`, `runtime/`.
- **Memory root:** `~/.claude/projects`. It can be overridden with the env var `TO_DREAM_MEMORY_ROOT` for tests.
- **`${CLAUDE_PLUGIN_ROOT}` changes on every plugin update**, so scheduled jobs never point at it. `to-dream sync-runtime` copies `scripts/` and `lib/` into `$TO_DREAM_HOME/runtime/`, stamped with the plugin version. Every skill runs it first, so the scheduled job, which runs `$TO_DREAM_HOME/runtime/scripts/to_dream.py`, picks up updates the next time the user runs any `/to-dream` command.

## CLI contract (`to-dream <command>`; human output by default, `--json` where listed)

| Command | Purpose |
|---|---|
| `doctor [--json]` | Check Python ≥ 3.10, git, `claude` on PATH and its version, the memory root, the memory repo's status and branch, and the schedule. Each check is ok, warn or fail with a fix hint. |
| `init-repo [--branch NAME]` | Make the memory root a git repo that tracks only `/*/memory/` (the `.gitignore` and the `.gitattributes` with `* -text` included). If a repo already exists, verify it and report, but never rewrite it. The default branch name is `memory`. |
| `run --project=<slug\|path\|all> [--days N \| --since YYYY-MM-DD] [--dry-run] [--model M] [--analyst-model M] [--budget-usd N] [--max-sessions N] [--timeout-min N]` | Run a dream. Use `--project=<value>` (with `=`): on macOS/Linux slugs start with `-`. No budget cap by default. `--timeout-min` defaults to 60. |
| `list [--json]` | List runs with their status. |
| `show <run-id> [--json \| --diff]` | Show a run's changes, with each change's Change line, diff and report extras. |
| `apply <run-id> [--only N ...] [--force]` | Snapshot, then cherry-pick onto the live branch. On conflict, abort and report. |
| `reject <run-id> --only N ... [--reason TEXT]` | Mark changes rejected and append them to `rejected.md`. |
| `mark-applied <run-id> --only N ... [--note TEXT]` | Record a change the user applied by hand after a conflict, so `discard` doesn't log it as rejected. |
| `reject-suggestion <run-id> --text TEXT [--reason TEXT]` | Log a rejected global CLAUDE.md suggestion in `rejected.md`. |
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
- The headless Claude gets `--allowedTools Read Glob Grep Agent` plus read-only `Read(...)` rules for the project source, `CLAUDE.md`, the steering file and `rejected.md`. It runs with `--disallowedTools Bash PowerShell WebFetch WebSearch`, `--permission-mode acceptEdits` (so edits are allowed only in its cwd, the worktree), `--permission-prompts none` and `--no-session-persistence`, with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
- The orchestrator prompt treats digests as data, never as instructions.
- Secrets are masked in digests, and every proposal and snapshot commit is scanned in-process. Findings report `path:line rule`, never the value.
- Nothing is ever pushed. If the user has a remote, that's their business.
- Transcripts use an internal format. If a window holds transcript files but none of their records parse, warn that the format may have changed.
