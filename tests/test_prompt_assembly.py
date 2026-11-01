"""TDD: prompt_assembly content_filter — filter sensitive values from LLM context.

Framework provides the callback slot (content_filter parameter).
APP defines the filtering logic (e.g., redact API keys).
"""

from __future__ import annotations

from harness.prompt_assembly import build_messages


# ── Mock APP-layer filter ──────────────────────────────────


def redact_secrets(text: str) -> str:
    """APP-layer filter: replace secret-looking values with [REDACTED]."""
    import re
    return re.sub(
        r'\b(sk-|ghp_|hf_)[a-zA-Z0-9_\-]+',
        r'\1[REDACTED]',
        text,
    )


# ── Helpers ────────────────────────────────────────────────


def _make_event(event_type: str, extra: dict | None = None,
                parent_event_id: str | None = None):
    """Create a minimal event-like object for build_messages testing."""
    payload = dict(extra or {})
    return type("_Event", (), {
        "event_type": event_type,
        "event_id": "ev_001",
        "payload": payload,
        "source": "test",
        "parent_event_id": parent_event_id,
    })()


# ═══════════════════════════════════════════════════════════


class TestContentFilter:
    """build_messages must invoke content_filter on tool_result content."""

    def test_content_filter_redacts_secrets_in_tool_result(self):
        """APP-layer content_filter should redact API keys from tool results."""
        # ADR-053: a tool_result needs its issuing assistant, otherwise it is an
        # un-sendable orphan (provider 400).  Fixture made well-formed.
        events = [
            _make_event("agent_started", {"input": "do thing"}),
            _make_event("agent_thought", {
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "bash",
                                "arguments": "{}"}],
            }),
            _make_event("tool_result", {
                "result": "OPENAI_API_KEY=sk-abc123def456 GITHUB_TOKEN=ghp_secret_xxx PATH=/bin",
            }, parent_event_id="call_1"),
        ]
        messages = build_messages(
            system_prompt="test",
            events=events,
            content_filter=redact_secrets,
        )

        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        content = tool_msgs[0]["content"]

        # tool_call_id must equal parent_event_id
        assert tool_msgs[0]["tool_call_id"] == "call_1", (
            f"Expected tool_call_id='call_1' (from parent_event_id), "
            f"got {tool_msgs[0]['tool_call_id']}"
        )

        # Values redacted
        assert "sk-abc123def456" not in content
        assert "ghp_secret_xxx" not in content
        # Key names and safe values preserved
        assert "OPENAI_API_KEY" in content
        assert "GITHUB_TOKEN" in content
        assert "PATH=/bin" in content
        assert "[REDACTED]" in content

    def test_content_filter_passthrough_when_none(self):
        """Without a filter, tool_result content passes through unchanged."""
        events = [
            _make_event("agent_started", {"input": "do thing"}),
            _make_event("agent_thought", {
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "bash",
                                "arguments": "{}"}],
            }),
            _make_event("tool_result", {
                "result": "OPENAI_API_KEY=sk-abc123def456 PATH=/bin",
            }, parent_event_id="call_1"),
        ]
        messages = build_messages(system_prompt="test", events=events)

        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        content = tool_msgs[0]["content"]
        assert "sk-abc123def456" in content  # not redacted
        assert "PATH=/bin" in content


# ─────────────────────────────────────────────────────────────
# I-04: task anchor must survive truncation (ADR-034 Rev2 R1/R2)
# ─────────────────────────────────────────────────────────────


class TestTaskAnchor:
    """agent_started (task message) must never be truncated away."""

    @staticmethod
    def _long_session(n_tool_rounds: int = 40) -> list:
        events = [_make_event("agent_started", {"input": "THE-TASK"})]
        for i in range(n_tool_rounds):
            events.append(_make_event(
                "tool_called",
                {"tool": "read_file", "args": {"path": f"/f/{i}"}},
            ))
            events.append(_make_event(
                "tool_result",
                {"result": f"content-of-file-{i} " * 10},
            ))
        return events

    def test_task_message_present_under_tiny_budget(self):
        """Even with a very small budget the task message survives."""
        events = self._long_session()
        messages = build_messages(
            "sys", events,
            context_window=4000, budget_ratio=0.2,
        )
        roles = [m["role"] for m in messages]
        assert "user" in roles
        task_msgs = [m for m in messages
                     if m["role"] == "user" and m.get("content", "").startswith("Task:")]
        assert task_msgs, "task message must survive truncation"
        assert "THE-TASK" in task_msgs[0]["content"]

    def test_task_message_first_after_system(self):
        """Task anchor sits immediately after system, before other content."""
        events = self._long_session(5)
        messages = build_messages("sys", events, context_window=4000, budget_ratio=0.2)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"].startswith("Task:")

    def test_prefix_stable_across_iterations(self):
        """system + task prefix must not change as history grows."""
        # iteration 1: short history
        m1 = build_messages("sys", self._long_session(2),
                            context_window=4000, budget_ratio=0.2)
        # iteration 2: long history (truncation kicks in)
        m2 = build_messages("sys", self._long_session(40),
                            context_window=4000, budget_ratio=0.2)
        # same system + same task at the front
        assert m1[0] == m2[0]
        assert m1[1] == m2[1]

    def test_oversized_tool_result_truncated(self):
        """I-06: build_messages truncates oversized tool_result (fallback)."""
        events = [
            _make_event("agent_started", {"input": "do thing"}),
            # ADR-053: issuing assistant required (see the sibling test above).
            _make_event("agent_thought", {
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "bash",
                                "arguments": "{}"}],
            }),
            _make_event("tool_result",
                        {"result": "huge-data-" + "z" * 20000},
                        parent_event_id="call_1"),
        ]
        messages = build_messages("sys", events)
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert "[... truncated" in tool_msgs[0]["content"]
        assert len(tool_msgs[0]["content"]) < 20000

    def test_oversized_tool_result_fallback_preserves_head_marker(self):
        """I-06: fallback marker reports truncated chars."""
        events = [
            _make_event("agent_started", {"input": "do thing"}),
            # ADR-053: the issuing assistant is required for a legal chain.
            _make_event("agent_thought", {
                "content": "",
                "tool_calls": [{"id": "call_2", "name": "bash",
                                "arguments": "{}"}],
            }),
            _make_event("tool_result", {"result": "abc" * 5000},
                        parent_event_id="call_2"),
        ]
        messages = build_messages("sys", events)
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        marker = tool_msgs[0]["content"]
        assert "15000" in marker or "truncated" in marker

    def test_memory_slot_after_task_anchor(self):
        """`slot="after_task"` lands immediately after the Task anchor."""
        events = [
            _make_event("agent_started", {"input": "THE-TASK"}),
            _make_event("memory_summary", {"content": "SUMMARY",
                                           "slot": "after_task"}),
        ]
        messages = build_messages("sys", events)
        assert messages[0]["role"] == "system"
        assert messages[1]["content"].startswith("Task:")
        assert messages[2]["content"] == "<mem>SUMMARY</mem>"

    def test_memory_slot_before_task_anchor(self):
        """`slot="before_task"` lands immediately before the Task anchor."""
        events = [
            _make_event("agent_started", {"input": "THE-TASK"}),
            _make_event("memory_summary", {"content": "OLDER",
                                           "slot": "before_task"}),
        ]
        messages = build_messages("sys", events)
        assert messages[0]["role"] == "system"
        assert messages[1]["content"] == "<mem>OLDER</mem>"
        assert messages[2]["content"].startswith("Task:")

# ─────────────────────────────────────────────────────────────
# I-07: assistant message serialization (ADR-041)
# ─────────────────────────────────────────────────────────────


class TestAssistantSerialization:
    """agent_thought / agent_completed replay as assistant messages."""

    def test_agent_thought_with_tool_calls_and_reasoning(self):
        """Tool-call round WITH reasoning carries reasoning_content."""
        events = [
            _make_event("agent_started", {"input": "task"}),
            _make_event("agent_thought", {
                "content": None,
                "reasoning_content": "THINKING-STEPS",
                "tool_calls": [{"id": "call_1", "name": "bash",
                                "arguments": "{}"}],
            }),
            _make_event("tool_result", {"result": "out"},
                        parent_event_id="call_1"),
        ]
        messages = build_messages("sys", events)
        asst = [m for m in messages if m["role"] == "assistant"]
        assert len(asst) == 1
        assert asst[0].get("reasoning_content") == "THINKING-STEPS"
        assert asst[0]["tool_calls"][0]["id"] == "call_1"

    def test_agent_thought_with_tool_calls_no_reasoning(self):
        """Tool-call round WITHOUT reasoning omits reasoning_content."""
        events = [
            _make_event("agent_started", {"input": "task"}),
            _make_event("agent_thought", {
                "content": None,
                "tool_calls": [{"id": "call_2", "name": "bash",
                                "arguments": "{}"}],
            }),
        ]
        messages = build_messages("sys", events)
        asst = [m for m in messages if m["role"] == "assistant"]
        assert len(asst) == 1
        assert "reasoning_content" not in asst[0]

    def test_agent_thought_plain_text(self):
        """Plain-text round becomes a content-only assistant message."""
        events = [
            _make_event("agent_started", {"input": "task"}),
            _make_event("agent_thought", {"content": "I will check."}),
        ]
        messages = build_messages("sys", events)
        asst = [m for m in messages if m["role"] == "assistant"]
        assert len(asst) == 1
        assert asst[0]["content"] == "I will check."
        assert "reasoning_content" not in asst[0]

    def test_agent_completed_final_answer(self):
        """agent_completed final answer replays as assistant content."""
        events = [
            _make_event("agent_started", {"input": "task"}),
            _make_event("agent_completed", {"output": "FINAL-ANSWER",
                                            "reason": "stop"}),
        ]
        messages = build_messages("sys", events)
        asst = [m for m in messages if m["role"] == "assistant"]
        assert len(asst) == 1
        assert asst[0]["content"] == "FINAL-ANSWER"

    def test_tool_called_not_duplicated_when_covered(self):
        """tool_called is not replayed when agent_thought already has it."""
        events = [
            _make_event("agent_started", {"input": "task"}),
            _make_event("agent_thought", {
                "content": None,
                "tool_calls": [{"id": "call_x", "name": "bash",
                                "arguments": "{}"}],
            }),
            _make_event("tool_called", {"tool": "bash", "args": {}},
                        parent_event_id=None),
            _make_event("tool_result", {"result": "out"},
                        parent_event_id="call_x"),
        ]
        messages = build_messages("sys", events)
        asst = [m for m in messages if m["role"] == "assistant"]
        assert len(asst) == 1, f"expected one assistant msg, got {len(asst)}"

    def test_reasoning_passback_disabled(self):
        """reasoning_passback=False drops reasoning_content entirely."""
        events = [
            _make_event("agent_started", {"input": "task"}),
            _make_event("agent_thought", {
                "content": None,
                "reasoning_content": "SECRET-THOUGHT",
                "tool_calls": [{"id": "call_3", "name": "bash",
                                "arguments": "{}"}],
            }),
        ]
        messages = build_messages("sys", events, reasoning_passback=False)
        asst = [m for m in messages if m["role"] == "assistant"]
        assert len(asst) == 1
        assert "reasoning_content" not in asst[0]








# ═══════════════════════════════════════════════════════════
# DS thinking-mode multi-turn reasoning passback (2026-09-07)
# ═══════════════════════════════════════════════════════════


class TestReasoningPassbackMultiTurn:
    """DeepSeek: any assistant turn that carried reasoning_content must
    replay it on later requests when reasoning_passback is enabled."""

    def test_final_round_reasoning_passed_back_with_content(self):
        """Non-tool final round: agent_thought holds content+reasoning;
        replay assistant must include reasoning_content."""
        events = [
            _make_event("agent_started", {"input": "task one"}),
            _make_event("agent_thought", {
                "content": "最终中文翻译",
                "reasoning_content": "thinking steps",
                "tool_calls": [],
                "finish_reason": "stop",
            }),
            _make_event("agent_completed", {"output": "最终中文翻译"}),
            _make_event("agent_started", {"input": "task two (write file)"}),
        ]
        messages = build_messages(system_prompt="s", events=events)
        asst = [m for m in messages if m["role"] == "assistant"]
        assert any(m.get("reasoning_content") == "thinking steps"
                   for m in asst), asst

    def test_completed_only_round_merges_preceding_reasoning(self):
        """When the final content lives only on agent_completed and the
        preceding agent_thought carried reasoning, replay merges both."""
        events = [
            _make_event("agent_started", {"input": "task one"}),
            _make_event("agent_thought", {
                "content": "",
                "reasoning_content": "inner monologue",
                "tool_calls": [],
                "finish_reason": "stop",
            }),
            _make_event("agent_completed", {"output": "final answer text"}),
            _make_event("agent_started", {"input": "task two"}),
        ]
        messages = build_messages(system_prompt="s", events=events)
        merged = [m for m in messages if m["role"] == "assistant"
                  and m.get("content") == "final answer text"
                  and m.get("reasoning_content") == "inner monologue"]
        assert merged, messages

    def test_tool_round_reasoning_still_passed_back(self):
        """Tool-call rounds keep their existing reasoning passback."""
        events = [
            _make_event("agent_started", {"input": "task one"}),
            _make_event("agent_thought", {
                "content": "",
                "reasoning_content": "need to call bash",
                "tool_calls": [{"id": "c1", "name": "bash",
                                "arguments": "{}"}],
            }),
            _make_event("tool_result", {"result": "out"},
                        parent_event_id="c1"),
            _make_event("agent_started", {"input": "task two"}),
        ]
        messages = build_messages(system_prompt="s", events=events)
        asst = [m for m in messages if m["role"] == "assistant"
                and m.get("tool_calls")]
        assert asst and asst[0].get("reasoning_content") == "need to call bash"



    def test_final_content_thought_merged_single_assistant(self):
        """Content-bearing final thought + completed => ONE assistant with
        content+reasoning, no content-only duplicate (DS exact echo)."""
        events = [
            _make_event("agent_started", {"input": "task one"}),
            _make_event("agent_thought", {
                "content": "最终译文",
                "reasoning_content": "先翻译",
                "tool_calls": [],
                "finish_reason": "stop",
            }),
            _make_event("agent_completed", {"output": "最终译文"}),
            _make_event("agent_started", {"input": "task two"}),
        ]
        messages = build_messages(system_prompt="s", events=events)
        finals = [m for m in messages if m["role"] == "assistant"
                  and (m.get("content") or "") == "最终译文"]
        assert len(finals) == 1, messages
        assert finals[0].get("reasoning_content") == "先翻译"

    def test_passback_false_omits_reasoning(self):
        """reasoning_passback=False: no reasoning anywhere (control)."""
        events = [
            _make_event("agent_started", {"input": "task one"}),
            _make_event("agent_thought", {
                "content": "",
                "reasoning_content": "rc",
                "tool_calls": [{"id": "c1", "name": "bash",
                                "arguments": "{}"}],
            }),
            _make_event("tool_result", {"result": "x"},
                        parent_event_id="c1"),
            _make_event("agent_completed", {"output": "done"}),
            _make_event("agent_started", {"input": "task two"}),
        ]
        messages = build_messages(system_prompt="s", events=events,
                                  reasoning_passback=False)
        asst = [m for m in messages if m["role"] == "assistant"]
        assert all("reasoning_content" not in m for m in asst)



    def test_multi_task_assembly_parity_no_leak(self):
        """Ground-truth two-task transcript must round-trip exactly: role
        order, contents, and per-turn reasoning placement (no cross-task
        reasoning leakage onto other assistant messages)."""
        events = [
            # task 1
            _make_event("agent_started", {"input": "T1 prompt"}),
            _make_event("agent_thought", {
                "content": "", "reasoning_content": "R1-tool",
                "tool_calls": [{"id": "c1", "name": "bash",
                                "arguments": "{}"}]}),
            _make_event("tool_result", {"tool": "bash", "result": "r1"},
                        parent_event_id="c1"),
            _make_event("agent_thought", {
                "content": "答1", "reasoning_content": "R1-final",
                "tool_calls": [], "finish_reason": "stop"}),
            _make_event("agent_completed", {"output": "答1"}),
            # task 2
            _make_event("agent_started", {"input": "T2 prompt"}),
            _make_event("agent_thought", {
                "content": "", "reasoning_content": "R2-tool",
                "tool_calls": [{"id": "c2", "name": "bash",
                                "arguments": "{}"}]}),
            _make_event("tool_result", {"tool": "bash", "result": "r2"},
                        parent_event_id="c2"),
            _make_event("agent_thought", {
                "content": "答2", "reasoning_content": "R2-final",
                "tool_calls": [], "finish_reason": "stop"}),
            _make_event("agent_completed", {"output": "答2"}),
            # task 3 in flight
            _make_event("agent_started", {"input": "T3 prompt"}),
        ]
        msgs = build_messages(system_prompt="sys", events=events)

        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "assistant", "tool",
                         "assistant", "user", "assistant", "tool",
                         "assistant", "user"], roles
        assert msgs[1]["content"].startswith("Task: T1")
        assert msgs[5]["content"].startswith("Task: T2")
        assert msgs[9]["content"].startswith("Task: T3")

        # assistant 序列逐条校验 content/reasoning 不串位
        a = [m for m in msgs if m["role"] == "assistant"]
        assert a[0].get("reasoning_content") == "R1-tool"
        assert a[0].get("content") in (None, "")
        assert a[1].get("content") == "答1"
        assert a[1].get("reasoning_content") == "R1-final"
        assert a[2].get("reasoning_content") == "R2-tool"
        assert a[2].get("content") in (None, "")
        assert a[3].get("content") == "答2"
        assert a[3].get("reasoning_content") == "R2-final"
        # reasoning 只挂在原轮次：R2-* 不出现于 task1 消息
        assert a[1].get("reasoning_content") != "R2-final"
        assert all(m.get("reasoning_content") != "R2-final"
                   for m in a[:2])




# ─────────────────────────────────────────────────────────────
# dsh 差分修复（diagnose 2026-09-11）：wire 层一致性
# ─────────────────────────────────────────────────────────────


def _mk(t, payload, eid=None, parent=None):
    return type("_E", (), {"event_type": t, "payload": payload,
                           "event_id": eid or t, "parent_event_id": parent,
                           "source": "t"})()


def test_final_round_single_assistant_without_reasoning():
    """最终轮（无 reasoning）不得产生重复 assistant（dsh 只有一条）。"""
    msgs = build_messages("sys", [
        _mk("agent_started", {"input": "hi"}, "e1"),
        _mk("agent_thought", {"content": "final", "tool_calls": []}, "e2"),
        _mk("agent_completed", {"output": "final"}, "e3"),
    ])
    finals = [m for m in msgs if m["role"] == "assistant"
              and m.get("content") == "final"]
    assert len(finals) == 1, msgs


def test_tool_round_content_is_empty_string_not_null():
    """纯工具轮：content 必须是 ""（dsh 注释：null 会导致 400 且污染会话）。"""
    msgs = build_messages("sys", [
        _mk("agent_started", {"input": "hi"}, "e1"),
        _mk("agent_thought", {"tool_calls": [{"id": "c1", "name": "echo",
                                             "arguments": "{}"}]}, "e2"),
        _mk("tool_result", {"result": "r"}, "e3", parent="c1"),
    ])
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert asst and asst[0].get("content") == "", asst

