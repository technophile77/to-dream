# Dream steering

This file tells the dreaming job what's worth remembering for this user. To customise it, copy it to `steering.md` in the to-dream state directory (`~/.claude/to-dream/steering.md` by default), or point the `steering_path` setting at your own file. Your copy takes priority over this one.

## Worth remembering (high value)

- **Corrections to how Claude works.** Anything the user had to say more than once, or said with "always", "never" or "from now on". This covers git and branch rules, how to run commands without triggering permission prompts, when to ask versus act, delegation to subagents, and testing standards. These become `feedback` memories with **Why** and **How to apply** lines.
- **Operational facts that were hard to find.** Server layout, deploy steps, which host or profile or endpoint to use, API quirks, database schema traps, and commands that only work a certain way on this user's machine or shell.
- **Failure recipes.** Errors that kept recurring, paired with the fix or workaround that worked.
- **Domain rules the user taught.** Business or product rules, decisions and their reasons, which aren't written down in the code.
- **Where secrets live.** For example "the key is in the secrets manager under …". Never the secret itself.
- **Permission friction.** Calls the user keeps having to approve by hand, or keeps having to stop. These become suggested permission rules in the report, not memories.

## Not worth remembering

- Progress on a single task, current counts, queues and temporary state, unless the memory exists to track that state and is kept current.
- Anything that can be found by reading the code, README, git log or the global `CLAUDE.md`.
- General programming knowledge.
- Restating rules that are already in the global `CLAUDE.md`. If a project memory duplicates a global rule, propose deleting it, and mention the global rule in the report.
- Praise, chit-chat, and one-off formatting requests.

## Style

- One fact per file. Keep it short and concrete, and lead with the rule.
- Write dates as absolute dates (YYYY-MM-DD).
- Keep the user's own words where they're clear.
- Prefer updating or merging an existing memory over adding a near-duplicate.

## Cross-project candidates

When a rule in this project's memory clearly applies to every project, such as git hygiene, worktree handling, subagent delegation or how to handle secrets, list it under "Suggestions for global CLAUDE.md" in the report. Don't edit the global file.
