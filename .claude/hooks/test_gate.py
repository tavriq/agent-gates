#!/usr/bin/env python3
"""PreToolUse(Bash): перед git commit гоняет тесты. Красные — коммита нет."""
import json
import os
import re
import subprocess
import sys

COMMIT = re.compile(r"\bgit\b[^;&|]*\bcommit\b")


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("tool_name") != "Bash" or not COMMIT.search(event.get("tool_input", {}).get("command", "")):
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or "."
    venv = os.path.join(root, ".venv", "bin", "python")
    python = venv if os.path.exists(venv) else sys.executable
    r = subprocess.run([python, "-m", "pytest", "-q", "-x"], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-15:])
        print(f"test-gate: тесты красные, коммит отклонён.\n{tail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
