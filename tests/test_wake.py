"""I-11 (ADR-043 wake / ADR-039 replay) — unit + integration tests.

Wake semantics: an IDLE agent that receives a message is woken by a worker
thread, runs a continuation round carrying the accumulated event history,
then returns to IDLE.  Messages arriving while RUNNING are queued (FIFO).
stop()/dispose() tears down the worker and clears the inbox.
"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec, Event
from harness.orchestration.pool import AgentInstancePool
from harness.prompt_assembly import assemblable
from harness.session_store.impl import SessionStore
from tests.conftest import register_test_tools
from tests.test_agent_runtime import _FakeMessage


# ─────────────────────────────────────────────────────────────
# Fixtures (mirror test_pool.py)
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
def pool(store, gateway, mock_sandbox_manager) -> AgentInstancePool:
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
        max_iterations=3,
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


def _wait_status(agent, status: str, timeout: float = 5.0) -> bool:
    return _wait_until(lambda: agent.status == status, timeout)


def _count_events(store: SessionStore, sid: str, etype: str) -> list[Event]:
    return [e for e in store.read_events(sid) if e.event_type == etype]


# ─────────────────────────────────────────────────────────────
# Unit: wake state machine
# ─────────────────────────────────────────────────────────────


class TestWakeUnit:
    def test_wake_idle_agent_runs_round_and_returns_idle(self, pool, store):
        """IDLE agent + message → RUNNING → continuation round → IDLE."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid

        def mock_llm(messages, tools=None):
            return _FakeMessage(content="DONE"), "stop"

        agent._call_llm = mock_llm
        assert agent.execute_task("first task", sid)["status"] == "completed"
        assert agent.status == "idle"

        agent.wake("follow up", sid)
        assert _wait_until(
            lambda: len(_count_events(store, sid, "agent_completed")) == 2
        ), "wake round did not complete"
        assert agent.status == "idle", f"expected idle, got {agent.status}"

        starts = _count_events(store, sid, "agent_started")
        assert len(starts) == 2, "each wake round must emit agent_started"

    def test_wake_during_running_queues_and_does_not_duplicate(self, pool, store):
        """Message while RUNNING is queued (FIFO) — no duplicate execution."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid

        barrier = threading.Event()
        calls = {"n": 0}

        def mock_llm(messages, tools=None):
            calls["n"] += 1
            if calls["n"] == 1:
                barrier.wait(timeout=5)  # block the first round's LLM call
            return _FakeMessage(content="DONE"), "stop"

        agent._call_llm = mock_llm

        agent.wake("first", sid)
        assert _wait_status(agent, "running"), "worker did not start round 1"
        agent.wake("second", sid)
        assert agent.status == "running"
        assert agent._inbox.qsize() == 1, "second message must be queued"

        barrier.set()
        assert _wait_until(
            lambda: len(_count_events(store, sid, "agent_completed")) == 2
        ), "queued round did not run after the first"
        assert agent.status == "idle"
        assert calls["n"] == 2, f"expected exactly 2 LLM rounds, got {calls['n']}"

    def test_wake_on_compiled_agent_starts_first_round(self, pool, store):
        """Wake works for a never-run (compiled) agent — first round."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        agent._call_llm = lambda messages, tools=None: (
            _FakeMessage(content="DONE"), "stop")
        agent.wake("start me", sid)
        assert _wait_until(
            lambda: len(_count_events(store, sid, "agent_completed")) == 1
        )
        assert agent.status == "idle"

    def test_wake_after_stop_is_noop(self, pool, store):
        """Wake on a STOPPED agent must not run anything."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        agent.stop()
        agent.wake("hello", sid)
        time.sleep(0.1)
        assert agent.status == "stopped"
        assert agent._inbox.qsize() == 0
        assert _count_events(store, sid, "agent_started") == []

    def test_dispose_clears_inbox_and_stops_worker(self, pool, store):
        """dispose() joins the worker thread and clears the inbox."""
        sid = _new_session(store)
        agent = pool.acquire(_spec())
        agent._current_session_id = sid
        agent._call_llm = lambda messages, tools=None: (
            _FakeMessage(content="DONE"), "stop")
        agent.wake("msg", sid)
        assert _wait_status(agent, "idle")
        assert len(_count_events(store, sid, "agent_started")) == 1

        agent.dispose()
        assert agent._disposed is True
        assert agent.status == "stopped"
        assert agent._inbox.qsize() == 0
        if agent._worker is not None:
            assert not agent._worker.is_alive(), "worker thread must be joined"

        # further wake is a no-op — no new round starts
        agent.wake("again", sid)
        time.sleep(0.05)
        assert agent._inbox.qsize() == 0
        assert len(_count_events(store, sid, "agent_started")) == 1


# ─────────────────────────────────────────────────────────────
# Unit: ADR-039 resume-point pairing (I-41: prompt_assembly.assemblable)
# ─────────────────────────────────────────────────────────────


class TestResumeEvents:
    def _ev(self, eid, etype, payload, parent=None) -> Event:
        return Event(event_id=eid, session_id="s", event_type=etype,
                     timestamp=0.0, source="t", parent_event_id=parent,
                     payload=payload)

    # Real event shapes (see _execute_single_tool):
    #   agent_thought: payload.tool_calls = [{"id": T1, ...}]
    #   tool_called:   event_id = T1 (the LLM call id), allowed=True
    #   tool_result:   parent_event_id = T1

    def test_paired_round_is_untouched(self, pool):
        agent = pool.acquire(_spec())
        events = [
            self._ev("s1", "agent_started", {"input": "task1"}),
            self._ev("th1", "agent_thought", {"content": None, "tool_calls": [
                {"id": "T1", "name": "echo", "arguments": "{}"}]}),
            self._ev("T1", "tool_called", {"tool": "echo", "args": {},
                                           "allowed": True}),
            self._ev("tr1", "tool_result", {"result": "ok"}, parent="T1"),
        ]
        cleaned = assemblable(events)
        assert [e.event_id for e in cleaned] == ["s1", "th1", "T1", "tr1"]

    def test_dangling_tool_call_round_is_stripped(self, pool):
        agent = pool.acquire(_spec())
        events = [
            self._ev("s1", "agent_started", {"input": "task1"}),
            # complete round
            self._ev("th1", "agent_thought", {"content": None, "tool_calls": [
                {"id": "T1", "name": "echo", "arguments": "{}"}]}),
            self._ev("T1", "tool_called", {"tool": "echo", "args": {},
                                           "allowed": True}),
            self._ev("tr1", "tool_result", {"result": "ok"}, parent="T1"),
            # interrupted round: T2 has no matching tool_result
            self._ev("th2", "agent_thought", {"content": None, "tool_calls": [
                {"id": "T2", "name": "echo", "arguments": "{}"}]}),
            self._ev("T2", "tool_called", {"tool": "echo", "args": {},
                                           "allowed": True}),
        ]
        cleaned = assemblable(events)
        ids = [e.event_id for e in cleaned]
        assert "th2" not in ids, "dangling agent_thought must be stripped"
        assert "T2" not in ids, "dangling tool_called must be stripped"
        assert "th1" in ids and "T1" in ids and "tr1" in ids
        assert "s1" in ids

    def test_dangling_legacy_tool_called_is_stripped(self, pool):
        """Pre-I-07 tool_called (not covered by agent_thought) without a
        matching tool_result must also be stripped."""
        agent = pool.acquire(_spec())
        events = [
            self._ev("s1", "agent_started", {"input": "task1"}),
            self._ev("T1", "tool_called", {"tool": "echo", "args": {},
                                           "allowed": True}),
            self._ev("tr1", "tool_result", {"result": "ok"}, parent="T1"),
            self._ev("T2", "tool_called", {"tool": "echo", "args": {},
                                           "allowed": True}),  # no tool_result
        ]
        cleaned = assemblable(events)
        ids = [e.event_id for e in cleaned]
        assert "T2" not in ids
        assert "T1" in ids and "tr1" in ids


# ─────────────────────────────────────────────────────────────
# Unit: pool plumbing
# ─────────────────────────────────────────────────────────────


class TestPoolPlumbing:
    def test_get_by_instance_id(self, pool):
        agent = pool.acquire(_spec())
        assert pool.get_by_instance_id(agent.instance_id) is agent
        assert pool.get_by_instance_id("nope") is None

    def test_destroy_disposes_instances(self, pool):
        agent = pool.acquire(_spec())
        pool.destroy(agent.spec.name)
        assert agent._disposed is True

    def test_stop_instance_disposes(self, pool):
        agent = pool.acquire(_spec())
        assert pool.stop_instance(agent.instance_id) is True
        assert agent._disposed is True
        assert pool.stop_instance(agent.instance_id) is False


# ─────────────────────────────────────────────────────────────
# Integration: wake round carries accumulated history (ADR-039)
# ─────────────────────────────────────────────────────────────


class TestWakeIntegration:
    def test_wake_round_sees_previous_output_and_new_task(self, pool, store):
        """The woken round's LLM context includes the previous round's
        assistant output AND the new follow-up task."""
        from tests.test_mock_llm import (
            MockTransport,
            _make_done_response,
            _make_tool_response,
        )
        import httpx
        import openai

        sid = _new_session(store)
        agent = pool.acquire(_spec(tools=["echo"]))
        register_test_tools(agent._tool_registry)
        agent._inject_tool_deps()
        agent._current_session_id = sid

        # Round 1 (direct execute_task): echo then done
        t1 = MockTransport([
            _make_tool_response("echo", {"message": "hi"}),
            _make_done_response("FIRST-DONE"),
        ])
        agent._client = openai.OpenAI(
            api_key="sk-test", http_client=httpx.Client(transport=t1))
        assert agent.execute_task("first task", sid)["status"] == "completed"
        assert agent.status == "idle"

        # Round 2 (wake): single done response — context must carry history
        t2 = MockTransport([_make_done_response("SECOND-DONE")])
        agent._client = openai.OpenAI(
            api_key="sk-test", http_client=httpx.Client(transport=t2))
        agent.wake("second task", sid)
        assert _wait_until(
            lambda: len(_count_events(store, sid, "agent_completed")) == 2
        ), "wake round did not complete"
        assert agent.status == "idle"

        msgs = t2.requests[0]["messages"]
        user_texts = [m["content"] for m in msgs if m["role"] == "user"]
        asst_texts = [m["content"] or "" for m in msgs if m["role"] == "assistant"]
        assert any("second task" in t for t in user_texts), \
            f"wake task not in context: {user_texts}"
        assert any("FIRST-DONE" in t for t in asst_texts), \
            f"previous round output lost: {asst_texts}"


# ─────────────────────────────────────────────────────────────
# Integration: SDK chain — events.send wakes the bound agent
# ─────────────────────────────────────────────────────────────


class TestWakeSdkChain:
    def test_events_send_wakes_bound_agent(self, tmp_path, monkeypatch):
        """Full chain: register → sessions.start → events.send → pooled
        agent runs a round → back to IDLE; follow-up reuses the instance."""
        from harness.sdk import HarnessClient

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        client = HarnessClient(sandbox_configs={})
        # Replace the real pool with a mock-sandbox pool (no bwrap).
        mock_mgr = MagicMock()
        mock_mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-instance-001", backend=MagicMock())
        client._pool = AgentInstancePool(
            store=client._store, gateway=client._gateway,
            api_key="sk-test", sandbox_manager=mock_mgr)

        # Deterministic LLM — no network.
        def fake_call_llm(self, messages, tools=None):
            return _FakeMessage(content="DONE"), "stop"

        monkeypatch.setattr(AgentRuntime, "_call_llm", fake_call_llm)

        spec = _spec(name="sdk-worker", tools=["echo"])
        spec.idle_timeout = 300  # keep the instance for follow-up reuse
        inst = client.agents.register(spec)
        sid = client.sessions.start(inst.instance_id).session_id

        ev_id = client.sessions.events.send(sid, "hello wake")
        assert ev_id
        assert _wait_until(
            lambda: len(_count_events(client._store, sid, "agent_completed")) == 1,
            timeout=5.0,
        ), "first send did not run a round"

        pooled = client._pool.list_instances(spec.name)
        assert len(pooled) == 1
        # agent_completed is written before _status flips to idle — poll the
        # state transition to avoid a race under loaded CI.
        assert _wait_until(lambda: pooled[0].status == "idle"), \
            f"expected idle after first round, got {pooled[0].status}"
        # pool-aware status API
        assert client.agents.status(inst.instance_id) == "idle"

        # follow-up wake reuses the same pooled instance
        client.sessions.events.send(sid, "follow up")
        assert _wait_until(
            lambda: len(_count_events(client._store, sid, "agent_completed")) == 2,
            timeout=5.0,
        ), "follow-up send did not wake the idle agent"
        assert _wait_until(lambda: pooled[0].status == "idle"), \
            f"expected idle after follow-up, got {pooled[0].status}"
        assert pooled[0].status == "idle"
        assert len(client._pool.list_instances(spec.name)) == 1, \
            "follow-up must reuse the idle instance, not spawn a new one"

    def test_events_send_without_bound_agent_only_appends(self, tmp_path,
                                                          monkeypatch):
        """Sessions not bound to a registered instance keep the old
        append-only behavior (no wake, no pool touch)."""
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

        sid = str(uuid.uuid4())
        client._store.create_session(sid, {"type": "unbound"})
        ev_id = client.sessions.events.send(sid, "no agent here")
        assert ev_id
        assert mock_mgr.get_or_create.call_count == 0, \
            "unbound session must not touch the pool"
        assert len(_count_events(client._store, sid, "user_message")) == 1
