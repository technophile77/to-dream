# 🌌 In Memory of Sir Patrick Stewart (1940–2026)

> *"To die, to sleep— / To sleep, perchance to dream—ay, there's the rub, / For in that sleep of death what dreams may come..."*  
> — **William Shakespeare**, *Hamlet* (Act III, Scene 1)

---

# to-dream

**Dreaming for Claude Code's auto memory.** to-dream is a Claude Code plugin that reviews your recent session transcripts offline, finds patterns no single session could see (repeated mistakes, your corrections, failing tool calls, missing knowledge, stale memories) and proposes changes to your memory files. Nothing reaches live memory until you accept it in a review.

The idea comes from the talk **"Learning while you sleep: Beyond memory to dreaming"** (AI Native DevCon, June 2026). to-dream is an **independent, unofficial implementation for Claude Code. It is not affiliated with Anthropic.**

## Install

In Claude Code:

```
/plugin marketplace add technophile77/to-dream
/plugin install to-dream@to-dream
/to-dream:setup
```

`/to-dream:setup` checks your environment, offers to make `~/.claude/projects` a git repo that tracks only memory, does a first dry run and can install a nightly schedule. It asks before every step.

### Requirements

- Claude Code (with the `claude` command on your PATH)
- Python 3.10 or newer (standard library only, nothing to `pip install`)
- git
- Windows: Git for Windows. Claude Code on Windows already uses Git Bash, and to-dream's `to-dream` command runs there.

## How it works

```
 session transcripts                memory files
 ~/.claude/projects/<p>/*.jsonl     ~/.claude/projects/<p>/memory/
          |                                   |
          v                                   |
  1. snapshot  (commit any uncommitted memory changes)
          |
          v
  2. distill   plain Python, no model: prompts, corrections, tool errors,
          |    denials, repeated failures, memory writes (secrets masked)
          v
  3. dream     claude -p headless: orchestrator splits digests across analyst
          |    subagents, checks evidence + staleness, edits memory in a git
          |    worktree on a proposal branch, writes DREAM_REPORT.md
          v
  4. commit    one commit per changed memory file (secret-scanned first)
          |
          v
  5. review    /to-dream:review: accept / reject / edit each proposal;
               accepted changes are cherry-picked onto your memory branch,
               rejections are logged so future dreams don't repeat them
```

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## Privacy and safety

- **It reads your session transcripts.** For each project you choose, to-dream reads all of its transcripts in `~/.claude/projects` within the time window, including subagent transcripts.
- **Secrets are masked before any model sees them.** The digests are built in plain Python with secret patterns redacted, and every commit it makes is scanned again. Findings report `path:line rule`, never the value. Masking is pattern-based, so it is a safety net, not a guarantee. Review what you accept.
- **The headless Claude has no shell and no web access.** It can read the project's source, but can only edit inside the proposal worktree.
- **Live memory changes only when you accept.** Proposals live on a separate branch and worktree until you review them.
- **Nothing is pushed.** to-dream never runs `git push`. If your memory repo has a remote, that is your business.
- **The transcript format is internal to Claude Code and may change.** If to-dream finds transcript files in a window but can't parse any of them, it warns you.
- It installs **no hooks**.

## Usage and cost

Dreams run `claude -p` with an Opus orchestrator and Sonnet analysts by default, so they **use your Claude plan usage**; a nightly run over many projects can be noticeable. On API billing, cap the spend with `--budget-usd`. Change the models with `--model` / `--analyst-model` or `to-dream config set model sonnet`.

## Commands

Skills (in Claude Code):

| Skill | What it does |
|---|---|
| `/to-dream:setup` | Guided first-time setup |
| `/to-dream:run` | Dream now for a project |
| `/to-dream:review` | Walk through proposals and accept / reject / edit |

The `to-dream` command (on the Bash tool's PATH while the plugin is enabled):

| Command | Purpose |
|---|---|
| `doctor [--json]` | Check Python, git, `claude`, the memory repo, updates and the schedule |
| `init-repo [--branch NAME]` | Make the memory root a git repo that tracks only `*/memory/` (verifies an existing repo, never rewrites it) |
| `run --project=<slug\|path\|all> [--days N \| --since DATE] [--dry-run] [--model M] [--analyst-model M] [--budget-usd N] [--max-sessions N] [--timeout-min N]` | Run a dream. Write `--project=<value>` with `=`: on macOS and Linux slugs start with `-` (`-home-sam-proj`) |
| `list [--json]` | List runs |
| `show <run-id> [--json \| --diff]` | Show a run's proposed changes |
| `apply <run-id> [--only N ...] [--force]` | Cherry-pick accepted changes onto the live branch |
| `reject <run-id> --only N ... [--reason TEXT]` | Reject changes and remember why |
| `mark-applied <run-id> --only N ... [--note TEXT]` | Record a change you applied by hand |
| `reject-suggestion <run-id> --text TEXT [--reason TEXT]` | Log a rejected CLAUDE.md suggestion |
| `discard <run-id> [--force]` | Remove a run's worktree and branch |
| `schedule install [--time HH:MM] \| remove \| status [--json]` | Nightly run of `run --project all` |
| `check-update [--json] [--force]` | Is a newer release out? |
| `sync-runtime` | Refresh the copy of the code used by scheduled jobs |
| `config show \| get KEY \| set KEY VALUE` | Read or change `config.json` |

State lives in `$TO_DREAM_HOME` (see [Uninstall](#uninstall)). Config keys: `model`, `analyst_model`, `default_days`, `timeout_min`, `branch_template`, `worktree_dir`, `worktree_template`, `steering_path`, `schedule_time`, `update_check`.

## Updating

Background auto-update is **off by default** for third-party marketplaces. To update by hand:

```
/plugin marketplace update to-dream
/reload-plugins
```

or `claude plugin update to-dream@to-dream` from a terminal. To turn on auto-update: `/plugin` → Marketplaces → to-dream → Enable auto-update.

to-dream also reminds you: each `/to-dream:*` skill checks (at most once a day) whether a newer release is published and shows a one-line note; the nightly run records it so `/to-dream:review` shows it too. `to-dream doctor` includes the status. To switch the check off, run `to-dream config set update_check false` or set `TO_DREAM_NO_UPDATE_CHECK=1`. The check only fetches `plugin.json` from this repository on GitHub and sends nothing else.

After an update, the next `/to-dream:*` command refreshes the copy of the code that the nightly job runs.

## Scheduling

`/to-dream:setup` can do this for you, or run `to-dream schedule install --time 03:30` (the default time is config `schedule_time`). The job runs `run --project all` and appends its output to `$TO_DREAM_HOME/logs/<date>.log`.

| OS | Mechanism | Missed start |
|---|---|---|
| Windows | Task Scheduler (`schtasks`), with StartWhenAvailable | Runs when you next log on |
| macOS | launchd LaunchAgent in `~/Library/LaunchAgents` | Runs on wake |
| Linux | systemd user timer (`Persistent=true`); crontab if `systemctl --user` is unavailable | systemd catches up; cron doesn't |

Check it with `to-dream schedule status`; remove it with `to-dream schedule remove`. On Linux, enable lingering (`loginctl enable-linger $USER`) if you want it to run while logged out.

## Optional: version your memory

`init-repo` gives you a git repo of `~/.claude/projects` that tracks only `<project>/memory/`. Dreams branch from it and snapshot into it. You can also keep your own memory history there (for example by committing after sessions) so every memory change, human or dreamed, is diffable and revertible.

## Uninstall

1. `to-dream schedule remove`
2. `/plugin uninstall to-dream@to-dream` (and `/plugin marketplace remove to-dream` if you like)
3. Optional cleanup of what remains:
   - `~/.claude/to-dream/` (or `~/.claude/plugins/data/to-dream-to-dream/` if Claude Code provided a data directory): state, run reports, logs, config and the runtime copy. Delete it to forget everything.
   - Any leftover proposal worktrees and `dream/*` branches in `~/.claude/projects` (`to-dream discard <run-id>` removes them).
   - The memory git repo in `~/.claude/projects` (`.git`, `.gitignore`, `.gitattributes`), if you want it gone. Your memory files are ordinary files and stay put.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md). Released under the [MIT License](LICENSE).
