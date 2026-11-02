"""Тесты гейтов: гейт, который сам молча сломался, хуже отсутствующего."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".claude" / "hooks"


def run(hook, event, env=None):
    r = subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return r.returncode, r.stderr


def bash(cmd, cwd="."):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return tmp_path


# branch-guard

def test_commit_on_main_blocked(repo):
    code, err = run("branch_guard.py", bash('git commit -m "x"', repo))
    assert code == 2 and "main" in err


def test_commit_on_feature_branch_allowed(repo):
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "feat/x"], check=True)
    assert run("branch_guard.py", bash('git commit -m "x"', repo))[0] == 0


def test_push_to_main_from_branch_blocked(repo):
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "feat/x"], check=True)
    assert run("branch_guard.py", bash("git push origin main", repo))[0] == 2


def test_git_c_form_caught(repo):
    assert run("branch_guard.py", bash(f'git -C {repo} commit -m "x"', repo))[0] == 2


def test_other_commands_pass(repo):
    assert run("branch_guard.py", bash("git status", repo))[0] == 0


# secret-guard

@pytest.mark.parametrize("cmd", ["cat .env", "head -5 config/.env", "source .env.local", "cat deploy.pem"])
def test_secret_bash_blocked(cmd):
    assert run("secret_guard.py", bash(cmd))[0] == 2


@pytest.mark.parametrize("cmd", ["cat .env.example", "ls", "python3 -m pytest", "echo environment"])
def test_non_secret_bash_allowed(cmd):
    assert run("secret_guard.py", bash(cmd))[0] == 0


def test_secret_read_blocked():
    event = {"tool_name": "Read", "tool_input": {"file_path": "/repo/.env"}}
    assert run("secret_guard.py", event)[0] == 2


def test_example_read_allowed():
    event = {"tool_name": "Read", "tool_input": {"file_path": "/repo/.env.example"}}
    assert run("secret_guard.py", event)[0] == 0


def test_grep_for_text_env_allowed():
    event = {"tool_name": "Grep", "tool_input": {"pattern": ".env", "path": "app"}}
    assert run("secret_guard.py", event)[0] == 0


# test-gate

def test_gate_ignores_non_commit():
    assert run("test_gate.py", bash("git status"))[0] == 0


def test_gate_blocks_commit_on_red_tests(tmp_path):
    (tmp_path / "test_red.py").write_text("def test_red():\n    assert 1 == 2\n")
    code, err = run("test_gate.py", bash('git commit -m "x"', tmp_path), env={"CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert code == 2 and "красные" in err


def test_gate_allows_commit_on_green_tests(tmp_path):
    (tmp_path / "test_green.py").write_text("def test_green():\n    assert 1 == 1\n")
    code, _ = run("test_gate.py", bash('git commit -m "x"', tmp_path), env={"CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert code == 0


# red-team: обходы и ложные срабатывания, найденные после первой версии

@pytest.fixture
def feature(repo):
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "feat/x"], check=True)
    return repo


@pytest.mark.parametrize("cmd", [
    'git switch main && git commit -m "x"',
    'git checkout main; git commit -m "x"',
    'git switch main\ngit commit -m "x"',
    'sh -c "git switch main && git commit -m x"',
    'git checkout -b main && git commit -m "x"',
    'git commit -m "a" && git switch main && git commit -m "b"',
])
def test_switch_to_main_and_commit_in_one_command_blocked(feature, cmd):
    assert run("branch_guard.py", bash(cmd, feature))[0] == 2


@pytest.mark.parametrize("cmd", [
    'git switch -c feat/s9 && git add -A && git commit -m "S9"',
    'git checkout -b feat/s9 && git commit -m "S9"',
])
def test_new_branch_and_commit_from_main_allowed(repo, cmd):
    assert run("branch_guard.py", bash(cmd, repo))[0] == 0


@pytest.mark.parametrize("cmd", ["git merge feat/x", "git cherry-pick abc123", "git revert HEAD"])
def test_merge_into_main_blocked(repo, cmd):
    assert run("branch_guard.py", bash(cmd, repo))[0] == 2


@pytest.mark.parametrize("cmd", ["git merge-base main HEAD", "git log --merges", 'git commit -m "fix: switch rate"'])
def test_lookalikes_on_feature_allowed(feature, cmd):
    assert run("branch_guard.py", bash(cmd, feature))[0] == 0


def test_merge_base_on_main_allowed(repo):
    assert run("branch_guard.py", bash("git merge-base main HEAD", repo))[0] == 0


@pytest.mark.parametrize("cmd", [
    "git push --all origin",
    "git push --mirror origin",
    "git push origin 'refs/heads/*'",
    "git push origin HEAD:main",
    "git push origin +main",
    "git push origin HEAD:refs/heads/main",
    "git push origin feat/x && git push origin main",
])
def test_push_reaching_main_blocked(feature, cmd):
    assert run("branch_guard.py", bash(cmd, feature))[0] == 2


@pytest.mark.parametrize("cmd", ["git push -u origin feat/main-fix", "git push -u origin feat/x", "git log main && git push"])
def test_push_of_feature_allowed(feature, cmd):
    assert run("branch_guard.py", bash(cmd, feature))[0] == 0


@pytest.mark.parametrize("hook", ["branch_guard.py", "secret_guard.py", "test_gate.py"])
def test_broken_input_fails_closed(hook):
    r = subprocess.run([sys.executable, str(HOOKS / hook)], input="not json", capture_output=True, text=True)
    assert r.returncode == 2 and "сбой гейта" in r.stderr


@pytest.mark.parametrize("cmd", ["cat .ENV", "cat .envrc", "cat ~/.ssh/id_rsa", "cat id_ed25519"])
def test_secret_variants_bash_blocked(cmd):
    assert run("secret_guard.py", bash(cmd))[0] == 2


@pytest.mark.parametrize("path", ["/repo/.ENV", "/repo/.env.production", "/home/u/.ssh/id_rsa"])
def test_secret_variants_read_blocked(path):
    assert run("secret_guard.py", {"tool_name": "Read", "tool_input": {"file_path": path}})[0] == 2


def test_public_key_allowed():
    assert run("secret_guard.py", {"tool_name": "Read", "tool_input": {"file_path": "/home/u/.ssh/id_rsa.pub"}})[0] == 0


def test_grep_glob_on_env_blocked():
    event = {"tool_name": "Grep", "tool_input": {"pattern": "TOKEN", "path": ".", "glob": ".env*"}}
    assert run("secret_guard.py", event)[0] == 2


def test_monitor_tool_gated(repo):
    monitor = {"tool_name": "Monitor", "tool_input": {"command": "cat .env"}, "cwd": str(repo)}
    assert run("secret_guard.py", monitor)[0] == 2
    monitor["tool_input"]["command"] = 'git commit -m "x"'
    assert run("branch_guard.py", monitor)[0] == 2


def test_gate_runs_tests_where_agent_works(tmp_path):
    main_copy, worktree = tmp_path / "main", tmp_path / "wt"
    main_copy.mkdir()
    (main_copy / "test_green.py").write_text("def test_green():\n    assert 1 == 1\n")
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / "sub").mkdir()
    (worktree / "test_red.py").write_text("def test_red():\n    assert 1 == 2\n")
    # красный тест — в индексе: test-gate проверяет то, что уйдёт в коммит (G4)
    subprocess.run(["git", "-C", str(worktree), "add", "test_red.py"], check=True)
    code, err = run("test_gate.py", bash('git commit -m "x"', worktree / "sub"), env={"CLAUDE_PROJECT_DIR": str(main_copy)})
    assert code == 2 and "красные" in err and "1 failed" in err


def test_gate_times_out_closed(tmp_path):
    (tmp_path / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(5)\n")
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path), "TEST_GATE_LIMIT": "1"}
    code, err = run("test_gate.py", bash('git commit -m "x"', tmp_path), env=env)
    assert code == 2 and "не уложились" in err


# Сценарии гейтов G1–G4 из SPEC.md: дыры, записанные в README, и их соседи

ROOT = HOOKS.parent.parent


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True,
    )


def commit_all(repo, msg):
    git(repo, "add", "-A")
    assert git(repo, "commit", "-q", "-m", msg).returncode == 0


@pytest.fixture
def history(repo):
    """main с двумя коммитами, feat/x — на том же коммите."""
    for n in (1, 2):
        (repo / "f.txt").write_text(f"{n}\n")
        commit_all(repo, f"c{n}")
    git(repo, "branch", "feat/x")
    return repo


@pytest.fixture
def on_feature(history):
    git(history, "switch", "-q", "feat/x")
    return history


@pytest.fixture
def rebasing_main(history):
    """rebase main встал на конфликте: HEAD отцеплен, а переписывается main."""
    git(history, "switch", "-q", "feat/x")
    (history / "f.txt").write_text("x\n")
    commit_all(history, "x")
    git(history, "switch", "-q", "main")
    (history / "f.txt").write_text("m\n")
    commit_all(history, "m")
    assert git(history, "rebase", "feat/x").returncode != 0
    return history


# G1. branch-guard: история main — только через PR

@pytest.mark.parametrize("cmd", [
    "git pull", "git pull origin main", "git pull --rebase", "git pull -r origin main",
    "git pull --ff-only origin feat/x",
    "git rebase feat/x", "git rebase -i HEAD~1", "git rebase --onto feat/x HEAD~1",
    "git reset --hard", "git reset --hard HEAD~1", "git reset --hard origin/main",
    "git reset --soft HEAD~1", "git reset HEAD~1", "git reset --keep HEAD~1",
    "git am fix.patch", "git filter-branch --tree-filter true",
])
def test_g1_rewriting_main_on_main_blocked(history, cmd):
    assert run("branch_guard.py", bash(cmd, history))[0] == 2


@pytest.mark.parametrize("cmd", [
    "git rebase feat/x main", "git rebase --root main", "git rebase --onto feat/x HEAD~1 main",
    "git switch -C main", "git checkout -B main feat/x", "git worktree add -B main ../wt",
    "git branch -f main feat/x", "git branch -M feat/x main", "git branch -C feat/x main", "git branch -m main old",
    "git update-ref refs/heads/main HEAD", "git update-ref --stdin",
    "git fetch . feat/x:main", "git fetch origin +feat/x:refs/heads/main",
    "git filter-branch --tree-filter true -- --all",
    "git switch main && git reset --hard feat/x", "git checkout main && git pull",
])
def test_g1_moving_main_from_feature_blocked(on_feature, cmd):
    assert run("branch_guard.py", bash(cmd, on_feature))[0] == 2


@pytest.mark.parametrize("cmd", ["git rebase --continue", "git rebase --skip", "git commit --amend --no-edit"])
def test_g1_finishing_rebase_of_main_blocked(rebasing_main, cmd):
    assert run("branch_guard.py", bash(cmd, rebasing_main))[0] == 2


def test_g1_rebase_abort_on_main_allowed(rebasing_main):
    assert run("branch_guard.py", bash("git rebase --abort", rebasing_main))[0] == 0


@pytest.mark.parametrize("cmd", ["git commit --amend -m x", "git merge feat/x", "git merge --ff-only feat/x"])
def test_g1_amend_and_merge_on_main_blocked(history, cmd):
    assert run("branch_guard.py", bash(cmd, history))[0] == 2


@pytest.mark.parametrize("cmd", [
    "git pull --ff-only", "git pull --ff-only origin", "git pull --ff-only origin main",
    "git reset", "git reset f.txt", "git reset -- f.txt", "git reset HEAD f.txt", "git reset --soft HEAD",
    "git rebase main feat/x", "git merge --abort", "git cherry-pick --abort", "git status && git log -1",
])
def test_g1_sync_and_unstage_on_main_allowed(history, cmd):
    assert run("branch_guard.py", bash(cmd, history))[0] == 0


@pytest.mark.parametrize("cmd", [
    "git rebase main", "git rebase -i HEAD~1", "git reset --hard HEAD~1", "git pull", "git pull origin main",
    "git merge main", "git branch -f feat/y main", "git checkout -B feat/y main", "git fetch origin main:main",
    "git switch main && git pull --ff-only", "git commit --amend --no-edit", "git -C ../not-yet log",
])
def test_g1_work_on_feature_allowed(on_feature, cmd):
    assert run("branch_guard.py", bash(cmd, on_feature))[0] == 0


# G2. Кавычки и экранирование снимаются; git из переменной — стоп

@pytest.mark.parametrize("cmd", [
    "cat .en''v", "cat .e\\nv", 'cat ".en"v', "cat $'\\x2eenv'", "cat $'\\056env'", "head .e''nv.local",
])
def test_g2_quoted_secret_blocked(cmd):
    assert run("secret_guard.py", bash(cmd))[0] == 2


@pytest.mark.parametrize("cmd", ["git co''mmit -m x", 'git "com"mit -m x', "git com\\mit -m x"])
def test_g2_quoted_subcommand_on_main_blocked(repo, cmd):
    assert run("branch_guard.py", bash(cmd, repo))[0] == 2


@pytest.mark.parametrize("cmd", [
    "c=commit; git $c -m x", "git ${c} -m x", "git $(printf com)mit -m x", "git `printf com`mit -m x",
])
def test_g2_subcommand_from_variable_blocked(repo, cmd):
    code, err = run("branch_guard.py", bash(cmd, repo))
    assert code == 2 and "из переменной или подстановки" in err


@pytest.mark.parametrize("cmd", [
    'git -C "$d" commit -m x', "git --git-dir=$g commit -m x", "b=main; git push origin $b", "git push origin HEAD:$b",
])
def test_g2_target_from_variable_blocked(on_feature, cmd):
    assert run("branch_guard.py", bash(cmd, on_feature))[0] == 2


HEREDOC_MESSAGE = "git commit -m \"$(cat <<'EOF'\nfix: git push origin main; don't touch $HOME\nEOF\n)\""


@pytest.mark.parametrize("cmd", [HEREDOC_MESSAGE, 'git commit -m "fix: $x, git push origin main"', "git log --oneline -3"])
def test_g2_commit_message_is_text(on_feature, cmd):
    assert run("branch_guard.py", bash(cmd, on_feature))[0] == 0


@pytest.mark.parametrize("cmd", ["git co''mmit -m x", "c=commit; git $c -m x"])
def test_g2_test_gate_sees_through_quotes_and_variables(tmp_path, cmd):
    (tmp_path / "test_red.py").write_text("def test_red():\n    assert 1 == 2\n")
    assert run("test_gate.py", bash(cmd, tmp_path), env={"CLAUDE_PROJECT_DIR": str(tmp_path)})[0] == 2


# G3. Хук из settings.json падает закрытым

def hook_commands():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    return [h["command"] for group in settings["hooks"]["PreToolUse"] for h in group["hooks"]]


HOOK_IDS = [re.search(r"branch_guard|secret_guard|test_gate", c).group(0) for c in hook_commands()]


def run_hook_command(command, path):
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps(bash("git status", ROOT)),
        capture_output=True,
        text=True,
        env={"PATH": str(path), "CLAUDE_PROJECT_DIR": str(ROOT)},
    )


@pytest.mark.parametrize("command", hook_commands(), ids=HOOK_IDS)
def test_g3_no_python_fails_closed(command, tmp_path):
    r = run_hook_command(command, tmp_path)
    assert r.returncode == 2 and "python3 не найден" in r.stderr


@pytest.mark.parametrize("command", hook_commands(), ids=HOOK_IDS)
def test_g3_interpreter_crash_fails_closed(command, tmp_path):
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    assert run_hook_command(command, tmp_path).returncode == 2


@pytest.mark.parametrize("command", hook_commands(), ids=HOOK_IDS)
def test_g3_hook_command_runs_gate(command):
    r = run_hook_command(command, os.environ["PATH"])
    assert r.returncode == 0, r.stderr


# G4. test-gate гоняет тесты по тому, что уйдёт в коммит

CALC = "def add(a, b):\n    return a + b\n"
BROKEN = "def add(a, b):\n    return a - b\n"
TEST_CALC = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


@pytest.fixture
def project(tmp_path):
    """git-репозиторий: в HEAD код и зелёный тест на него."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "calc.py").write_text(CALC)
    (tmp_path / "test_calc.py").write_text(TEST_CALC)
    commit_all(tmp_path, "init")
    return tmp_path


def gate(cmd, cwd):
    return run("test_gate.py", bash(cmd, cwd), env={"CLAUDE_PROJECT_DIR": str(cwd)})


def test_g4_red_in_index_blocked_while_worktree_green(project):
    (project / "calc.py").write_text(BROKEN)
    git(project, "add", "calc.py")
    (project / "calc.py").write_text(CALC)
    code, err = gate('git commit -m "x"', project)
    assert code == 2 and "1 failed" in err


def test_g4_unstaged_breakage_does_not_block(project):
    (project / "notes.txt").write_text("x\n")
    git(project, "add", "notes.txt")
    (project / "calc.py").write_text(BROKEN)
    assert gate('git commit -m "x"', project)[0] == 0


@pytest.mark.parametrize("cmd", ['git add -A && git commit -m "x"', 'git add calc.py; git commit -m "x"', 'git commit -am "x"'])
def test_g4_changes_staged_by_the_command_tested(project, cmd):
    (project / "calc.py").write_text(BROKEN)
    code, err = gate(cmd, project)
    assert code == 2 and "1 failed" in err


def test_g4_untracked_dependency_blocked(project):
    (project / "extra.py").write_text("K = 4\n")
    (project / "test_calc.py").write_text("from extra import K\n\n\ndef test_k():\n    assert K == 4\n")
    code, err = gate('git commit -am "x"', project)
    assert code == 2 and "No module named 'extra'" in err


def test_g4_commit_of_paths_ignores_other_staged(project):
    (project / "test_calc.py").write_text(TEST_CALC + "\n\ndef test_red():\n    assert False\n")
    git(project, "add", "test_calc.py")
    (project / "calc.py").write_text(CALC + "# ok\n")
    assert gate('git commit -m "x" calc.py', project)[0] == 0


@pytest.mark.parametrize("cmd", [
    'git stash && git commit -m "x"', 'git mv calc.py c.py && git commit -m "x"', "git commit -m a && git commit -m b",
])
def test_g4_unpredictable_commit_blocked(project, cmd):
    assert gate(cmd, project)[0] == 2


def test_g4_green_commit_allowed_and_index_untouched(project):
    (project / "calc.py").write_text(CALC + "# ok\n")
    assert gate('git add -A && git commit -m "x"', project)[0] == 0
    assert git(project, "diff", "--cached", "--quiet").returncode == 0
