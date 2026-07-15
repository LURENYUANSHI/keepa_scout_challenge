#!/usr/bin/env python
"""PreToolUse Bash hook: only inject the pre-commit gate reminder when the
Bash command actually contains `git commit`. Avoids polluting every Bash
call with the reminder text.
"""
from __future__ import annotations

import json
import re
import sys

REMINDER = (
    "[pre-commit gate] STOP. Before running git commit, you MUST run BOTH "
    "skills IN ORDER: (1) auto-test-writer — generate & run tests for "
    "changed functions, must all pass; (2) code-review-eval — full review "
    "(minimality / side-effects / quality / breaking / coverage), risk "
    "must be Low. If any step fails or risk >= Medium, fix the code and "
    "rerun from (1). Do NOT commit until auto-test-writer is green AND "
    "code-review-eval risk is Low."
)

# Match `git commit` as a discrete subcommand: start of line OR after && / ; / |,
# then optional `git ` then `commit`. Skips false positives like
# `git log --grep=commit` or `echo "git commit"`.
PATTERN = re.compile(r"(?:^|[\s;&|])git\s+commit(?:\s|$)")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if PATTERN.search(cmd):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": REMINDER,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
