"""I-39: 任务 prompt 全文存取 + RunResult.instance_status + 异常回 IDLE — TDD."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec


class _Store:
    def __init__(self):
        self.events = []

    def append_event(self, event):
        self.events.append(event)

    def read_events(self, sid):
        return list(self.events)


def _spec():
    return AgentSpec(name="t39", description="t", tools=[], model="deepseek-v4-flash",
                     sandbox="main", max_iterations=2, llm_profile="default",
                     permissions={"subagent_spawn": False})


@pytest.fixture
def rt():
    from harness.agent_runtime.impl import AgentRuntime
    runtime = AgentRuntime(spec=_spec(), gateway=Gateway(event_bus=None),
                           store=_Store(), api_key="x", sandbox=MagicMock())
    return runtime


LONG_TASK = ("步骤说明：" + "甲" * 600 + " —— 关键尾标：WRITTEN_TAIL_MARKER")


def _stub_loop(runtime, result=None, raises=None):
    def fake(session_id, system_prompt, tools):
        if raises is not None:
            raise raises
        return result or {"status": "completed", "reason": "done"}
    runtime._agent_loop = fake


def test_agent_started_stores_full_prompt(rt):
    _stub_loop(rt)
    rt.execute_task(LONG_TASK, "s1")
    started = [e for e in rt.store.events if e.event_type == "agent_started"][0]
    assert started.payload["input"] == LONG_TASK
    assert len(started.payload["input"]) > 200


def test_current_task_prompt_full(rt):
    _stub_loop(rt)
    rt.execute_task(LONG_TASK, "s1")
    assert rt._current_task["prompt"] == LONG_TASK
    assert len(rt._current_task["prompt"]) > 500


def test_completed_result_carries_instance_status_idle(rt):
    _stub_loop(rt)
    result = rt.execute_task(LONG_TASK, "s1")
    assert result["status"] == "completed"
    assert result["instance_status"] == "idle"
    assert rt._status == "idle"


def test_exception_returns_instance_to_idle(rt):
    _stub_loop(rt, raises=RuntimeError("boom"))
    result = rt.execute_task(LONG_TASK, "s1")
    assert result["status"] == "failed"
    assert rt._status == "idle", "异常后实例必须回 IDLE，否则永久占用"
    assert result["instance_status"] == "idle"
