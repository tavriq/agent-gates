"""Тесты гейтов: гейт, который сам молча сломался, хуже отсутствующего."""
import json
import os
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
    code, err = run("test_gate.py", bash('git commit -m "x"', worktree / "sub"), env={"CLAUDE_PROJECT_DIR": str(main_copy)})
    assert code == 2 and "красные" in err


def test_gate_times_out_closed(tmp_path):
    (tmp_path / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(5)\n")
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path), "TEST_GATE_LIMIT": "1"}
    code, err = run("test_gate.py", bash('git commit -m "x"', tmp_path), env=env)
    assert code == 2 and "не уложились" in err
