#!/usr/bin/env python3
"""PreToolUse(Bash): перед git commit гоняет тесты. Красные — коммита нет."""
import json
import os
import re
import subprocess
import sys

COMMIT = re.compile(r"\bgit\b[^;&|]*\bcommit\b")
# зависший хук Claude Code по своему таймауту (120 с) пропускает, поэтому режем раньше сами
LIMIT = int(os.environ.get("TEST_GATE_LIMIT", "100"))


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("tool_name") not in ("Bash", "Monitor") or not COMMIT.search(event.get("tool_input", {}).get("command", "")):
        return 0
    project = os.environ.get("CLAUDE_PROJECT_DIR") or "."
    # cwd идёт за агентом, в том числе в worktree; CLAUDE_PROJECT_DIR остаётся в основной копии
    cwd = event.get("cwd") or project
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True).stdout.strip()
    root = top or cwd
    venvs = [os.path.join(d, ".venv", "bin", "python") for d in (root, project)]
    python = next((v for v in venvs if os.path.exists(v)), sys.executable)
    try:
        r = subprocess.run([python, "-m", "pytest", "-q", "-x"], cwd=root, capture_output=True, text=True, timeout=LIMIT)
    except subprocess.TimeoutExpired:
        print(f"test-gate: тесты не уложились в {LIMIT} с, коммит отклонён.", file=sys.stderr)
        return 2
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-15:])
        print(f"test-gate: тесты красные, коммит отклонён.\n{tail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # упавший гейт закрывает: exit 1 Claude Code считает «пропустить»
        print(f"test-gate: сбой гейта ({e!r}), коммит остановлен.", file=sys.stderr)
        sys.exit(2)
