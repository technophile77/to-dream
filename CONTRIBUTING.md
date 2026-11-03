# Contributing

Thanks for helping. to-dream is Python 3.10+, standard library only, and must work on Windows (Git Bash), macOS and Linux.

## Development

- Code lives in `plugins/to-dream/lib/to_dream/`; the plugin's skills are in `plugins/to-dream/skills/`.
- Run the tests from the repo root: `python -m unittest discover -s tests`
- Validate the manifests: `claude plugin validate .` and `claude plugin validate ./plugins/to-dream`
- Tests must assert independently derived expected values, not recompute them with the code under test. Never touch a real scheduler, the real `~/.claude`, or the network in tests (use the injectable runners and `TO_DREAM_HOME` / `TO_DREAM_MEMORY_ROOT`).
- `plugins/to-dream/bin/to-dream` must keep LF line endings and the executable bit.
- Don't commit personal paths or private repository names.

## Releasing

1. Bump `version` in `plugins/to-dream/.claude-plugin/plugin.json` (the only place; don't add a version to `marketplace.json`).
2. Add a `CHANGELOG.md` entry.
3. Run the tests and both `claude plugin validate` commands.
4. Merge to the default branch, then tag the commit `to-dream--v<version>` (Claude Code's plugin tag convention), for example `to-dream--v0.2.0`.

The update check reads `plugin.json` from the default branch, so users are told about a release as soon as the bump is merged.
