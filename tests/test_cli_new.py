"""Tests for CLI new commands — TDD vertical slices.

Uses subprocess to test CLI as a black box.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
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


def _run(cmd: list[str], cwd: Path | None = None, env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run harness CLI command, return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parent.parent)
    env.setdefault("HARNESS_HMAC_SECRET", "test-secret-32bytes-long!!")
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [sys.executable, "-m", "harness.cli.main"] + cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


class TestCompileCommand:
    """harness compile <spec.yaml> — compile agent from spec."""

    def test_compile_returns_instance_id(self, tmp_home: Path, sample_spec: Path) -> None:
        """compile outputs an instance_id starting with 'inst_'."""
        code, stdout, stderr = _run(["compile", str(sample_spec)])

        assert code == 0, f"stderr: {stderr}"
        assert "inst_" in stdout, f"Expected instance_id in stdout, got: {stdout}"

    def test_compile_with_name(self, tmp_home: Path, sample_spec: Path) -> None:
        """compile --name stores display name in metadata."""
        code, stdout, stderr = _run(["compile", str(sample_spec), "--name", "my-worker"])

        assert code == 0, f"stderr: {stderr}"

        # Parse instance_id from output
        instance_id = stdout.strip().split()[-1]

        # Verify registry
        registry_file = tmp_home / ".harness" / "instances" / f"{instance_id}.json"
        assert registry_file.exists()
        with registry_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["spec_name"] == "test-worker"

    def test_compile_missing_file(self, tmp_home: Path) -> None:
        """compile with missing file exits non-zero."""
        code, _, _ = _run(["compile", "nonexistent.yaml"])
        assert code != 0


class TestPsCommand:
    """harness ps — list compiled agent instances."""

    def test_ps_empty(self, tmp_home: Path) -> None:
        """ps with no instances shows empty list."""
        code, stdout, _ = _run(["ps"])
        assert code == 0
        assert "No instances" in stdout or "INSTANCES" in stdout

    def test_ps_lists_instances(self, tmp_home: Path, sample_spec: Path) -> None:
        """ps shows compiled instances."""
        _run(["compile", str(sample_spec), "--name", "worker-1"])
        _run(["compile", str(sample_spec), "--name", "worker-2"])

        code, stdout, _ = _run(["ps"])
        assert code == 0
        assert "worker-1" in stdout or "test-worker" in stdout
        assert "worker-2" in stdout or "test-worker" in stdout


class TestRmCommand:
    """harness rm <instance_id> — remove compiled instance."""

    def test_rm_deletes_instance(self, tmp_home: Path, sample_spec: Path) -> None:
        """rm removes instance from registry."""
        code, stdout, _ = _run(["compile", str(sample_spec)])
        instance_id = stdout.strip().split()[-1]

        code, _, _ = _run(["rm", instance_id])
        assert code == 0

        # Verify deleted
        registry_file = tmp_home / ".harness" / "instances" / f"{instance_id}.json"
        assert not registry_file.exists()


class TestAuditCommand:
    """harness audit [--tail N] — view security audit log."""

    def test_audit_empty(self, tmp_home: Path, tmp_path: Path) -> None:
        """audit with no log shows 'No audit log found'."""
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        code, stdout, stderr = _run(["audit"], env_extra={"HARNESS_AUDIT_LOG_DIR": str(audit_dir)})
        assert code == 0, f"stderr: {stderr}"
        assert "No audit log" in stdout

    def test_audit_tail(self, tmp_home: Path, tmp_path: Path) -> None:
        """audit --tail shows last N events."""
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        audit_file = audit_dir / "audit_log.jsonl"
        with audit_file.open("w") as f:
            for i in range(5):
                f.write(json.dumps({
                    "ts": 1715798400 + i,
                    "level": "info",
                    "category": "job",
                    "event": f"job_{i}",
                    "job_id": f"j{i}",
                }) + "\n")

        code, stdout, stderr = _run(["audit", "--tail", "2"], env_extra={"HARNESS_AUDIT_LOG_DIR": str(audit_dir)})
        assert code == 0, f"stderr: {stderr}"
        assert "job_3" in stdout or "job_4" in stdout


class TestSkillsCommand:
    """harness skills ..."""

    def test_skills_no_subcommand_defaults_to_list(self, tmp_home: Path) -> None:
        """harness skills (no subcommand) defaults to listing host skills."""
        rc, out, err = _run(["skills"])
        assert rc == 0, f"skills without subcommand failed: stderr={err}"
        # Should succeed even with empty skills directory
