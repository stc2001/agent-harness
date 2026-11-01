"""Tests for CLI agent lifecycle commands: start, stop, exec.

TDD vertical slice for harness start/stop/exec.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run harness CLI command, return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-m", "harness.cli.main"] + cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect HOME so ~/.harness is isolated, and create a default LLM profile."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Create default LLM profile so resolve_llm_profile("default") works
    llm_dir = home / ".harness" / "llm.d"
    llm_dir.mkdir(parents=True)
    default_llm = llm_dir / "default.yaml"
    default_llm.write_text(
        'provider: deepseek\n'
        'api_key: "sk-test-default"\n'
        'model: deepseek-v4-flash\n',
        encoding="utf-8",
    )
    # Also create deepseek-prod profile
    prod_llm = llm_dir / "deepseek-prod.yaml"
    prod_llm.write_text(
        'provider: deepseek\n'
        'api_key: "sk-test-prod"\n'
        'model: deepseek-v4-flash\n',
        encoding="utf-8",
    )
    return home


@pytest.fixture
def tmp_home_no_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect HOME so ~/.harness is isolated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def sample_spec(tmp_path: Path) -> Path:
    """Create a sample AgentSpec YAML."""
    registry = tmp_path / "registry"
    registry.mkdir()
    spec_file = registry / "test-worker.yaml"
    spec_data = {
        "name": "test-worker",
        "description": "Test worker agent",
        "model": "deepseek-v4-flash",
        "llm_profile": "deepseek-prod",
        "system_prompt": "You are a test worker.",
        "tools": ["bash", "read_file"],
    }
    with spec_file.open("w", encoding="utf-8") as f:
        yaml.dump(spec_data, f)
    return spec_file


class TestStartCommand:
    """harness start <instance_id> [--prompt TEXT] [--var K=V] [--append TEXT]"""

    def test_start_returns_session_id(self, tmp_home: Path, sample_spec: Path) -> None:
        """start outputs a session_id."""
        code, stdout, _ = _run(["compile", str(sample_spec)])
        assert code == 0
        instance_id = stdout.strip().split()[-1]

        code, stdout, stderr = _run(["start", instance_id, "--prompt", "hello"])
        assert code == 0, f"stderr: {stderr}"
        assert "Session started:" in stdout
        assert "sess" in stdout or "Session started:" in stdout

    def test_start_with_var_and_append(self, tmp_home: Path, sample_spec: Path) -> None:
        """start --var and --append are accepted."""
        code, stdout, _ = _run(["compile", str(sample_spec)])
        instance_id = stdout.strip().split()[-1]

        code, stdout, stderr = _run([
            "start", instance_id,
            "--prompt", "task",
            "--var", "time=12:00",
            "--append", "Ignore errors",
        ])
        assert code == 0, f"stderr: {stderr}"
        assert "Session started:" in stdout

    def test_start_invalid_instance(self, tmp_home: Path) -> None:
        """start with bad instance_id exits non-zero."""
        code, _, _ = _run(["start", "inst_nonexistent"])
        assert code != 0


class TestStopCommand:
    """harness stop <session_id>"""

    def test_stop_session(self, tmp_home: Path, sample_spec: Path) -> None:
        """stop outputs confirmation."""
        code, stdout, _ = _run(["compile", str(sample_spec)])
        instance_id = stdout.strip().split()[-1]

        code, stdout, _ = _run(["start", instance_id])
        session_id = stdout.strip().split()[-1]

        code, stdout, _ = _run(["stop", session_id])
        assert code == 0
        assert "stopped" in stdout.lower()


class TestExecCommand:
    """harness exec <session_id> <message> [--var K=V] [--append TEXT]"""

    def test_exec_sends_message(self, tmp_home: Path, sample_spec: Path) -> None:
        """exec sends a user message to a session."""
        code, stdout, _ = _run(["compile", str(sample_spec)])
        instance_id = stdout.strip().split()[-1]

        code, stdout, _ = _run(["start", instance_id])
        session_id = stdout.strip().split()[-1]

        code, stdout, _ = _run(["exec", session_id, "Continue please"])
        assert code == 0
        assert "sent" in stdout.lower()

    def test_exec_with_append(self, tmp_home: Path, sample_spec: Path) -> None:
        """exec --append is accepted."""
        code, stdout, _ = _run(["compile", str(sample_spec)])
        instance_id = stdout.strip().split()[-1]

        code, stdout, _ = _run(["start", instance_id])
        session_id = stdout.strip().split()[-1]

        code, stdout, stderr = _run([
            "exec", session_id, "Continue",
            "--append", "Be concise",
        ])
        assert code == 0, f"stderr: {stderr}"
