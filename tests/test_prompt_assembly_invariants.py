"""装配产物不变量（跨场景统一断言，diagnose 2026-09-11）。

背景：dsh 差分暴露出两个与 provider 无关的自身缺陷（最终轮重复 assistant、
纯工具轮 content=null）。逐场景断言不足以防回归，这里对整个事件流产物
（build_messages 输出）统一施加不变量，任何场景都不得违反。
"""

from __future__ import annotations

import pytest

from harness.prompt_assembly import build_messages


def _mk(t, payload, eid=None, parent=None):
    return type("_E", (), {"event_type": t, "payload": payload,
                           "event_id": eid or t, "parent_event_id": parent,
                           "source": "t"})()


def _scenario_simple():
    return [
        _mk("agent_started", {"input": "hi"}, "e1"),
        _mk("agent_thought", {"content": "hello", "tool_calls": []}, "e2"),
        _mk("agent_completed", {"output": "hello"}, "e3"),
    ]


def _scenario_tool_round():
    return [
        _mk("agent_started", {"input": "hi"}, "e1"),
        _mk("agent_thought", {"content": "let me echo",
                              "tool_calls": [{"id": "c1", "name": "echo",
                                              "arguments": '{"text":"one"}'}]}, "e2"),
        _mk("tool_result", {"tool": "echo", "result": "echo: one"}, "e3", parent="c1"),
        _mk("agent_thought", {"content": "done", "tool_calls": []}, "e4"),
        _mk("agent_completed", {"output": "done"}, "e5"),
    ]


def _scenario_multi_tool_same_round():
    return [
        _mk("agent_started", {"input": "go"}, "e1"),
        _mk("agent_thought", {"content": "",
                              "tool_calls": [
                                  {"id": "c1", "name": "echo", "arguments": '{"text":"a"}'},
                                  {"id": "c2", "name": "echo", "arguments": '{"text":"b"}'}]}, "e2"),
        _mk("tool_result", {"result": "a"}, "e3", parent="c1"),
        _mk("tool_result", {"result": "b"}, "e4", parent="c2"),
        _mk("agent_completed", {"output": "both"}, "e5"),
    ]


def _scenario_multi_turn_with_reasoning():
    return [
        _mk("agent_started", {"input": "one"}, "e1"),
        _mk("agent_thought", {"content": "",
                              "reasoning_content": "think-1",
                              "tool_calls": [{"id": "c1", "name": "echo",
                                              "arguments": "{}"}]}, "e2"),
        _mk("tool_result", {"result": "r"}, "e3", parent="c1"),
        _mk("agent_thought", {"content": "answer-1",
                              "reasoning_content": "think-2",
                              "tool_calls": []}, "e4"),
        _mk("agent_completed", {"output": "answer-1"}, "e5"),
        _mk("agent_started", {"input": "two", "_retry_error": "boom"}, "e6"),
        _mk("agent_thought", {"content": "answer-2", "tool_calls": []}, "e7"),
        _mk("agent_completed", {"output": "answer-2"}, "e8"),
    ]


def _scenario_memory_fold():
    return [
        _mk("agent_started", {"input": "one"}, "e1"),
        _mk("agent_thought", {"content": "old", "tool_calls": []}, "e2"),
        _mk("memory_summary", {"content": "FOLDED", "after_event_id": "e2"}, "m1"),
        _mk("agent_thought", {"content": "new", "tool_calls": []}, "e3"),
        _mk("agent_completed", {"output": "new"}, "e4"),
    ]


def _scenario_huge_tool_result():
    return [
        _mk("agent_started", {"input": "big"}, "e1"),
        _mk("agent_thought", {"tool_calls": [{"id": "c1", "name": "bash",
                                              "arguments": "{}"}]}, "e2"),
        _mk("tool_result", {"result": "x" * 20000}, "e3", parent="c1"),
        _mk("agent_completed", {"output": "ok"}, "e4"),
    ]


def _scenario_standalone_empty_thought():
    return [
        _mk("agent_started", {"input": "one"}, "e1"),
        _mk("agent_thought", {"content": "", "tool_calls": []}, "e2"),
        _mk("agent_started", {"input": "two"}, "e3"),
    ]


def _scenario_orphan_tool_result():
    """A result whose issuing assistant is missing must not be emitted.

    ADR-053 / docs/evidence/l3-03-grill-evidence.md §2: truncation used to drop a round's
    assistant while keeping its result, producing `role:"tool"` with no
    preceding `assistant.tool_calls` — invariant 2 violated, provider 400.

    The first result is *already* unpaired in the log (its call id exists
    nowhere); the second is paired.  Neither may end up orphaned in the output.
    """
    return [
        _mk("agent_started", {"input": "task"}, "e1"),
        _mk("tool_result", {"result": "I have no issuing assistant"}, "e2",
            parent="c_missing"),
        _mk("agent_thought", {"tool_calls": [{"id": "c1", "name": "bash",
                                              "arguments": "{}"}]}, "e3"),
        _mk("tool_result", {"result": "paired"}, "e4", parent="c1"),
        _mk("agent_completed", {"output": "ok"}, "e5"),
    ]


def _scenario_long_task_needs_truncation():
    """One long task (everything after the last agent_started) must be
    truncated by ROUND, never leaving a half round behind.

    Before ADR-053 the whole suffix was `keep ALL`, so a long single task was
    sent until the server rejected it (logs §1 scenario C).
    """
    events = [_mk("agent_started", {"input": "one long task"}, "e0")]
    for i in range(10):
        events.append(_mk("agent_thought", {
            "tool_calls": [{"id": f"c{i}", "name": "bash",
                            "arguments": "{}"}]}, f"t{i}"))
        events.append(_mk("tool_result", {"result": "y" * 4000}, f"r{i}",
                          parent=f"c{i}"))
    events.append(_mk("agent_completed", {"output": "done"}, "end"))
    return events


SCENARIOS = {
    "simple": _scenario_simple,
    "tool_round": _scenario_tool_round,
    "multi_tool": _scenario_multi_tool_same_round,
    "multi_turn_reasoning_retry": _scenario_multi_turn_with_reasoning,
    "memory_fold": _scenario_memory_fold,
    "huge_tool_result": _scenario_huge_tool_result,
    "standalone_empty_thought": _scenario_standalone_empty_thought,
    "orphan_tool_result": _scenario_orphan_tool_result,
    "long_task_needs_truncation": _scenario_long_task_needs_truncation,
}


def _build(name, **kw):
    return build_messages("sys", SCENARIOS[name](), **kw)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_round_boundary_invariant_under_tight_budget(name):
    """ADR-053 invariant 5: truncation happens on whole-round boundaries.

    Runs every scenario at a budget small enough to force truncation, so the
    pairing invariant is exercised against the TRUNCATION path rather than only
    against the happy path.  Measured before the fix: the prefix loop cut inside
    a round and emitted a bare `tool` message.
    """
    msgs = _build(name, context_window=3000, budget_ratio=0.8)
    seen: set = set()
    for m in msgs:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                seen.add(tc["id"])
        elif m["role"] == "tool":
            assert m.get("tool_call_id") in seen, (
                f"{name}: orphan tool message {m.get('tool_call_id')}")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_null_content_anywhere(name):
    """任何消息的 content 都不得为 None（null 会被持久化并毒化后续请求）。"""
    for m in _build(name):
        assert m["role"] in {"system", "user", "assistant", "tool"}, m
        assert m.get("content") is not None, m


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_tool_messages_have_matching_call_id(name):
    """每条 tool 消息必须有 tool_call_id，且能对应到之前的 assistant tool_calls。"""
    msgs = _build(name)
    seen_ids = set()
    for m in msgs:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                seen_ids.add(tc["id"])
        elif m["role"] == "tool":
            cid = m.get("tool_call_id")
            assert cid, m
            assert cid in seen_ids, (cid, m)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_two_consecutive_assistants_with_same_content(name):
    """不得出现连续两条内容相同的 assistant（最终轮重复的回归防线）。"""
    asst = [m for m in _build(name) if m["role"] == "assistant"]
    for a, b in zip(asst, asst[1:]):
        assert not (a.get("content") and a.get("content") == b.get("content")), (a, b)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_assistant_with_tool_calls_has_string_content(name):
    """带 tool_calls 的 assistant 必须是字符串 content（"" 或文本），不得 null。"""
    for m in _build(name):
        if m["role"] == "assistant" and m.get("tool_calls"):
            assert isinstance(m.get("content"), str), m


def test_huge_tool_result_is_clipped_with_marker():
    """超大工具结果被裁剪并带 marker（不出现 null / 不无限增长）。"""
    tools = [m for m in _build("huge_tool_result") if m["role"] == "tool"]
    assert tools, "expected a tool message"
    content = tools[0]["content"]
    assert "truncated" in content
    assert len(content) < 20000
