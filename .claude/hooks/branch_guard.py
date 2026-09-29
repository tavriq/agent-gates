#!/usr/bin/env python3
"""PreToolUse(Bash): коммит и push в main запрещены — только ветка и PR."""
import json
import os
import re
import subprocess
import sys

PROTECTED = {"main", "master"}
# то, что двигает текущую ветку: commit, merge, cherry-pick, revert (merge-base и --merges — нет)
COMMIT = re.compile(r"\bgit\b.*?\b(?:commit|merge|cherry-pick|revert)(?![\w-])")
PUSH = re.compile(r"\bgit\b.*?\bpush\b")
# смена ветки: "git switch main", "git -C x checkout main"
SWITCH = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:switch|checkout)\b(.*)")
NEW_BRANCH = re.compile(r"\s(?:-[cbCB]|--create|--force-create)\s+['\"]?([^\s'\"]+)")
# цель push — main: "origin main", "HEAD:main", "+main", "refs/heads/main"; feat/main-fix — не main
TO_MAIN = re.compile(r"(?:[\s:+]|refs/heads/)(?:main|master)(?![\w./-])")
# push всех веток разом: --all, --mirror, refspec со звёздочкой
MANY = re.compile(r"\s--(?:all|mirror)\b|\*")


def current_branch(cwd: str) -> str:
    r = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip()


def main() -> int:
    event = json.load(sys.stdin)
    if event.get("tool_name") not in ("Bash", "Monitor"):
        return 0
    cmd = event.get("tool_input", {}).get("command", "")
    parts = re.split(r"[;&|\n]+", cmd)
    acts = [bool(COMMIT.search(p) or PUSH.search(p)) for p in parts]
    if not any(acts):
        return 0
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", ".")
    # гейт смотрит до запуска, поэтому проходит команду по частям и следит, на какой ветке окажется агент
    branch = current_branch(cwd)
    for i, part in enumerate(parts):
        switch = SWITCH.search(part)
        if switch:
            new = NEW_BRANCH.search(switch.group(1))
            if new and new.group(1) not in PROTECTED:
                branch = new.group(1)
            elif any(acts[i + 1:]):
                print("branch-guard: переключение ветки и commit/push — разными командами.", file=sys.stderr)
                return 2
            continue
        if COMMIT.search(part) and branch in PROTECTED:
            print("branch-guard: коммит в main запрещён. Создай ветку: git switch -c feat/<задача>", file=sys.stderr)
            return 2
        if PUSH.search(part) and (branch in PROTECTED or TO_MAIN.search(part) or MANY.search(part)):
            print("branch-guard: push в main запрещён. В main — только через PR.", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # упавший гейт закрывает: exit 1 Claude Code считает «пропустить»
        print(f"branch-guard: сбой гейта ({e!r}), действие остановлено.", file=sys.stderr)
        sys.exit(2)
