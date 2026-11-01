"""`_cost` must equal the messages actually emitted (ADR-053 decision 3, R1/P1).

The budget judge and the assembler now share ONE event→message mapping, so the
invariant is exact: for every event set, Σ `_cost` == Σ emitted message cost
(excluding the system prompt, which is costed separately by the caller).

MEASURED on a real 69-event session before the fix:
    Σ _cost(all events)          = 7123
    Σ actually-sent messages     = 2898     (2.46x overstatement)
The old gap came from counting `str(payload)` (audit fields never sent) and from
miscounting skip/coverage pairing (agent_failed / uncovered tool_called /
final thought merged into agent_completed / memory prefix / retry text).
"""
from __future__ import annotations

from harness.interfaces import Event
from harness.prompt_assembly import _cost, _count_tokens, build_messages


def _ev(et, payload, eid="e1", parent=None):
    return Event(event_id=eid, session_id="s", event_type=et, timestamp=0.0,
                 source="test", parent_event_id=parent, payload=payload)


def _sent_cost(msgs) -> int:
    """Cost of emitted event messages; the system prompt is not an event."""
    total = 0
    for m in msgs:
        if m.get("role") == "system":
            continue
        if m.get("content"):
            total += _count_tokens(str(m["content"]), "deepseek")
        if m.get("reasoning_content"):
            total += _count_tokens(str(m["reasoning_content"]), "deepseek")
        for tc in (m.get("tool_calls") or []):
            total += _count_tokens(
                str((tc.get("function") or {}).get("arguments")), "deepseek")
    return total


def _assert_cost_matches_emission(evs, *, reasoning_passback=True) -> tuple[int, int]:
    msgs = build_messages("sys", evs, context_window=10**9, budget_ratio=1.0,
                          provider="deepseek",
                          reasoning_passback=reasoning_passback)
    sent = _sent_cost(msgs)
    cost = sum(_cost(e, "deepseek", "tokenizer_dir", reasoning_passback)
               for e in evs)
    assert cost == sent, (
        f"Σ_cost={cost} != Σ_sent={sent} for event types "
        f"{[e.event_type for e in evs]}; cost must be exactly what is emitted")
    return cost, sent


def test_cost_of_agent_thought_matches_what_is_sent():
    """`_cost` must not count audit-only payload fields (exact, no 1.25x slack)."""
    ev = _ev("agent_thought", {
        "content": "",
        "reasoning_content": "think " * 400,
        "tool_calls": [{"id": "c1", "name": "bash",
                        "arguments": '{"command":"ls"}'}],
        "finish_reason": "tool_calls",          # audit-only
        "llm_backend": "primary",               # audit-only
    })
    _assert_cost_matches_emission([ev])


def test_cost_matches_what_the_assembler_emits_for_a_whole_session():
    """Strong exact invariant over a realistic mixed session."""
    evs = []
    for i in range(6):
        evs.append(_ev("agent_started", {"input": f"translate chunk {i}"},
                       eid=f"a{i}"))
        evs.append(_ev("agent_thought", {
            "content": "",
            "reasoning_content": "plan " * 150,
            "tool_calls": [{"id": f"c{i}", "name": "bash",
                            "arguments": '{"command":"cat /ws/book/ja/chunk_%03d.txt"}' % i}],
            "finish_reason": "tool_calls",     # audit-only
            "llm_backend": "primary",          # audit-only
        }, eid=f"t{i}"))
        # audit-only duplicate that the assembler SKIPS (covered by t{i})
        evs.append(_ev("tool_called", {"tool": "bash",
                                       "args": {"command": "x" * 300},
                                       "allowed": True}, eid=f"c{i}"))
        evs.append(_ev("tool_result", {"tool": "bash",
                                       "result": "廈門の朝 " * 200},
                       eid=f"r{i}", parent=f"c{i}"))
    _assert_cost_matches_emission(evs)


def test_cost_still_counts_tool_result_content():
    """Guard the other direction: emitted tool result content is fully counted.

    The result needs its issuing agent_thought; an orphan result is dropped by
    `_split_orphans` (and therefore costs 0, asserted separately below).
    """
    evs = [
        _ev("agent_thought", {"content": "", "tool_calls": [
            {"id": "c1", "name": "bash", "arguments": "{}"}]}, eid="t1"),
        _ev("tool_result", {"tool": "bash", "result": "x" * 2000},
            eid="r1", parent="c1"),
    ]
    cost, sent = _assert_cost_matches_emission(evs)
    assert cost == sent > 0


def test_orphan_tool_result_costs_zero():
    """Dropped by the assembler (no issuing call) => not in the request."""
    ev = _ev("tool_result", {"tool": "bash", "result": "x" * 2000}, eid="r1")
    cost, sent = _assert_cost_matches_emission([ev])
    assert (cost, sent) == (0, 0), (cost, sent)


def test_assembler_keeps_more_rounds_after_the_fix():
    """The user-visible consequence: with a truthful cost, MORE rounds fit."""
    evs = []
    for i in range(20):
        evs.append(_ev("agent_started", {"input": f"task {i}"}, eid=f"a{i}"))
        evs.append(_ev("agent_thought", {
            "content": "",
            "reasoning_content": "r" * 120,
            "tool_calls": [{"id": f"c{i}", "name": "bash",
                            "arguments": '{"command":"ls"}'}],
            "finish_reason": "tool_calls",
        }, eid=f"t{i}"))
        evs.append(_ev("tool_result", {"tool": "bash", "result": "ok"},
                       eid=f"r{i}", parent=f"c{i}"))
    true_total = sum(_cost(e, "deepseek", "tokenizer_dir") for e in evs)
    msgs = build_messages("sys", evs, context_window=int(true_total * 1.30),
                          budget_ratio=1.0, provider="deepseek")
    kept = sum(1 for m in msgs if m.get("role") == "assistant")
    assert kept >= 20, (
        f"only {kept}/20 rounds kept although the true total ({true_total}) "
        f"fits the budget — cost is still inflated")


def test_cost_honours_reasoning_passback():
    """When reasoning is NOT replayed, it must not be counted."""
    ev = _ev("agent_thought", {
        "content": "",
        "reasoning_content": "think " * 300,
        "tool_calls": [{"id": "c1", "name": "bash",
                        "arguments": '{"command":"ls"}'}],
    })
    with_reasoning = _cost(ev, "deepseek", "tokenizer_dir", reasoning_passback=True)
    without = _cost(ev, "deepseek", "tokenizer_dir", reasoning_passback=False)
    assert with_reasoning > without, "passback=False must cost strictly less"
    delta = with_reasoning - without
    expected = _count_tokens("think " * 300, "deepseek")
    assert abs(delta - expected) <= 2, (delta, expected)


def test_assembler_keeps_rounds_when_reasoning_is_not_replayed():
    """Consequence: with passback off, MORE rounds fit the same budget."""
    evs = []
    for i in range(10):
        evs.append(_ev("agent_started", {"input": f"task {i}"}, eid=f"a{i}"))
        evs.append(_ev("agent_thought", {
            "content": "",
            "reasoning_content": "r" * 400,
            "tool_calls": [{"id": f"c{i}", "name": "bash",
                            "arguments": '{"command":"ls"}'}],
        }, eid=f"t{i}"))
    budget = 1200
    on = build_messages("sys", evs, context_window=budget, budget_ratio=1.0,
                        provider="deepseek", reasoning_passback=True)
    off = build_messages("sys", evs, context_window=budget, budget_ratio=1.0,
                         provider="deepseek", reasoning_passback=False)
    a_on = sum(1 for m in on if m.get("role") == "assistant")
    a_off = sum(1 for m in off if m.get("role") == "assistant")
    assert a_off > a_on, (
        f"with reasoning suppressed more rounds must fit; got on={a_on} off={a_off}")


# ── R1/P1: the five previously-mismatched edge paths, exact ─────────────

def test_agent_failed_is_counted_as_the_emitted_tool_error():
    ev = _ev("agent_failed",
             {"reason": "boom", "detail": {"x": "y" * 50}}, eid="f1")
    cost, sent = _assert_cost_matches_emission([ev])
    assert cost == sent > 0


def test_uncovered_tool_called_is_counted_as_an_assistant_call():
    """Legacy session with no agent_thought carrying the id: it IS emitted."""
    ev = _ev("tool_called", {"tool": "bash", "args": {"command": "x" * 100},
                            "allowed": True}, eid="c1")
    cost, sent = _assert_cost_matches_emission([ev])
    assert cost == sent > 0


def test_covered_tool_called_is_not_double_counted():
    """Normal path: the agent_thought carries the call, the legacy event is not."""
    evs = [
        _ev("agent_thought", {
            "content": "", "reasoning_content": "r" * 20,
            "tool_calls": [{"id": "c1", "name": "bash",
                            "arguments": '{"command":"ls"}'}],
        }, eid="t1"),
        _ev("tool_called", {"tool": "bash", "args": {"command": "ls"},
                           "allowed": True}, eid="c1"),
    ]
    _assert_cost_matches_emission(evs)


def test_final_thought_is_merged_into_agent_completed_not_double_counted():
    """Final round stores content twice; only the completed message is sent."""
    content = "FINAL ANSWER " * 10
    evs = [
        _ev("agent_thought", {"content": content,
                              "reasoning_content": "R" * 100}, eid="t1"),
        _ev("agent_completed", {"output": content}, eid="done"),
    ]
    cost, sent = _assert_cost_matches_emission(evs)
    expected = (_count_tokens(content, "deepseek")
                + _count_tokens("R" * 100, "deepseek"))
    assert cost == expected == sent, (cost, expected, sent)


def test_memory_summary_slot_placeholder_is_counted():
    """The assembler emits `<mem>…</mem>`; the cost must include the wrapper."""
    ev = _ev("memory_summary",
             {"content": "m" * 100, "slot": "before_task"}, eid="m1")
    cost, sent = _assert_cost_matches_emission([ev])
    expected = _count_tokens("<mem>" + "m" * 100 + "</mem>", "deepseek")
    assert cost == expected == sent, (cost, expected, sent)


def test_memory_summary_without_slot_is_ignored():
    """Legacy (pre-slot) summaries are not assembled, so they cost 0.

    P5: this is explicit compatibility behaviour, reported via
    `meta.ignored_legacy_summary_count`, not a silent drop.
    """
    ev = _ev("memory_summary", {"content": "m" * 100}, eid="m1")
    cost, sent = _assert_cost_matches_emission([ev])
    assert (cost, sent) == (0, 0), (cost, sent)


def test_agent_started_retry_error_is_counted():
    """`_retry_error` is appended to the emitted Task message."""
    ev = _ev("agent_started", {"input": "task", "_retry_error": "boom"},
             eid="a1")
    cost, sent = _assert_cost_matches_emission([ev])
    expected = _count_tokens("Task: task\n\n[Previous attempt failed: boom]\n",
                             "deepseek")
    assert cost == expected == sent, (cost, expected, sent)


# ── I-64: per-build cost context (deterministic, no global ordering) ───

def _pair_events(i: int) -> list:
    # The audit-only tool_called duplicate is COVERED by the thought's call.
    # Its explicit-map cost is 0; the no-context fallback would emit it, so this
    # fixture makes the interleaved-map test fail if explicit context is ignored.
    return [
        _ev("agent_thought", {
            "content": "", "reasoning_content": f"r{i}" * 80,
            "tool_calls": [{"id": f"c{i}", "name": "bash",
                            "arguments": '{"command":"ls"}'}]},
            eid=f"t{i}"),
        _ev("tool_called", {"tool": "bash", "args": {"command": "x" * 40},
                           "allowed": True}, eid=f"c{i}"),
        _ev("tool_result", {"tool": "bash", "result": f"out{i}" * 60},
            eid=f"r{i}", parent=f"c{i}"),
    ]


def test_cost_map_out_is_per_build_and_interleaved_builds_stay_exact():
    """Two builds, then `_cost` against each build's own snapshot."""
    evs1, evs2 = _pair_events(1), _pair_events(2)
    m1: dict = {}
    m2: dict = {}
    msgs1 = build_messages("sys", evs1, context_window=10**9,
                           provider="deepseek", cost_map_out=m1)
    msgs2 = build_messages("sys", evs2, context_window=10**9,
                           provider="deepseek", cost_map_out=m2)
    assert m1 and m2
    assert sum(_cost(e, "deepseek", "tokenizer_dir", cost_map=m1)
               for e in evs1) == _sent_cost(msgs1)
    assert sum(_cost(e, "deepseek", "tokenizer_dir", cost_map=m2)
               for e in evs2) == _sent_cost(msgs2)
    # the second build did not overwrite the first snapshot
    assert m1 != m2


def test_cost_map_out_is_safe_across_concurrent_builds():
    """Concurrent builds keep independent snapshots (no global-map race)."""
    from concurrent.futures import ThreadPoolExecutor

    def run(i):
        evs = _pair_events(10 + i)
        local: dict = {}
        msgs = build_messages("sys", evs, context_window=10**9,
                              provider="deepseek", cost_map_out=local)
        total = sum(_cost(e, "deepseek", "tokenizer_dir", cost_map=local)
                    for e in evs)
        return total, _sent_cost(msgs)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert results and all(total == sent for total, sent in results)
