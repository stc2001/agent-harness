"""Tests for Gateway authorization hooks (ADR-042 migration).

PreHook/PostHook were migrated to the unified hooks registry:
- authorize.veto   (was PreHook)
- authorize.observe (was PostHook)
"""

from __future__ import annotations

import threading

import pytest

from harness.gateway.gateway import (
    Gateway,
    GateContext,
    AuthorizeResult,
)
from harness.gateway.hooks import install_sandbox_mutex
from harness.hooks import HookMode, HookRegistry, points
from harness.interfaces import AgentSpec


@pytest.fixture
def agent_spec():
    return AgentSpec(
        name="test-agent",
        description="test",
        model="deepseek-v4-flash",
        tools=["bash"],
    )


@pytest.fixture
def gateway():
    return Gateway(event_bus=None)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    HookRegistry.get().clear()


class TestAuthorizeVeto:
    """authorize.veto hooks (was PreHook)."""

    def test_veto_blocks_authorization(self, gateway, agent_spec):
        """A veto hook can block authorization before rules run."""
        blocked = []

        def block(ctx):
            blocked.append(ctx.payload.tool_name)
            return "blocked by veto hook"

        HookRegistry.get().on(points.AUTHORIZE_VETO, HookMode.VETO, block)
        ctx = GateContext("", "s1", agent_spec, tool_name="bash")
        result = gateway.authorize(ctx)
        assert result.decision == "denied"
        assert "veto" in (result.reason or "")
        assert blocked == ["bash"]

    def test_veto_passes_to_rules(self, gateway, agent_spec):
        """Returning None from a veto hook allows rules to evaluate."""

        def pass_through(ctx):
            return None

        HookRegistry.get().on(points.AUTHORIZE_VETO, HookMode.VETO, pass_through)
        ctx = GateContext("", "s1", agent_spec)
        result = gateway.authorize(ctx)
        assert result.decision == "approved"

    def test_veto_error_fails_closed(self, gateway, agent_spec):
        """A throwing veto hook must deny (fail-closed)."""
        HookRegistry.get().on(points.AUTHORIZE_VETO, HookMode.VETO,
                           lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
        ctx = GateContext("", "s1", agent_spec)
        result = gateway.authorize(ctx)
        assert result.decision == "denied"


class TestAuthorizeObserve:
    """authorize.observe hooks (was PostHook)."""

    def test_observe_called_after_authorize(self, gateway, agent_spec):
        post_calls = []

        def counter(ctx):
            payload = ctx.payload or {}
            gctx = payload.get("ctx") if isinstance(payload, dict) else None
            result = payload.get("result") if isinstance(payload, dict) else None
            post_calls.append((gctx.tool_name, result.decision))

        HookRegistry.get().on(points.AUTHORIZE_OBSERVE, HookMode.OBSERVE, counter)
        ctx = GateContext("", "s1", agent_spec, tool_name="bash")
        result = gateway.authorize(ctx)
        assert result.decision == "approved"
        assert post_calls == [("bash", "approved")]

    def test_observe_fires_even_on_denied(self, gateway, agent_spec):
        """observe hooks fire regardless of authorization outcome."""
        post_calls = []

        def counter(ctx):
            payload = ctx.payload or {}
            result = payload.get("result") if isinstance(payload, dict) else None
            post_calls.append(result.decision)

        HookRegistry.get().on(points.AUTHORIZE_OBSERVE, HookMode.OBSERVE, counter)
        gateway.register_rule(lambda ctx: True, name="deny-all")
        ctx = GateContext("", "s1", agent_spec)
        result = gateway.authorize(ctx)
        assert result.decision == "denied"
        assert post_calls == ["denied"]

    def test_observe_fires_on_gate_timeout(self, gateway, agent_spec):
        """observe fires even when the human gate times out."""
        post_fired = []

        def counter(ctx):
            payload = ctx.payload or {}
            result = payload.get("result") if isinstance(payload, dict) else None
            post_fired.append(result.decision)

        HookRegistry.get().on(points.AUTHORIZE_OBSERVE, HookMode.OBSERVE, counter)
        ctx = GateContext("atomic", "s1", agent_spec, tool_name="bash")
        result = gateway.authorize(ctx, timeout=0.001)
        assert result.decision == "denied"
        assert post_fired == ["denied"]


class TestSandboxSerialization:
    """Real-world use case: serialize access to shared sandbox."""

    def test_serialize_sandbox_access(self, gateway, agent_spec):
        """observe hook releases lock acquired by veto hook."""
        locks = {}
        released = []

        def sandbox_veto(ctx):
            gctx = ctx.payload
            sandbox = getattr(gctx.spec, "sandbox", "default")
            lock = locks.setdefault(sandbox, threading.Lock())
            if not lock.acquire(blocking=False):
                return "sandbox occupied"
            gctx._lock = lock
            return None

        def sandbox_observe(ctx):
            payload = ctx.payload or {}
            gctx = payload.get("ctx") if isinstance(payload, dict) else None
            lock = getattr(gctx, "_lock", None)
            if lock:
                lock.release()
                released.append(True)

        reg = HookRegistry.get()
        reg.on(points.AUTHORIZE_VETO, HookMode.VETO, sandbox_veto)
        reg.on(points.AUTHORIZE_OBSERVE, HookMode.OBSERVE, sandbox_observe)

        ctx = GateContext("", "s1", agent_spec, tool_name="bash")
        result = gateway.authorize(ctx)
        assert result.decision == "approved"
        assert len(released) == 1

        ctx2 = GateContext("", "s1", agent_spec, tool_name="read_file")
        result2 = gateway.authorize(ctx2)
        assert result2.decision == "approved"
        assert len(released) == 2


class TestSandboxMutexBuiltin:
    """Built-in install_sandbox_mutex for shared sandbox concurrency."""

    def test_acquires_and_releases_lock(self, gateway, agent_spec):
        install_sandbox_mutex(timeout=1.0)

        ctx = GateContext("", "s1", agent_spec, tool_name="bash")
        result = gateway.authorize(ctx)
        assert result.decision == "approved"

        ctx2 = GateContext("", "s1", agent_spec, tool_name="read_file")
        result2 = gateway.authorize(ctx2)
        assert result2.decision == "approved"

    def test_concurrent_blocked(self, gateway, agent_spec):
        """Cross-thread: second agent blocked while first holds the lock."""
        install_sandbox_mutex(timeout=1.0)

        ctx1 = GateContext("", "s1", agent_spec, tool_name="bash")
        r1 = gateway.authorize(ctx1)
        assert r1.decision == "approved"

        result_holder = []

        def try_acquire():
            ctx2 = GateContext("", "s1", agent_spec, tool_name="write_file")
            result_holder.append(gateway.authorize(ctx2))

        t = threading.Thread(target=try_acquire)
        t.start()
        try:
            t.join(timeout=2.0)
            assert len(result_holder) == 1
            assert result_holder[0].decision == "denied"
            assert "occupied" in (result_holder[0].reason or "")
        finally:
            # always release, even on assertion failure
            lock = getattr(ctx1, "_sandbox_mutex_lock", None)
            if lock is not None:
                lock.release()
                ctx1._sandbox_mutex_lock = None

    def test_reentrant_calls_do_not_deadlock(self, gateway, agent_spec):
        """Same thread calling authorize twice must not deadlock (RLock)."""
        install_sandbox_mutex(timeout=1.0)

        ctx = GateContext("", "s1", agent_spec, tool_name="bash")
        r1 = gateway.authorize(ctx)
        assert r1.decision == "approved"

        r2 = gateway.authorize(ctx)
        assert r2.decision == "approved"


class TestAuthorizeObserveGuaranteed:
    """observe hooks must fire even when authorization errors occur."""

    def test_observe_fires_on_veto_error(self, gateway, agent_spec):
        """A throwing veto hook still results in an observe event."""
        post_fired = []

        def broken_veto(ctx):
            raise RuntimeError("boom")

        def counter(ctx):
            payload = ctx.payload or {}
            result = payload.get("result") if isinstance(payload, dict) else None
            post_fired.append(result.decision)

        reg = HookRegistry.get()
        reg.on(points.AUTHORIZE_VETO, HookMode.VETO, broken_veto)
        reg.on(points.AUTHORIZE_OBSERVE, HookMode.OBSERVE, counter)
        ctx = GateContext("", "s1", agent_spec)
        result = gateway.authorize(ctx)
        assert result.decision == "denied"
        assert post_fired == ["denied"]
