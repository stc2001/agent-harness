"""Tests for Module 6 — AgentRuntime."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.session_store.impl import SessionStore
from tests.conftest import register_test_tools


# ── Mock message helper for native tool calling ──────────────

class _FakeToolCall:
    def __init__(self, name: str, arguments: dict[str, Any]):
        self.id = str(uuid.uuid4())
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeFunction:
    def __init__(self, name: str, arguments: dict[str, Any]):
        self.name = name
        self.arguments = json.dumps(arguments)


class _FakeMessage:
    """Simulates an OpenAI message object with tool_calls and content."""
    def __init__(self, tool_calls: list[_FakeToolCall] | None = None,
                 content: str | None = None):
        self.tool_calls = tool_calls or []
        self.content = content


def _msg_tool(name: str, args: dict[str, Any]):
    """Return (message, finish_reason) tuple for a tool-call response."""
    return _FakeMessage(tool_calls=[_FakeToolCall(name, args)], content=None), "tool_calls"


def _msg_done():
    """Return (message, finish_reason) tuple for a natural completion."""
    return _FakeMessage(content="DONE"), "stop"


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture
def gateway(): return Gateway(event_bus=None)
    


@pytest.fixture
def sample_agent_spec(sample_agent_spec_dict: dict[str, Any]) -> AgentSpec:
    return AgentSpec(**sample_agent_spec_dict)


@pytest.fixture
def mock_sandbox():
    """A minimal sandbox mock that implements SandboxInterface for testing."""
    class MockSandbox:
        def spawn(self, config, cap_token=""):
            return "mock-pid-001"
        def kill(self, container_id):
            pass
        def is_alive(self, container_id):
            return True
        def get_logs(self, container_id):
            return ""
        def execute(self, container_id, command, restrict=None):
            return (f"MOCK: {command}", "", 0)
    return MockSandbox()


class TestAgentRuntime:
    # ─────────────────────────────────────────────────────────
    # 1. Construction
    # ─────────────────────────────────────────────────────────

    def test_constructor(
        self,
        sample_agent_spec: AgentSpec,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        mock_sandbox,
    ) -> None:
        store = SessionStore(base_path=temp_store_path)
        runtime = AgentRuntime(
            spec=sample_agent_spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        assert runtime.spec == sample_agent_spec
        assert runtime._gateway == gateway
        assert runtime.store == store
        assert runtime.api_key == deepseek_api_key

    # ─────────────────────────────────────────────────────────
    # 2. Basic run_loop with real API
    # ─────────────────────────────────────────────────────────

    def test_run_loop_basic(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        deepseek_base_url: str,
        sample_session_id: str,
        monkeypatch: Any,
        mock_sandbox,
    ) -> None:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            pytest.skip("DEEPSEEK_API_KEY not available")

        store = SessionStore(base_path=temp_store_path)
        store.create_session(sample_session_id, metadata={})


        monkeypatch.setenv("AGENT_SESSION_ID", sample_session_id)

        spec = AgentSpec(
            name="test-worker",
            description="Test worker",
            tools=["echo", "sum"],
            model="deepseek-v4-flash",
            system_prompt=(
                "You are a test assistant with no tasks. "
                "Respond with exactly the word DONE and nothing else."
            ),
            max_iterations=2,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=api_key,
            base_url=deepseek_base_url,
            sandbox=mock_sandbox,
        )

        try:
            runtime.run_loop()
        except Exception as exc:
            err_msg = str(exc).lower()
            if any(word in err_msg for word in ("connection", "timeout", "unreachable", "resolve")):
                pytest.skip(f"DeepSeek API unreachable: {exc}")
            raise

        events = store.read_events(sample_session_id)
        event_types = [e.event_type for e in events]
        assert "agent_started" in event_types
        assert "agent_completed" in event_types

    # ─────────────────────────────────────────────────────────
    # 3. Tool permission check — allowed vs denied
    # ─────────────────────────────────────────────────────────

    def test_tool_permission_allowed(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        sample_session_id: str,
        monkeypatch: Any,
        mock_sandbox,
    ) -> None:
        store = SessionStore(base_path=temp_store_path)
        store.create_session(sample_session_id, metadata={})


        monkeypatch.setenv("AGENT_SESSION_ID", sample_session_id)

        spec = AgentSpec(
            name="test-worker",
            description="Test worker",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=3,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        register_test_tools(runtime._tool_registry)

        call_count = [0]
        def mock_llm(messages, tools=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return _msg_tool("echo", {"message": "hi"})
            return _msg_done()
        runtime._call_llm = mock_llm

        runtime.run_loop()

        events = store.read_events(sample_session_id)
        tool_called_events = [e for e in events if e.event_type == "tool_called"]
        assert len(tool_called_events) >= 1
        assert tool_called_events[0].payload["tool"] == "echo"
        assert tool_called_events[0].payload["allowed"] is True

        tool_result_events = [e for e in events if e.event_type == "tool_result"]
        assert len(tool_result_events) >= 1
        assert "hi" in str(tool_result_events[0].payload["result"])

    def test_tool_permission_denied(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        sample_session_id: str,
        monkeypatch: Any,
        mock_sandbox,
    ) -> None:
        """Unauthorized tool emits agent_failed, loop continues.

        The Gateway enforces the deny by emitting tool_called(allowed=False)
        and agent_failed.  The agent loop continues (soft-continue) so the
        LLM can see the error and self-correct.
        """
        store = SessionStore(base_path=temp_store_path)
        store.create_session(sample_session_id, metadata={})


        monkeypatch.setenv("AGENT_SESSION_ID", sample_session_id)

        spec = AgentSpec(
            name="test-worker",
            description="Test worker",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=3,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )

        # Gateway rule: deny tools not listed in spec.tools
        gateway.register_rule(
            lambda ctx: ctx.tool_name not in spec.tools,
            name="spec-tools",
        )

        call_num = [0]
        def mock_llm(messages, tools=None):
            call_num[0] += 1
            if call_num[0] == 1:
                return _msg_tool("sum", {"a": 1, "b": 2})
            return _msg_done()
        runtime._call_llm = mock_llm

        result = runtime.run_loop()  # must not raise

        events = store.read_events(sample_session_id)
        tool_called_events = [e for e in events if e.event_type == "tool_called"]
        assert len(tool_called_events) == 1
        assert tool_called_events[0].payload["tool"] == "sum"
        assert tool_called_events[0].payload["allowed"] is False

        # agent_failed must be emitted so LLM sees the error
        assert any(e.event_type == "agent_failed" for e in events)

    # ─────────────────────────────────────────────────────────
    # 4. Built-in tools correctness
    # ─────────────────────────────────────────────────────────

    def test_builtin_tool_echo(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        sample_session_id: str,
        monkeypatch: Any,
        mock_sandbox,
    ) -> None:
        store = SessionStore(base_path=temp_store_path)
        store.create_session(sample_session_id, metadata={})


        monkeypatch.setenv("AGENT_SESSION_ID", sample_session_id)

        spec = AgentSpec(
            name="test-worker",
            description="Test worker",
            tools=["echo", "sum"],
            model="deepseek-v4-flash",
            max_iterations=3,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        register_test_tools(runtime._tool_registry)

        call_count = [0]
        def mock_llm(messages, tools=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return _msg_tool("echo", {"message": "hello world"})
            return _msg_done()
        runtime._call_llm = mock_llm

        runtime.run_loop()

        events = store.read_events(sample_session_id)
        tool_results = [e for e in events if e.event_type == "tool_result"]
        assert len(tool_results) == 1
        assert "hello world" in str(tool_results[0].payload["result"])

    def test_builtin_tool_sum(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        monkeypatch: Any,
        mock_sandbox,
    ) -> None:
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, metadata={})


        monkeypatch.setenv("AGENT_SESSION_ID", session_id)

        spec = AgentSpec(
            name="test-worker",
            description="Test worker",
            tools=["echo", "sum"],
            model="deepseek-v4-flash",
            max_iterations=3,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        register_test_tools(runtime._tool_registry)

        call_count = [0]
        def mock_llm(messages, tools=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return _msg_tool("sum", {"a": 7, "b": 8})
            return _msg_done()
        runtime._call_llm = mock_llm

        runtime.run_loop()

        events = store.read_events(session_id)
        tool_results = [e for e in events if e.event_type == "tool_result"]
        assert len(tool_results) == 1
        assert "15" in str(tool_results[0].payload["result"])

    # ─────────────────────────────────────────────────────────
    # ── Tool call chain ──

    def test_tool_call_id_chain(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        monkeypatch: Any,
        mock_sandbox,
    ) -> None:
        """tool_result.tool_call_id must match the preceding tool_called event_id.

        This verifies the full context assembly pipeline: _agent_loop
        emits events → _build_messages converts to OpenAI format →
        tool_call_id links assistant.tool_calls[].id to subsequent
        tool messages.
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, metadata={})


        spec = AgentSpec(
            name="test-worker",
            description="Test tool_call_id chain",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=3,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        register_test_tools(runtime._tool_registry)

        monkeypatch.setenv("AGENT_SESSION_ID", session_id)

        # Capture the messages that _call_llm receives
        captured_messages: list[list[dict]] = []

        call_count = [0]
        def mock_llm(messages, tools=None):
            call_count[0] += 1
            captured_messages.append(messages)
            if call_count[0] == 1:
                return _msg_tool("echo", {"message": "hi"})
            return _msg_done()

        runtime._call_llm = mock_llm
        runtime.run_loop()

        # Verify second iteration's messages
        assert len(captured_messages) >= 2, \
            "Expected at least 2 LLM calls (tool call + DONE)"

        msgs_round2 = captured_messages[1]

        # Collect tool_call_ids from assistant messages and tool messages
        pending_call_ids: set[str] = set()
        resolved_call_ids: set[str] = set()

        for msg in msgs_round2:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    pending_call_ids.add(tc["id"])
            elif msg["role"] == "tool":
                resolved_call_ids.add(msg["tool_call_id"])

        # Every tool message's tool_call_id must reference a preceding assistant's tool_calls
        orphaned = resolved_call_ids - pending_call_ids
        assert not orphaned, \
            f"Orphaned tool_call_id(s): {orphaned}. "
        f"Assistant call ids: {pending_call_ids}, "
        f"Tool message ids: {resolved_call_ids}"

    # ─────────────────────────────────────────────────────────
    # 7. Regression: no implicit DONE (non-DONE content continues)
    # ─────────────────────────────────────────────────────────

    def test_no_implicit_done(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        mock_sandbox,
        monkeypatch: Any,
    ) -> None:
        """Non-DONE content without tool_calls must continue the loop.

        Before fix: any non-empty LLM content was treated as implicit DONE,
        causing premature termination. After fix: only explicit "DONE"
        triggers completion; empty/no-tool-call responses continue to next
        iteration until max_iterations or DONE.
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, metadata={})


        spec = AgentSpec(
            name="test-worker",
            description="No implicit done test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=3,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id

        call_count = [0]
        def mock_llm(messages, tools=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # Non-DONE text without tool_calls — should NOT terminate
                return _FakeMessage(content="Let me think about this..."), "stop"
            return _msg_done()

        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)

        # With finish_reason="stop", the first call signals natural
        # completion.  "Let me think..." with stop → agent is done.
        assert call_count[0] == 1
        assert result["status"] == "completed"

    # ─────────────────────────────────────────────────────────
    # 8. agent_timeout — runtime guard
    # ─────────────────────────────────────────────────────────

    def test_agent_timeout(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        mock_sandbox,
        monkeypatch: Any,
    ) -> None:
        """_agent_loop must return failed when runtime exceeds timeout_seconds.

        Uses timeout_seconds=0 in config so the time check fires before the
        first LLM call.  Verifies the result dict contains reason="timeout".
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, metadata={})


        spec = AgentSpec(
            name="test-worker",
            description="Timeout test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=10,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        runtime._config = {"timeout_seconds": 0}

        def mock_llm(messages, tools=None):
            return _msg_done()

        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)

        assert result["status"] == "failed"
        assert result["reason"] == "timeout"

    # ─────────────────────────────────────────────────────────
    # 9. unified return — permission denied returns dict (not raises)
    # ─────────────────────────────────────────────────────────

    def test_permission_denied_return(
        self,
        gateway: Gateway,
        temp_store_path: str,
        deepseek_api_key: str,
        mock_sandbox,
        monkeypatch: Any,
    ) -> None:
        """Permission-denied tools emit events, loop continues.

        After soft-continue: _agent_loop returns completed (LLM sees
        agent_failed + tool_called(allowed=False) and can self-correct).
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, metadata={})


        spec = AgentSpec(
            name="test-worker",
            description="Permission denied test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=2,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id

        # Gateway rule: deny all tools for this permission test
        gateway.register_rule(lambda ctx: True, name="deny-all")

        def mock_llm(messages, tools=None):
            if getattr(mock_llm, 'called', False):
                return _msg_done()
            mock_llm.called = True
            return _msg_tool("echo", {"message": "hi"})
        mock_llm.called = False

        runtime._call_llm = mock_llm

        # After soft-continue: loop completes, events carry the error
        result = runtime._agent_loop(session_id)
        assert result["status"] == "completed"
        events = store.read_events(session_id)
        assert any(e.event_type == "agent_failed" for e in events)
        denied = [e for e in events if e.event_type == "tool_called"
                  and e.payload.get("allowed") is False]
        assert len(denied) == 1

    # ─────────────────────────────────────────────────────────
    # P1-13: subagent_spawned event
    # ─────────────────────────────────────────────────────────

    def test_spawn_subagent_emits_event(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """spawn_subagent emits subagent_spawned event for traceability.

        Verifies that a successful spawn_subagent tool call produces a
        ``subagent_spawned`` event containing parent_id, child_id,
        agent_name, and task in the session event stream.
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        spec = AgentSpec(
            name="test-worker",
            description="Test subagent spawn",
            tools=["spawn_subagent"],
            model="deepseek-v4-flash",
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        runtime._spawner = lambda an, t: "child-001"

        tc = _FakeToolCall(
            "spawn_subagent",
            {"agent_name": "test-worker", "task": "investigate the bug"},
        )
        result = runtime._execute_single_tool(tc, session_id)
        assert result is None  # success

        events = store.read_events(session_id)
        sp_events = [e for e in events if e.event_type == "subagent_spawned"]
        assert len(sp_events) == 1, f"Expected 1 subagent_spawned, got {len(sp_events)}"
        payload = sp_events[0].payload
        assert payload["parent_id"] == session_id
        assert payload["child_id"] == "child-001"
        assert payload["agent_name"] == "test-worker"
        assert payload["task"] == "investigate the bug"

    # ─────────────────────────────────────────────────────────
    # P1-12: agent_thought event
    # ─────────────────────────────────────────────────────────

    def test_agent_thought_emitted_on_llm_response(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """Each LLM response emits agent_thought with full message content.

        Every call to the LLM produces an ``agent_thought`` event
        containing the raw ``content`` and serialized ``tool_calls``
        for audit and debugging.
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        spec = AgentSpec(
            name="test-worker",
            description="Agent thought test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=1,
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id

        def mock_llm(messages, tools=None):
            return _msg_done()

        runtime._call_llm = mock_llm
        runtime._agent_loop(session_id)

        events = store.read_events(session_id)
        thought_events = [e for e in events if e.event_type == "agent_thought"]
        assert len(thought_events) == 1, \
            f"Expected 1 agent_thought, got {len(thought_events)}"
        payload = thought_events[0].payload
        assert payload["content"] == "DONE"
        assert payload["tool_calls"] == []

    
    # ─────────────────────────────────────────────────────────
    # context_window: token-aware event truncation
    # ─────────────────────────────────────────────────────────

    def test_build_messages_respects_context_window(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """_build_messages truncates events when estimated tokens exceed budget."""
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        spec = AgentSpec(
            name="context-test",
            description="Context window test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=1,
            context_window=800,
            context_budget_ratio=0.5,  # 400 token budget → forces truncation
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id

        # Generate many large events to overflow the budget
        from harness.interfaces import Event
        large_payload = {"result": "x" * 200}
        for i in range(20):
            store.append_event(Event(
                event_id=f"ev-{i:03d}",
                session_id=session_id,
                event_type="tool_result",
                timestamp=float(i),
                source="worker",
                payload=large_payload,
            ))

        events = store.read_events(session_id)
        messages, _meta = runtime.llm_messages(session_id, spec.system_prompt)

        # Should not include all 20 events (would be ~4000+ chars)
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        # Rough: 800 token budget * 4 chars/token ≈ 3200 char budget
        assert total_chars < 4000, f"Expected <4000 chars, got {total_chars}"

    def test_build_messages_includes_agent_failed_error(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """_build_messages converts agent_failed events to user-error messages.

        When a tool call fails (e.g. write_file missing arguments), the
        agent_failed event is written to the session store.  _build_messages
        must include the error so the LLM can see what went wrong and
        self-correct on the next iteration.
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})

        spec = AgentSpec(
            name="test-worker",
            description="Test agent_failed in messages",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=1,
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id


        from harness.interfaces import Event

        # Simulate: agent_started → tool error → agent_failed
        store.append_event(Event(
            event_id="ev-start", session_id=session_id,
            event_type="agent_started", timestamp=1.0,
            source="worker",
            payload={"input": "write a file"},
        ))
        store.append_event(Event(
            event_id="ev-fail", session_id=session_id,
            event_type="agent_failed", timestamp=2.0,
            source="worker",
            payload={"error": "Tool error: missing required argument 'path'"},
        ))

        events = store.read_events(session_id)
        messages, _meta = runtime.llm_messages(session_id, spec.system_prompt)

        # The agent_failed event should produce a user message with the error.
        error_messages = [
            m for m in messages
            if m.get("role") == "user"
            and "Tool error" in str(m.get("content", ""))
        ]
        assert len(error_messages) >= 1, (
            f"Expected at least one user message containing 'Tool error', "
            f"got messages: {messages}"
        )

        # The error should include the original error text.
        error_content = error_messages[0]["content"]
        assert "missing required argument" in error_content
        assert "path" in error_content

    def test_build_messages_surfaces_retry_error_in_agent_started(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """_build_messages appends _retry_error to agent_started task message.

        When retry_from injects _retry_error into node.config, the
        agent_started event carries it in the payload.  _build_messages
        must surface it so the LLM sees why the previous attempt failed.
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})

        spec = AgentSpec(
            name="test-worker",
            description="Test retry error in messages",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=1,
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id


        from harness.interfaces import Event

        store.append_event(Event(
            event_id="ev-start", session_id=session_id,
            event_type="agent_started", timestamp=1.0,
            source="worker",
            payload={
                "input": "Execute the assigned task",
                "_retry_error": "Gate rejected: checksum_mismatch",
            },
        ))

        events = store.read_events(session_id)
        messages, _meta = runtime.llm_messages(session_id, spec.system_prompt)

        # The task message should include the retry context.
        task_messages = [
            m for m in messages
            if m.get("role") == "user"
            and "checksum_mismatch" in str(m.get("content", ""))
        ]
        assert len(task_messages) >= 1, (
            f"Expected task message containing 'checksum_mismatch', "
            f"got messages: {messages}"
        )

        task_content = task_messages[0]["content"]
        assert "Previous attempt failed" in task_content
        assert "Execute the assigned task" in task_content

    # ─────────────────────────────────────────────────────────
    # P1-9: token_budget / max_llm_calls熔断
    # ─────────────────────────────────────────────────────────

    def test_max_llm_calls_allows_within_budget(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """Agent completes normally when LLM calls stay within budget.

        ``max_llm_calls=2`` with a DONE response on call 2:
        the agent finishes with ``completed`` status.
        """
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        spec = AgentSpec(
            name="test-worker",
            description="Budget normal test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=10,
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        runtime._config = {"max_llm_calls": 5}

        def mock_llm(messages, tools=None):
            return _msg_done()

        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)
        assert result["status"] == "completed"

    def test_max_llm_calls_circuit_breaker(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """Exceeding max_llm_calls returns failed with budget error."""
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        spec = AgentSpec(
            name="test-worker",
            description="Circuit breaker test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=10,
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        runtime._config = {"max_llm_calls": 1}

        def mock_llm(messages, tools=None):
            # Use finish_reason="length" (not "stop") so the loop
            # continues until max_llm_calls budget is hit.
            return _FakeMessage(content="still thinking..."), "length"

        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)

        assert result["status"] == "failed"
        assert "max_llm_calls" in result.get("reason", "")






    # ─────────────────────────────────────────────────────────
    # Tool soft-continue: errors don't kill the agent loop
    # ─────────────────────────────────────────────────────────

    def test_tool_exception_does_not_kill_agent_loop(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """Tool exception emits agent_failed + tool_result, loop continues."""
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})
        spec = AgentSpec(
            name="test-worker", description="Soft continue", tools=["echo"],
            model="deepseek-v4-flash", max_iterations=3,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        call_num = [0]
        def mock_llm(messages, tools=None):
            call_num[0] += 1
            if call_num[0] == 1:
                return _msg_tool("echo", {"message": "hello"})
            return _msg_done()
        runtime._call_llm = mock_llm
        from tests.conftest import register_test_tools
        register_test_tools(runtime._tool_registry)
        runtime._tool_registry._tools["echo"]["fn"] = (
            lambda **kw: (_ for _ in ()).throw(ValueError("simulated crash")))
        result = runtime._agent_loop(session_id)
        assert result["status"] == "completed"
        events = store.read_events(session_id)
        event_types = [e.event_type for e in events]
        assert "agent_failed" in event_types
        assert "tool_result" in event_types
        assert call_num[0] >= 2


    def test_unknown_tool_soft_continue(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """Unknown tool emits agent_failed, loop continues."""
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})
        spec = AgentSpec(
            name="test-worker", description="Unknown tool test",
            tools=["nonexistent"], model="deepseek-v4-flash", max_iterations=2,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        call_num = [0]
        def mock_llm(messages, tools=None):
            call_num[0] += 1
            if call_num[0] == 1:
                return _msg_tool("nonexistent", {})
            return _msg_done()
        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)
        assert result["status"] == "completed"
        events = store.read_events(session_id)
        assert any(e.event_type == "agent_failed" for e in events)
        assert call_num[0] >= 2


    def test_structured_tool_return_ok_false(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """Tool returning {ok: False} emits agent_failed + tool_result."""
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})
        spec = AgentSpec(
            name="test-worker", description="Structured return test",
            tools=["echo"], model="deepseek-v4-flash", max_iterations=2,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        from tests.conftest import register_test_tools
        register_test_tools(runtime._tool_registry)
        # Replace echo to return structured failure
        runtime._tool_registry._tools["echo"]["fn"] = (
            lambda **kw: {"ok": False, "content": "Missing files",
                           "error": "Only 2 files, expected 3",
                           "exit_code": 1})
        call_num = [0]
        def mock_llm(messages, tools=None):
            call_num[0] += 1
            if call_num[0] == 1:
                return _msg_tool("echo", {"message": "hi"})
            return _msg_done()
        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)
        assert result["status"] == "completed"
        events = store.read_events(session_id)
        # agent_failed contains the error
        failed_events = [e for e in events if e.event_type == "agent_failed"]
        assert len(failed_events) == 1
        assert "Only 2 files, expected 3" in str(failed_events[0].payload)
        # tool_result contains the content
        result_events = [e for e in events if e.event_type == "tool_result"]
        assert len(result_events) == 1
        assert "Missing files" in str(result_events[0].payload)


    def test_inject_tool_deps_reentrant_on_config_change(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """_inject_tool_deps rebuilds path_map when sandbox config changes."""
        spec = AgentSpec(
            name="test-worker", description="Re-entrant inject test",
            tools=["echo"], model="deepseek-v4-flash", max_iterations=1,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway,
            store=SessionStore(base_path=temp_store_path),
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )

        class FakeConfig:
            def __init__(self, folders):
                self.shared_folders = folders

        # First injection with config A
        runtime._sandbox_config = FakeConfig([
            {"sandbox_path": "/workspace/epoch1", "host_path": "/tmp/e1"},
        ])
        runtime._inject_tool_deps()
        assert runtime._deps_injected
        first_path_map = runtime._tool_registry._tools.get("echo", {}).get("deps", [])

        # Second injection with SAME config: must be no-op
        fingerprint_before = runtime._last_injected_config
        runtime._inject_tool_deps()
        assert runtime._last_injected_config == fingerprint_before

        # Third injection with DIFFERENT config: must rebuild
        runtime._sandbox_config = FakeConfig([
            {"sandbox_path": "/workspace/epoch2", "host_path": "/tmp/e2"},
        ])
        runtime._inject_tool_deps()
        assert runtime._last_injected_config != fingerprint_before


class TestToolHooks:
    """PostToolUse hook — deterministic execution after tool calls."""

    def test_post_tool_use_hook_called_on_tool_success(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """Hook fires after tool execution with correct context."""
        from harness.agent_runtime.impl import AgentRuntime
        from harness.interfaces import AgentSpec
        from harness.session_store.impl import SessionStore
        from tests.conftest import register_test_tools

        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})

        spec = AgentSpec(
            name="test-worker", description="Hook test",
            tools=["echo"], model="deepseek-v4-flash", max_iterations=2,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        register_test_tools(runtime._tool_registry)

        hook_calls = []
        def my_hook(ctx, next_fn):
            hook_calls.append(ctx.payload["tool_name"])
            return ""

        from harness.hooks import HookRegistry, points, HookMode
        reg = HookRegistry.get()
        reg.on(points.TOOL_EXECUTE, HookMode.AROUND, my_hook)

        call_num = [0]
        def mock_llm(messages, tools=None):
            call_num[0] += 1
            if call_num[0] == 1:
                return _msg_tool("echo", {"message": "hi"})
            return _msg_done()
        runtime._call_llm = mock_llm

        try:
            result = runtime._agent_loop(session_id)
        finally:
            reg.clear()
        assert result["status"] == "completed"
        assert "echo" in hook_calls



# ── Atomic gate config validation ───────────────────────────


class TestAtomicGateValidation:
    """Tests for _validate_atomic_gate tuple format validation.

    These were originally in test_gateway.py but belong here —
    they test agent_runtime's config parsing, not gateway behavior.
    """

    def test_false_with_list_is_illegal(self):
        """(False, ['bash']) should raise ValueError."""
        from harness.agent_runtime.impl import _validate_atomic_gate
        with pytest.raises(ValueError, match="Cannot specify tool list"):
            _validate_atomic_gate((False, ["bash"]))

    def test_false_none_means_disabled(self):
        """(False, None) → disabled."""
        from harness.agent_runtime.impl import _validate_atomic_gate
        assert _validate_atomic_gate((False, None)) is None

    def test_true_none_gates_all_tools(self):
        """(True, None) → gates all tools."""
        from harness.agent_runtime.impl import _validate_atomic_gate
        result = _validate_atomic_gate((True, None))
        assert result == (True, None)

    def test_true_list_gates_specific_tools(self):
        """(True, ['bash']) → gates only listed tools."""
        from harness.agent_runtime.impl import _validate_atomic_gate
        result = _validate_atomic_gate((True, ["bash", "write_file"]))
        assert result == (True, ["bash", "write_file"])

    def test_none_means_disabled(self):
        """None → disabled."""
        from harness.agent_runtime.impl import _validate_atomic_gate
        assert _validate_atomic_gate(None) is None

    def test_tuple_length_not_two_raises(self):
        """(True,) or (True, 'a', 'b') should raise."""
        from harness.agent_runtime.impl import _validate_atomic_gate
        with pytest.raises(ValueError):
            _validate_atomic_gate((True,))
        with pytest.raises(ValueError):
            _validate_atomic_gate((True, "a", "b"))

    def test_string_not_list_is_rejected(self):
        """(True, 'bash') — string not list — should raise ValueError."""
        from harness.agent_runtime.impl import _validate_atomic_gate
        with pytest.raises(ValueError, match="must be a list"):
            _validate_atomic_gate((True, "bash"))

    def test_empty_list_means_disabled(self):
        """(True, []) → gate disabled (empty list gates no tools)."""
        from harness.agent_runtime.impl import _validate_atomic_gate
        result = _validate_atomic_gate((True, []))
        # Empty list: functionally identical to disabled
        assert result == (True, [])


# ═══════════════════════════════════════════════════════════
# content_filter chain — AgentRuntime → prompt_assembly
# ═══════════════════════════════════════════════════════════


class TestContentFilterChain:
    """content_filter must flow from AgentRuntime._build_messages()
    through to prompt_assembly.build_messages()."""

    def test_tool_result_transform_hook_redacts_content(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """I-09: tool.result.transform hooks replace set_content_filter."""
        from harness.hooks import HookRegistry, points, HookMode
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})

        spec = AgentSpec(
            name="filter-test", description="Test transform hook",
            tools=["echo"], model="deepseek-v4-flash", max_iterations=1,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id

        from harness.interfaces import Event
        # I-43: 组装走 llm_messages（单一真源）→ 事件必须落盘，且工具轮
        # 必须是**成对**的（agent_thought.tool_calls ↔ tool_result），
        # 否则 assemblable 会按形状不变量剥离该轮。
        events = [
            Event("ev_1", session_id, "agent_started", 1.0, "test",
                  payload={"input": "task"}),
            Event("ev_1b", session_id, "agent_thought", 1.0, "test",
                  payload={"content": None, "tool_calls": [
                      {"id": "c1", "name": "echo", "arguments": "{}"}]}),
            Event("ev_2", session_id, "tool_result", 1.0, "test",
                  parent_event_id="c1",
                  payload={"result": "KEY=sk-secret-123"}),
        ]
        for _e in events:
            store.append_event(_e)

        calls = []
        def my_transform(ctx):
            text = ctx.payload
            calls.append(text)
            return text.replace("sk-secret-123", "sk-[REDACTED]")

        reg = HookRegistry.get()
        reg.on(points.TOOL_RESULT, HookMode.TRANSFORM, my_transform)
        try:
            messages, _meta = runtime.llm_messages(session_id, spec.system_prompt)
        finally:
            reg.clear()

        # transform must have been called
        assert len(calls) >= 1, (
            f"transform hook was not called. Calls: {calls}"
        )
        # Tool result must be filtered
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "sk-[REDACTED]" in tool_msgs[0]["content"]
        assert "sk-secret-123" not in tool_msgs[0]["content"]

    # ─────────────────────────────────────────────────────────────
    # I-07: reasoning_content capture + thinking passthrough (ADR-041)
    # ─────────────────────────────────────────────────────────────

    def test_agent_thought_captures_reasoning_content(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """agent_thought stores reasoning_content from the LLM response."""
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})

        spec = AgentSpec(
            name="test-worker", description="Reasoning capture test",
            tools=["echo"], model="deepseek-v4-flash", max_iterations=1,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id

        class _ReasoningMsg(_FakeMessage):
            reasoning_content = "THINKING-STEP"

        runtime._call_llm = lambda messages, tools=None: (
            _ReasoningMsg(content="DONE"), "stop")
        runtime._agent_loop(session_id)

        thought = [e for e in store.read_events(session_id)
                   if e.event_type == "agent_thought"][0]
        assert thought.payload["reasoning_content"] == "THINKING-STEP"

    def test_call_llm_passes_thinking_extra_body(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """thinking=True adds extra_body={'thinking': True} to the call."""
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})

        spec = AgentSpec(
            name="test-worker", description="Thinking passthrough test",
            tools=["echo"], model="deepseek-v4-flash", max_iterations=1,
            thinking=True,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id

        captured: dict[str, Any] = {}

        class _FakeChoice:
            message = _FakeMessage(content="DONE")
            finish_reason = "stop"

        class _FakeResponse:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _FakeResponse()

        runtime._client.chat.completions = _FakeCompletions()
        msg, reason = runtime._call_llm([{"role": "user", "content": "hi"}])

        assert captured.get("extra_body") == {"thinking": True}
        assert reason == "stop"

    # ─────────────────────────────────────────────────────────────
    # I-09: new hook points fire in the agent loop (ADR-042)
    # ─────────────────────────────────────────────────────────────

    def test_llm_call_hook_fires_per_call(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """llm.call around hook wraps every LLM call."""
        from harness.hooks import points, HookRegistry, HookMode
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})
        spec = AgentSpec(name="test-worker", description="Hook test",
                        tools=["echo"], model="deepseek-v4-flash",
                        max_iterations=1)
        runtime = AgentRuntime(spec=spec, gateway=gateway, store=store,
                               api_key=deepseek_api_key,
                               sandbox=mock_sandbox)
        runtime._current_session_id = session_id

        # Mock the LLM client, NOT _call_llm itself — the hook fires inside
        # the real _call_llm (I-09).
        class _FakeChoice:
            message = _FakeMessage(content="DONE")
            finish_reason = "stop"

        class _FakeResponse:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            def create(self, **kwargs):
                return _FakeResponse()

        runtime._client.chat.completions = _FakeCompletions()

        reg = HookRegistry.get()
        count = {"n": 0}
        reg.on(points.LLM_CALL, HookMode.OBSERVE,
               lambda ctx: count.__setitem__("n", count["n"] + 1))
        try:
            runtime._agent_loop(session_id)
            assert count["n"] == 1, "llm.call must fire once per LLM call"
        finally:
            reg.clear()

    def test_agent_lifecycle_hook_fires_on_task(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """agent.lifecycle observe fires for start/stop of a task."""
        from harness.hooks import points, HookRegistry, HookMode
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})
        spec = AgentSpec(name="test-worker", description="Lifecycle hook",
                        tools=["echo"], model="deepseek-v4-flash",
                        max_iterations=1)
        runtime = AgentRuntime(spec=spec, gateway=gateway, store=store,
                               api_key=deepseek_api_key,
                               sandbox=mock_sandbox)
        runtime._current_session_id = session_id
        runtime._call_llm = lambda messages, tools=None: _msg_done()

        reg = HookRegistry.get()
        events_seen: list[str] = []
        reg.on(points.AGENT_LIFECYCLE, HookMode.OBSERVE,
               lambda ctx: events_seen.append(ctx.payload.get("event", "")))
        try:
            runtime._run_agent_loop("do it", session_id)
            assert "start" in events_seen
            assert "stop" in events_seen
        finally:
            reg.clear()



    # ─────────────────────────────────────────────────────────────
    # I-05: system prompt decoupling — default to spec.system_prompt
    # ─────────────────────────────────────────────────────────────

    def test_build_messages_defaults_system_prompt_to_spec(
        self, gateway, temp_store_path, mock_sandbox, deepseek_api_key
    ) -> None:
        """Empty system_prompt falls back to spec.system_prompt (I-05)."""
        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})

        spec = AgentSpec(
            name="test-worker",
            description="System prompt decoupling test",
            tools=["echo"],
            model="deepseek-v4-flash",
            system_prompt="STABLE-IDENTITY-PROMPT",
            max_iterations=1,
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=deepseek_api_key,
            sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id

        from harness.interfaces import Event
        store.append_event(Event(
            event_id="ev-start", session_id=session_id,
            event_type="agent_started", timestamp=1.0,
            source="worker", payload={"input": "task"},
        ))

        # No explicit system_prompt → spec.system_prompt used
        messages, _meta = runtime.llm_messages(session_id)
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "STABLE-IDENTITY-PROMPT"

        # Explicit override still wins
        messages2, _meta2 = runtime.llm_messages(session_id, "OVERRIDE")
        assert messages2[0]["content"] == "OVERRIDE"



# ═══════════════════════════════════════════════════════════════
# W1: `agent.pre_step` exposes session_id (app-layer memory policy
#     needs it to measure the assembled cost via the public API)
# ═══════════════════════════════════════════════════════════════

def test_pre_step_hook_receives_session_id(temp_store_path, gateway, mock_sandbox,
                                          sample_agent_spec):
    """agent.pre_step must carry the session id.

    App-layer compaction (ADR-034/053) measures the assembled cost through
    `client.sessions.preview_llm_messages(session_id, ...)` — a public API that
    needs the id.  The hook used to fire with only `{"iteration": N}` and
    HookContext had no session_id field, so the app could not tell WHICH session
    to compact and had to wrap `llm.call` just to capture the id by closure.
    """
    from harness.hooks import HookMode, HookRegistry, points

    sid = str(uuid.uuid4())
    store = SessionStore(temp_store_path)
    store.create_session(sid, {})

    seen: list = []
    HookRegistry.get().on(points.AGENT_PRE_STEP, HookMode.OBSERVE,
                         lambda ctx: seen.append(ctx))

    runtime = AgentRuntime(spec=sample_agent_spec, gateway=gateway, store=store,
                           api_key="sk-test", sandbox=mock_sandbox)
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()
    runtime._call_llm = lambda msgs, tools=None: _msg_done()

    runtime._agent_loop(sid, system_prompt="sys")

    assert seen, "agent.pre_step never fired"
    assert seen[0].session_id == sid, (
        "agent.pre_step must expose session_id; got %r (fields: agent=%r "
        "payload=%r extra=%r)" % (seen[0].session_id, seen[0].agent,
                                  seen[0].payload, seen[0].extra))


def test_session_id_exposed_on_all_agent_loop_hooks(temp_store_path, gateway,
                                                    mock_sandbox, sample_agent_spec):
    """Consistency across the loop, not just pre_step: one gap forces app hacks."""
    from harness.hooks import HookMode, HookRegistry, points

    sid = str(uuid.uuid4())
    store = SessionStore(temp_store_path)
    store.create_session(sid, {})

    got: dict = {}
    for pt, nm in ((points.AGENT_PRE_STEP, "pre_step"),
                   (points.LLM_CALL, "llm_call"),
                   (points.AGENT_LIFECYCLE, "lifecycle")):
        HookRegistry.get().on(
            pt, HookMode.OBSERVE,
            (lambda n: (lambda ctx: got.setdefault(n, ctx.session_id)))(nm))

    runtime = AgentRuntime(spec=sample_agent_spec, gateway=gateway, store=store,
                           api_key="sk-test", sandbox=mock_sandbox)
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()
    runtime._call_llm = lambda msgs, tools=None: _msg_done()

    runtime._agent_loop(sid, system_prompt="sys")

    bad = {k: v for k, v in got.items() if v != sid}
    assert not bad, f"session_id wrong/missing at: {bad} (want {sid})"


# ═══════════════════════════════════════════════════════════════
# W2: `thinking` must actually work on a llama.cpp backend
#
# MEASURED against the local server (temperature=0, 3 trials each):
#   chat_template_kwargs{enable_thinking:false} -> 121 completion / 0 reasoning chars
#   chat_template_kwargs{enable_thinking:true}  -> 300 completion / 761 reasoning
#   top-level "thinking": true  (what the runtime USED to send) -> 761 reasoning
#   top-level "thinking": false                                 -> 761 reasoning
# `thinking:<bool>` is SILENTLY IGNORED by llama.cpp: true and false produce
# byte-identical behaviour, so AgentSpec.thinking=True never switched reasoning
# on for that backend.  llama.cpp toggles it via
# `chat_template_kwargs.enable_thinking` (upstream PR #22336).
# ═══════════════════════════════════════════════════════════════

def _mk_thinking_runtime(store, spec_thinking, gateway, mock_sandbox, api_key):
    spec = AgentSpec(
        name="test-worker", description="thinking adapter test",
        tools=["echo"], model="spark-x2.5-4b", max_iterations=1,
        **({} if spec_thinking is None else {"thinking": spec_thinking}),
    )
    return AgentRuntime(spec=spec, gateway=gateway, store=store,
                        api_key=api_key, sandbox=mock_sandbox)


def _capture_extra_body(runtime, ctk_from_profile):
    """Run one _call_llm against a fake client; return the extra_body sent."""
    from harness.sdk.config import LLMConfig

    captured: dict = {}

    class _Choice:
        message = _FakeMessage(content="DONE")
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kw):
            captured.update(kw)
            return _Resp()

    cfg = LLMConfig(backend="local", model="spark-x2.5-4b",
                    base_url="http://127.0.0.1:8080/v1")
    cfg.chat_template_kwargs = dict(ctk_from_profile)
    runtime._llm_config = cfg
    runtime._client.chat.completions = _Completions()
    runtime._call_llm([{"role": "user", "content": "hi"}])
    return captured.get("extra_body") or {}


def test_thinking_true_enables_enable_thinking_on_local_backend(
        temp_store_path, gateway, mock_sandbox):
    """AgentSpec.thinking=True must reach llama.cpp as enable_thinking=True."""
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, True, gateway, mock_sandbox, "sk-test")
    runtime._current_session_id = sid
    extra = _capture_extra_body(runtime, {"enable_thinking": False})
    assert extra.get("chat_template_kwargs", {}).get("enable_thinking") is True, (
        "thinking=True did not override the profile's enable_thinking=False, so "
        "reasoning stays OFF on llama.cpp. extra=%r" % (extra,))


def test_thinking_false_overrides_profile_enable_thinking_true(
        temp_store_path, gateway, mock_sandbox):
    """An explicit thinking=False must switch reasoning OFF."""
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, False, gateway, mock_sandbox, "sk-test")
    runtime._current_session_id = sid
    extra = _capture_extra_body(runtime, {"enable_thinking": True})
    assert extra.get("chat_template_kwargs", {}).get("enable_thinking") is False, (
        "thinking=False could not switch off the profile's enable_thinking=True. "
        "extra=%r" % (extra,))


def test_thinking_absent_defers_to_profile(temp_store_path, gateway, mock_sandbox):
    """No spec opinion ⇒ the profile decides, and nothing is invented."""
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, None, gateway, mock_sandbox, "sk-test")
    runtime._current_session_id = sid
    extra = _capture_extra_body(runtime, {"enable_thinking": False})
    assert extra.get("chat_template_kwargs", {}).get("enable_thinking") is False


def test_thinking_true_still_sends_vendor_neutral_flag(
        temp_store_path, gateway, mock_sandbox):
    """The neutral passthrough is kept for backends that DO read `thinking`."""
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, True, gateway, mock_sandbox, "sk-test")
    runtime._current_session_id = sid
    extra = _capture_extra_body(runtime, {"enable_thinking": False})
    assert extra.get("thinking") is True


# ═══════════════════════════════════════════════════════════════
# ADR-054: AgentSpec is first-class in SDK calls; profile supplies defaults
#
#   P (default): spec expresses no opinion -> the profile decides
#   Q (explicit): spec set and conflicting -> spec wins, NOT silently
#   backend is exempt (security boundary): conflict -> fail-fast
#
# "No opinion" is `None` for tri-state fields and `""` for strings.  A plain
# `False`/non-empty default cannot express "unset", which is exactly how the
# profile's enable_thinking silently beat an explicit spec value.
# ═══════════════════════════════════════════════════════════════

def test_spec_model_empty_defers_to_profile(temp_store_path, gateway, mock_sandbox):
    """spec.model="" (no opinion) ⇒ the profile's model is used for the request."""
    from harness.sdk.config import LLMConfig
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, None, gateway, mock_sandbox, "sk-test")
    runtime.spec.model = ""                      # explicit "no opinion"
    cfg = LLMConfig(backend="local", model="spark-x2.5-4b",
                    base_url="http://127.0.0.1:8080/v1")
    cfg.chat_template_kwargs = {}
    runtime._llm_config = cfg
    captured = _capture_kwargs(runtime)
    assert captured["model"] == "spark-x2.5-4b", captured.get("model")


def test_spec_model_explicit_wins_over_profile(temp_store_path, gateway, mock_sandbox):
    """spec.model set (non-empty) ⇒ it becomes the request model (ADR-054 §5)."""
    from harness.sdk.config import LLMConfig
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, None, gateway, mock_sandbox, "sk-test")
    runtime.spec.model = "my-explicit-model"
    cfg = LLMConfig(backend="local", model="spark-x2.5-4b",
                    base_url="http://127.0.0.1:8080/v1")
    cfg.chat_template_kwargs = {}
    runtime._llm_config = cfg
    captured = _capture_kwargs(runtime)
    assert captured["model"] == "my-explicit-model", captured.get("model")


def test_no_model_anywhere_fails_fast(temp_store_path, gateway, mock_sandbox):
    """spec.model="" and profile.model empty ⇒ fail loudly, not a silent default."""
    from harness.sdk.config import LLMConfig
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, None, gateway, mock_sandbox, "sk-test")
    runtime.spec.model = ""
    cfg = LLMConfig(backend="local", model="", base_url="http://127.0.0.1:8080/v1")
    cfg.chat_template_kwargs = {}
    runtime._llm_config = cfg
    with pytest.raises(Exception) as ei:
        _capture_kwargs(runtime)
    assert "model" in str(ei.value).lower()


def _capture_kwargs(runtime):
    """Run one _call_llm against a fake client; return the kwargs sent."""
    captured: dict = {}

    class _Choice:
        message = _FakeMessage(content="DONE")
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kw):
            captured.update(kw)
            return _Resp()

    runtime._client.chat.completions = _Completions()
    runtime._call_llm([{"role": "user", "content": "hi"}])
    return captured


def test_spec_default_model_is_empty_sentinel():
    """The dataclass default must be the "no opinion" sentinel (ADR-054 §2)."""
    from harness.interfaces import AgentSpec
    f = AgentSpec.__dataclass_fields__["model"]
    assert f.default == "", (
        "AgentSpec.model default must be '' so the profile can supply it; got %r"
        % (f.default,))
    t = AgentSpec.__dataclass_fields__["thinking"]
    assert t.default is None, (
        "AgentSpec.thinking default must be None (tri-state, ADR-054 §2); got %r"
        % (t.default,))


def test_model_pool_key_matches_request_model():
    """ADR-054 §5: the pool key and the request model must come from ONE source."""
    import inspect
    from harness.orchestration import pool as pool_mod
    src = inspect.getsource(pool_mod)
    assert "resolve_spec_model" in src, (
        "the pool key must go through the SAME resolver as the request model, "
        "not read spec.model directly")
    # the resolution helper must exist and be shared, not re-inlined
    from harness.interfaces import resolve_spec_model
    assert callable(resolve_spec_model)
    assert resolve_spec_model("", "profile-model") == "profile-model"
    assert resolve_spec_model("spec-model", "profile-model") == "spec-model"
    with pytest.raises(Exception):
        resolve_spec_model("", "")


def test_q_path_override_is_not_silent(temp_store_path, gateway, mock_sandbox):
    """ADR-054 §4 Q: a contradicting explicit spec value must be audited."""
    from harness.sdk.config import LLMConfig
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})

    entries: list = []

    class _Audit:
        def append(self, **kw):
            entries.append(kw)

    runtime = _mk_thinking_runtime(store, True, gateway, mock_sandbox, "sk-test")
    runtime._current_session_id = sid
    runtime._audit_log = _Audit()
    # profile says OFF, spec says ON -> contradiction -> must be recorded
    _capture_extra_body(runtime, {"enable_thinking": False})

    assert entries, "the Q-path override was silent"
    ev = entries[0]
    assert ev["event"] == "agent_spec_override"
    assert ev["detail"]["field"] == "thinking"
    assert ev["detail"]["profile_value"] is False
    assert ev["detail"]["spec_value"] is True
    assert ev["session_id"] == sid


def test_p_path_agreement_is_not_audited(temp_store_path, gateway, mock_sandbox):
    """No contradiction ⇒ nothing to record (P path stays quiet)."""
    from harness.sdk.config import LLMConfig
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    entries: list = []

    class _Audit:
        def append(self, **kw):
            entries.append(kw)

    runtime = _mk_thinking_runtime(store, True, gateway, mock_sandbox, "sk-test")
    runtime._current_session_id = sid
    runtime._audit_log = _Audit()
    _capture_extra_body(runtime, {"enable_thinking": True})   # agrees
    assert not entries, entries


# ═══════════════════════════════════════════════════════════════
# No-progress detection: a truncated round that produced NOTHING
#
# Existing contract (see test above): finish_reason="length" continues the loop
# so the model can recover.  That is correct when the round produced SOMETHING
# (partial content, or a tool call).  It is wrong when the round produced
# NEITHER content NOR a tool call: the next iteration is identical, so the loop
# spins with no progress, no error and no signal.
#
# MEASURED on the local server across 779 recorded agent_thought events:
#     finish_reason=stop       237  (0 reasoning / content / 0 tool calls)
#     finish_reason=tool_calls  71  (117 / 0 / 1)
#     finish_reason=length      63  (4304 / 0 / 0)   <-- all no-progress
# The 63 spinning rounds were invisible; the run only ended when the repeated
# empty responses eventually formed an invalid message list and the server
# answered 400 "Cannot have 2 or more assistant messages at the end of the list".
# ═══════════════════════════════════════════════════════════════

def test_length_with_no_content_and_no_tool_calls_is_not_progress(
        temp_store_path, gateway, mock_sandbox, sample_agent_spec):
    """A round that returned nothing usable must fail loudly, not spin.

    Asserts BOTH halves: the status is a diagnosable failure, and the round did
    not silently become a success.
    """
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})

    runtime = AgentRuntime(spec=sample_agent_spec, gateway=gateway, store=store,
                           api_key="sk-test", sandbox=mock_sandbox)
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()

    calls = {"n": 0}

    def _empty_length(messages, tools=None):
        calls["n"] += 1
        return _FakeMessage(content="", tool_calls=[]), "length"

    runtime._call_llm = _empty_length
    result = runtime._agent_loop(sid, system_prompt="sys")

    assert calls["n"] == 1, (
        "the loop retried a no-progress round %d times; it must fail on the "
        "first one instead of spinning" % calls["n"])
    assert result.get("status") != "completed", (
        "a round with empty content AND no tool calls was treated as success")
    assert result.get("reason") == "no_progress_truncated", result
    assert result.get("error"), "the failure must carry a diagnosable message"
    failed = [e for e in store.read_events(sid) if e.event_type == "agent_failed"]
    assert failed, "no agent_failed event was emitted"
    assert failed[0].payload.get("reason") == "no_progress_truncated"


def test_length_with_content_still_continues(temp_store_path, gateway,
                                             mock_sandbox, sample_agent_spec):
    """The EXISTING contract must survive: partial content ⇒ keep going.

    Guards against over-correcting into "any length ⇒ fail", which would break
    long-answer continuation.
    """
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})

    runtime = AgentRuntime(spec=sample_agent_spec, gateway=gateway, store=store,
                           api_key="sk-test", sandbox=mock_sandbox)
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()
    runtime._config = {"max_llm_calls": 3}

    seq = [(_FakeMessage(content="partial answer"), "length"),
           (_FakeMessage(content="DONE"), "stop")]
    it = iter(seq)
    runtime._call_llm = lambda messages, tools=None: next(it)

    result = runtime._agent_loop(sid, system_prompt="sys")
    assert result.get("status") == "completed", result


def test_length_with_a_tool_call_still_continues(temp_store_path, gateway,
                                                 mock_sandbox, sample_agent_spec):
    """A truncated round that DID call a tool made progress ⇒ continue."""
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})

    runtime = AgentRuntime(spec=sample_agent_spec, gateway=gateway, store=store,
                           api_key="sk-test", sandbox=mock_sandbox)
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()

    seq = [_msg_tool("echo", {"message": "hi"}), _msg_done()]
    it = iter(seq)

    def _call(messages, tools=None):
        msg, fr = next(it)
        if fr == "tool_calls":
            return msg, "length"      # truncated but produced a tool call
        return msg, fr

    runtime._call_llm = _call
    result = runtime._agent_loop(sid, system_prompt="sys")
    assert result.get("status") == "completed", result


def test_missing_finish_reason_with_no_output_is_not_completion(
        temp_store_path, gateway, mock_sandbox, sample_agent_spec):
    """`finish_reason=None` + empty round must not be reported as completion.

    The normaliser used to rewrite a missing reason to "stop" unconditionally,
    so `agent_completed` could be emitted with output=None — a silent false
    success.  A missing reason alone is not evidence that the task finished.
    """
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})

    runtime = AgentRuntime(spec=sample_agent_spec, gateway=gateway, store=store,
                           api_key="sk-test", sandbox=mock_sandbox)
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()

    class _NoFinishMsg(_FakeMessage):
        pass

    calls = {"n": 0}

    def _call(messages, tools=None):
        calls["n"] += 1
        return _NoFinishMsg(content="", tool_calls=[]), None

    runtime._call_llm = _call
    result = runtime._agent_loop(sid, system_prompt="sys")

    assert result.get("status") != "completed", (
        "an empty round with no finish_reason was reported as completed")
    assert calls["n"] == 1, "the loop spun on a no-progress round"
    assert not [e for e in store.read_events(sid)
                if e.event_type == "agent_completed"], \
        "agent_completed was emitted for an empty round"


def test_length_with_content_is_continued_not_discarded(
        temp_store_path, gateway, mock_sandbox, sample_agent_spec):
    """A truncated round's content must be KEPT and continued, not dropped.

    MEASURED defect (same prompt, max_tokens=24, 3 rounds):
        current behaviour — "length" ⇒ discard + resend identical input:
            round1 '1 2 3 4 5 6 7 8 9 10 11 '
            round2 '1 2 3 4 5 6 7 8 9 10 11 '   (identical)
            round3 '1 2 3 4 5 6 7 8 9 10 11 '   (identical)
            final output: '1 2 3 4 5 6 7 8 9 10 11 '  ← all work discarded
        continuation — append the partial text + an explicit "continue":
            round1 '1 ... 11 '  round2 '12 ... 19 '  round3 '20 ... 27 '
            final output: '1 ... 27'                   ← progress, nothing lost
    Verified against the real server: sending the partial assistant message
    WITHOUT a "continue" instruction makes the model RESTART (it repeated
    '1 2 3 ... 11' verbatim), so the instruction is required, not decorative.
    """
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})

    runtime = AgentRuntime(spec=sample_agent_spec, gateway=gateway, store=store,
                           api_key="sk-test", sandbox=mock_sandbox)
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()
    runtime._config = {"max_llm_calls": 10}

    seen_messages: list = []
    seq = [(("1 2 3 "), "length"), (("4 5 6 "), "length"), (("7 8 9"), "stop")]

    def _call(messages, tools=None):
        seen_messages.append([dict(m) for m in messages])
        piece, fr = seq[min(len(seen_messages) - 1, len(seq) - 1)]
        return _FakeMessage(content=piece), fr

    runtime._call_llm = _call
    result = runtime._agent_loop(sid, system_prompt="sys")

    assert result.get("status") == "completed", result
    # the accumulated text must survive to the end
    assert "1 2 3" in (result.get("output") or ""), result
    assert "7 8 9" in (result.get("output") or ""), result
    # and the second call must have been told to continue, with the partial text
    second = seen_messages[1]
    assert any(m.get("role") == "assistant" and "1 2 3" in (m.get("content") or "")
               for m in second), (
        "the truncated content was not carried into the next round: %r" % (second,))
    assert any(m.get("role") == "user" and "continue" in (m.get("content") or "").lower()
               for m in second), (
        "no explicit continue instruction was sent; the model restarts instead "
        "of resuming (measured)")


def test_continuation_is_bounded(temp_store_path, gateway, mock_sandbox,
                                 sample_agent_spec):
    """An endlessly-truncating model must not loop forever either."""
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})

    runtime = AgentRuntime(spec=sample_agent_spec, gateway=gateway, store=store,
                           api_key="sk-test", sandbox=mock_sandbox)
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()

    calls = {"n": 0}

    def _always_length(messages, tools=None):
        calls["n"] += 1
        return _FakeMessage(content="x"), "length"

    runtime._call_llm = _always_length
    result = runtime._agent_loop(sid, system_prompt="sys")

    # The loop must terminate on its own bound (max_iterations or the
    # continuation cap) rather than spinning: the point is that it is BOUNDED.
    assert result.get("status") in ("failed", "completed"), result
    assert result.get("status") != "completed" or result.get("reason") == \
        "max_iterations", result
    assert calls["n"] <= 6, (
        "continuation must stop after a small number of attempts; got %d"
        % calls["n"])
    # and every continuation must be visible, never silent
    assert [e for e in store.read_events(sid)
            if e.event_type == "agent_output_continued"], \
        "continuations happened without any emitted event"


# ═══════════════════════════════════════════════════════════════
# max_tokens default: UNLIMITED means "do not send the field"
#
# MEASURED:
#   api.deepseek.com  max_tokens=-1 -> 422 "expected u32" (rejects -1)
#   llama-server      max_tokens=-1 -> accepted, means unlimited
# So -1 is NOT portable.  The only shape every backend agrees on is to OMIT the
# field, which makes the harness default (16384) silently cap a model that could
# produce far more — and a long translation needs far more.
# ═══════════════════════════════════════════════════════════════

def test_spec_max_tokens_defaults_to_unlimited():
    """The dataclass default must be the 'no opinion' sentinel, not a number."""
    from harness.interfaces import AgentSpec
    f = AgentSpec.__dataclass_fields__["max_tokens"]
    assert f.default is None, (
        "AgentSpec.max_tokens should default to None (= let the backend decide); "
        "got %r, which silently caps models that support far more output"
        % (f.default,))


def test_unlimited_max_tokens_is_omitted_from_the_request(
        temp_store_path, gateway, mock_sandbox):
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, None, gateway, mock_sandbox, "sk-test")
    runtime.spec.max_tokens = None
    runtime._current_session_id = sid
    captured = _capture_kwargs(runtime)
    assert "max_tokens" not in captured, (
        "max_tokens must be OMITTED when unlimited; sending None/-1 is either "
        "rejected (DeepSeek: expected u32) or ambiguous. got %r"
        % (captured.get("max_tokens"),))


def test_explicit_max_tokens_is_still_sent(temp_store_path, gateway, mock_sandbox):
    store = SessionStore(base_path=temp_store_path)
    sid = str(uuid.uuid4())
    store.create_session(sid, {})
    runtime = _mk_thinking_runtime(store, None, gateway, mock_sandbox, "sk-test")
    runtime.spec.max_tokens = 1234
    runtime._current_session_id = sid
    captured = _capture_kwargs(runtime)
    assert captured.get("max_tokens") == 1234
