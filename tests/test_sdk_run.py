"""I-23 (ADR-015): SDK 公共执行入口 client.agents.run — TDD.

决策（grill 闭合）：同步 run + RunResult；spec|instance_id 双形态；session 续用
轻校验；装配错误 raise / 执行结果返回；忙时 raise；output=事件流终态输出；
stop_reason 仅 stopped；tools=None → 按 spec.tools 过滤（Q7 现存 bug 修复）。
"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.orchestration.pool import AgentInstancePool
from harness.session_store.impl import SessionStore
from harness.sdk.models import RunResult
from tests.test_agent_runtime import _FakeMessage


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def client(tmp_home):
    """HarnessClient with a mock-sandbox pool (no bwrap)."""
    from harness.sdk import HarnessClient
    c = HarnessClient(sandbox_configs={})
    mock_mgr = MagicMock()
    mock_mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-instance-001", backend=MagicMock())
    c._pool = AgentInstancePool(store=c._store, gateway=c._gateway,
                                api_key="sk-test", sandbox_manager=mock_mgr)
    return c


@pytest.fixture
def gateway() -> Gateway:
    return Gateway(event_bus=None)


def _spec(name: str = "run-worker", tools: list[str] | None = None,
          **kw) -> AgentSpec:
    return AgentSpec(
        name=name,
        description=f"Test {name}",
        tools=tools or ["echo"],
        model="deepseek-v4-flash",
        sandbox="main",
        max_iterations=5,
        permissions={"subagent_spawn": False},
        **kw,
    )


def _done_llm(monkeypatch):
    """Class-level _call_llm that always completes."""
    def fake_call_llm(self, messages, tools=None):
        return _FakeMessage(content="DONE"), "stop"
    monkeypatch.setattr(AgentRuntime, "_call_llm", fake_call_llm)


def _wait_until(cond, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _stall_llm(monkeypatch, barrier, calls=None):
    """LLM stub: call #1 blocks, returns 'length'; later calls DONE."""
    calls = calls or {}

    def fake_call_llm(self, messages, tools=None):
        calls["n"] = calls.get("n", 0) + 1
        if calls["n"] == 1:
            barrier.wait(timeout=5)
            return _FakeMessage(content="partial"), "length"
        return _FakeMessage(content="DONE"), "stop"
    monkeypatch.setattr(AgentRuntime, "_call_llm", fake_call_llm)
    return calls


def _count(store, sid, etype):
    return [e for e in store.read_events(sid) if e.event_type == etype]


# ─────────────────────────────────────────────────────────────
# run 基础：spec / instance_id 双形态
# ─────────────────────────────────────────────────────────────


class TestRunBasics:
    def test_run_spec_creates_session_and_returns_completed(self, client,
                                                            monkeypatch):
        """run(spec) 一步端到端：注册→装配→新 session→执行→RunResult."""
        _done_llm(monkeypatch)
        res = client.agents.run(_spec(), "hello")
        assert isinstance(res, RunResult)
        assert res.status == "completed"
        assert res.output == "DONE"
        assert res.instance_id
        assert res.session_id
        # session bound to instance (metadata)
        meta = client._store.get_session_metadata(res.session_id)
        assert meta.get("instance_id") == res.instance_id
        # instance registered in registry
        assert client.agents.get(res.instance_id).spec_name == "run-worker"
        # terminal event present
        assert _count(client._store, res.session_id, "agent_completed")

    def test_run_instance_id_reuses_instance_new_session(self, client,
                                                         monkeypatch):
        """run(instance_id) 复用同一池实例；每次 run 新 session（Q11）."""
        _done_llm(monkeypatch)
        spec = _spec()
        spec.idle_timeout = 300
        inst = client.agents.register(spec)
        r1 = client.agents.run(inst.instance_id, "t1")
        r2 = client.agents.run(inst.instance_id, "t2")
        pooled = client._pool.list_instances(spec.name)
        assert len(pooled) == 1, "same instance must be reused"
        assert r1.instance_id == r2.instance_id == inst.instance_id
        assert r1.session_id != r2.session_id, "each run = new session"

    def test_run_with_session_id_continues_history(self, client, monkeypatch):
        """run(session_id=...) 续用同一 session，事件累积（Q3）."""
        _done_llm(monkeypatch)
        spec = _spec()
        spec.idle_timeout = 300
        r1 = client.agents.run(spec, "first")
        r2 = client.agents.run(spec, "second", session_id=r1.session_id)
        assert r2.session_id == r1.session_id
        assert len(_count(client._store, r1.session_id, "agent_started")) == 2
        assert len(_count(client._store, r1.session_id, "agent_completed")) == 2

    def test_run_session_not_found_raises(self, client, monkeypatch):
        """session_id 不存在 → 轻校验失败 → ValueError（装配错误 raise）."""
        _done_llm(monkeypatch)
        with pytest.raises(ValueError, match="session"):
            client.agents.run(_spec(), "t", session_id="no-such-session")

    def test_run_invalid_spec_raises(self, client):
        """非法 spec → register 校验失败 → ValueError 透出（fail-fast）."""
        bad = AgentSpec(name="", description="", tools=[], model="",
                        sandbox="", max_iterations=1, permissions={})
        with pytest.raises(ValueError):
            client.agents.run(bad, "t")


# ─────────────────────────────────────────────────────────────
# 忙时 / stopped 语义
# ─────────────────────────────────────────────────────────────


class TestRunLifecycle:
    def test_run_busy_raises(self, client, monkeypatch):
        """同一实例 RUNNING 时再次 run(inst_id) → raise（不阻塞不排队，Q6）。

        I-25：busy 是实例级的——run(spec) 每次注册新实例，旧按名塌缩的
        busy 已不再适用；本测试用同一 inst_id 验证守卫。
        """
        barrier = threading.Event()
        _stall_llm(monkeypatch, barrier)
        spec = _spec()
        inst = client.agents.register(spec)
        holder: dict = {}

        def run_first():
            holder["r"] = client.agents.run(inst.instance_id, "first")

        t = threading.Thread(target=run_first, daemon=True)
        t.start()
        assert _wait_until(
            lambda: (client._pool.get_by_instance_id(inst.instance_id)
                     is not None and client._pool.get_by_instance_id(
                         inst.instance_id).status == "running")
        ), "first run did not reach running"
        with pytest.raises(RuntimeError, match="busy"):
            client.agents.run(inst.instance_id, "second")
        barrier.set()
        t.join(timeout=5)
        assert holder["r"].status in ("completed", "stopped")

    def test_run_stopped_with_stop_reason(self, client, monkeypatch):
        """run 期间 app 裁决停 → RunResult{stopped, stop_reason}（Q10）."""
        barrier = threading.Event()
        _stall_llm(monkeypatch, barrier)
        spec = _spec()
        spec.idle_timeout = 300
        inst = client.agents.register(spec)
        holder: dict = {}

        def run_first():
            holder["r"] = client.agents.run(inst.instance_id, "first")

        t = threading.Thread(target=run_first, daemon=True)
        t.start()
        assert _wait_until(
            lambda: any(a.status == "running"
                        for a in client._pool.list_instances(spec.name))
        )
        assert client.agents.stop(inst.instance_id, reason="objective_met") is True
        barrier.set()
        t.join(timeout=5)
        r = holder["r"]
        assert r.status == "stopped"
        assert r.stop_reason == "objective_met"
        assert r.error is None


# ─────────────────────────────────────────────────────────────
# Q7: execute_task tools=None → spec.tools 过滤（现存 bug 修复）
# ─────────────────────────────────────────────────────────────


class TestToolsAssembly:
    def _runtime(self, store, gateway, spec, responses):
        from tests.test_mock_llm import MockTransport
        import httpx
        import openai
        mock_sandbox = MagicMock()
        mock_sandbox.is_alive.return_value = True
        runtime = AgentRuntime(spec=spec, gateway=gateway, store=store,
                               api_key="sk-test", sandbox=mock_sandbox)
        transport = MockTransport(responses)
        runtime._client = openai.OpenAI(
            api_key="sk-test", http_client=httpx.Client(transport=transport))
        return runtime, transport

    def test_execute_task_filters_tools_by_spec(self, temp_store_path, gateway):
        """tools=None → 请求只含 spec.tools 白名单内的内置工具."""
        from tests.test_mock_llm import _make_done_response
        store = SessionStore(temp_store_path)
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        runtime, transport = self._runtime(
            store, gateway, _spec(tools=["read_file"]), [_make_done_response()])
        runtime.execute_task("t", sid)
        names = {t["function"]["name"] for t in transport.requests[0]["tools"]}
        assert names == {"read_file"}, f"got tools {names}"

    def test_execute_task_explicit_empty_tools(self, temp_store_path, gateway):
        """显式 tools=[] → 请求无工具（真无工具，非过滤）. """
        from tests.test_mock_llm import _make_done_response
        store = SessionStore(temp_store_path)
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        runtime, transport = self._runtime(
            store, gateway, _spec(tools=["read_file"]), [_make_done_response()])
        runtime.execute_task("t", sid, tools=[])
        assert transport.requests[0]["tools"] == []

    def test_execute_task_spawn_subagent_conditional(self, temp_store_path,
                                                     gateway):
        """subagent_spawn=True → spawn_subagent 追加进请求. """
        from tests.test_mock_llm import _make_done_response
        store = SessionStore(temp_store_path)
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        spec = _spec(tools=["read_file"])
        spec.permissions = {"subagent_spawn": True}
        runtime, transport = self._runtime(store, gateway, spec,
                                           [_make_done_response()])
        runtime.execute_task("t", sid)
        names = {t["function"]["name"] for t in transport.requests[0]["tools"]}
        assert names == {"read_file", "spawn_subagent"}

    def test_execute_task_non_builtin_tools_gives_empty(self, temp_store_path,
                                                        gateway):
        """spec.tools 全是非内置（如 echo）→ 过滤后空列表（不崩）."""
        from tests.test_mock_llm import _make_done_response
        store = SessionStore(temp_store_path)
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        runtime, transport = self._runtime(store, gateway, _spec(),
                                           [_make_done_response()])
        runtime.execute_task("t", sid)
        assert transport.requests[0]["tools"] == []
