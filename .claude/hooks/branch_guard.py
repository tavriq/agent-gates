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
# где решение зависит от текущей ветки или меняет её
TRACKED = ON_BRANCH | {"reset", "pull", "push", "send-pack", "rebase", "switch", "checkout", "branch", "update-ref",
                       "symbolic-ref"}
# без ветки: решение по самой команде
UNTRACKED = {"worktree", "fetch", "config", "filter-branch", "filter-repo", "fast-import"}
# настройки, которые уводят обычный git push в main: remote.origin.push=HEAD:main, push.default=upstream
PUSH_CONFIG = re.compile(r"(?:remote\..+\.(?:push|mirror)|push\.default)(?:=|$)", re.I)
SYNC = "Синхронизация main с сервером — git pull --ff-only."


def protected(ref) -> bool:
    """main, +main, heads/main, refs/heads/main — всё это main."""
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


def on_main(branch) -> bool:
    return branch in PROTECTED or branch == SWITCHED


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
    if a.operands:
        return None, "main" if protected(a.operands[0]) else SWITCHED
    if a.flags & {"--detach", "-d"}:
        return None, SWITCHED
    return None, None


def check(call, branch, head, cwd):
    """→ (причина стопа или None, ветка после команды или None).
    branch() — текущая ветка (лениво); head — ревизия, на которой окажется команда, None — неизвестно."""
    sub, a = call.sub, call.args
    ops = a.operands
    if any(PUSH_CONFIG.match(c) for c in call.config):
        return "git -c с настройкой push (remote.*.push, push.default) может увести push в main — запрещено.", None
    if sub in ("switch", "checkout"):
        return switch(call)
    if sub == "symbolic-ref":  # symbolic-ref HEAD refs/heads/main — переключение ветки без checkout
        if len(ops) > 1 and ops[0] == "HEAD":
            return None, "main" if protected(ops[1]) else SWITCHED
        return None, None
    if sub == "worktree":
        if ops[:1] == ["add"] and protected(a.values.get("-B")):
            return "git worktree add -B main пересоздаёт main на другом коммите.", None
        return None, None
    if sub == "config":
        reading = len(ops) < 2 or ops[0] == "get"
        if any(PUSH_CONFIG.match(o) for o in ops) and not (reading and not a.flags & {"--add", "--replace-all", "--unset", "--unset-all"}):
            return "git config с настройкой push (remote.*.push, push.default) может увести push в main — запрещено.", None
        return None, None
    if sub in ("filter-branch", "filter-repo", "fast-import"):  # какие ветки перепишут, видно только им самим
        return f"git {sub} переписывает историю или ссылки веток — только человеком и через PR.", None
    if sub == "branch":
        move, copy = a.flags & {"-m", "-M", "--move"}, a.flags & {"-c", "-C", "--copy"}
        if a.flags & {"-d", "-D", "--delete"}:
            return None, None
        if move or copy:
            src, dst = (ops[0], ops[1]) if len(ops) > 1 else (branch(), ops[0] if ops else None)
            if protected(dst) or (move and (protected(src) or src == SWITCHED)):
                return "git branch -m/-c переносит main — запрещено.", None
        elif a.flags & {"-f", "--force"} and ops and protected(ops[0]):
            return "git branch -f main переносит main на другой коммит — запрещено.", None
        return None, None
    if sub == "update-ref":
        if "--stdin" in a.flags:
            return "git update-ref --stdin: гейт не видит, какие ветки меняются.", None
        ref = ops[0] if ops else ""
        if protected(ref) or (ref == "HEAD" and on_main(branch())):
            return "git update-ref переносит main — запрещено.", None
        return None, None
    if sub == "fetch":
        for spec in ops:
            src, sep, dst = spec.partition(":")
            if sep and protected(dst) and not protected(src):
                return f"git fetch {spec} записывает в main чужую ветку — запрещено.", None
        return None, None
    if sub in ("push", "send-pack"):
        # куда пишет refspec: часть после ":" (или весь, если ":" нет); первый операнд — remote, но и его
        # проверяем: в нём ":" бывает только у URL, а лишний стоп лучше пропуска
        many = a.flags & {"--all", "--branches", "--mirror"} or any("*" in s for s in ops)
        to_main = any(protected(s.rpartition(":")[2]) for s in ops)
        if on_main(branch()) or many or to_main:
            return "push в main запрещён. В main — только через PR.", None
        return None, None
    if sub == "rebase":
        if "--abort" in a.flags:
            return None, None
        # rebase <база> <ветка> сначала переключается на <ветку>; с --root ветка — единственный аргумент.
        # Значение незнакомой опции сдвинет аргументы, поэтому main ищем среди всех кандидатов
        targets = ops if "--root" in a.flags else ops[1:]
        if any(map(protected, targets)) or (not targets and on_main(branch())):
            return "rebase переписывает историю main — запрещено. " + SYNC, None
        return None, targets[-1] if targets else None
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
        if target and (head is None or target != commit_of(cwd, call.where, head)):
            return f"git reset {a.pos[0]} переносит main — запрещено. " + SYNC, None
        return None, None  # reset без ревизии (или на ту же) только снимает файлы с индекса
    if sub == "pull":
        rebase = "-r" in a.flags or ("--rebase" in a.flags and a.values.get("--rebase", "true") != "false")
        if rebase or "--ff-only" not in a.flags or "--no-ff" in a.flags:
            return "на main можно только git pull --ff-only: остальное сливает или переписывает.", None
        for spec in ops[1:]:
            if not protected(spec.partition(":")[0]):
                return f"git pull --ff-only {spec} вливает в main другую ветку — только через PR.", None
        return None, None
    return None, None


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from shellwords import git_calls

    event = json.load(sys.stdin)
    if event.get("tool_name") not in ("Bash", "Monitor"):
        return 0
    cmd = event.get("tool_input", {}).get("command", "")
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", ".")
    # гейт смотрит до запуска, поэтому проходит команду по вызовам git и следит, на какой ветке окажется агент.
    # Ключ — git-папка рабочей копии: git -C . и git без -C — одна и та же копия
    repos, branches, moved = {}, {}, set()
    for call in git_calls(cmd):
        if call.dynamic:
            what = "подкоманда git" if call.dynamic == call.sub else f"в git {call.sub or ''} слово".replace("  ", " ")
            print(f"branch-guard: {what} {call.dynamic} берётся из переменной или подстановки — "
                  "гейт не видит, что выполнится. Напиши подкоманду, ветку и путь явно; "
                  "подстановка допустима только в тексте сообщения (-m, -F).", file=sys.stderr)
            return 2
        if call.sub not in TRACKED | UNTRACKED and not call.config:
            continue
        where = tuple(call.where)
        if where not in repos:
            r = git(cwd, call.where, "rev-parse", "--absolute-git-dir")
            repos[where] = r.stdout.strip() if r.returncode == 0 else where
        key = repos[where]

        def branch(key=key, where=call.where):
            if key not in branches:
                branches[key] = current_branch(cwd, where)
            return branches[key]

        def head(key=key):
            if key not in moved:
                return "HEAD"
            b = branches.get(key)
            return None if b in ("", SWITCHED, None) else f"refs/heads/{b}"

        reason, after = check(call, branch, head(), cwd)
        if reason and branches.get(key) == SWITCHED:
            # остановило бы и на обычной ветке? Тогда настоящая причина — она; иначе мешает только неизвестная ветка
            reason = check(call, lambda: "", head(), cwd)[0] or (
                f"переключение ветки и git {call.sub} — разными командами: гейт не знает, на какой ветке окажется агент.")
        if reason:
            print(f"branch-guard: {reason}", file=sys.stderr)
            return 2
        if after is not None:
            branches[key] = after
            moved.add(key)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # упавший гейт закрывает: exit 1 Claude Code считает «пропустить»
        print(f"branch-guard: сбой гейта ({e!r}), действие остановлено.", file=sys.stderr)
        sys.exit(2)
