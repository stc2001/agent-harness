"""Red-team tests: Gateway cannot be bypassed through any API surface.

These tests verify that the three Gateway injection points are
NOT optional — APP code cannot create agents or execute tools
without going through Gateway authorization.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.orchestration.pool import AgentInstancePool
from harness.sandbox.manager import SandboxManager
from harness.session_store.impl import SessionStore


# ── Helpers ────────────────────────────────────────────────


def _make_spec(name: str = "test") -> AgentSpec:
    return AgentSpec(
        name=name, description="test", sandbox="main",
        tools=["echo"], model="mock", max_iterations=1,
    )


# ═══════════════════════════════════════════════════════════
# Attack surface 1: Pool requires Gateway
# ═══════════════════════════════════════════════════════════


class TestPoolRequiresGateway:
    """Pool.acquire() must route through Gateway.authorize().

    APP cannot bypass Gateway by calling pool directly without
    a properly initialized Gateway.
    """

    def test_pool_acquire_calls_gateway_authorize(
        self, tmp_path: Path, monkeypatch
    ):
        """Pool.acquire() invokes gateway.authorize() with
        gate_type='sandbox_spawn'."""
        from harness.interfaces import SandboxConfig
        from harness.sandbox.manager import SandboxManager

        store = SessionStore(base_path=str(tmp_path / "store"))
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "none"},
            shared_folders=[{"host_path": "/tmp", "sandbox_path": "/ws"}],
        )
        mgr = SandboxManager(configs={"main": config})

        gateway = MagicMock(spec=Gateway)
        gateway.authorize.return_value = MagicMock(decision="approved")

        pool = AgentInstancePool(
            store=store, gateway=gateway, api_key="sk-test",
            sandbox_manager=mgr,
        )

        # Patch spawn to avoid real bwrap process
        monkeypatch.setattr(
            "harness.sandbox.manager.get_sandbox",
            lambda _: MagicMock(
                spawn=lambda c, sandbox_id=None: "pid-1",
                is_alive=lambda p: True,
                execute=lambda cid, cmd: ("ready", "", 0),
            )
        )

        pool.acquire(_make_spec())

        assert gateway.authorize.called, (
            "pool.acquire() must call gateway.authorize()"
        )

    def test_pool_requires_gateway_in_constructor(self):
        """Pool constructor requires gateway parameter — no default."""
        import inspect
        sig = inspect.signature(AgentInstancePool.__init__)
        params = list(sig.parameters.values())
        gateway_param = next(
            (p for p in params if p.name == "gateway"), None
        )
        assert gateway_param is not None, "Pool must have gateway parameter"
        assert gateway_param.default is inspect.Parameter.empty, (
            "Gateway must not have a default value — APP must provide it"
        )


# ═══════════════════════════════════════════════════════════
# Attack surface 2: AgentRuntime requires Gateway
# ═══════════════════════════════════════════════════════════


class TestAgentRuntimeRequiresGateway:
    """AgentRuntime._execute_single_tool() MUST call gateway.authorize().

    The agent loop cannot bypass Gateway at the tool execution level.
    """

    def test_agent_runtime_constructor_requires_gateway(self):
        """AgentRuntime.__init__ requires gateway parameter — no default."""
        import inspect
        sig = inspect.signature(AgentRuntime.__init__)
        params = list(sig.parameters.values())
        gateway_param = next(
            (p for p in params if p.name == "gateway"), None
        )
        assert gateway_param is not None, (
            "AgentRuntime must have gateway parameter"
        )
        assert gateway_param.default is inspect.Parameter.empty, (
            "Gateway must not have a default value"
        )

    def test_execute_single_tool_requires_authorize(
        self, tmp_path: Path
    ):
        """_execute_single_tool always calls gateway.authorize()
        before executing any tool."""
        from unittest.mock import MagicMock, patch
        import uuid

        store = SessionStore(base_path=str(tmp_path / "store2"))
        sid = str(uuid.uuid4())
        store.create_session(sid, {})

        spec = _make_spec("gateway-test")
        gateway = MagicMock(spec=Gateway)
        gateway.authorize.return_value = MagicMock(decision="approved")

        mock_sandbox = MagicMock()
        mock_sandbox.execute.return_value = ("OK", "", 0)

        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key="sk-test", sandbox=mock_sandbox,
        )
        runtime._tool_registry.inject({
            "execute": lambda cmd: ("OK", "", 0),
            "permissions": spec.permissions,
            "audit_log": MagicMock(),
            "path_map": {},
            "sandbox_config": None,
        })
        runtime._current_session_id = sid

        tc = MagicMock()
        tc.id = "call_test"
        tc.function.name = "echo"
        tc.function.arguments = '{"message":"test"}'

        # Trigger tool execution
        runtime._execute_single_tool(tc, sid)

        # Gateway.authorize() MUST have been called
        assert gateway.authorize.called, (
            "_execute_single_tool must call gateway.authorize() "
            "before any tool execution"
        )

    def test_gateway_deny_blocks_tool_execution(
        self, tmp_path: Path
    ):
        """When Gateway denies, the tool MUST NOT execute."""
        from unittest.mock import MagicMock
        import uuid

        store = SessionStore(base_path=str(tmp_path / "store3"))
        sid = str(uuid.uuid4())
        store.create_session(sid, {})

        spec = _make_spec("gateway-deny-test")
        gateway = MagicMock(spec=Gateway)
        gateway.authorize.return_value = MagicMock(
            decision="denied", reason="red team block"
        )

        mock_sandbox = MagicMock()
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key="sk-test", sandbox=mock_sandbox,
        )
        runtime._tool_registry.inject({
            "execute": mock_sandbox.execute,
            "permissions": spec.permissions,
            "audit_log": MagicMock(),
            "path_map": {},
            "sandbox_config": None,
        })
        runtime._current_session_id = sid

        tc = MagicMock()
        tc.id = "call_blocked"
        tc.function.name = "echo"
        tc.function.arguments = '{"message":"test"}'

        runtime._execute_single_tool(tc, sid)

        # Sandbox.execute MUST NOT have been called
        mock_sandbox.execute.assert_not_called(), (
            "Gateway denied but tool still executed — security bypass!"
        )
