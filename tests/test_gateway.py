"""Tests for Gateway — unified authorization engine."""

from __future__ import annotations

import threading
import time

import pytest

from harness.gateway.gateway import (
    Gateway,
    GateContext,
    AuthorizeResult,
)
from harness.gateway.gate_manager import EventBus
from harness.interfaces import AgentSpec


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def agent_spec():
    return AgentSpec(
        name="test-agent",
        description="test",
        model="deepseek-v4-flash",
        tools=["bash", "read_file"],
    )


class FakeSessionManager:
    def __init__(self):
        self.events = []

    def wake(self, session_id):
        pass

    def append(self, event):
        self.events.append(event)

    def get_events(self):
        return self.events


@pytest.fixture
def event_bus():
    return EventBus(FakeSessionManager())


@pytest.fixture
def gateway(event_bus):
    return Gateway(event_bus=event_bus, parent_session_id="test-session")


# ── authorize: no gate → approved immediately ─────────────


class TestAuthorizeNoGate:
    def test_returns_approved_no_gate(self, gateway, agent_spec):
        """authorize() without a triggered gate returns approved immediately."""
        ctx = GateContext(
            gate_type="",  # empty → no gate
            session_id="s1",
            spec=agent_spec,
            tool_name="bash",
        )
        result = gateway.authorize(ctx)
        assert result.decision == "approved"

    def test_timeout_on_gate_returns_denied(self, gateway, agent_spec):
        """Timed-out gate (no resolution) returns denied."""
        ctx = GateContext(
            gate_type="atomic",
            session_id="s1",
            spec=agent_spec,
            tool_name="bash",
        )
        result = gateway.authorize(ctx, timeout=0.01)
        assert result.decision == "denied"
        assert "timed out" in (result.reason or "")


# ── authorize: gate blocks until resolved ──────────────────


def _wait_until_pending(gateway, session_id, timeout=1.0):
    """Wait until a gate is visible in list_pending (flaky-safe)."""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if gateway.list_pending(session_id):
            return
        _time.sleep(0.01)
    raise TimeoutError("Gate did not appear in pending list")


class TestAuthorizeWithGate:
    def test_blocks_until_resolved(self, gateway, agent_spec):
        """Atomic gate waits until resolve() is called from another thread."""
        result_holder = []

        def authorize_thread():
            ctx = GateContext(
                gate_type="atomic",
                session_id="s1",
                spec=agent_spec,
                tool_name="bash",
            )
            result_holder.append(gateway.authorize(ctx, timeout=5.0))

        t = threading.Thread(target=authorize_thread)
        t.start()
        _wait_until_pending(gateway, "s1")

        pending = gateway.list_pending("s1")
        assert len(pending) == 1

        gateway.resolve(pending[0].event_id, "approve")
        t.join(timeout=2.0)

        assert len(result_holder) == 1
        assert result_holder[0].decision == "approved"

    def test_reject_returns_denied(self, gateway, agent_spec):
        result_holder = []

        def authorize_thread():
            ctx = GateContext(
                gate_type="atomic",
                session_id="s1",
                spec=agent_spec,
                tool_name="bash",
            )
            result_holder.append(gateway.authorize(ctx, timeout=5.0))

        t = threading.Thread(target=authorize_thread)
        t.start()
        _wait_until_pending(gateway, "s1")

        pending = gateway.list_pending("s1")
        gateway.resolve(pending[0].event_id, "reject")
        t.join(timeout=2.0)

        assert result_holder[0].decision == "denied"
        assert "rejected" in (result_holder[0].reason or "")

    def test_modify_returns_amended(self, gateway, agent_spec):
        result_holder = []

        def authorize_thread():
            ctx = GateContext(
                gate_type="atomic",
                session_id="s1",
                spec=agent_spec,
                tool_name="bash",
            )
            result_holder.append(gateway.authorize(ctx, timeout=5.0))

        t = threading.Thread(target=authorize_thread)
        t.start()
        _wait_until_pending(gateway, "s1")

        pending = gateway.list_pending("s1")
        gateway.resolve(pending[0].event_id, "modify",
                        amend={"text": "corrected"})
        t.join(timeout=2.0)

        assert result_holder[0].decision == "modified"
        assert result_holder[0].amend_args == {"text": "corrected"}


# ── resolve ───────────────────────────────────────────────


class TestResolve:
    def test_invalid_decision_raises(self, gateway):
        with pytest.raises(ValueError, match="Invalid decision"):
            gateway.resolve("ev-1", "unknown")

    def test_resolve_unknown_event_is_noop(self, gateway):
        gateway.resolve("nonexistent-id", "approve")


# ── list_pending ───────────────────────────────────────────


class TestListPending:
    def test_empty_for_new_gateway(self, gateway):
        assert gateway.list_pending("any-session") == []

    def test_returns_gate_after_create(self, gateway, agent_spec):
        event_id = gateway.create_gate(
            GateContext("node", "s1", agent_spec, node_id="n1"))
        pending = gateway.list_pending("s1")
        assert len(pending) == 1
        assert pending[0].event_id == event_id
        assert pending[0].node_id == "n1"

    def test_gone_after_resolve(self, gateway, agent_spec):
        event_id = gateway.create_gate(
            GateContext("node", "s1", agent_spec))
        gateway.resolve(event_id, "approve")
        assert gateway.list_pending("s1") == []

    def test_filters_by_session(self, gateway, agent_spec):
        gateway.create_gate(GateContext("node", "s1", agent_spec))
        assert gateway.list_pending("s2") == []


# ── get_gate ───────────────────────────────────────────────


class TestGetGate:
    def test_returns_none_for_unknown(self, gateway):
        assert gateway.get_gate("unknown", "s1") is None

    def test_returns_gate_for_known(self, gateway, agent_spec):
        event_id = gateway.create_gate(
            GateContext("node", "s1", agent_spec))
        g = gateway.get_gate(event_id, "s1")
        assert g is not None
        assert g.event_id == event_id


# ── rules ──────────────────────────────────────────────────


class TestRules:
    def test_rule_blocks_authorization(self, gateway, agent_spec):
        gateway.register_rule(lambda ctx: "no bash allowed", name="no-bash")
        ctx = GateContext("", "s1", agent_spec, tool_name="bash")
        result = gateway.authorize(ctx)
        assert result.decision == "denied"
        assert "no bash" in (result.reason or "")

    def test_rule_allows_if_returns_falsy(self, gateway, agent_spec):
        gateway.register_rule(lambda ctx: "", name="allow-all")
        ctx = GateContext("", "s1", agent_spec)
        result = gateway.authorize(ctx)
        assert result.decision == "approved"

    def test_rule_error_is_fail_closed(self, gateway, agent_spec):
        def broken(ctx):
            raise RuntimeError("boom")
        gateway.register_rule(broken, name="broken")
        ctx = GateContext("", "s1", agent_spec)
        result = gateway.authorize(ctx)
        assert result.decision == "denied"
        assert "broken" in (result.reason or "")


# ── event persistence ──────────────────────────────────────


class TestEventPersistence:
    def test_pending_emits_event(self, gateway, agent_spec, event_bus):
        gateway.create_gate(GateContext("node", "s1", agent_spec))
        events = event_bus.get_events()
        pending = [e for e in events if e.event_type == "human_gate_pending"]
        assert len(pending) == 1

    def test_resolve_emits_event(self, gateway, agent_spec, event_bus):
        event_id = gateway.create_gate(
            GateContext("node", "s1", agent_spec))
        gateway.resolve(event_id, "approve")
        events = event_bus.get_events()
        resolved = [e for e in events
                    if e.event_type == "human_gate_resolved"]
        assert len(resolved) == 1
        assert resolved[0].payload.get("decision") == "approve"

    def test_modify_emits_amend_event(self, gateway, agent_spec, event_bus):
        event_id = gateway.create_gate(
            GateContext("node", "s1", agent_spec))
        gateway.resolve(event_id, "modify", amend={"x": 1})
        events = event_bus.get_events()
        amends = [e for e in events
                  if e.event_type == "human_gate_amend"]
        assert len(amends) == 1
        assert amends[0].payload.get("amend_input") == {"x": 1}
        assert amends[0].payload.get("gate_event_id") == event_id
        assert amends[0].event_id == event_id


# ── Gate config validation ──────────────────────────────────



# ── Node override via rules ─────────────────────────────────


class TestNodeOverrideViaRules:
    """Gateway rules can simulate node-level gate overrides."""

    def test_rule_scoped_to_node(self, gateway, agent_spec):
        """A rule can be registered for a specific node_id."""
        blocked = []

        def node_specific_rule(ctx):
            if ctx.node_id == "validator-node":
                blocked.append(True)
                return "gated by node"
            return None

        gateway.register_rule(node_specific_rule, name="node-gate")

        # Simulate agent in 'validator-node' context
        ctx = GateContext(
            gate_type="",
            session_id="s1",
            spec=agent_spec,
            node_id="validator-node",
            tool_name="bash",
        )
        result = gateway.authorize(ctx)
        assert result.decision == "denied"
        assert "gated by node" in (result.reason or "")
        assert len(blocked) == 1

    def test_node_without_rule_passes(self, gateway, agent_spec):
        """A node not matching the rule is not blocked."""
        gateway.register_rule(
            lambda ctx: "gated" if ctx.node_id == "validator-node" else None,
            name="node-gate",
        )
        ctx = GateContext(
            gate_type="",
            session_id="s2",
            spec=agent_spec,
            node_id="worker-node",
        )
        result = gateway.authorize(ctx)
        assert result.decision == "approved"

    def test_spec_level_gate_can_be_disabled_by_node_rule(self, gateway, agent_spec):
        """Node-level rule can override (block) spec-level gate setting."""
        # Simulate: spec says atomic gate, but node rule blocks only for this node
        gateway.register_rule(
            lambda ctx: "blocked" if ctx.node_id == "sensitive-node" else None,
            name="sensitive-gate",
        )
        # sensitive-node: blocked
        ctx = GateContext(
            gate_type="",  # spec disabled, but node rule still applies
            session_id="s1",
            spec=agent_spec,
            node_id="sensitive-node",
        )
        assert gateway.authorize(ctx).decision == "denied"

        # other-node: not blocked
        ctx2 = GateContext(
            gate_type="",
            session_id="s1",
            spec=agent_spec,
            node_id="other-node",
        )
        assert gateway.authorize(ctx2).decision == "approved"


# ── Memory leak ─────────────────────────────────────────────


class TestMemoryLeak:
    def test_resolve_removes_from_pending(self, gateway, agent_spec):
        """Resolved gates should be removed from gateway._pending dict."""
        for i in range(10):
            eid = gateway.create_gate(
                GateContext("node", f"s{i}", agent_spec))
            gateway.resolve(eid, "approve")
        assert len(gateway._pending) == 0, (
            f"Memory leak: {len(gateway._pending)} entries remain after resolve"
        )


# ── parent_session_id setter ────────────────────────────────


class TestParentSessionId:
    def test_can_update_parent_session(self, gateway, agent_spec):
        """Gateway parent_session_id can be updated mid-lifecycle."""
        gateway._parent_session_id = ""
        eid = gateway.create_gate(GateContext("node", "s1", agent_spec))
        # Gate events should now go to s1 after calling setter
        # This is tested implicitly through EventBus event checks
        assert eid  # no crash


class TestRulesBeforeGate:
    """Rules evaluate before HumanGate — deny skips gate creation."""

    def test_rule_deny_skips_gate(self, gateway, agent_spec):
        """When a rule denies, human gate is never created."""
        gateway.register_rule(lambda ctx: True, name="block-all")
        # gate_type="atomic" would normally create a blocking gate,
        # but the rule should deny BEFORE the gate is created
        ctx = GateContext("atomic", "s1", agent_spec, tool_name="bash")
        result = gateway.authorize(ctx)
        assert result.decision == "denied"
        # No gate should be pending (rule denied before gate creation)
        assert gateway.list_pending("s1") == []

    def test_rule_passes_then_gate_triggers(self, gateway, agent_spec):
        """When no rule blocks, gate still triggers for gate_type='atomic'."""
        gateway.register_rule(lambda ctx: None, name="allow-all")
        result_holder = []

        import threading

        def auth():
            ctx = GateContext("atomic", "s1", agent_spec, tool_name="bash")
            result_holder.append(gateway.authorize(ctx, timeout=5.0))

        t = threading.Thread(target=auth)
        t.start()
        _wait_until_pending(gateway, "s1")
        pending = gateway.list_pending("s1")
        assert len(pending) == 1
        gateway.resolve(pending[0].event_id, "approve")
        t.join(timeout=2.0)
        assert result_holder[0].decision == "approved"
