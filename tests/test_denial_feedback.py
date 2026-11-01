"""TDD spec: a denied tool call must be fed back to the model (soft-continue).

ADR-033's soft-continue table promises the LLM context shows
`[tool error] …` for a permission denial / unknown tool, but the runtime only
emitted `tool_called(allowed=False)` + `agent_failed`.  `assemblable` strips an
orphan tool call, so the model never saw the error and could only repeat the
same call until `max_iterations`.

Fix (this spec): emit a `tool_result` carrying the denial reason (paired to the
tool-call id) while keeping the existing events for observability.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec, Event
from harness.prompt_assembly import (_event_cost, _event_messages,
                                     build_messages)
from harness.session_store.impl import SessionStore
from tests.conftest import register_test_tools


class _FakeFunction:
    def __init__(self, name: str, arguments: dict[str, Any]):
        self.name = name
        self.arguments = json.dumps(arguments)


class _FakeToolCall:
    def __init__(self, name: str, arguments: dict[str, Any]):
        self.id = f"call_{uuid.uuid4().hex[:8]}"
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls or []
        self.content = content


def _msg_tool(name: str, args: dict[str, Any]):
    return _FakeMessage(tool_calls=[_FakeToolCall(name, args)]), "tool_calls"


def _msg_done():
    return _FakeMessage(content="DONE"), "stop"


class MockSandbox:
    def spawn(self, config, cap_token=""):
        return "mock-pid"

    def kill(self, container_id):
        pass

    def is_alive(self, container_id):
        return True

    def get_logs(self, container_id):
        return ""

    def execute(self, container_id, command, restrict=None):
        return (f"MOCK: {command}", "", 0)


def _runtime(gateway: Gateway, store: SessionStore,
             api_key: str) -> AgentRuntime:
    spec = AgentSpec(name="denial-test", description="denial feedback",
                     tools=["echo"], model="deepseek-v4-flash",
                     max_iterations=4)
    runtime = AgentRuntime(spec=spec, gateway=gateway, store=store,
                           api_key=api_key, sandbox=MockSandbox())
    register_test_tools(runtime._tool_registry)
    return runtime


class TestDeniedToolFeedback:
    def test_denial_is_visible_to_the_model_as_a_tool_result(
            self, temp_store_path, deepseek_api_key):
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, metadata={})

        gateway = Gateway(event_bus=None)
        gateway.register_rule(lambda ctx: "blocked by test rule", name="deny-all")
        runtime = _runtime(gateway, store, deepseek_api_key)
        runtime._current_session_id = session_id

        calls: list[list[dict]] = []

        def mock_llm(messages, tools=None):
            calls.append(messages)
            if len(calls) == 1:
                return _msg_tool("echo", {"message": "hi"})
            return _msg_done()

        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)

        assert result["status"] == "completed"
        # 1) the model's second request contains exactly ONE denial notice, as
        #    a tool message paired with the tool call
        tool_msgs = [m for m in calls[1] if m.get("role") == "tool"]
        assert tool_msgs, "denial was not fed back to the model"
        content = str(tool_msgs[-1].get("content"))
        assert "[tool error]" in content
        assert "blocked by test rule" in content
        mentions = [m for m in calls[1]
                    if "blocked by test rule" in str(m.get("content"))]
        assert len(mentions) == 1, "the denial must not be duplicated in the prompt"

        # 2) the event log still carries the observability contract
        events = store.read_events(session_id)
        assert any(e.event_type == "tool_called"
                   and e.payload.get("allowed") is False for e in events)
        assert any(e.event_type == "agent_failed" for e in events)
        results = [e for e in events if e.event_type == "tool_result"]
        assert results, "a denial tool_result event must be emitted"
        assert "blocked by test rule" in str(results[-1].payload.get("result"))

    def test_unknown_tool_is_visible_to_the_model(
            self, temp_store_path, deepseek_api_key):
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, metadata={})
        runtime = _runtime(Gateway(event_bus=None), store, deepseek_api_key)
        runtime._current_session_id = session_id

        calls: list[list[dict]] = []

        def mock_llm(messages, tools=None):
            calls.append(messages)
            if len(calls) == 1:
                return _msg_tool("no_such_tool", {"x": 1})
            return _msg_done()

        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)

        assert result["status"] == "completed"
        tool_msgs = [m for m in calls[1] if m.get("role") == "tool"]
        assert tool_msgs
        assert "Unknown tool: no_such_tool" in str(tool_msgs[-1].get("content"))


@pytest.fixture(autouse=True)
def _isolate_hooks():
    from harness.hooks import HookRegistry
    reg = HookRegistry.get()
    reg.clear()
    yield
    reg.clear()


# ── represented_by_tool_result: denial is emitted exactly once ─────────

def _ev(eid, et, payload, parent=None):
    return Event(event_id=eid, session_id="s", event_type=et, timestamp=0.0,
                 source="test", parent_event_id=parent, payload=payload)


def test_represented_failure_is_not_reemitted_as_a_user_message():
    """`agent_failed(represented_by_tool_result=True)` must cost/emit 0.

    The runtime emits BOTH the paired tool_result and the observability
    agent_failed for a denial; the prompt must show the error exactly once.
    """
    failed = _ev("f1", "agent_failed",
                 {"error": "blocked by rule",
                  "represented_by_tool_result": True}, parent="c1")
    assert _event_messages(failed, True, None, set(), set(), {}) == []
    assert _event_cost(failed, "deepseek", "tokenizer_dir") == 0

    events = [
        _ev("t1", "agent_thought", {"content": "", "tool_calls": [
            {"id": "c1", "name": "echo", "arguments": "{}"}]}),
        _ev("r1", "tool_result", {"tool": "echo",
                                  "result": "[tool error] blocked by rule"},
            parent="c1"),
        failed,
    ]
    msgs = build_messages("SYS", events, context_window=10**9,
                          provider="deepseek")
    mentions = [m for m in msgs
                if "blocked by rule" in str(m.get("content"))]
    assert len(mentions) == 1, mentions


def test_failure_without_marker_is_still_emitted_once():
    """An unrepresented failure keeps the old `[tool error]` user message."""
    failed = _ev("f2", "agent_failed", {"error": "boom"})
    out = _event_messages(failed, True, None, set(), set(), {})
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "[tool error] boom" == out[0]["content"]
    assert _event_cost(failed, "deepseek", "tokenizer_dir") > 0
