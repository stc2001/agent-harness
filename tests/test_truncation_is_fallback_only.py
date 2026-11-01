"""Truncation is a FALLBACK, not a policy (ADR-053 revision).

WHY
  `budget_tokens = context_window * budget_ratio` (ratio 0.8) made the harness
  start dropping rounds at ~64% of the model's window.  That is the harness
  taking a POLICY decision, which belongs to the app layer (ADR-001: the app's
  state machine decides when to compact).  It also made the app's high-water
  signal unreachable: truncation CAPS the assembled cost, so usage could never
  approach the mark that is supposed to trigger compaction.

  The harness's job is narrower and absolute: **never overflow the model's
  context**.  So it must drop rounds only when the request would exceed the hard
  limit, and then only enough to fit.

WHAT MUST NOT CHANGE
  * the hard limit still reserves the OUTPUT budget (`ctx - max_tokens`)
  * the anchor is never dropped
  * drops still happen in whole rounds (an orphan `tool` message violates the
    ADR-052 §5 invariant and produced a real 400)
  * `meta` keeps reporting what was dropped, so the runtime can warn
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.interfaces import Event  # noqa: E402
from harness.prompt_assembly import _count_tokens, build_messages  # noqa: E402

PROVIDER = "deepseek"


def _rounds(n: int, chars: int = 200, start: int = 1) -> list:
    """n rounds of (agent_thought, tool_result), plus the task anchor."""
    evs = [Event(event_id="a0", session_id="s", event_type="agent_started",
                 timestamp=0.0, source="t", parent_event_id=None,
                 payload={"input": "translate the book"})]
    for i in range(start, start + n):
        evs.append(Event(event_id=f"t{i}", session_id="s",
                         event_type="agent_thought", timestamp=0.0, source="t",
                         parent_event_id=None,
                         payload={"content": "", "tool_calls": [
                             {"id": f"c{i}", "name": "bash",
                              "arguments": "{}"}]}))
        evs.append(Event(event_id=f"r{i}", session_id="s",
                         event_type="tool_result", timestamp=0.0, source="t",
                         parent_event_id=f"c{i}",
                         payload={"tool": "bash", "result": "x" * chars}))
    return evs


def _size(msgs: list) -> int:
    total = 0
    for m in msgs:
        if m.get("content"):
            total += _count_tokens(str(m["content"]), PROVIDER)
        if m.get("reasoning_content"):
            total += _count_tokens(str(m["reasoning_content"]), PROVIDER)
        for tc in (m.get("tool_calls") or []):
            total += _count_tokens(
                str((tc.get("function") or {}).get("arguments")), PROVIDER)
    return total


def _build(events, *, ctx, max_tokens=0, ratio=None):
    """`ratio=None` means DO NOT PASS IT — the default is what is under test."""
    meta = {}
    kw = {} if ratio is None else {"budget_ratio": ratio}
    msgs = build_messages("sys", events, context_window=ctx,
                          provider=PROVIDER, meta_out=meta,
                          max_tokens=max_tokens, **kw)
    return msgs, meta


# ── the fallback must NOT fire while everything fits ────────────────────

def test_no_truncation_while_the_request_fits():
    """Everything fits the hard limit ⇒ nothing may be dropped.

    Uses the DEFAULT configuration: the bug is in the default, so a test that
    overrides the ratio would pass whether or not the bug exists.
    """
    events = _rounds(30)
    full = build_messages("sys", events, context_window=10**9,
                          budget_ratio=1.0, provider=PROVIDER, max_tokens=0)
    total = _size(full)
    msgs, meta = _build(events, ctx=int(total * 1.1))     # defaults on purpose
    assert meta["truncated"] is False, meta
    assert meta["dropped_rounds"] == 0, meta


def test_default_ratio_does_not_force_early_truncation():
    """The DEFAULT must not pre-truncate: with plenty of room every round stays.

    MEASURED defect: the default `budget_ratio=0.8` made assembly drop rounds at
    ~64% of the model's window — a policy decision taken by the mechanism, which
    also made the app's high-water trigger unreachable (truncation caps cost).
    """
    events = _rounds(30)
    msgs, meta = _build(events, ctx=10**9)                # defaults on purpose
    assert meta["truncated"] is False, meta
    assert len([m for m in msgs if m["role"] == "tool"]) == 30


# ── when it does fire, it drops the MINIMUM ─────────────────────────────

def test_drops_only_what_is_needed_to_fit():
    """Just over the limit ⇒ drop the fewest oldest rounds that make it fit."""
    events = _rounds(30)
    full = build_messages("sys", events, context_window=10**9,
                          budget_ratio=1.0, provider=PROVIDER, max_tokens=0)
    total = _size(full)
    per_round = total // 30
    ctx = total - per_round * 2            # room for ~2 fewer rounds

    msgs, meta = _build(events, ctx=ctx)
    assert meta["truncated"] is True
    dropped = meta["dropped_rounds"]
    assert 1 <= dropped <= 4, f"dropped {dropped} rounds for a 2-round deficit"
    assert _size(msgs) <= ctx, "still over the limit after truncating"


def test_result_always_fits_the_hard_limit():
    """Whatever the input, the request must not exceed the limit."""
    for n in (5, 20, 60):
        events = _rounds(n)
        msgs, meta = _build(events, ctx=1500)
        assert _size(msgs) <= 1500 or meta.get("over_budget_unresolvable"), \
            f"n={n}: {_size(msgs)} > 1500 and not reported unresolvable"


# ── invariants that must survive the change ─────────────────────────────

def test_anchor_survives_truncation():
    events = _rounds(40)
    msgs, meta = _build(events, ctx=1200)
    joined = "\n".join(str(m.get("content") or "") for m in msgs)
    assert "translate the book" in joined, "the task anchor was dropped"


def test_no_orphan_tool_messages_after_truncation():
    """Whole rounds only: every `tool` message needs a preceding tool_call."""
    events = _rounds(40)
    msgs, _ = _build(events, ctx=1200)
    seen_calls: set = set()
    for m in msgs:
        if m["role"] == "assistant":
            for tc in (m.get("tool_calls") or []):
                seen_calls.add(tc.get("id"))
        elif m["role"] == "tool":
            assert m.get("tool_call_id") in seen_calls, \
                f"orphan tool message: {m.get('tool_call_id')}"


def test_output_budget_is_reserved():
    """The hard limit must leave room for the model's own output.

    I-64: an explicit max_tokens larger than a quarter of the window is still
    clamped for BUDGET purposes (the request field itself remains 2000); hence
    hard_limit = 6000 - 1500 = 4500, not 4000.
    """
    events = _rounds(30)
    msgs_a, meta_a = _build(events, ctx=6000, max_tokens=0)
    msgs_b, meta_b = _build(events, ctx=6000, max_tokens=2000)
    assert _size(msgs_b) <= 4500, "output reservation not honoured"
    assert meta_b["hard_limit"] == 4500
    assert meta_b["output_reserve_effective"] == 1500


def test_meta_reports_what_the_runtime_needs_to_warn():
    events = _rounds(40)
    msgs, meta = _build(events, ctx=1200)
    if meta["truncated"]:
        for key in ("dropped_event_count", "kept_rounds", "dropped_rounds",
                    "hard_limit", "budget_tokens"):
            assert key in meta, f"meta is missing {key}"


# ── the runtime must WARN when the fallback fires ───────────────────────

def test_runtime_emits_an_audit_warning_on_truncation():
    """Every truncation is evidence that the app's memory policy under-delivered.

    The app controls compaction (ADR-001); the harness only guards the limit.  So
    a truncation is not routine — it means rounds were discarded for budget
    reasons without the app having compacted them first.  That is exactly the
    condition an operator needs to see, so the runtime records it.

    The harness already reports the fact in `meta`; this pins that the runtime
    turns it into an audit entry rather than letting it pass silently.
    """
    import inspect

    from harness.agent_runtime import impl as _impl
    src = inspect.getsource(_impl)
    assert 'event="history_context_truncated"' in src, (
        "the runtime does not emit history_context_truncated on history drops")
    assert 'event="over_budget_unresolvable"' in src, (
        "the runtime does not emit over_budget_unresolvable at level error")
    assert 'event="prompt_truncated"' not in src, (
        "the obsolete umbrella event is still emitted")


# ── the SPEC default must not pre-truncate either (MR-A follow-up) ──────

def test_spec_default_ratio_does_not_pre_truncate():
    """`AgentSpec.context_budget_ratio` must also default to "no opinion".

    MEASURED gap: the real request path goes through
    `prompt_preview.compose_llm_context`, which reads
    `spec.context_budget_ratio` (default **0.8**).  Fixing only
    `build_messages`' own default therefore changed nothing for real runs —
    a 4000-token window still pre-truncated at 3200, while the app's high-water
    mark (85%) sat at 3400 and could never be reached.

    A deployment may still tighten this deliberately; the DEFAULT must leave
    truncation as the fallback it is documented to be.
    """
    from harness.interfaces import AgentSpec

    f = AgentSpec.__dataclass_fields__["context_budget_ratio"]
    assert f.default == 1.0, (
        f"AgentSpec.context_budget_ratio defaults to {f.default!r}; the real "
        "request path honours it, so a sub-1.0 default silently pre-truncates "
        "and makes the app's high-water mark unreachable")

