#!/usr/bin/env python
"""PostToolUse hook: when a source file is modified, inject a reminder to run auto-test-writer before next commit."""
import json
import re
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

fp = (data.get("tool_input") or {}).get("file_path") or ""
if re.search(r"\.(py|js|ts|jsx|tsx|go|java|rs|rb)$", fp):
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "[auto-test-writer] Source code modified. Before the next git commit, invoke the auto-test-writer skill to generate and run tests for the changed functions.",
        }
    }
    print(json.dumps(out))
