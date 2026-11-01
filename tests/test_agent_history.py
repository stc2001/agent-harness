"""I-32: agent binding history recorded on run — TDD."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import uuid

from harness.interfaces import AgentSpec


@pytest.fixture
def tmp_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return str(home)


@pytest.fixture
def client(tmp_home, tmp_path, monkeypatch):
    from harness.sdk import HarnessClient
    from harness.sdk.config import HarnessSettings
    from harness.orchestration.pool import AgentInstancePool
    from harness.sandbox.manager import SandboxEntity

    monkeypatch.setattr(
        "harness.sdk.client.SandboxManager", lambda **kw: MagicMock())
    c = HarnessClient(settings=HarnessSettings(
        hmac_secret="test-secret-0123456789abcdef",
        sessions_dir=str(tmp_path / "sessions")))
    mgr = MagicMock()
    entity = SandboxEntity(sandbox_id="sbx-1", config_name="main",
                           backend=MagicMock())
    mgr.get_or_create.return_value = entity
    mgr.get_config.return_value = None
    mgr.ensure.return_value = entity
    c._pool = AgentInstancePool(
        store=c._store, gateway=c._gateway, api_key="sk-test",
        sandbox_manager=mgr, audit_log=c._audit,
        model_pool=c._model_pool)

    import harness.agent_runtime.impl as impl_mod

    def fake_openai(**kwargs):
        cl = MagicMock()
        cl.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(role="assistant", content="DONE",
                                        tool_calls=None),
                finish_reason="stop")])
        return cl

    monkeypatch.setattr(impl_mod.openai, "OpenAI", fake_openai)
    return c


def _spec(name="hist-worker"):
    return AgentSpec(name=name, description="t", tools=["echo"],
                     model="deepseek-v4-flash", sandbox="main",
                     max_iterations=1, llm_profile="default",
                     permissions={"subagent_spawn": False})


def test_run_records_binding_edge(client) -> None:
    inst = client.agents.register(_spec())
    res = client.agents.run(inst.instance_id, "task")

    meta = client._store.get_session_metadata(res.session_id)
    assert meta["spec_name"] == "hist-worker"
    assert meta["sandbox_id"] == "sbx-1"
    hist = meta["binding_history"]
    assert len(hist) == 1
    edge = hist[0]
    assert edge["agent"] == "hist-worker"
    assert edge["instance_id"] == inst.instance_id
    assert edge["session_id"] == res.session_id
    assert edge["sandbox_id"] == "sbx-1"
    assert edge["first_ts"] == edge["last_ts"]


def test_rerun_same_pair_merges_not_duplicates(client) -> None:
    inst = client.agents.register(_spec())
    r1 = client.agents.run(inst.instance_id, "task")
    r2 = client.agents.run(inst.instance_id, "task",
                           session_id=r1.session_id)

    meta = client._store.get_session_metadata(r1.session_id)
    assert len(meta["binding_history"]) == 1
    assert r2.session_id == r1.session_id


# ─────────────────────────────────────────────────────────────
# S3: history_sessions / history_sandboxes / history_bindings
# ─────────────────────────────────────────────────────────────


def _mk_sessions(client, edges_by_session):
    """edges_by_session: {sid: [edge dict with agent/instance/sandbox/ts]}"""
    import uuid as _u
    for sid, edges in edges_by_session.items():
        client._store.create_session(sid, {"spec_name": edges[0]["agent"]})
        client._store.update_session_metadata(sid, {"binding_history": edges})
    return list(edges_by_session)


def test_history_sessions_and_sandboxes_order(client) -> None:
    from harness.sdk.client import _AgentsClient
    ag = _AgentsClient(client)
    s1 = "h-s1-" + str(uuid.uuid4())
    s2 = "h-s2-" + str(uuid.uuid4())
    _mk_sessions(client, {
        s1: [{"agent": "A", "instance_id": "i1", "session_id": s1,
              "sandbox_id": "sbx-old", "first_ts": 1.0, "last_ts": 1.0},
             {"agent": "A", "instance_id": "i1", "session_id": s1,
              "sandbox_id": "sbx-new", "first_ts": 10.0, "last_ts": 10.0}],
        s2: [{"agent": "A", "instance_id": "i1", "session_id": s2,
              "sandbox_id": "sbx-new", "first_ts": 5.0, "last_ts": 5.0}],
    })
    sessions = ag.history_sessions("A")
    assert [r["session_id"] for r in sessions] == [s1, s2]
    assert sessions[0]["sandbox_id"] == "sbx-new"
    sbs = ag.history_sandboxes("A")
    assert [r["sandbox_id"] for r in sbs] == ["sbx-new", "sbx-old"]
    assert sbs[0]["session_count"] == 2


def test_history_sandboxes_ignores_other_agents(client) -> None:
    from harness.sdk.client import _AgentsClient
    ag = _AgentsClient(client)
    sid = "h-shared-" + str(uuid.uuid4())
    _mk_sessions(client, {
        sid: [{"agent": "A", "instance_id": "ia", "session_id": sid,
               "sandbox_id": "sbx-a", "first_ts": 1.0, "last_ts": 1.0},
              {"agent": "B", "instance_id": "ib", "session_id": sid,
               "sandbox_id": "sbx-b", "first_ts": 2.0, "last_ts": 2.0}],
    })
    assert [r["sandbox_id"] for r in ag.history_sandboxes("A")] == ["sbx-a"]
    # history_sessions 仅含 A 绑过的（B 独占的不出现）
    assert ag.history_sessions("B")[0]["session_id"] == sid


def test_history_bindings_agent_scoped(client) -> None:
    from harness.sdk.client import _AgentsClient
    ag = _AgentsClient(client)
    sid = "h-mix-" + str(uuid.uuid4())
    _mk_sessions(client, {
        sid: [{"agent": "A", "instance_id": "ia", "session_id": sid,
               "sandbox_id": "sbx-a1", "first_ts": 1.0, "last_ts": 1.0},
              {"agent": "B", "instance_id": "ib", "session_id": sid,
               "sandbox_id": "sbx-b", "first_ts": 2.0, "last_ts": 2.0},
              {"agent": "A", "instance_id": "ia", "session_id": sid,
               "sandbox_id": "sbx-a2", "first_ts": 3.0, "last_ts": 3.0}],
    })
    # A 在该 session 的绑定：sbx-a2(3.0) → sbx-a1(1.0)，不含 B 的 sbx-b
    got = ag.history_bindings("A", sid)
    assert [e["sandbox_id"] for e in got] == ["sbx-a2", "sbx-a1"]
    # A 未曾绑定其他 session → 空
    assert ag.history_bindings("A", "h-none-" + str(uuid.uuid4())) == []




def test_run_result_carries_instance_status(client) -> None:
    """I-39: RunResult 显式携带实例状态快照（completed -> idle）。"""
    inst = client.agents.register(_spec())
    res = client.agents.run(inst.instance_id, "task")
    assert res.status == "completed"
    assert res.instance_status == "idle"

