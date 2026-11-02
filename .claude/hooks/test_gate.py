#!/usr/bin/env python3
"""PreToolUse(Bash): перед git commit гоняет тесты по тому, что уйдёт в коммит (SPEC.md, G4). Красные — коммита нет."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

# зависший хук Claude Code по своему таймауту (120 с) пропускает, поэтому режем раньше сами
LIMIT = int(os.environ.get("TEST_GATE_LIMIT", "100"))
# индекс не трогают: до коммита в той же команде их можно не повторять
READONLY = {
    "status", "diff", "log", "show", "branch", "tag", "fetch", "push", "remote", "rev-parse", "ls-files", "ls-tree",
    "ls-remote", "describe", "blame", "grep", "shortlog", "config", "help", "version", "reflog", "show-ref",
    "cat-file", "rev-list", "name-rev", "merge-base", "for-each-ref", "check-ignore", "check-attr", "worktree",
}
INTERACTIVE = {"-p", "--patch", "-i", "--interactive", "-e", "--edit", "--pathspec-from-file"}


class Stop(Exception):
    pass


def git(cwd, where, *args, env=None):
    return subprocess.run(["git", *where, *args], cwd=cwd, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=30)


def replay(cwd, where, env, *args):
    """Повторить git-команду на копии индекса: настоящий индекс и рабочая копия не меняются."""
    r = git(cwd, where, *args, env=env)
    if r.returncode != 0:
        raise Stop(f"не удалось повторить git {' '.join(args)} на копии индекса: {r.stderr.strip()}")


def predict(calls, k, cwd, tmp) -> dict:
    """Копия индекса плюс то, что команда добавит в него до коммита calls[k]. → env с GIT_INDEX_FILE снимка."""
    commit = calls[k]
    where = commit.where
    index = os.path.join(tmp, "index")
    gitdirs = {}

    def gitdir_of(w):  # git -C . и git без -C — одна копия
        if tuple(w) not in gitdirs:
            gitdirs[tuple(w)] = git(cwd, w, "rev-parse", "--absolute-git-dir").stdout.strip() or tuple(w)
        return gitdirs[tuple(w)]

    gitdir = gitdir_of(where)
    if gitdir and os.path.exists(os.path.join(gitdir, "index")):  # у worktree свой индекс — в его git-папке
        # copy2, а не copyfile: со свежим mtime копии git поверит stat-данным и пропустит правку
        # того же размера в ту же секунду (racy git)
        shutil.copy2(os.path.join(gitdir, "index"), index)
    env = {**os.environ, "GIT_INDEX_FILE": index}
    for c in calls[:k]:
        new_branch_only = c.sub in ("switch", "checkout") and not c.args.pos and not c.args.paths and (
            c.args.values.keys() & {"-c", "--create", "-b", "--orphan"})
        if c.sub in READONLY or new_branch_only:
            continue
        if c.sub not in ("add", "rm") or gitdir_of(c.where) != gitdir:
            raise Stop(f"git {c.sub} до коммита в той же команде меняет индекс так, что гейт не может предсказать "
                       "содержимое коммита. Выполни её отдельной командой, коммит — следующей.")
        if c.args.flags & INTERACTIVE:
            raise Stop(f"интерактивный git {c.sub} до коммита гейт не повторит. Добавь файлы отдельной командой.")
        if c.args.flags & {"-n", "--dry-run"}:
            continue
        if c.sub == "add":
            replay(cwd, where, env, "add", *c.rest)
        else:
            replay(cwd, where, env, "rm", "--cached", "-f", "-q", *c.rest)
    a = commit.args
    if a.flags & {"-p", "--patch", "--interactive", "--pathspec-from-file"}:
        raise Stop("git commit -p и --pathspec-from-file гейт не повторит. Подготовь индекс отдельной командой.")
    if a.flags & {"-a", "--all"}:
        replay(cwd, where, env, "add", "-u")
    paths = a.pos + a.paths
    if paths and a.flags & {"-i", "--include"}:
        replay(cwd, where, env, "add", "--", *paths)
    elif paths:  # commit <пути>: HEAD плюс эти пути из рабочей копии, остальное из индекса в коммит не идёт
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index-only")}
        head = git(cwd, where, "rev-parse", "-q", "--verify", "HEAD^{commit}").returncode == 0
        replay(cwd, where, env, "read-tree", "HEAD" if head else "--empty")
        replay(cwd, where, env, "add", "--", *paths)
    return env


def run_tests(root, python):
    try:
        r = subprocess.run([python, "-m", "pytest", "-q", "-x"], cwd=root, capture_output=True, text=True, timeout=LIMIT)
    except subprocess.TimeoutExpired:
        raise Stop(f"тесты не уложились в {LIMIT} с, коммит отклонён.")
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-15:])
        raise Stop(f"тесты красные, коммит отклонён (проверено то, что уйдёт в коммит).\n{tail}")


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from shellwords import DYNAMIC, expand_aliases, git_calls

    event = json.load(sys.stdin)
    if event.get("tool_name") not in ("Bash", "Monitor"):
        return 0
    project = os.environ.get("CLAUDE_PROJECT_DIR") or "."
    # cwd идёт за агентом, в том числе в worktree; CLAUDE_PROJECT_DIR остаётся в основной копии
    cwd = event.get("cwd") or project
    # алиас (git ci → git commit) проверяется как то, во что он раскрыт
    calls = expand_aliases(list(git_calls(event.get("tool_input", {}).get("command", ""))), cwd)
    # подкоманда или репозиторий из подстановки: это может быть commit, и где — неизвестно
    dynamic = next((c for c in calls if c.dynamic and (not c.sub or DYNAMIC.search(c.sub) or
                                                       any(DYNAMIC.search(w) for w in c.where))), None)
    if dynamic:
        raise Stop(f"в git {dynamic.dynamic} берётся из переменной или подстановки — может оказаться commit "
                   "или другой репозиторий. Напиши подкоманду и путь явно.")
    commits = [i for i, c in enumerate(calls) if c.sub == "commit"]
    if not commits:
        return 0
    if len(commits) > 1:
        raise Stop("несколько git commit в одной команде — гейт проверяет по одному. Раздели команды.")
    k = commits[0]
    commit = calls[k]
    for c in calls[:k + 1]:
        if c.sub not in READONLY and (c.env.keys() & {"GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"}):
            raise Stop(f"git {c.sub} с GIT_INDEX_FILE/GIT_OBJECT_DIRECTORY работает с другим индексом — "
                       "гейт не может проверить, что уйдёт в коммит.")
        if c.dynamic and c.sub not in READONLY:
            raise Stop(f"в git {c.sub} слово {c.dynamic} берётся из переменной или подстановки — "
                       "гейт не может предсказать содержимое коммита.")
    top = git(cwd, commit.where, "rev-parse", "--show-toplevel").stdout.strip()
    venvs = [os.path.join(d, ".venv", "bin", "python") for d in (top or cwd, project)]
    python = next((v for v in venvs if os.path.exists(v)), sys.executable)
    if not top:  # не git-репозиторий: коммита не будет, проверяем папку, как раньше
        run_tests(cwd, python)
        return 0
    with tempfile.TemporaryDirectory(prefix="test-gate-") as tmp:
        env = predict(calls, k, cwd, tmp)
        snapshot = os.path.join(tmp, "commit")
        # из подпапки checkout-index пишет пути относительно неё (../calc.py) — мимо снимка; поэтому из корня
        replay(top, commit.where + ["-C", top], env, "checkout-index", "-a", "-f", f"--prefix={snapshot}{os.sep}")
        os.makedirs(snapshot, exist_ok=True)  # пустой индекс — пустой снимок
        run_tests(snapshot, python)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as e:
        print(f"test-gate: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # упавший гейт закрывает: exit 1 Claude Code считает «пропустить»
        print(f"test-gate: сбой гейта ({e!r}), коммит остановлен.", file=sys.stderr)
        sys.exit(2)
