"""Unit tests for AgentInstancePool (orchestration/pool.py).

Tests acquire/release semantics, instance reuse, and cleanup with a mock
SandboxManager.  Integration tests that exercise the real bwrap path live
in test_integration.py.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.orchestration.pool import AgentInstancePool
from harness.session_store.impl import SessionStore


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_sandbox_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-instance-001", backend=MagicMock())
    return mgr


@pytest.fixture
def gateway() -> Gateway:
    return Gateway(event_bus=None)


@pytest.fixture
def store(temp_store_path: str) -> SessionStore:
    return SessionStore(base_path=temp_store_path)


@pytest.fixture
def pool(
    store: SessionStore,
    gateway: Gateway,
    mock_sandbox_manager: MagicMock,
) -> AgentInstancePool:
    return AgentInstancePool(
        store=store,
        gateway=gateway,
        api_key="sk-test",
        sandbox_manager=mock_sandbox_manager,
    )


def _spec(name: str = "test-agent", tools: list[str] | None = None) -> AgentSpec:
    return AgentSpec(
        name=name,
        description=f"Test agent {name}",
        tools=tools or ["echo"],
        model="deepseek-v4-flash",
        sandbox="main",
        max_iterations=1,
        permissions={"subagent_spawn": False},
    )


# ─────────────────────────────────────────────────────────────
# Acquire / reuse / create_new
# ─────────────────────────────────────────────────────────────


class TestAcquire:
    def test_acquire_returns_agent_runtime(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """acquire returns an AgentRuntime with the correct spec and API key."""
        spec = _spec("worker", ["echo", "bash"])
        agent = pool.acquire(spec)

        assert isinstance(agent, AgentRuntime)
        assert agent.spec.name == "worker"
        assert agent.api_key == "sk-test"
        assert agent._sandbox_id == "mock-instance-001"
        mock_sandbox_manager.get_or_create.assert_called_once_with("main")

    def test_acquire_reuses_existing_agent(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """Second acquire without create_new returns the same instance."""
        spec = _spec("worker")
        agent1 = pool.acquire(spec)
        agent2 = pool.acquire(spec)

        assert agent1 is agent2
        # SandboxManager is called on every acquire (no longer cached internally)

    def test_acquire_create_new_forces_new_instance(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """create_new=True always creates a new instance."""
        spec = _spec("worker")
        agent1 = pool.acquire(spec)
        agent2 = pool.acquire(spec, create_new=True)

        assert agent1 is not agent2
        assert mock_sandbox_manager.get_or_create.call_count == 2

    def test_acquire_different_specs_creates_separate_instances(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """Different agent names create separate instances."""
        worker = pool.acquire(_spec("worker"))
        reviewer = pool.acquire(_spec("reviewer"))

        assert worker is not reviewer
        assert pool.active_count == 2

    def test_acquire_passes_api_key_override(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """api_key override is used for the agent instance."""
        spec = _spec("worker")
        agent = pool.acquire(spec, api_key="sk-custom")
        assert agent.api_key == "sk-custom"

    def test_acquire_passes_base_url_override(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """base_url override is used for the agent instance."""
        spec = _spec("worker")
        agent = pool.acquire(spec, base_url="https://custom.api.com")
        assert agent.base_url == "https://custom.api.com"

    def test_acquire_injects_spawner(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """spawner is injected into the agent after creation."""
        spec = _spec("worker", ["spawn_subagent"])
        spawner_called = []

        def fake_spawner(agent_name: str, task: str) -> str:
            spawner_called.append((agent_name, task))
            return "child-001"

        agent = pool.acquire(spec, spawner=fake_spawner)
        # Verify spawner was set by calling it
        result = agent._spawner("child-agent", "do work")
        assert spawner_called == [("child-agent", "do work")]
        assert "child-001" in result

    def test_acquire_recreates_agent_on_sandbox_config_change(
        self, store: SessionStore, gateway: Gateway
    ) -> None:
        """Config change triggers sandbox respawn via real SandboxManager.

        Pool no longer caches fingerprints; delegates to sandbox_manager
        which detects config change and respawns."""
        from harness.interfaces import SandboxConfig
        from harness.sandbox.manager import SandboxManager
        from tests.conftest import AlwaysAliveSandbox

        def _cfg(path): return SandboxConfig(
            backend="bubblewrap", firewall={"network": "none"},
            shared_folders=[{"host_path": path, "sandbox_path": "/ws"}],
        )

        backend = AlwaysAliveSandbox()
        mgr = SandboxManager(configs={"main": _cfg("/host/e1")})
        pool = AgentInstancePool(
            store=store, gateway=gateway, api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _spec("worker")

        import harness.sandbox.manager as sm
        _orig = sm.get_sandbox
        sm.get_sandbox = lambda _: backend
        try:
            agent1 = pool.acquire(spec)
            sandbox_id = agent1._sandbox_id

            agent2 = pool.acquire(spec)
            assert agent1 is agent2

            # I-26: config change → manager respawns UNDER THE SAME uuid;
            # entity identity & agent binding are stable (respawn is internal).
            mgr._configs["main"] = _cfg("/host/e2")
            agent3 = pool.acquire(spec)
            assert agent3 is agent1
            assert agent3._sandbox_id == sandbox_id  # uuid 稳定
            assert mgr.get_entity(sandbox_id).sandbox_id == sandbox_id
            # pool 同步了最新配置引用（复用路径刷新 _sandbox_config）
            assert agent3._sandbox_config.shared_folders[0]["host_path"] == "/host/e2"
        finally:
            sm.get_sandbox = _orig


# ─────────────────────────────────────────────────────────────
# Release / destroy / queries
# ─────────────────────────────────────────────────────────────


class TestReleaseDestroy:
    def test_release_removes_agent_from_pool(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """release removes the agent; next acquire creates a new one."""
        spec = _spec("worker")
        agent1 = pool.acquire(spec)
        assert pool.active_count == 1

        pool.release(agent1)
        assert pool.active_count == 0

        agent2 = pool.acquire(spec)
        assert agent1 is not agent2

    def test_destroy_removes_all_of_same_name(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """destroy(name) removes all instances of that name."""
        pool.acquire(_spec("worker"))
        pool.acquire(_spec("reviewer"))
        pool.acquire(_spec("worker"), create_new=True)
        assert pool.active_count == 3

        pool.destroy("worker")
        assert pool.active_count == 1
        assert pool.list_active() == ["reviewer"]

    def test_destroy_all_clears_pool(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """destroy_all removes every instance."""
        pool.acquire(_spec("a"))
        pool.acquire(_spec("b"))
        pool.acquire(_spec("c"))

        pool.destroy_all()
        assert pool.active_count == 0
        assert pool.list_active() == []


# ─────────────────────────────────────────────────────────────
# Query methods
# ─────────────────────────────────────────────────────────────


class TestQueries:
    def test_list_instances_returns_all_for_name(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """list_instances returns all instances of a given agent name."""
        pool.acquire(_spec("worker"))
        pool.acquire(_spec("worker"), create_new=True)
        pool.acquire(_spec("reviewer"))

        workers = pool.list_instances("worker")
        assert len(workers) == 2
        assert all(a.spec.name == "worker" for a in workers)

    def test_list_active_returns_sorted_names(
        self, pool: AgentInstancePool, mock_sandbox_manager: MagicMock
    ) -> None:
        """list_active returns sorted unique agent names."""
        pool.acquire(_spec("zulu"))
        pool.acquire(_spec("alpha"))
        assert pool.list_active() == ["alpha", "zulu"]

class TestAgentLifecycle:
    """I-10 (ADR-043): COMPILED → IDLE ⇄ RUNNING → STOPPED."""

    def test_execute_task_then_idle(self, pool, store) -> None:
        """After a task, the runtime transitions to idle (not destroyed)."""
        agent = pool.acquire(_spec())
        assert agent.status == "compiled"

        def mock_llm(messages, tools=None):
            from tests.test_agent_runtime import _FakeMessage
            return _FakeMessage(content="DONE"), "stop"
        agent._call_llm = mock_llm
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        agent._current_session_id = sid

        result = agent.execute_task("do it", sid)
        assert result["status"] == "completed"
        assert agent.status == "idle", f"expected idle, got {agent.status}"

    def test_stop_marks_stopped(self, pool) -> None:
        """Explicit stop() moves the runtime to STOPPED."""
        agent = pool.acquire(_spec())
        agent.stop()
        assert agent.status == "stopped"

    def test_idle_timeout_instance_not_reused(self, pool) -> None:
        """Acquire must not reuse an idle instance past its timeout."""
        import time as _time

        agent1 = pool.acquire(_spec())
        agent1._status = "idle"
        agent1._last_idle_at = _time.time() - 9999  # long ago

        agent2 = pool.acquire(_spec())
        assert agent2 is not agent1, "expired idle instance must not be reused"

    def test_status_query_after_stop(self, pool) -> None:
        """Pool exposes per-instance status lookup."""
        agent = pool.acquire(_spec())
        iid = agent.instance_id
        assert pool.get_status(iid) == "compiled"
        agent.stop()
        assert pool.get_status(iid) == "stopped"


class TestLlmProfileResolution:
    """I-23 migration: per-instance LLM resolution (relocated from
    test_sdk_client.TestLLMProfileResolution — pool behavior belongs here)."""

    def test_acquire_resolves_per_profile(self, tmp_path, store, gateway,
                                          mock_sandbox_manager) -> None:
        """Acquire resolves api_key/base_url per spec.llm_profile."""
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "llm.yaml").write_text(
            'deepseek-prod:\n'
            '  provider: deepseek\n'
            '  api_key: "sk-deepseek-xxx"\n'
            'openai-gpt4o:\n'
            '  provider: openai\n'
            '  api_key: "sk-openai-yyy"\n'
            '  base_url: "https://api.openai.com/v1"\n',
            encoding="utf-8",
        )
        from harness.sdk.config import resolve_llm_profile

        pool = AgentInstancePool(
            store=store, gateway=gateway, api_key="",
            sandbox_manager=mock_sandbox_manager,
            resolve_llm=resolve_llm_profile, project_dir=tmp_path)

        spec_a = AgentSpec(
            name="worker-a", description="A", tools=["echo"],
            model="gpt-4o", sandbox="main", max_iterations=1,
            llm_profile="deepseek-prod",
            permissions={"subagent_spawn": False})
        spec_b = AgentSpec(
            name="worker-b", description="B", tools=["echo"],
            model="gpt-4o", sandbox="main", max_iterations=1,
            llm_profile="openai-gpt4o",
            permissions={"subagent_spawn": False})

        ra = pool.acquire(spec_a)
        rb = pool.acquire(spec_b)

        assert ra.api_key == "sk-deepseek-xxx"
        assert rb.api_key == "sk-openai-yyy"
        assert ra.base_url == "https://api.deepseek.com/v1"
        assert rb.base_url == "https://api.openai.com/v1"
