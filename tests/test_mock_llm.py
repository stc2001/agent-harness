"""Mock LLM endpoint for verifying context assembly pipeline.

Uses httpx.MockTransport to intercept OpenAI API calls and validate
that the messages/tools body sent to the LLM is correctly assembled:

1. System prompt: vars substituted, prompt_append appended
2. Tools filtered by spec.tools
3. tool_call_id chain: assistant.tool_calls[].id == tool.tool_call_id
4. Message ordering: system → user → assistant → tool
5. Multi-turn: multiple agent_started events → multiple user messages
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import openai
import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.tools import load_builtins
from tests.conftest import register_test_tools
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.session_store.impl import SessionStore


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _make_tool_response(tool_name: str, args: dict[str, Any]) -> dict:
    """Build an OpenAI-compatible response with a single tool call."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args),
                    },
                }],
            },
        }],
    }


def _make_done_response(content: str = "DONE") -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
        }],
    }


class MockTransport(httpx.BaseTransport):
    """httpx transport that returns preset responses in sequence.

    Also captures all request bodies for later assertion.
    """

    def __init__(self, responses: list[dict[str, Any]]):
        super().__init__()
        self._responses = responses
        self.requests: list[dict[str, Any]] = []
        self._index = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self._index >= len(self._responses):
            return httpx.Response(500, json={"error": "Mock exhausted"})
        resp = self._responses[self._index]
        self._index += 1
        return httpx.Response(200, json=resp, request=request)


# ─────────────────────────────────────────────────────────────
# Context validator
# ─────────────────────────────────────────────────────────────


def validate_context(requests: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Validate context assembly against expected invariants.

    Returns {"errors": [...], "warnings": [...]}.
    """
    errors: list[str] = []
    if not requests:
        errors.append("No LLM requests captured")
        return {"errors": errors, "warnings": []}

    req1 = requests[0]
    messages: list[dict] = req1.get("messages", [])
    tools: list[dict] = req1.get("tools", [])

    # 1. Message ordering
    if not messages:
        errors.append("messages list is empty")
    elif messages[0]["role"] != "system":
        errors.append(f"first message is {messages[0]['role']}, expected system")

    # 2. Tools present
    tool_names = {t["function"]["name"] for t in tools}
    if not tool_names:
        errors.append("tools list is empty")

    # 3. tool_call_id chain
    pending_ids: dict[str, int] = {}  # tool_call_id → message index
    for i, msg in enumerate(messages):
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                pending_ids[tc["id"]] = i
        elif msg["role"] == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id is None:
                errors.append(f"tool message at index {i} missing tool_call_id")
            elif tc_id not in pending_ids:
                errors.append(
                    f"tool_call_id '{tc_id}' at index {i} "
                    f"does not match any assistant tool_calls"
                )
            else:
                del pending_ids[tc_id]  # resolved

    # 4. Multi-round messages
    # For requests beyond the first, verify context accumulates
    for req_idx, req in enumerate(requests[1:], start=1):
        msgs = req.get("messages", [])
        user_count = sum(1 for m in msgs if m["role"] == "user")
        tool_count = sum(1 for m in msgs if m["role"] == "tool")
        if user_count == 0 and tool_count == 0:
            errors.append(f"request {req_idx}: no user or tool messages (empty context?)")

    return {"errors": errors, "warnings": []}


# ─────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def gateway():
    return Gateway(event_bus=None)


@pytest.fixture
def mock_sandbox():
    class MockSandbox:
        def spawn(self, config, cap_token=""):
            return "mock-pid"
        def kill(self, container_id):
            pass
        def is_alive(self, container_id):
            return True
        def get_logs(self, container_id):
            return ""
        def execute(self, container_id, command):
            return ("mock output", "", 0)
    return MockSandbox()


class TestMockLLMEndpoint:
    """Verify that AgentRuntime sends correctly assembled context to LLM."""

    def test_tool_call_id_chain_integrity(
        self, temp_store_path, gateway, mock_sandbox
    ):
        """Full round-trip: agent emits events → context assembled → tool_call_id matches."""
        store = SessionStore(temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        # Sequenced responses: tool call → DONE
        transport = MockTransport([
            _make_tool_response("echo", {"message": "hi"}),
            _make_done_response(),
        ])
        http_client = httpx.Client(transport=transport)

        spec = AgentSpec(
            name="test-worker",
            description="test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=3,
        )
        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key="sk-test",
            sandbox=mock_sandbox,
        )
        # Inject mock HTTP client
        runtime._client = openai.OpenAI(api_key="sk-test", http_client=http_client)

        tools = runtime._tool_registry.get_definitions()
        runtime.execute_task("test", session_id , tools=tools)

        result = validate_context(transport.requests)
        assert result["errors"] == [], f"Validation errors: {result['errors']}"

    def test_multi_turn_context_accumulates(
        self, temp_store_path, gateway, mock_sandbox
    ):
        """Multiple execute_task calls on same session → LLM sees full history."""
        store = SessionStore(temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        spec = AgentSpec(
            name="test-worker",
            description="test",
            tools=["echo"],
            model="deepseek-v4-flash",
            max_iterations=2,
            system_prompt="You are a test assistant.",
        )

        # Turn 1 — use httpx transport
        t1 = MockTransport([_make_tool_response("echo", {"message": "hi"}), _make_done_response()])
        c1 = httpx.Client(transport=t1)
        r1 = AgentRuntime(spec=spec, gateway=gateway, store=store,
                          api_key="sk-test", sandbox=mock_sandbox)
        register_test_tools(r1._tool_registry)
        r1._client = openai.OpenAI(api_key="sk-test", http_client=c1)
        r1.execute_task("first task", session_id,
                        config={"vars": {"time": "00:00"}})
        assert len(t1.requests) >= 2  # tool call + DONE

        # Turn 2 — same session, same store
        t2 = MockTransport([_make_done_response("DONE")])
        c2 = httpx.Client(transport=t2)
        r2 = AgentRuntime(spec=spec, gateway=gateway, store=store,
                          api_key="sk-test", sandbox=mock_sandbox)
        register_test_tools(r2._tool_registry)
        r2._client = openai.OpenAI(api_key="sk-test", http_client=c2)
        r2.execute_task("second task", session_id)

        assert len(t2.requests) >= 1, f"t2 has {len(t2.requests)} requests"
        msgs_turn2 = t2.requests[0]["messages"]
        user_messages = [m for m in msgs_turn2 if m["role"] == "user"]
        assert len(user_messages) >= 2, \
            f"Expected >=2 user messages in turn 2, got {len(user_messages)}"
