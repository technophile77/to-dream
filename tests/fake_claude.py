"""Scripted stand-in for ``claude -p`` used by the end-to-end tests (via ``TO_DREAM_CLAUDE_CMD``).

Behaviour comes from the JSON plan at ``$FAKE_CLAUDE_PLAN``; relative paths are relative to the cwd,
which the runner sets to the worktree's ``<slug>/memory`` dir. Plan keys:

``log`` (file to record argv/cwd/stdin/env), ``write`` {path: text}, ``blank`` [paths] (emptied: the
"delete me" convention), ``report`` (text of DREAM_REPORT.md), ``exit`` (exit code), ``is_error``,
``sleep`` (seconds), ``stdout`` (raw stdout override).
"""

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    """Apply the plan and print a ``--output-format json`` style result."""
    plan = json.loads(Path(os.environ["FAKE_CLAUDE_PLAN"]).read_text(encoding="utf-8"))
    cwd = Path.cwd()
    if plan.get("log"):
        Path(plan["log"]).write_text(
            json.dumps({
                "argv": sys.argv[1:],
                "cwd": str(cwd),
                "stdin": sys.stdin.buffer.read().decode("utf-8"),
                "auto_memory": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"),
            }),
            encoding="utf-8",
        )
    time.sleep(plan.get("sleep", 0))
    for rel, text in plan.get("write", {}).items():
        target = cwd / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    for rel in plan.get("blank", []):
        (cwd / rel).write_text("", encoding="utf-8")
    if "report" in plan:
        (cwd / "DREAM_REPORT.md").write_text(plan["report"], encoding="utf-8", newline="\n")
    if "stdout" in plan:
        print(plan["stdout"])
    else:
        print(json.dumps({
            "type": "result", "subtype": "error_max_turns" if plan.get("is_error") else "success",
            "is_error": bool(plan.get("is_error")), "total_cost_usd": 0.42, "result": "done",
        }))
    return int(plan.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
