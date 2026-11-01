"""Tests for Skills system — simplified: metadata pointer in system_prompt,
Agent reads full SKILL.md from sandbox via read_file.

Refactored 2026-05-19: SkillRegistry, list_skills, invoke_skill removed.
Skills now live as filesystem artifacts in the sandbox.
"""

from __future__ import annotations

import pytest


class TestNoBodyInjection:
    """Verify old injection mechanisms are removed."""

    @pytest.fixture
    def gateway(self):
        from harness.gateway.gateway import Gateway
        return Gateway(event_bus=None)

    @pytest.fixture
    def mock_sandbox(self):
        class M:
            def spawn(self, *a, **kw): return "pid"
            def kill(self, *a): pass
            def is_alive(self, *a): return True
            def get_logs(self, *a): return ""
            def execute(self, *a): return ("", "", 0)
        return M()

    def test_no_activated_skills_attr(self, temp_store_path, gateway,
                                        mock_sandbox):
        """_activated_skills set no longer exists on runtime."""
        from harness.agent_runtime.impl import AgentRuntime
        from harness.interfaces import AgentSpec
        from harness.session_store.impl import SessionStore

        store = SessionStore(base_path=temp_store_path)
        spec = AgentSpec(name="test", description="test", tools=[],
                         model="deepseek-v4-flash")
        runtime = AgentRuntime(
            spec=spec, gateway=gateway,
            store=store, api_key="sk-test", sandbox=mock_sandbox,
        )
        assert not hasattr(runtime, "_activated_skills")

    def test_no_skills_placement_field(self):
        """skills_placement is removed from AgentSpec."""
        from harness.interfaces import AgentSpec
        spec = AgentSpec(name="test", description="test", tools=[],
                         model="deepseek-v4-flash")
        assert not hasattr(spec, "skills_placement")

    def test_no_skill_registry_in_runtime(self, temp_store_path, gateway,
                                            mock_sandbox):
        """_skill_registry no longer exists on AgentRuntime."""
        from harness.agent_runtime.impl import AgentRuntime
        from harness.interfaces import AgentSpec
        from harness.session_store.impl import SessionStore

        store = SessionStore(base_path=temp_store_path)
        spec = AgentSpec(name="test", description="test", tools=[],
                         model="deepseek-v4-flash")
        runtime = AgentRuntime(
            spec=spec, gateway=gateway,
            store=store, api_key="sk-test", sandbox=mock_sandbox,
        )
        assert not hasattr(runtime, "_skill_registry")
