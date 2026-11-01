"""I-12 (ADR-044): Stop 双轨制 — framework 硬闸门 + app 软裁决.

- framework 硬闸门：timeout / max_llm_calls / sandbox_death 是事实 →
  `agent.stop.framework` veto 链 → 强制停（app 无法覆盖，fail-closed）。
- app 软裁决：`request_stop(reason)` 是请求 → 步窗口 `agent.stop.app` veto 链
  → 批准优雅停（agent_completed reason=stopped，回 IDLE 可恢复）；
  否决写 `agent_stop_denied` 续跑；reason 非法 fail-closed 拒绝。
"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.hooks import HookMode, HookRegistry, points
from harness.interfaces import AgentSpec
from harness.orchestration.pool import AgentInstancePool
from harness.session_store.impl import SessionStore
from tests.test_agent_runtime import _FakeMessage


# ─────────────────────────────────────────────────────────────
# Fixtures (mirror test_wake.py)
# ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_registry():
    """Isolated process-level hook registry for each test."""
    HookRegistry.get().clear()
    yield
    HookRegistry.get().clear()


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
def pool(store, gateway, mock_sandbox_manager) -> AgentInstancePool:
    return AgentInstancePool(store=store, gateway=gateway, api_key="sk-test",
                             sandbox_manager=mock_sandbox_manager)


def _spec(name: str = "test-agent", tools: list[str] | None = None,
          max_iterations: int = 5) -> AgentSpec:
    return AgentSpec(
        name=name,
        description=f"Test agent {name}",
        tools=tools or ["echo"],
        model="deepseek-v4-flash",
        sandbox="main",
        max_iterations=max_iterations,
        permissions={"subagent_spawn": False},
    )


def _new_session(store: SessionStore) -> str:
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    return sid


def _wait_until(cond, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _count(store: SessionStore, sid: str, etype: str) -> list:
    return [e for e in store.read_events(sid) if e.event_type == etype]


class _ThreadRun:
    """Run execute_task in a background thread; joins even on failure."""

    def __init__(self, agent, task, sid):
        self.result: dict = {}
        self._t = threading.Thread(
            target=lambda: self.result.update(
                result=agent.execute_task(task, sid)),
            daemon=True)
        self._t.start()

    def join(self, timeout: float = 5.0) -> None:
        self._t.join(timeout=timeout)
        assert not self._t.is_alive(), "background execute_task did not finish"
        return self.result.get("result")


def _run_in_thread(agent, task, sid) -> _ThreadRun:
    return _ThreadRun(agent, task, sid)


def _stall_then_done_llm(barrier: threading.Event, calls: dict):
    """LLM stub: call #1 blocks on the barrier and returns 'length'
    (loop continues), later calls return DONE/stop."""
    def mock_llm(messages, tools=None):
        calls["n"] = calls.get("n", 0) + 1
        if calls["n"] == 1:
            barrier.wait(timeout=5)
            return _FakeMessage(content="partial"), "length"
        return _FakeMessage(content="DONE"), "stop"
    return mock_llm


# ─────────────────────────────────────────────────────────────
# Framework 硬闸门
# ─────────────────────────────────────────────────────────────


class TestFrameworkGate:
    def test_timeout_fires_framework_hook_and_fails(self, pool, store):
        """timeout 是事实 → agent.stop.framework 触发 → agent_failed."""
        sid = _new_session(store)
        spec = _spec()
        spec.timeout_seconds = 0.01
        agent = pool.acquire(spec)
        agent._current_session_id = sid

        def slow_llm(messages, tools=None):
            time.sleep(0.05)  # exceed the 10ms timeout on round 2
            return _FakeMessage(content="partial"), "length"

        agent._call_llm = slow_llm

        seen = []
        HookRegistry.get().on(points.AGENT_STOP_FRAMEWORK, HookMode.OBSERVE,
                              lambda ctx: seen.append(ctx.payload))

        result = agent.execute_task("task", sid)
        assert result["status"] == "failed"
        failed = _count(store, sid, "agent_failed")
        assert failed and failed[0].payload.get("reason") == "timeout"
        assert seen, "framework stop hook must fire on timeout"
        assert "timeout" in seen[0]["condition"]

    def test_max_llm_calls_fails(self, pool, store):
        """预算熔断（max_llm_calls）走 framework 硬闸门."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        agent._call_llm = lambda messages, tools=None: (
            _FakeMessage(content="DONE"), "stop")
        result = agent.execute_task("task", sid,
                                    config={"max_llm_calls": 0})
        assert result["status"] == "failed"
        failed = _count(store, sid, "agent_failed")
        assert failed and failed[0].payload.get("reason") == \
            "max_llm_calls_exceeded"

    def test_sandbox_death_fails(self, store, gateway):
        """沙箱死亡（is_alive=False）是 framework 硬闸门条件."""
        dead = MagicMock()
        dead.is_alive.return_value = False
        mgr = MagicMock()
        mgr.get_or_create.return_value = MagicMock(sandbox_id="dead-iid", backend=dead)
        pool2 = AgentInstancePool(store=store, gateway=gateway,
                                  api_key="sk-test", sandbox_manager=mgr)
        sid = _new_session(store)
        agent = pool2.acquire(_spec())
        agent._current_session_id = sid
        agent._call_llm = lambda messages, tools=None: (
            _FakeMessage(content="DONE"), "stop")
        result = agent.execute_task("task", sid)
        assert result["status"] == "failed"
        failed = _count(store, sid, "agent_failed")
        assert failed and failed[0].payload.get("reason") == "sandbox_death"

    def test_app_cannot_override_framework_gate(self, pool, store):
        """app 在 agent.stop.framework 注册更高优先级 veto 返回 None →
        framework 仍 fail-closed 停（硬闸门不可覆盖）."""
        sid = _new_session(store)
        spec = _spec()
        spec.timeout_seconds = 0.01
        agent = pool.acquire(spec)
        agent._current_session_id = sid

        def slow_llm(messages, tools=None):
            time.sleep(0.05)
            return _FakeMessage(content="partial"), "length"

        agent._call_llm = slow_llm
        HookRegistry.get().on(points.AGENT_STOP_FRAMEWORK, HookMode.VETO,
                              lambda ctx: None, priority=2000)
        result = agent.execute_task("task", sid)
        assert result["status"] == "failed"
        failed = _count(store, sid, "agent_failed")
        assert failed and failed[0].payload.get("reason") == "timeout"


# ─────────────────────────────────────────────────────────────
# App 软裁决
# ─────────────────────────────────────────────────────────────


class TestAppVerdict:
    def test_legal_reason_approved_graceful_stop(self, pool, store):
        """objective_met → 默认批准 → stopped + agent_completed{stop_reason}
        → 回 IDLE（可恢复）."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        barrier = threading.Event()
        agent._call_llm = _stall_then_done_llm(barrier, {})

        holder = _run_in_thread(agent, "task", sid)
        assert _wait_until(lambda: agent.status == "running")
        assert agent.request_stop("objective_met") is True
        barrier.set()
        result = holder.join(timeout=5)

        assert result["status"] == "stopped"
        assert result["stop_reason"] == "objective_met"
        comp = _count(store, sid, "agent_completed")
        assert comp and comp[-1].payload.get("reason") == "stopped"
        assert comp[-1].payload.get("stop_reason") == "objective_met"
        assert agent.status == "idle"

    def test_needs_confirm_reason_denied_and_continues(self, pool, store):
        """budget_policy（需确认）→ 默认否决 → agent_stop_denied + 续跑."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        barrier = threading.Event()
        calls: dict = {}
        agent._call_llm = _stall_then_done_llm(barrier, calls)

        holder = _run_in_thread(agent, "task", sid)
        assert _wait_until(lambda: agent.status == "running")
        assert agent.request_stop("budget_policy") is True
        barrier.set()
        result = holder.join(timeout=5)

        denied = _count(store, sid, "agent_stop_denied")
        assert denied and denied[0].payload.get("reason") == "budget_policy"
        # 否决后 loop 继续并正常完成
        assert result["status"] == "completed"
        comp = _count(store, sid, "agent_completed")
        assert comp and comp[-1].payload.get("reason") == "stop"

    def test_illegal_reason_fail_closed_rejected(self, pool, store):
        """无 reason / no_reason / 未知 → request_stop 立即拒绝，不触发 hook."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        barrier = threading.Event()
        calls: dict = {}
        agent._call_llm = _stall_then_done_llm(barrier, calls)

        holder = _run_in_thread(agent, "task", sid)
        assert _wait_until(lambda: agent.status == "running")

        app_seen = []
        HookRegistry.get().on(points.AGENT_STOP_APP, HookMode.OBSERVE,
                              lambda ctx: app_seen.append(ctx.payload))
        assert agent.request_stop("no_reason") is False
        assert agent.request_stop(None) is False
        assert agent.request_stop("some_made_up_reason") is False
        barrier.set()
        result = holder.join(timeout=5)
        assert result["status"] == "completed"
        assert app_seen == [], "illegal reasons must not reach the hook"
        assert _count(store, sid, "agent_stop_denied") == []

    def test_request_stop_when_idle_is_noop(self, pool, store):
        """非 RUNNING（IDLE）→ request_stop 拒绝（§5 状态不符）."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        agent._call_llm = lambda messages, tools=None: (
            _FakeMessage(content="DONE"), "stop")
        assert agent.execute_task("task", sid)["status"] == "completed"
        assert agent.status == "idle"
        assert agent.request_stop("objective_met") is False


# ─────────────────────────────────────────────────────────────
# App 覆盖默认裁决表
# ─────────────────────────────────────────────────────────────


class TestAppOverride:
    def test_veto_hook_can_deny_legal_reason(self, pool, store):
        """app veto（priority>0）否决 objective_met → 续跑."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        barrier = threading.Event()
        calls: dict = {}
        agent._call_llm = _stall_then_done_llm(barrier, calls)
        HookRegistry.get().on(points.AGENT_STOP_APP, HookMode.VETO,
                              lambda ctx: "keep-going", priority=10)

        holder = _run_in_thread(agent, "task", sid)
        assert _wait_until(lambda: agent.status == "running")
        assert agent.request_stop("objective_met") is True
        barrier.set()
        result = holder.join(timeout=5)

        assert _count(store, sid, "agent_stop_denied")
        assert result["status"] == "completed"

    def test_around_hook_can_approve_needs_confirm(self, pool, store):
        """app around hook 批准 budget_policy（绕过默认否决）→ 停."""
        from harness.hooks.registry import TriggerResult
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        barrier = threading.Event()
        calls: dict = {}
        agent._call_llm = _stall_then_done_llm(barrier, calls)

        def around(ctx, next_fn):
            if ctx.payload.get("reason") == "budget_policy":
                return TriggerResult(payload={"approved": True})
            return next_fn()

        HookRegistry.get().on(points.AGENT_STOP_APP, HookMode.AROUND,
                              around, priority=10)

        holder = _run_in_thread(agent, "task", sid)
        assert _wait_until(lambda: agent.status == "running")
        assert agent.request_stop("budget_policy") is True
        barrier.set()
        result = holder.join(timeout=5)

        assert result["status"] == "stopped"
        comp = _count(store, sid, "agent_completed")
        assert comp[-1].payload.get("stop_reason") == "budget_policy"
        assert _count(store, sid, "agent_stop_denied") == []


# ─────────────────────────────────────────────────────────────
# 终态一致 + SSE 续跑语义
# ─────────────────────────────────────────────────────────────


class TestTerminalConsistency:
    def test_stopped_agent_idle_and_pool_reusable(self, pool, store):
        """approved stop 后实例 IDLE，池复用守卫返回同一实例."""
        sid = _new_session(store)
        spec = _spec()
        spec.idle_timeout = 300
        agent = pool.acquire(spec)
        agent._current_session_id = sid
        barrier = threading.Event()
        agent._call_llm = _stall_then_done_llm(barrier, {})

        holder = _run_in_thread(agent, "task", sid)
        assert _wait_until(lambda: agent.status == "running")
        assert agent.request_stop("user_requested") is True
        barrier.set()
        result = holder.join(timeout=5)

        assert agent.status == "idle"
        assert pool.acquire(spec) is agent, \
            "stopped-then-idle instance must be reusable"

    def test_stop_then_wake_resumes_with_history(self, pool, store):
        """SSE 语义：停（保留状态）→ wake 续跑，前轮输出在上下文."""
        from tests.test_mock_llm import (
            MockTransport, _make_done_response, _make_tool_response,
        )
        import httpx
        import openai
        from tests.conftest import register_test_tools

        sid = _new_session(store)
        agent = pool.acquire(_spec(tools=["echo"]))
        register_test_tools(agent._tool_registry)
        agent._inject_tool_deps()
        agent._current_session_id = sid

        # Round 1: echo then done
        t1 = MockTransport([
            _make_tool_response("echo", {"message": "hi"}),
            _make_done_response("FIRST-DONE"),
        ])
        agent._client = openai.OpenAI(api_key="sk-test",
                                      http_client=httpx.Client(transport=t1))
        assert agent.execute_task("first task", sid)["status"] == "completed"
        assert agent.status == "idle"

        # Round 2: stopped by request (length keeps the loop alive)
        barrier = threading.Event()
        calls: dict = {}
        t2 = MockTransport([_make_done_response("SHOULD-NOT-RUN")])
        agent._client = openai.OpenAI(api_key="sk-test",
                                      http_client=httpx.Client(transport=t2))

        def llm2(messages, tools=None):
            calls["n"] = calls.get("n", 0) + 1
            if calls["n"] == 1:
                barrier.wait(timeout=5)
            return _FakeMessage(content="partial"), "length"

        agent._call_llm = llm2
        holder = _run_in_thread(agent, "second task", sid)
        assert _wait_until(lambda: agent.status == "running")
        assert agent.request_stop("client_disconnected") is True
        barrier.set()
        result = holder.join(timeout=5)
        assert result["status"] == "stopped"
        assert agent.status == "idle"

        # Round 3: wake resumes — previous rounds' output visible
        t3 = MockTransport([_make_done_response("THIRD-DONE")])
        agent._client = openai.OpenAI(api_key="sk-test",
                                      http_client=httpx.Client(transport=t3))
        del agent._call_llm  # restore the real (transport-driven) path
        agent.wake("third task", sid)
        assert _wait_until(
            lambda: len(_count(store, sid, "agent_completed")) >= 3
        ), "wake round after stop did not complete"
        msgs = t3.requests[0]["messages"]
        user_texts = [m["content"] for m in msgs if m["role"] == "user"]
        asst_texts = [m["content"] or "" for m in msgs if m["role"] == "assistant"]
        assert any("third task" in t for t in user_texts)
        assert any("FIRST-DONE" in t for t in asst_texts), \
            f"round-1 output lost after stop+wake: {asst_texts}"


# ─────────────────────────────────────────────────────────────
# SDK 执行面
# ─────────────────────────────────────────────────────────────


class TestSdkStop:
    def test_agents_stop_end_to_end(self, tmp_path, monkeypatch):
        """client.agents.stop(instance_id, reason) 端到端."""
        from harness.sdk import HarnessClient

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        client = HarnessClient(sandbox_configs={})
        mock_mgr = MagicMock()
        mock_mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-instance-001", backend=MagicMock())
        client._pool = AgentInstancePool(
            store=client._store, gateway=client._gateway,
            api_key="sk-test", sandbox_manager=mock_mgr)

        barrier = threading.Event()
        calls: dict = {}

        def fake_call_llm(self, messages, tools=None):
            calls["n"] = calls.get("n", 0) + 1
            if calls["n"] == 1:
                barrier.wait(timeout=5)
                return _FakeMessage(content="partial"), "length"
            return _FakeMessage(content="DONE"), "stop"

        monkeypatch.setattr(AgentRuntime, "_call_llm", fake_call_llm)

        spec = _spec(name="sdk-stop-worker")
        spec.idle_timeout = 300
        inst = client.agents.register(spec)
        sid = client.sessions.start(inst.instance_id).session_id
        client.sessions.events.send(sid, "start")

        pooled = client._pool.list_instances(spec.name)
        assert _wait_until(lambda: pooled and pooled[0].status == "running")
        assert client.agents.stop(inst.instance_id,
                                  reason="objective_met") is True
        barrier.set()
        assert _wait_until(
            lambda: len(_count(client._store, sid, "agent_completed")) >= 1
        )
        comp = _count(client._store, sid, "agent_completed")[-1]
        assert comp.payload.get("reason") == "stopped"
        assert comp.payload.get("stop_reason") == "objective_met"
        # agent_completed is written before _status flips to idle — poll it.
        assert _wait_until(lambda: pooled[0].status == "idle"), \
            f"expected idle after stop, got {pooled[0].status}"
        assert client.agents.status(inst.instance_id) == "idle"

    def test_agents_stop_unknown_instance_false(self, tmp_path, monkeypatch):
        """未池化/未知实例 → agents.stop 返回 False."""
        from harness.sdk import HarnessClient

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        client = HarnessClient(sandbox_configs={})
        mock_mgr = MagicMock()
        mock_mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-instance-001", backend=MagicMock())
        client._pool = AgentInstancePool(
            store=client._store, gateway=client._gateway,
            api_key="sk-test", sandbox_manager=mock_mgr)
        spec = _spec(name="never-run")
        inst = client.agents.register(spec)
        # not pooled / not running
        assert client.agents.stop(inst.instance_id, reason="user_requested") is False
