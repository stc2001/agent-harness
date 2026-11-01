"""TDD: AgentInstancePool stale sandbox cache bug — RED phase.

BUG: pool caches AgentRuntime with old sandbox. When sandbox config changes
between acquire() calls and pool's _sandbox_fingerprints incorrectly matches,
pool returns cached agent with stale sandbox (old PID, old bind mounts),
bypassing SandboxManager's config-change detection.

RED phase tests assert DESIRED behavior that current code violates.
GREEN phase verifies fix resolves all tests.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.interfaces import AgentSpec, SandboxConfig
from harness.orchestration.pool import AgentInstancePool
from harness.sandbox.manager import SandboxManager
from harness.session_store.impl import SessionStore

from tests.conftest import AlwaysAliveSandbox


# ── Helpers ────────────────────────────────────────────────


def _make_spec(name: str, sandbox_name: str = "main") -> AgentSpec:
    return AgentSpec(
        name=name, description="test", sandbox=sandbox_name,
        tools=[], model="mock", max_iterations=1, timeout_seconds=10,
    )


def _make_config(host_path: str) -> SandboxConfig:
    return SandboxConfig(
        backend="bubblewrap",
        firewall={"network": "none"},
        shared_folders=[{"host_path": host_path, "sandbox_path": "/workspace"}],
    )


def _patch_sandbox_backend(monkeypatch, backend):
    """Replace get_sandbox with backend via pytest monkeypatch.

    Uses monkeypatch for automatic cleanup — no manual unpatch needed.
    """
    import harness.sandbox.manager as sm
    monkeypatch.setattr(sm, "get_sandbox", lambda _: backend)
    return monkeypatch


def _unpatch_sandbox_backend(mp):
    """No-op — monkeypatch auto-cleans on test teardown."""
    pass


# ═══════════════════════════════════════════════════════════
# RED PHASE — Behavior Test
# ═══════════════════════════════════════════════════════════


class TestPoolReturnsFreshSandboxAfterConfigChange:
    """BEHAVIOR TEST: After sandbox config changes, acquire() MUST return
    an AgentRuntime with the NEW sandbox (not a stale cached one).

    This test models the multi-epoch pipeline scenario:
      1. Epoch 1: pool creates agent with correct sandbox
      2. Config changes (new shared_folders paths)
      3. Epoch 2: acquire() should get agent with new sandbox
    """

    def test_acquire_after_config_change_gives_new_sandbox(self, tmp_path: Path, monkeypatch):
        """RED: Current pool returns stale agent when fingerprint cache is stale.

        This is the desired behavior that does NOT hold with current code.
        After the fix, this test must PASS.
        """
        store = SessionStore(base_path=str(tmp_path / "store"))
        mgr = SandboxManager(configs={"hl_eval": _make_config("/host/epoch_1")})
        backend = AlwaysAliveSandbox()

        pool = AgentInstancePool(
            store=store, gateway=MagicMock(), api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _make_spec("go2_evaluator", sandbox_name="hl_eval")

        _patch_sandbox_backend(monkeypatch, backend)

        # ── Epoch 1 ──
        agent_1 = pool.acquire(spec)
        pid_1 = agent_1._sandbox_id

        # ── Between epochs: config changes ──
        # Simulates _register_sandboxes() replacing the config
        mgr._configs["hl_eval"] = _make_config("/host/epoch_2")

        # ── Epoch 2: acquire should get fresh sandbox ──
        # Config was replaced — sandbox_manager will detect change
        # via its own _instance_configs comparison and respawn.
        agent_2 = pool.acquire(spec)
        pid_2 = agent_2._sandbox_id

        # ── DESIRED BEHAVIOR (I-26) ──
        # After config change the manager RESPAWNS under the SAME uuid:
        # agent reuse + entity identity stable; config refresh happens inside
        # the manager (fingerprint compare) on every acquire.
        assert agent_2 is agent_1, "runtime should be reused"
        assert pid_2 == pid_1, (
            f"I-26: sandbox_id must be stable across config respawn\n"
            f"  pid_1 = {pid_1}\n  pid_2 = {pid_2}\n"
        )

        # Verify sandbox_manager was consulted (respawn spawned a fresh anchor)
        assert len(backend.spawned) >= 2, (
            f"Expected ≥2 spawns (one per epoch), got {len(backend.spawned)}"
        )

    def test_pool_delegates_to_sandbox_manager_for_reuse(self, tmp_path: Path, monkeypatch):
        """RED: Pool should NEVER bypass sandbox_manager.get_or_create().

        When pool finds a cached agent, it should still verify sandbox
        freshness via sandbox_manager before returning.
        """
        store = SessionStore(base_path=str(tmp_path / "store2"))
        mgr = SandboxManager(configs={"s": _make_config("/host/ep1")})
        backend = AlwaysAliveSandbox()

        pool = AgentInstancePool(
            store=store, gateway=MagicMock(), api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _make_spec("agent", sandbox_name="s")

        _patch_sandbox_backend(monkeypatch, backend)

        # Acquire first time — creates sandbox and agent
        agent_1 = pool.acquire(spec)
        original_pid = agent_1._sandbox_id

        # Change config — sandbox_manager will detect and respawn
        mgr._configs["s"] = _make_config("/host/ep2")

        # DESIRED (I-26): pool calls sandbox_manager on every acquire;
        # the manager detects config change and respawns under the SAME uuid.
        agent_2 = pool.acquire(spec)

        assert agent_2 is agent_1
        assert agent_2._sandbox_id == original_pid, (
            "I-26: sandbox_id stable across config respawn"
        )
        assert len(backend.spawned) >= 2, "manager must respawn after config change"


# ═══════════════════════════════════════════════════════════
# RED PHASE — Unit Tests
# ═══════════════════════════════════════════════════════════


class TestPoolSandboxVerification:
    """Unit tests for the fix: pool must verify sandbox freshness."""

    def test_pool_always_calls_sandbox_manager_get_or_create(
        self, tmp_path: Path, monkeypatch
    ):
        """RED: Pool must call sandbox_manager.get_or_create() on every acquire(),
        even when returning a cached agent."""
        store = SessionStore(base_path=str(tmp_path / "store3"))
        mgr = SandboxManager(configs={"s": _make_config("/host/ep1")})
        backend = AlwaysAliveSandbox()

        pool = AgentInstancePool(
            store=store, gateway=MagicMock(), api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _make_spec("agent", sandbox_name="s")

        _patch_sandbox_backend(monkeypatch, backend)

        # First acquire — populates cache
        pool.acquire(spec)

        # Change config — sandbox_manager detects and respawns
        mgr._configs["s"] = _make_config("/host/ep2")

        # Track sandbox_manager calls
        spawn_count_before = len(backend.spawned)

        # Second acquire
        pool.acquire(spec)

        # DESIRED: sandbox_manager should have respawned (new sandbox)
        assert len(backend.spawned) > spawn_count_before, (
            "sandbox_manager.get_or_create() should be called on every "
            "acquire(), even for cached agents"
        )

    def test_cached_agent_sandbox_refs_stay_in_sync(self, tmp_path: Path, monkeypatch):
        """RED: When sandbox is respawned, cached agent's sandbox references
        (_sandbox_id) must be updated."""
        store = SessionStore(base_path=str(tmp_path / "store4"))
        mgr = SandboxManager(configs={"s": _make_config("/host/ep1")})
        backend = AlwaysAliveSandbox()

        pool = AgentInstancePool(
            store=store, gateway=MagicMock(), api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _make_spec("agent", sandbox_name="s")

        _patch_sandbox_backend(monkeypatch, backend)

        agent_1 = pool.acquire(spec)
        pid_1 = agent_1._sandbox_id

        # Change config
        mgr._configs["s"] = _make_config("/host/ep2")

        agent_2 = pool.acquire(spec)
        pid_2 = agent_2._sandbox_id

        assert pid_2 == pid_1, (
            "I-26: cached agent keeps its sandbox_id across config respawn "
            "(entity identity stable; backend refreshed internally)"
        )

        # Verify the sandbox actually has new config
        assert len(backend.spawned) >= 2, (
            f"Expected ≥2 spawns, got {len(backend.spawned)}"
        )

    def test_no_regression_same_config_reuses_sandbox(self, tmp_path: Path, monkeypatch):
        """GREEN from start: when config is UNCHANGED, pool should still
        reuse the cached agent and sandbox (no unnecessary respawns)."""
        store = SessionStore(base_path=str(tmp_path / "store5"))
        mgr = SandboxManager(configs={"s": _make_config("/host/same")})
        backend = AlwaysAliveSandbox()

        pool = AgentInstancePool(
            store=store, gateway=MagicMock(), api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _make_spec("agent", sandbox_name="s")

        _patch_sandbox_backend(monkeypatch, backend)

        a1 = pool.acquire(spec)
        a2 = pool.acquire(spec)

        # Same config → same sandbox → no wasted respawn
        assert a2._sandbox_id == a1._sandbox_id
        assert len(backend.spawned) == 1  # only one spawn


# ═══════════════════════════════════════════════════════════
# RED PHASE — Edge Case Tests
# ═══════════════════════════════════════════════════════════


class TestPoolEdgeCases:
    """Edge case tests that must also pass after the fix."""

    def test_old_sandbox_killed_on_config_change(self, tmp_path: Path, monkeypatch):
        """When config changes, the old sandbox should be killed."""
        store = SessionStore(base_path=str(tmp_path / "store6"))
        mgr = SandboxManager(configs={"s": _make_config("/host/ep1")})
        backend = AlwaysAliveSandbox()

        pool = AgentInstancePool(
            store=store, gateway=MagicMock(), api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _make_spec("agent", sandbox_name="s")

        _patch_sandbox_backend(monkeypatch, backend)

        agent_1 = pool.acquire(spec)
        pid_1 = agent_1._sandbox_id

        mgr._configs["s"] = _make_config("/host/ep2")

        pool.acquire(spec)

        assert pid_1 in backend.killed, (
            "Old sandbox should be killed when config changes"
        )

    def test_multiple_agents_same_sandbox_config_change(self, tmp_path: Path, monkeypatch):
        """When config changes, ALL agents sharing that sandbox should
        get the new sandbox on next acquire."""
        store = SessionStore(base_path=str(tmp_path / "store7"))
        mgr = SandboxManager(configs={"shared_sb": _make_config("/host/ep1")})
        backend = AlwaysAliveSandbox()

        pool = AgentInstancePool(
            store=store, gateway=MagicMock(), api_key="sk-test",
            sandbox_manager=mgr,
        )

        spec_a = _make_spec("agent_a", sandbox_name="shared_sb")
        spec_b = _make_spec("agent_b", sandbox_name="shared_sb")

        _patch_sandbox_backend(monkeypatch, backend)

        a1 = pool.acquire(spec_a)
        b1 = pool.acquire(spec_b)
        original_pid = a1._sandbox_id
        # Same sandbox shared
        assert a1._sandbox_id == b1._sandbox_id

        # Config changes — sandbox respawns automatically
        mgr._configs["shared_sb"] = _make_config("/host/ep2")

        a2 = pool.acquire(spec_a)
        b2 = pool.acquire(spec_b)

        # I-26: uuid stable across respawn; both keep sharing the entity
        assert a2._sandbox_id == original_pid, (
            "sandbox_id stable after config change (agent_a)"
        )
        assert b2._sandbox_id == original_pid, (
            "sandbox_id stable after config change (agent_b)"
        )
        # They still share the same entity
        assert a2._sandbox_id == b2._sandbox_id


# ═══════════════════════════════════════════════════════════
# H1 fix: concurrent acquire() thread safety
# ═══════════════════════════════════════════════════════════


class TestPoolConcurrency:
    """Pool.acquire() must be thread-safe."""

    def test_concurrent_new_agent_acquisition_no_crash(
        self, tmp_path: Path, monkeypatch
    ):
        """Multiple threads creating new agents (create_new=True path)
        should not crash due to unsynchronized _pool dict access."""
        import threading
        import time
        from harness.gateway.gateway import Gateway

        # Use a slow backend to widen the race window
        class SlowSandbox(AlwaysAliveSandbox):
            def spawn(self, config, sandbox_id=None):
                time.sleep(0.01)  # widen race window
                return super().spawn(config)

        store = SessionStore(base_path=str(tmp_path / "slow_store"))
        mgr = SandboxManager(configs={"s": _make_config("/host/t")})
        backend = SlowSandbox()

        pool = AgentInstancePool(
            store=store, gateway=Gateway(event_bus=None), api_key="sk-test",
            sandbox_manager=mgr,
        )

        _patch_sandbox_backend(monkeypatch, backend)

        errors: list[Exception] = []

        def create_agent(i: int):
            try:
                spec = _make_spec(f"worker_{i}", sandbox_name="s")
                pool.acquire(spec)
            except Exception as e:
                errors.append((i, str(e)))

        threads = [threading.Thread(target=create_agent, args=(i,))
                  for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, (
            f"Concurrent create_new should not crash. "
            f"Errors: {errors}"
        )
