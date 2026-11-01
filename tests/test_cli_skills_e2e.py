"""E2E tests for AgentSpec.skills — compile → sandbox → agent discovery."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect HOME to tmp_path so ~/.harness is isolated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HARNESS_HMAC_SECRET", "e2e-test-secret-min-32-bytes!!")
    return home


class TestSkillsE2E:
    """End-to-end: spec.skills → sandbox mount → agent discovery."""

    def test_spec_skills_survive_register_roundtrip(self, tmp_home, tmp_path):
        """AgentSpec.skills survives SDK register → load."""
        from harness.sdk import HarnessClient
        from harness.interfaces import AgentSpec

        client = HarnessClient(sandbox_configs={
            "main": {
                "backend": "bubblewrap",
                "firewall": {"network": "host"},
                "shared_folders": [],
            },
        })

        spec = AgentSpec(
            name="skilled-e2e",
            description="E2E skills test",
            tools=["bash", "read_file"],
            skills=["bash-expert", "test-agent-skill"],
        )
        client.agents.register(spec, name="skilled-e2e")

        loaded = client._spec_svc.load("skilled-e2e")
        assert loaded.skills == ["bash-expert", "test-agent-skill"]

    def test_spec_without_skills_defaults_empty(self, tmp_home):
        """AgentSpec without skills → empty list."""
        from harness.sdk import HarnessClient
        from harness.interfaces import AgentSpec

        client = HarnessClient(sandbox_configs={
            "main": {
                "backend": "bubblewrap",
                "firewall": {"network": "host"},
                "shared_folders": [],
            },
        })

        spec = AgentSpec(
            name="no-skills-e2e",
            description="No skills",
            tools=["bash"],
        )
        client.agents.register(spec, name="no-skills-e2e")

        loaded = client._spec_svc.load("no-skills-e2e")
        assert loaded.skills == []

    def test_cli_compile_preserves_skills(self, tmp_home, tmp_path):
        """CLI compile reads skills from YAML, SDK registers them."""
        from harness.sdk import HarnessClient

        spec_yaml = tmp_path / "skilled.yaml"
        spec_yaml.write_text(yaml.dump({
            "name": "cli-skilled",
            "description": "CLI skills test",
            "tools": ["bash"],
            "skills": ["bash-expert"],
        }))

        # Simulate CLI compile
        client = HarnessClient(sandbox_configs={
            "main": {
                "backend": "bubblewrap",
                "firewall": {"network": "host"},
                "shared_folders": [],
            },
        })

        with spec_yaml.open() as f:
            raw = yaml.safe_load(f)
        from harness.interfaces import AgentSpec
        spec = AgentSpec(
            name=raw["name"],
            description=raw.get("description", ""),
            tools=raw.get("tools", []),
            skills=raw.get("skills", []),
        )
        client.agents.register(spec, name=raw["name"])

        loaded = client._spec_svc.load("cli-skilled")
        assert loaded.skills == ["bash-expert"]
