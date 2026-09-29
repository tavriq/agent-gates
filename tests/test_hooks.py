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
