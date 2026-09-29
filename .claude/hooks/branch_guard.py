#!/usr/bin/env python3
"""PreToolUse(Bash): коммит и push в main запрещены — только ветка и PR."""
import json
import os
import re
import subprocess
import sys

PROTECTED = {"main", "master"}
COMMIT = re.compile(r"\bgit\b[^;&|]*\bcommit\b")
PUSH = re.compile(r"\bgit\b[^;&|]*\bpush\b")


def current_branch(cwd: str) -> str:
    r = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip()


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("tool_name") != "Bash":
        return 0
    cmd = event.get("tool_input", {}).get("command", "")
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", ".")
    on_main = current_branch(cwd) in PROTECTED
    if COMMIT.search(cmd) and on_main:
        print("branch-guard: коммит в main запрещён. Создай ветку: git switch -c feat/<задача>", file=sys.stderr)
        return 2
    if PUSH.search(cmd) and (on_main or re.search(r"\b(main|master)\b", cmd)):
        print("branch-guard: push в main запрещён. В main — только через PR.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
