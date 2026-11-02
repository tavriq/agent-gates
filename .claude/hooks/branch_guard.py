#!/usr/bin/env python3
"""PreToolUse(Bash): история main меняется только через PR (SPEC.md, G1–G2)."""
import json
import os
import re
import subprocess
import sys

PROTECTED = {"main", "master"}
SWITCHED = "?"  # ветка после switch/checkout на существующую: гейт её не угадывает и считает за main
# добавляют коммиты в текущую ветку
ON_BRANCH = {"commit", "merge", "cherry-pick", "revert", "am", "subtree", "citool", "gui", "svn", "p4"}
# где решение зависит от текущей ветки или меняет её
TRACKED = ON_BRANCH | {"reset", "pull", "push", "send-pack", "http-push", "rebase", "switch", "checkout", "branch",
                       "update-ref", "symbolic-ref"}
# без ветки: решение по самой команде и настройкам репозитория
UNTRACKED = {"worktree", "fetch", "remote", "config", "notes", "filter-branch", "filter-repo", "fast-import"}
SYNC = "Синхронизация main с сервером — git pull --ff-only."
# встроенные команды git, которые не двигают ветки; всё прочее — алиас или внешняя git-<имя>
SAFE_BUILTINS = {
    "status", "log", "diff", "show", "add", "rm", "mv", "restore", "stash", "tag", "describe", "blame", "grep",
    "shortlog", "help", "version", "reflog", "show-ref", "cat-file", "rev-list", "rev-parse", "ls-files", "ls-tree",
    "ls-remote", "name-rev", "merge-base", "for-each-ref", "check-ignore", "check-attr", "clean", "init", "clone",
    "archive", "bisect", "bundle", "gc", "fsck", "prune", "repack", "count-objects", "submodule", "sparse-checkout",
    "mergetool", "difftool", "format-patch", "request-pull", "range-diff", "whatchanged", "var", "hash-object",
    "write-tree", "read-tree", "commit-tree", "update-index", "diff-files", "diff-index", "diff-tree", "apply",
    "maintenance", "replace", "show-branch", "cherry", "verify-commit", "verify-tag", "credential", "blame", "annotate",
    "merge-tree", "merge-file", "interpret-trailers", "column", "check-ref-format", "symbolic-ref", "pack-refs",
    "lfs",
}
# для алиаса и внешней команды — прежнее правило по словам: гейт не знает, что они сделают
ALIAS_ON_MAIN = re.compile(r"\b(?:commit|merge|cherry-pick|revert|am|rebase|reset|pull)\b")


def protected(ref) -> bool:
    """main, +main, heads/main, refs/heads/main — всё это main."""
    ref = (ref or "").lstrip("+")
    for prefix in ("refs/heads/", "heads/"):
        ref = ref.removeprefix(prefix)
    return ref in PROTECTED


def protected_upstream(ref) -> bool:
    """origin/main, refs/remotes/origin/main — main на сервере."""
    ref = (ref or "").removeprefix("refs/remotes/")
    return protected(ref) or protected(ref.partition("/")[2])


def config_risk(key, value):
    """Может ли настройка увести push, pull или fetch в main или перенести ветку. value None — значение не видно."""
    k = key.lower()
    if k == "*":
        return True
    if re.fullmatch(r"remote\..+\.(?:push|mirror)", k):
        return True
    if re.fullmatch(r"remote\..+\.fetch", k):
        if value is None:
            return True
        src, _, dst = value.lstrip("+").partition(":")
        return (protected(dst) and not protected(src)) or (dst.startswith("refs/heads/") and "*" in dst)
    if k == "push.default":
        return value is None or value.lower() in ("upstream", "tracking", "matching")
    m = re.fullmatch(r"branch\.(.+)\.(merge|remote)", key, re.I)
    if m:
        return protected(m.group(1)) and not (m.group(2).lower() == "merge" and value is not None and protected(value))
    if k == "rebase.updaterefs":
        return value is None or value.lower() in ("true", "yes", "on", "1")
    if k == "core.notesref":
        return value is None or protected(value)
    return False


def git(cwd, where, *args):
    return subprocess.run(["git", *where, *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def settings(cwd, call, pattern):
    """Настройки репозитория с учётом -c этой команды: [(ключ, значение)]."""
    opts = [x for key, value in call.config if value is not None for x in ("-c", f"{key}={value}")]
    out = git(cwd, call.where + opts, "config", "--get-regexp", pattern).stdout
    return [tuple(line.partition(" ")[::2]) for line in out.splitlines()]


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


def config_command(a):
    """git config: опасная запись → ключ, иначе None. Чтение и удаление настроек не опасны."""
    ops = a.operands
    if ops[:1] == ["set"] and len(ops) > 2:
        key, value = ops[1], ops[2]
    elif ops[:1] in (["get"], ["list"], ["unset"], ["edit"], ["rename-section"], ["remove-section"]) or a.flags & {
            "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "--unset", "--unset-all",
            "--remove-section", "--rename-section", "--edit"}:
        return None
    elif len(ops) > 1:
        key, value = ops[0], ops[1]
    else:
        return None
    return key if config_risk(key, value) else None


def check(call, branch, head, cwd):
    """→ (причина стопа или None, ветка после команды или None).
    branch() — текущая ветка (лениво); head — "HEAD", если ветку в этой команде не переключали."""
    sub, a = call.sub, call.args
    ops = a.operands
    risky = next((key for key, value in call.config if config_risk(key, value)), None)
    if risky:
        what = "подменён файл настроек" if risky == "*" else f"настройка {risky}"
        return f"в git {sub}: {what} — может увести push, pull или fetch в main. Без неё или через PR.", None
    if sub in ("switch", "checkout"):
        return switch(call)
    if sub == "symbolic-ref":  # symbolic-ref HEAD refs/heads/main — переключение ветки без checkout
        if "HEAD" in ops and len(ops) > 1:
            return None, "main" if any(map(protected, ops)) else SWITCHED
        return None, None
    if sub == "worktree":
        if ops[:1] == ["add"] and protected(a.values.get("-B")):
            return "git worktree add -B main пересоздаёт main на другом коммите.", None
        return None, None
    if sub == "config":
        key = config_command(a)
        return (f"git config {key}: настройка может увести push, pull или fetch в main — запрещено.", None) if key else (None, None)
    if sub in ("filter-branch", "filter-repo", "fast-import"):  # какие ветки перепишут, видно только им самим
        return f"git {sub} переписывает историю или ссылки веток — только человеком и через PR.", None
    if sub == "notes":
        ref = a.values.get("--ref") or next((v for k, v in settings(cwd, call, r"^core\.notesref$")), None)
        if ref and protected(ref):
            return "git notes пишет в ветку main — запрещено.", None
        return None, None
    if sub == "branch":
        move, copy = a.flags & {"-m", "-M", "--move"}, a.flags & {"-c", "-C", "--copy"}
        upstream = a.values.get("-u") or a.values.get("--set-upstream-to")
        if a.flags & {"-d", "-D", "--delete"}:
            return None, None
        if move or copy:
            src, dst = (ops[0], ops[1]) if len(ops) > 1 else (branch(), ops[0] if ops else None)
            if protected(dst) or (move and (protected(src) or src == SWITCHED)):
                return "git branch -m/-c переносит main — запрещено.", None
        elif a.flags & {"-f", "--force"} and ops and protected(ops[0]):
            return "git branch -f main переносит main на другой коммит — запрещено.", None
        elif upstream:
            target = ops[0] if ops else branch()
            if (protected(target) or target == SWITCHED) and not protected_upstream(upstream):
                return f"git branch -u {upstream} для main: потом pull --ff-only перемотает main на чужую ветку.", None
        return None, None
    if sub == "update-ref":
        if "--stdin" in a.flags:
            return "git update-ref --stdin: гейт не видит, какие ветки меняются.", None
        ref = ops[0] if ops else ""
        if protected(ref) or (ref == "HEAD" and on_main(branch())):
            return "git update-ref переносит main — запрещено.", None
        return None, None
    if sub == "remote":
        if ops[:1] == ["add"] and "--mirror" in a.flags:
            return "git remote add --mirror: такой remote пишет ветки целиком, в том числе main — запрещено.", None
        if ops[:1] not in (["update"], ["prune"]):
            return None, None
    if sub in ("fetch", "pull", "remote"):
        for spec in ops if sub != "remote" else []:
            src, sep, dst = spec.partition(":")
            if sep and protected(dst) and not protected(src):
                return f"git {sub} {spec} записывает в main чужую ветку — запрещено.", None
        refmap = a.values.get("--refmap")
        if refmap and config_risk("remote.x.fetch", refmap):
            return f"git {sub} --refmap={refmap} записывает в main чужую ветку — запрещено.", None
        # fetch обновляет ветки и по настройке remote.*.fetch — даже когда refspec указан явно
        risky = next((k for k, v in settings(cwd, call, r"^remote\..*\.fetch$") if config_risk(k, v)), None)
        if risky:
            return f"настройка {risky} записывает в main чужую ветку — fetch запрещён, пока она есть.", None
        if sub != "pull":
            return None, None
    if sub == "subtree":  # subtree push пушит, split --branch переносит ветку; add/merge/pull — коммиты на main
        if ops[:1] == ["push"] and any(protected(s.rpartition(":")[2]) for s in ops[1:]):
            return "git subtree push в main запрещён. В main — только через PR.", None
        if protected(a.values.get("-b") or a.values.get("--branch")):
            return "git subtree split --branch main переносит main — запрещено.", None
    if sub in ("push", "send-pack", "http-push"):
        if "--stdin" in a.flags:
            return f"git {sub} --stdin: гейт не видит, какие ветки уйдут.", None
        # куда пишет refspec: часть после ":" (или весь, если ":" нет); первый операнд — remote, но и его
        # проверяем: в нём ":" бывает только у URL, а лишний стоп лучше пропуска
        many = a.flags & {"--all", "--branches", "--mirror"} or any("*" in s for s in ops)
        to_main = any(protected(s.rpartition(":")[2]) for s in ops)
        if on_main(branch()) or many or to_main:
            return "push в main запрещён. В main — только через PR.", None
        # без refspec — и для refspec без ":" — получателя берут из настроек (push origin feat при
        # remote.origin.push=refs/heads/feat:refs/heads/main уходит в main)
        if sub == "push" and (len(ops) < 2 or any(":" not in s for s in ops[1:])):
            for key, value in settings(cwd, call, r"^(remote\..*\.(push|mirror)|push\.default)$"):
                k = key.lower()
                bad = (k.endswith(".push") and (protected(value.rpartition(":")[2]) or "*" in value)) or (
                    k.endswith(".mirror") and value.lower() in ("true", "yes", "on", "1")) or (
                    k == "push.default" and value.lower() == "matching") or (
                    k == "push.default" and value.lower() in ("upstream", "tracking") and any(
                        protected(v) for _, v in settings(cwd, call, rf"^branch\.{re.escape(branch())}\.merge$")))
                if bad:
                    return f"настройка {key} = {value} уводит git push в main — укажи ветку явно: git push origin <ветка>.", None
        return None, None
    if sub == "rebase":
        if "--abort" in a.flags:
            return None, None
        update_refs = "--update-refs" in a.flags or (
            "--no-update-refs" not in a.flags
            and any(config_risk(k, v) for k, v in settings(cwd, call, r"^rebase\.updaterefs$")))
        if update_refs:
            return "rebase --update-refs переносит и другие ветки, в том числе main, — запрещено.", None
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
        if a.pos and head != "HEAD":
            # HEAD~1, @{-1}, HEAD@{1} гейт вычислил бы до переключения — а выполнятся они после
            return "переключение на main и git reset на ревизию — разными командами.", None
        target = commit_of(cwd, call.where, a.pos[0]) if a.pos else None
        if target and target != commit_of(cwd, call.where, "HEAD"):
            return f"git reset {a.pos[0]} переносит main — запрещено. " + SYNC, None
        return None, None  # reset без ревизии (или на ту же) только снимает файлы с индекса
    if sub == "pull":
        rebase = "-r" in a.flags or ("--rebase" in a.flags and a.values.get("--rebase", "true") != "false")
        if rebase or "--ff-only" not in a.flags or "--no-ff" in a.flags:
            return "на main можно только git pull --ff-only: остальное сливает или переписывает.", None
        for spec in ops[1:]:
            if not protected(spec.partition(":")[0]):
                return f"git pull --ff-only {spec} вливает в main другую ветку — только через PR.", None
        if len(ops) < 2:  # без ветки pull берёт её из branch.<ветка>.merge
            name = branch() if branch() != SWITCHED else "main"
            merge = [v for _, v in settings(cwd, call, rf"^branch\.{re.escape(name)}\.merge$")]
            if any(not protected(v) for v in merge):
                return f"у main upstream — {merge[0]}: pull --ff-only перемотает main на чужую ветку.", None
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
        unknown = call.sub not in TRACKED | UNTRACKED | SAFE_BUILTINS
        if not unknown and call.sub not in TRACKED | UNTRACKED and not call.config:
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

        head = "HEAD" if key not in moved else None
        reason, after = check(call, branch, head, cwd)
        if not reason and unknown:
            words = [f"{k}={v}" for k, v in call.config] + [call.sub] + call.rest
            text = " " + " ".join(words)
            pushes = re.search(r"\bpush\b", text) and (any(map(protected, call.rest)) or "--all" in text or "--mirror" in text)
            if pushes or (on_main(branch()) and ALIAS_ON_MAIN.search(text)):
                reason = (f"git {call.sub} — алиас или внешняя команда; на main или с push в main гейт её не пропускает: "
                          "не видно, что она сделает.")
        if reason and branches.get(key) == SWITCHED:
            # остановило бы и на обычной ветке? Тогда настоящая причина — она; иначе мешает только неизвестная ветка
            reason = check(call, lambda: "", head, cwd)[0] or (
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
