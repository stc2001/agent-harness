"""Unit tests for compute_skill_mounts — selective skill bind-mount construction."""

from __future__ import annotations

import pytest


class TestComputeSkillMounts:
    """compute_skill_mounts(skill_names, skills_dir) → list[dict]."""

    def test_basic_single_skill(self):
        """Single skill name → one mount entry."""
        from harness.skills import compute_skill_mounts

        mounts = compute_skill_mounts(["bash-expert"])
        assert mounts == [
            {"host_path": "skills/bash-expert", "sandbox_path": ".skills/bash-expert"},
        ]

    def test_multiple_skills(self):
        """Multiple skills → multiple mount entries."""
        from harness.skills import compute_skill_mounts

        mounts = compute_skill_mounts(["bash-expert", "rdkit"])
        assert len(mounts) == 2
        host_paths = {m["host_path"] for m in mounts}
        assert host_paths == {"skills/bash-expert", "skills/rdkit"}

    def test_empty_skills(self):
        """Empty skill list → empty mounts."""
        from harness.skills import compute_skill_mounts

        mounts = compute_skill_mounts([])
        assert mounts == []

    def test_deduplication(self):
        """Duplicate skill names → single mount entry."""
        from harness.skills import compute_skill_mounts

        mounts = compute_skill_mounts(["bash-expert", "bash-expert", "rdkit"])
        host_paths = [m["host_path"] for m in mounts]
        assert host_paths.count("skills/bash-expert") == 1
        assert len(mounts) == 2

    def test_custom_skills_dir(self):
        """Custom skills_dir is reflected in both host and sandbox paths."""
        from harness.skills import compute_skill_mounts

        mounts = compute_skill_mounts(["bash-expert"], skills_dir="my_skills")
        assert mounts[0]["host_path"] == "my_skills/bash-expert"
        assert mounts[0]["sandbox_path"] == ".skills/bash-expert"

    def test_union_from_multiple_specs(self):
        """Union of skill lists from multiple AgentSpecs → deduplicated mounts."""
        from harness.skills import compute_skill_mounts

        agent_a_skills = ["bash-expert", "rdkit"]
        agent_b_skills = ["rdkit", "git-expert"]
        union = list(set(agent_a_skills + agent_b_skills))

        mounts = compute_skill_mounts(union)
        host_paths = {m["host_path"] for m in mounts}
        assert host_paths == {
            "skills/bash-expert",
            "skills/rdkit",
            "skills/git-expert",
        }


class TestPromptSkillFiltering:
    """assemble_prompt(skills=...) respects AgentSpec.skills filtering."""

    def test_only_spec_skills_in_prompt(self):
        """Sandbox has [bash-expert, rdkit], spec.skills=[bash-expert] →
        prompt shows only bash-expert."""
        from harness.prompt_assembly import assemble_prompt
        from harness.skills import SkillMeta

        all_skills = [
            SkillMeta(name="bash-expert", description="Bash tips"),
            SkillMeta(name="rdkit", description="Cheminformatics"),
        ]
        spec_skill_names = ["bash-expert"]
        filtered = [s for s in all_skills if s.name in spec_skill_names]

        result = assemble_prompt(
            base_prompt="You are a test agent.",
            skills=filtered,
        )
        assert "bash-expert" in result
        assert "rdkit" not in result
        assert "Available skills" in result

    def test_spec_skills_empty_shows_none(self):
        """spec.skills=[] → no skills section in prompt."""
        from harness.prompt_assembly import assemble_prompt
        from harness.skills import SkillMeta

        all_skills = [SkillMeta(name="bash-expert", description="Bash")]
        filtered = [s for s in all_skills if s.name in []]

        result = assemble_prompt(
            base_prompt="You are a test agent.",
            skills=filtered,
        )
        assert "Available skills" not in result
