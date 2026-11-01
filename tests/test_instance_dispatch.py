"""I-25: run/stop/status/wake 按注册实例分派（同模板多实例并发）— TDD.

grill 决策（2026-09-04）：
- D1 身份合并：运行时键与 runtime.instance_id = 注册 inst_id（同模板两实例 uuid 互异）
- D2 分派键：run/wake/stop/status 一律按 inst_id 精确定位（消除按名塌缩）
- D3 busy：同实例 RUNNING 时 run 保持 raise；follow-up 走 events.send
- D4 兼容：pool.acquire(spec) legacy 语义不变
- D5 沙箱不动（共享容器 + session workspace 隔离）
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.interfaces import AgentSpec


# ─────────────────────────────────────────────────────────────
# Fixtures & Helpers
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """Redirect HOME to tmp_path so ~/.harness is isolated per test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return str(home)


def _spec(name: str = "worker", **kw) -> AgentSpec:
    base = dict(
        name=name, description="t", tools=["echo"],
        model="deepseek-v4-flash", sandbox="main", max_iterations=1,
        llm_profile="default",
        permissions={"subagent_spawn": False},
    )
    base.update(kw)
    return AgentSpec(**base)


def _fake_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every openai.OpenAI construction to a canned fake client.

    The runtime builds self._client = openai.OpenAI(...) inside pool.acquire.
    Swap the constructor so any acquired runtime completes instantly with a
    DONE assistant message and no usage (I-15 None path).
    """
    import harness.agent_runtime.impl as impl_mod

    def fake_openai(**kwargs):
        client = MagicMock()
        done = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(role="assistant", content="DONE",
                                        tool_calls=None),
                finish_reason="stop",
            )],
        )
        client.chat.completions.create.return_value = done
        return client

    monkeypatch.setattr(impl_mod.openai, "OpenAI", fake_openai)


@pytest.fixture
def client(tmp_home, tmp_path, monkeypatch):
    """HarnessClient with isolated HOME and mocked sandbox/LLM."""
    from harness.sdk import HarnessClient
    from harness.orchestration.pool import AgentInstancePool

    c = HarnessClient()
    mgr = MagicMock()
    mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-iid", backend=MagicMock())
    mgr.get_config.return_value = None
    c._pool = AgentInstancePool(
        store=c._store, gateway=c._gateway, api_key="sk-test",
        sandbox_manager=mgr, audit_log=c._audit,
        model_pool=c._model_pool)
    _fake_openai(monkeypatch)
    return c


# ─────────────────────────────────────────────────────────────
# S1: pool.acquire(instance_id=...) 键控区分性（RED）
# ─────────────────────────────────────────────────────────────


class TestPoolInstanceKeying:
    def test_acquire_instance_creates_distinct_runtimes_per_inst(
            self, client) -> None:
        """同模板两注册实例 → instance 键控 acquire 产出两个独立运行时（uuid 互异）."""
        inst_a = client.agents.register(_spec("worker"))
        inst_b = client.agents.register(_spec("worker"))

        rt_a = client._pool.acquire(_spec("worker"),
                                    instance_id=inst_a.instance_id)
        rt_b = client._pool.acquire(_spec("worker"),
                                    instance_id=inst_b.instance_id)

        assert rt_a is not rt_b
        assert rt_a.instance_id == inst_a.instance_id
        assert rt_b.instance_id == inst_b.instance_id
        assert client._pool.get_by_instance_id(inst_a.instance_id) is rt_a
        assert client._pool.get_by_instance_id(inst_b.instance_id) is rt_b

    def test_acquire_instance_reuses_same_runtime(self, client) -> None:
        """同 inst_id 再次 acquire → 复用同一运行时（不新建、不按名塌缩）."""
        inst = client.agents.register(_spec("worker"))
        rt1 = client._pool.acquire(_spec("worker"),
                                   instance_id=inst.instance_id)
        rt2 = client._pool.acquire(_spec("worker"),
                                   instance_id=inst.instance_id)
        assert rt1 is rt2


# ─────────────────────────────────────────────────────────────
# S2/S3: SDK run/status/stop 按注册实例分派（RED）
# ─────────────────────────────────────────────────────────────


class TestSdkInstanceDispatch:
    def test_run_gives_each_instance_its_own_runtime(self, client) -> None:
        """run(inst_A)+run(inst_B) → 各自独立运行时（B 不被按名塌缩到 A）."""
        inst_a = client.agents.register(_spec("worker"))
        inst_b = client.agents.register(_spec("worker"))

        ra = client.agents.run(inst_a.instance_id, "task A")
        rb = client.agents.run(inst_b.instance_id, "task B")

        assert ra.status == "completed" and rb.status == "completed"
        rt_a = client._pool.get_by_instance_id(inst_a.instance_id)
        rt_b = client._pool.get_by_instance_id(inst_b.instance_id)
        assert rt_a is not None and rt_b is not None
        assert rt_a is not rt_b
        assert ra.session_id != rb.session_id

    def test_run_busy_is_per_instance(self, client) -> None:
        """A 实例 RUNNING 时 run(A) raise；run(B) 不受影响."""
        inst_a = client.agents.register(_spec("worker"))
        inst_b = client.agents.register(_spec("worker"))
        client.agents.run(inst_a.instance_id, "task A")
        rt_a = client._pool.get_by_instance_id(inst_a.instance_id)
        rt_a._status = "running"  # 模拟 A 正在 RUNNING

        import pytest
        with pytest.raises(RuntimeError, match="busy"):
            client.agents.run(inst_a.instance_id, "again")

        rb = client.agents.run(inst_b.instance_id, "task B")
        assert rb.status == "completed"

    def test_status_is_per_instance(self, client) -> None:
        """run 过的 A → idle；未 run 的 B → compiled（不返回 A 的状态）."""
        inst_a = client.agents.register(_spec("worker"))
        inst_b = client.agents.register(_spec("worker"))
        client.agents.run(inst_a.instance_id, "task A")

        assert client.agents.status(inst_a.instance_id) == "idle"
        assert client.agents.status(inst_b.instance_id) == "compiled"

    def test_stop_targets_exact_instance(self, client) -> None:
        """A running / B idle → stop(B) 返回 False 且不误停 A."""
        import pytest
        inst_a = client.agents.register(_spec("worker"))
        inst_b = client.agents.register(_spec("worker"))
        client.agents.run(inst_a.instance_id, "task A")
        rt_a = client._pool.get_by_instance_id(inst_a.instance_id)
        rt_a._status = "running"
        # B 从未 run（无运行时）→ stop(B) 应 False 且不触碰 A
        assert client.agents.stop(inst_b.instance_id) is False
        assert rt_a._stop_request is None


# ─────────────────────────────────────────────────────────────
# S4: events.send 唤醒精确实例（RED）
# ─────────────────────────────────────────────────────────────


class TestWakeDispatch:
    def test_wake_targets_bound_instance(self, client, monkeypatch) -> None:
        """session 绑 B → 唤醒 B 的运行时（而非按名复用 A）."""
        from harness.agent_runtime.impl import AgentRuntime
        inst_a = client.agents.register(_spec("worker"))
        inst_b = client.agents.register(_spec("worker"))
        client.agents.run(inst_a.instance_id, "task A")  # A 存在 idle
        sid_b = client.sessions.start(inst_b.instance_id).session_id
        # 确保 B 运行时已存在（懒建路径由 wake 触发）
        client._pool.acquire(_spec("worker"), instance_id=inst_b.instance_id)
        rt_b = client._pool.get_by_instance_id(inst_b.instance_id)

        woke: list[str] = []

        def spy_wake(self, message, session_id):
            woke.append(self.instance_id)
            return True

        monkeypatch.setattr(AgentRuntime, "wake", spy_wake)
        client._wake_session(sid_b, "hi")
        assert woke == [inst_b.instance_id], f"woke: {woke}"
