#!/usr/bin/env python3
"""PreToolUse(Bash): история main меняется только через PR (SPEC.md, G1–G2)."""
import json
import os
import re
import subprocess
import sys

PROTECTED = {"main", "master"}
SWITCHED = "?"  # ветка после switch/checkout на существующую: гейт её не угадывает и считает за main
ON_BRANCH = {"commit", "merge", "cherry-pick", "revert", "am"}  # добавляют коммиты в текущую ветку
# цель push — main: "origin main", "HEAD:main", "+main", "refs/heads/main"; feat/main-fix — не main
TO_MAIN = re.compile(r"(?:[\s:+]|refs/heads/)(?:main|master)(?![\w./-])")
# push всех веток разом: --all, --mirror, --branches, refspec со звёздочкой
MANY = re.compile(r"\s--(?:all|mirror|branches)\b|\*")
SYNC = "Синхронизация main с сервером — git pull --ff-only."


def protected(ref) -> bool:
    ref = (ref or "").lstrip("+")
    for prefix in ("refs/heads/", "heads/"):
        ref = ref.removeprefix(prefix)
    return ref in PROTECTED


def git(cwd, where, *args):
    return subprocess.run(["git", *where, *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def current_branch(cwd, where) -> str:
    r = git(cwd, where, "symbolic-ref", "-q", "--short", "HEAD")
    if r.returncode == 0:
        return r.stdout.strip()
    if r.returncode == 1:  # HEAD отцеплен; посреди rebase переписывается ветка из rebase-*/head-name
        gitdir = git(cwd, where, "rev-parse", "--absolute-git-dir").stdout.strip()
        for d in ("rebase-merge", "rebase-apply"):
            path = os.path.join(gitdir, d, "head-name")
            if gitdir and os.path.isfile(path):
                with open(path) as f:
                    return f.read().strip().removeprefix("refs/heads/")
        return ""
    if "not a git repository" in r.stderr:
        return ""
    raise RuntimeError(f"git не ответил, на какой ветке HEAD: {r.stderr.strip()}")


def commit_of(cwd, where, rev):
    r = git(cwd, where, "rev-parse", "-q", "--verify", f"{rev}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else None


def switch(call):
    """switch/checkout → (причина стопа, ветка после команды)."""
    a = call.args
    if call.sub == "checkout" and a.paths:  # checkout [ревизия] -- пути: файлы, а не ветка
        return None, None
    force = a.values.get("-C") or a.values.get("--force-create") or a.values.get("-B")
    if force:
        if protected(force):
            return f"git {call.sub} {'-C' if call.sub == 'switch' else '-B'} {force} пересоздаёт main на другом коммите.", None
        return None, force
    new = a.values.get("-c") or a.values.get("--create") or a.values.get("-b") or a.values.get("--orphan")
    if new:
        return None, "main" if protected(new) else new
    if a.pos:
        return None, "main" if protected(a.pos[0]) else SWITCHED
    if a.flags & {"--detach", "-d"}:
        return None, SWITCHED
    return None, None


def check(call, branch, cwd):
    """→ (причина стопа или None, ветка после команды или None). branch() — текущая ветка, лениво."""
    sub, a = call.sub, call.args
    if sub in ("switch", "checkout"):
        return switch(call)
    if sub == "worktree":
        if a.pos[:1] == ["add"] and protected(a.values.get("-B")):
            return "git worktree add -B main пересоздаёт main на другом коммите.", None
        return None, None
    if sub == "branch":
        move, copy = a.flags & {"-m", "-M", "--move"}, a.flags & {"-c", "-C", "--copy"}
        if a.flags & {"-d", "-D", "--delete"}:
            return None, None
        if move or copy:
            src, dst = (a.pos[0], a.pos[1]) if len(a.pos) > 1 else (branch(), a.pos[0] if a.pos else None)
            if protected(dst) or (move and (protected(src) or src == SWITCHED)):
                return "git branch -m/-c переносит main — запрещено.", None
        elif a.flags & {"-f", "--force"} and a.pos and protected(a.pos[0]):
            return "git branch -f main переносит main на другой коммит — запрещено.", None
        return None, None
    if sub == "update-ref":
        if "--stdin" in a.flags:
            return "git update-ref --stdin: гейт не видит, какие ветки меняются.", None
        ref = a.pos[0] if a.pos else ""
        if protected(ref) or (ref == "HEAD" and on_main(branch())):
            return "git update-ref переносит main — запрещено.", None
        return None, None
    if sub == "fetch":
        for spec in a.pos[1:]:
            src, sep, dst = spec.partition(":")
            if sep and protected(dst) and not protected(src):
                return f"git fetch {spec} записывает в main чужую ветку — запрещено.", None
        return None, None
    if sub == "filter-branch":
        if on_main(branch()) or "--all" in a.flags | set(a.paths) or any(map(protected, a.pos + a.paths)):
            return "git filter-branch переписывает историю main — запрещено.", None
        return None, None
    if sub == "push":
        if on_main(branch()) or TO_MAIN.search(" " + " ".join(call.rest)) or MANY.search(" " + " ".join(call.rest)):
            return "push в main запрещён. В main — только через PR.", None
        return None, None
    if sub == "rebase":
        if "--abort" in a.flags:
            return None, None
        # rebase <база> <ветка> сначала переключается на <ветку>; с --root ветка — единственный аргумент
        target = a.pos[1] if len(a.pos) > 1 else (a.pos[0] if "--root" in a.flags and a.pos else None)
        if protected(target) or (not target and on_main(branch())):
            return "rebase переписывает историю main — запрещено. " + SYNC, None
        return None, target
    if sub not in ON_BRANCH | {"reset", "pull"} or not on_main(branch()):
        return None, None
    # дальше — команды, опасные только на main
    if sub in ON_BRANCH:
        if sub != "commit" and "--abort" in a.flags:
            return None, None
        if sub == "commit":
            return "коммит в main запрещён. Создай ветку: git switch -c feat/<задача>", None
        return f"git {sub} в main запрещён — main меняется только через PR.", None
    if sub == "reset":
        if "--hard" in a.flags:
            return "git reset --hard на main запрещён. " + SYNC, None
        target = commit_of(cwd, call.where, a.pos[0]) if a.pos else None
        if target and target != commit_of(cwd, call.where, "HEAD"):
            return f"git reset {a.pos[0]} переносит main — запрещено. " + SYNC, None
        return None, None  # reset без ревизии только снимает файлы с индекса
    if sub == "pull":
        rebase = "-r" in a.flags or ("--rebase" in a.flags and a.values.get("--rebase", "true") != "false")
        if rebase or "--ff-only" not in a.flags or "--no-ff" in a.flags:
            return "на main можно только git pull --ff-only: остальное сливает или переписывает.", None
        for spec in a.pos[1:]:
            if not protected(spec.partition(":")[0]):
                return f"git pull --ff-only {spec} вливает в main другую ветку — только через PR.", None
        return None, None
    return None, None


def on_main(branch) -> bool:
    return branch in PROTECTED or branch == SWITCHED


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from shellwords import git_calls

    event = json.load(sys.stdin)
    if event.get("tool_name") not in ("Bash", "Monitor"):
        return 0
    cmd = event.get("tool_input", {}).get("command", "")
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", ".")
    # гейт смотрит до запуска, поэтому проходит команду по вызовам git и следит, на какой ветке окажется агент
    branches = {}
    for call in git_calls(cmd):
        if call.dynamic:
            what = "подкоманда git" if call.dynamic == call.sub else f"в git {call.sub} слово"
            print(f"branch-guard: {what} {call.dynamic} берётся из переменной или подстановки — "
                  "гейт не видит, что выполнится. Напиши подкоманду, ветку и путь явно; "
                  "подстановка допустима только в тексте сообщения (-m, -F).", file=sys.stderr)
            return 2
        key = tuple(call.where)

        def branch(key=key, where=call.where):
            if key not in branches:
                branches[key] = current_branch(cwd, where)
            return branches[key]

        reason, after = check(call, branch, cwd)
        if reason and branches.get(key) == SWITCHED:
            # остановило бы и на обычной ветке? Тогда настоящая причина — она; иначе мешает только неизвестная ветка
            reason = check(call, lambda: "", cwd)[0] or (
                f"переключение ветки и git {call.sub} — разными командами: гейт не знает, на какой ветке окажется агент.")
        if reason:
            print(f"branch-guard: {reason}", file=sys.stderr)
            return 2
        if after is not None:
            branches[key] = after
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # упавший гейт закрывает: exit 1 Claude Code считает «пропустить»
        print(f"branch-guard: сбой гейта ({e!r}), действие остановлено.", file=sys.stderr)
        sys.exit(2)
