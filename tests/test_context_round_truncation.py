"""ADR-053: current-task (suffix) round truncation, mem placement, fail-loudly.

These lock the behaviour specified in ADR-053 and evidenced in
`docs/evidence/l3-03-grill-evidence.md`.  Each test names the defect it prevents from
regressing; the "before" behaviour is quoted where it was measured.

Round = one `agent_thought` carrying tool_calls + every `tool_result` that
answers one of those ids.  Truncation never splits a round.
"""

from __future__ import annotations

import pytest

from harness.prompt_assembly import build_messages

FILL = "lorem ipsum dolor sit amet " * 60   # ~1620 ASCII chars, tokenizer-agnostic


class _E:
    """Minimal event object (same shape the rest of the suite uses)."""

    def __init__(self, event_type, payload, event_id, parent=None):
        self.event_type = event_type
        self.payload = payload
        self.event_id = event_id
        self.parent_event_id = parent
        self.source = "test"


def _round(tag, anchor=None):
    """One round: optional agent_started, then thought(tool_call) + result."""
    out = []
    if anchor is not None:
        out.append(_E("agent_started", {"input": anchor}, f"a_{tag}"))
    out.append(_E("agent_thought", {
        "content": "", "tool_calls": [{"id": f"c_{tag}", "name": "bash",
                                       "arguments": "{}"}]}, f"t_{tag}"))
    out.append(_E("tool_result", {"tool": "bash", "result": FILL},
                  f"r_{tag}", parent=f"c_{tag}"))
    return out


def _tool_ok(msgs):
    """Every role:'tool' message must trace back to an earlier assistant call."""
    seen = set()
    for m in msgs:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                seen.add(tc["id"])
        elif m["role"] == "tool":
            if m.get("tool_call_id") not in seen:
                return False, m.get("tool_call_id")
    return True, None


# ── A: a call whose issuing assistant is gone must not leave an orphan ──


def _full_cost(events) -> int:
    """Tokens the events occupy when nothing is dropped.

    Budgets below are DERIVED from this instead of hardcoded: token counting is
    exact for CJK as of the 2026-10-16 fallback fix, so previously-fixed numbers
    no longer force truncation (CJK used to count as 0, which made every fixture
    look tiny).
    """
    from harness.prompt_assembly import _count_tokens
    msgs = build_messages("sys", events, context_window=10**9,
                          budget_ratio=1.0, max_tokens=0)
    total = 0
    for m in msgs:
        if m.get("content"):
            total += _count_tokens(str(m["content"]), "deepseek")
        if m.get("reasoning_content"):
            total += _count_tokens(str(m["reasoning_content"]), "deepseek")
        for tc in (m.get("tool_calls") or []):
            total += _count_tokens(
                str((tc.get("function") or {}).get("arguments")), "deepseek")
    return total

def test_orphan_tool_result_is_dropped():
    """A `tool` message whose assistant was truncated away is un-sendable.

    Measured before the fix (logs §2): the prefix loop dropped the ASSISTANT
    of the last prefix round while keeping its result, emitting
    [system, tool(c_OLD2), user(anchor), ...] -> provider 400 (invariant 2).

    Note the distinction: a result whose call id exists NOWHERE in the log was
    already unpaired in the log and stays inert (historical behaviour, pinned
    by test_oversized_tool_result_fallback_preserves_head_marker).  Only
    results orphaned *by assembly-time truncation* are dropped.
    """
    events = [_E("agent_started", {"input": "current task"}, "a0")]
    older = _round("T0")                              # thought + result
    # Shrink ONLY the older round's ability to fit: make its result huge so the
    # older round cannot fit while the newer one does => the older assistant is
    # dropped, leaving its result orphaned unless the fix drops it too.
    older[1].payload = {"tool": "bash", "result": FILL * 40}
    events += older
    events += _round("KEEP")                          # newer round survives

    full = _full_cost(events)
    meta: dict = {}
    # Budget sized so the huge older round cannot fit while the newer one can.
    msgs = build_messages("sys", events, context_window=int(full * 0.35),
                          budget_ratio=1.0, meta_out=meta, max_tokens=0)
    roles = [m["role"] for m in msgs]
    ok, bad = _tool_ok(msgs)
    assert ok, f"orphan tool_call_id={bad} in {roles}"
    # The older round was dropped ATOMICALLY: its assistant AND its result are
    # both gone, so no orphan is ever produced (the `_split_orphans` net is the
    # second line of defence, for results whose round-mate is missing from the
    # log itself).  Either way the emitted chain stays legal.
    assert meta["dropped_rounds"] >= 1, meta
    assistant_calls = {tc["id"] for m in msgs if m["role"] == "assistant"
                       for tc in (m.get("tool_calls") or [])}
    tool_results = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
    assert tool_results <= assistant_calls, (tool_results, assistant_calls)


# ── C: prefix truncation must be round-granular ─────────────────────────

def test_prefix_truncation_never_splits_round():
    """Dropping the oldest prefix rounds must not drop half a round.

    Before ADR-053 the prefix loop dropped **event by event**
    (`for ev in reversed(prefix_events): ... break`), which cut inside a round
    and emitted `tool(c_OLD2)` with no assistant — the exact regression this
    test pins.  Reproduces scripts/probe_suffix_truncation.py.
    """
    events = []
    events += _round("OLD1")
    events += _round("OLD2")
    events += _round("R1", anchor="current task")
    events += _round("R3")
    events += _round("R4")

    # tiny budget: forces prefix truncation on top of the round rules
    msgs = build_messages("sys", events, context_window=200, budget_ratio=0.8)
    ok, bad = _tool_ok(msgs)
    assert ok, f"orphan tool_call_id={bad} in {[m['role'] for m in msgs]}"
    # and the round that WAS kept is complete (assistant + its result)
    kept_calls = {tc["id"] for m in msgs if m["role"] == "assistant"
                  for tc in (m.get("tool_calls") or [])}
    kept_results = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
    assert kept_calls == kept_results


# ── B: suffix participates in truncation ────────────────────────────────

def test_suffix_truncation_by_round():
    """A single long task must be truncated, not sent until the server 400s.

    Measured before the fix (logs §1 scenario C): suffix cost 340,929 vs
    budget 104,857 and **not one event was dropped**.
    """
    events = [_E("agent_started", {"input": "one long task"}, "a0")]
    for i in range(12):
        events += _round(f"T{i}")

    full = _full_cost(events)
    meta: dict = {}
    msgs = build_messages("sys", events, context_window=int(full * 0.5),
                          budget_ratio=1.0, meta_out=meta)
    assert meta["dropped_event_count"] > 0, (meta, full)
    assert meta["truncated"] is True
    assert len(msgs) < len(events)
    ok, bad = _tool_ok(msgs)
    assert ok, bad


def test_anchor_survives_even_when_its_round_is_dropped():
    """The task anchor is always sent; the anchor's ROUND is not protected."""
    events = [_E("agent_started", {"input": "TASK TEXT"}, "a0")]
    for i in range(12):
        events += _round(f"T{i}")

    msgs = build_messages("sys", events, context_window=6000, budget_ratio=0.8)
    anchors = [m for m in msgs
               if m["role"] == "user" and str(m.get("content", "")).startswith("Task:")]
    assert len(anchors) == 1, [m["role"] for m in msgs]
    assert "TASK TEXT" in anchors[0]["content"]
    # the first round may be gone, but whatever remains is chain-legal
    ok, bad = _tool_ok(msgs)
    assert ok, bad


def test_min_keep_rounds_keeps_the_newest_rounds():
    """`min_keep_rounds` raised above 1 keeps that many newest rounds."""
    events = [_E("agent_started", {"input": "task"}, "a0")]
    for i in range(10):
        events += _round(f"T{i}")

    meta: dict = {}
    build_messages("sys", events, context_window=6000, budget_ratio=0.8,
                   meta_out=meta, min_keep_rounds=3)
    assert meta["kept_rounds"] >= 3, meta


def test_min_keep_rounds_below_one_raises():
    """N < 1 is a configuration error (the anchor + one round are irreducible)."""
    events = [_E("agent_started", {"input": "task"}, "a0")] + _round("T0")
    with pytest.raises(ValueError):
        build_messages("sys", events, 100000, 0.8, min_keep_rounds=0)


# ── D: budget measured on what is actually SENT ─────────────────────────

def test_budget_measured_on_emitted_messages():
    """A huge tool result is clipped before sending, so it must not be billed
    at its raw size.

    Measured before the fix (logs §6): 200,000-char result counted 25,009
    tokens while only 1,041 were emitted — a 24x overcount, which discarded
    history that would have fit.
    """
    events = [
        _E("agent_started", {"input": "task"}, "a0"),
        _E("agent_thought", {"content": "", "tool_calls": [
            {"id": "c1", "name": "bash", "arguments": "{}"}]}, "t1"),
        _E("tool_result", {"tool": "bash", "result": "x" * 200_000},
           "r1", parent="c1"),
    ]
    meta: dict = {}
    msgs = build_messages("sys", events, context_window=100000,
                          budget_ratio=0.8, meta_out=meta)
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    # the emitted text is clipped (marker present) and small
    assert len(tool_msgs[0]["content"]) < 20_000
    assert "[... truncated" in tool_msgs[0]["content"]
    # the round is kept because its SENT size is small
    assert meta["dropped_event_count"] == 0, meta


# ── E: mem lands where the dropped history was (event-level positioning) ─

def test_mem_lands_before_anchor_when_prefix_dropped():
    """`slot="before_task"` must precede the task anchor, not follow it.

    Measured before the fix (logs §3): insertion used
    `kept_prefix + 1`, which is 1 when the whole prefix is dropped, so the
    summary of OLD history landed AFTER the task prompt.  Placement is now
    explicit (the app names the slot); the harness only guarantees the position.
    """
    events = []
    for i in range(6):
        events += _round(f"OLD{i}")
    events += _round("R1", anchor="current task")
    events.append(_E("memory_summary",
                     {"content": "OLD SUMMARY", "slot": "before_task",
                      "after_event_id": "t_OLD0"},
                     "m1"))

    msgs = build_messages("sys", events, context_window=9000, budget_ratio=0.8)
    idx_mem = next(i for i, m in enumerate(msgs)
                   if m["role"] == "user"
                   and str(m.get("content", "")).startswith("<mem>"))
    idx_anchor = next(i for i, m in enumerate(msgs)
                      if m["role"] == "user"
                      and str(m.get("content", "")).startswith("Task:"))
    assert idx_mem < idx_anchor, (
        f"mem at {idx_mem} must precede the task anchor at {idx_anchor}: "
        f"{[m['role'] for m in msgs]}")


def test_two_memories_are_both_emitted():
    """One summary per slot: `before_task` and `after_task` both survive."""
    events = []
    for i in range(4):
        events += _round(f"OLD{i}")
    events += _round("R1", anchor="current task")
    events += _round("T1")
    events += _round("T2")
    events.append(_E("memory_summary",
                     {"content": "PREFIX MEM", "slot": "before_task",
                      "after_event_id": "t_OLD0"}, "m1"))
    events.append(_E("memory_summary",
                     {"content": "SUFFIX MEM", "slot": "after_task",
                      "after_event_id": "t_T1"}, "m2"))

    msgs = build_messages("sys", events, context_window=100000, budget_ratio=0.8)
    mems = [m for m in msgs if m["role"] == "user"
            and str(m.get("content", "")).startswith("<mem>")]
    assert len(mems) == 2, [m.get("content") for m in mems]


# ── F: mem text is part of the budget ───────────────────────────────────

def test_mem_counts_against_budget():
    """Summary text is sent, so a large summary must squeeze out history.

    Before ADR-053 the `memory_summary` cost was never added to `used`.
    """
    def run(mem_text: str) -> dict:
        events = []
        for i in range(8):
            events += _round(f"OLD{i}")
        events += _round("R1", anchor="current task")
        events.append(_E("memory_summary",
                         {"content": mem_text, "slot": "before_task",
                          "after_event_id": "t_OLD0"}, "m1"))
        full = _full_cost(events)
        meta: dict = {}
        build_messages("sys", events, context_window=int(full * 0.6),
                       budget_ratio=1.0, meta_out=meta)
        return meta

    small = run("s")
    large = run("x" * 20_000)
    assert large["dropped_event_count"] > small["dropped_event_count"], (
        small, large)


# ── G: irreducible set overflow is reported, not sent ───────────────────

def test_irreducible_set_overflow_is_flagged():
    """When SYS + mems + anchor + N rounds cannot fit, say so.

    Before ADR-053 the over-long prompt was sent and the server rejected it
    after a full prefill (measured: 204s wasted, logs §4).
    """
    events = [_E("agent_started", {"input": "task"}, "a0")] + _round("T0")
    meta: dict = {}
    build_messages("sys", events, context_window=100, budget_ratio=0.8,
                   meta_out=meta, min_keep_rounds=1)
    assert meta.get("over_budget_unresolvable") is True, meta
    assert meta.get("unresolvable_kind") in {"single_tool_result", "multi_round"}
    assert meta["needed_tokens"] > meta["hard_limit"]


def test_hard_limit_accounts_for_output_reservation():
    """The raise threshold is `context_window - max_tokens` (physical), not
    `budget_tokens` (policy) — so lowering budget_ratio cannot cause a raise."""
    events = [_E("agent_started", {"input": "task"}, "a0")] + _round("T0")
    meta: dict = {}
    build_messages("sys", events, context_window=100_000, budget_ratio=0.1,
                   meta_out=meta, min_keep_rounds=1, max_tokens=1000)
    assert meta.get("hard_limit") == 99_000, meta
    assert not meta.get("over_budget_unresolvable"), meta


# ── G (runtime half): the guard must fire BEFORE any LLM call ───────────
#
# The meta-side assertion above only proves the flag is set.  The contract also
# promises AgentRuntime turns it into a diagnosable status instead of sending a
# request it knows will be rejected.  That half had no test (found in review).
#
# The guard must return before `chat.completions.create`, so this test needs no
# API key and can never hit the network — a sentinel raises if it is reached.

class _MockSandbox:
    def spawn(self, config, cap_token=""):
        return "mock-pid"

    def kill(self, container_id):
        pass

    def is_alive(self, container_id):
        return True

    def get_logs(self, container_id):
        return ""

    def execute(self, container_id, command, restrict=None):
        return ("", "", 0)


def test_runtime_returns_context_overflow_without_calling_llm():
    """Irreducible-set overflow ⇒ status=\"context_overflow\", no LLM call."""
    import tempfile
    import uuid

    from harness.agent_runtime.impl import AgentRuntime
    from harness.gateway.gateway import Gateway
    from harness.interfaces import AgentSpec, Event
    from harness.session_store.impl import SessionStore

    sid = "t-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(base_path=tmp)
        store.create_session(sid, metadata={})

        def put(et, payload, eid, parent=None):
            store.append_event(Event(event_id=eid, session_id=sid, event_type=et,
                                     timestamp=0.0, source="test",
                                     parent_event_id=parent, payload=payload))

        put("agent_started", {"input": "task"}, "a0")
        put("agent_thought", {"content": "", "tool_calls": [
            {"id": "c1", "name": "bash", "arguments": "{}"}]}, "t1")
        put("tool_result", {"tool": "bash", "result": "x" * 50_000}, "r1",
            parent="c1")

        spec = AgentSpec(
            name="overflow-probe", description="probe", tools=[],
            model="deepseek-v4-flash", system_prompt="sys",
            context_window=100,            # irreducible set cannot possibly fit
            context_budget_ratio=0.8, context_min_keep_rounds=1,
            max_tokens=0, max_iterations=2,
        )
        runtime = AgentRuntime(spec=spec, gateway=Gateway(event_bus=None),
                               store=store, api_key="not-used",
                               sandbox=_MockSandbox())

        # Sentinel: reaching the LLM at all is a FAILURE of this test.
        def _boom(*a, **k):
            raise AssertionError("LLM was called despite an unsatisfiable context")

        runtime._call_llm = _boom  # type: ignore[assignment]

        result = runtime._agent_loop(sid, system_prompt="sys")

        assert result.get("status") == "context_overflow", result
        assert result.get("reason") == "irreducible_set_exceeds_limit", result
        assert result.get("error"), "must carry a diagnosable error string"
        failed = [e for e in store.read_events(sid)
                  if e.event_type == "agent_failed"]
        assert len(failed) == 1, [e.event_type for e in store.read_events(sid)]
        assert failed[0].payload.get("reason") == \
            "context_irreducible_set_exceeds_limit"


# ── End-to-end (deterministic): full assembly → LLM → tool → answer ─────
#
# The MR originally cited `harness_smoke.py` as e2e evidence, but that script was
# never in the repo and needs a live local model server, so a reviewer could not
# reproduce it (found in review).  This test reproduces the same path with the
# repo's established in-process MockTransport (ADR-012): no network, no server,
# no API key — and it asserts on the assembled prompt the model would receive.

def test_e2e_assembled_prompt_reaches_tool_and_answer():
    """Whole loop: compose messages → tool call → tool result → final answer."""
    import json as _json
    import tempfile
    import uuid

    import httpx
    import openai

    from harness.agent_runtime.impl import AgentRuntime
    from harness.gateway.gateway import Gateway
    from harness.interfaces import AgentSpec, Event
    from harness.session_store.impl import SessionStore

    def _resp(message: dict, finish: str) -> dict:
        return {"id": "c1", "object": "chat.completion", "created": 0,
                "model": "mock", "choices": [{"index": 0, "message": message,
                                              "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}

    calls: list[dict] = []

    class _T(httpx.BaseTransport):
        def __init__(self):
            super().__init__()
            self._n = 0

        def handle_request(self, request):
            calls.append(_json.loads(request.content))
            self._n += 1
            if self._n == 1:
                return httpx.Response(200, json=_resp({
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "echo",
                                                 "arguments": '{"message":"hi"}'}}],
                }, "tool_calls"), request=request)
            return httpx.Response(200, json=_resp(
                {"role": "assistant", "content": "DONE"}, "stop"),
                request=request)

    sid = "e2e-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(base_path=tmp)
        store.create_session(sid, metadata={})
        store.append_event(Event(event_id="a0", session_id=sid,
                                 event_type="agent_started", timestamp=0.0,
                                 source="test", payload={"input": "say hi"},
                                 parent_event_id=None))

        spec = AgentSpec(name="e2e", description="e2e", tools=["echo"],
                         model="deepseek-v4-flash", system_prompt="sys",
                         context_window=128000, context_min_keep_rounds=1,
                         max_iterations=4)
        runtime = AgentRuntime(spec=spec, gateway=Gateway(event_bus=None),
                               store=store, api_key="sk-test",
                               sandbox=_MockSandbox())
        # `echo` is a test-only tool (see conftest.register_test_tools), not a
        # production builtin — register it the sanctioned way.
        from tests.conftest import register_test_tools
        register_test_tools(runtime._tool_registry)
        runtime._inject_tool_deps()
        runtime._client = openai.OpenAI(
            api_key="sk-test", http_client=httpx.Client(transport=_T()))

        result = runtime._agent_loop(sid, system_prompt="sys")

    assert result.get("status") == "completed", result
    assert "DONE" in (result.get("output") or "")
    # the loop really did two round trips, and the tool result was fed back
    assert len(calls) == 2, len(calls)
    second = calls[1]["messages"]
    assert second[0]["role"] == "system"
    assert any(m["role"] == "assistant" and m.get("tool_calls") for m in second)
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"


# ── R1/P3: output budget reserve is independent of `max_tokens` ─────────

def test_output_reserve_applies_when_max_tokens_is_omitted():
    """max_tokens=None (request omits field) must still reserve output room."""
    events = [_E("agent_started", {"input": "task"}, "a0")] + _round("T0")
    meta: dict = {}
    build_messages("sys", events, context_window=100_000, budget_ratio=1.0,
                   meta_out=meta, min_keep_rounds=1, max_tokens=None,
                   output_reserve_tokens=16_384)
    assert meta["hard_limit"] == 100_000 - 16_384, meta
    assert meta["output_reserve_tokens"] == 16_384, meta
    assert meta["output_reserve_effective"] == 16_384, meta
    assert not meta.get("over_budget_unresolvable"), meta


def test_explicit_max_tokens_wins_over_output_reserve():
    """An explicit request cap is the reserve; output_reserve is the fallback."""
    events = [_E("agent_started", {"input": "task"}, "a0")] + _round("T0")
    meta: dict = {}
    build_messages("sys", events, context_window=100_000, budget_ratio=1.0,
                   meta_out=meta, min_keep_rounds=1, max_tokens=100,
                   output_reserve_tokens=16_384)
    assert meta["hard_limit"] == 100_000 - 100, meta
    assert meta["output_reserve_effective"] == 100, meta


def test_output_reserve_is_capped_to_a_quarter_of_the_window():
    """I-64: a huge reserve must not consume the entire tiny window.

    Old behaviour clamped to `context_window - 1` (hard_limit=1), making the
    prompt unusable and the app's 85% high-water mark unreachable.
    """
    events = [_E("agent_started", {"input": "task"}, "a0")]
    meta: dict = {}
    build_messages("sys", events, context_window=1_000, budget_ratio=1.0,
                   meta_out=meta, min_keep_rounds=1,
                   output_reserve_tokens=16_384)
    assert meta["output_reserve_effective"] == 250, meta
    assert meta["hard_limit"] == 750, meta


def test_tiny_window_keeps_a_reachable_high_water_mark():
    """3200-window + default 16384 reserve leaves a usable prompt budget."""
    events = [_E("agent_started", {"input": "task"}, "a0")] + _round("T0")
    meta: dict = {}
    build_messages("sys", events, context_window=3_200, budget_ratio=1.0,
                   meta_out=meta, min_keep_rounds=1,
                   output_reserve_tokens=16_384)
    assert meta["output_reserve_effective"] == 800, meta
    assert meta["hard_limit"] == 2_400, meta
    assert meta["budget_tokens"] == 2_400, meta
    assert meta["hard_limit"] > 1, "tiny window must keep a usable prompt budget"


def test_explicit_max_tokens_is_also_capped_for_tiny_windows():
    """The request cap is sent unchanged, but the budget reserve is clamped.

    A tiny window must not let an oversized explicit max_tokens starve the
    prompt: `output_reserve_tokens` keeps the original for the request/meta,
    `effective_reserve` is the budget quantity.
    """
    events = [_E("agent_started", {"input": "task"}, "a0")]
    meta: dict = {}
    build_messages("sys", events, context_window=3_200, budget_ratio=1.0,
                   meta_out=meta, min_keep_rounds=1, max_tokens=5_000,
                   output_reserve_tokens=16_384)
    assert meta["output_reserve_tokens"] == 5_000, meta
    assert meta["output_reserve_effective"] == 800, meta
    assert meta["hard_limit"] == 2_400, meta


def test_agent_spec_output_reserve_reaches_the_preview_hard_limit():
    """P3 end-to-end: AgentSpec default 16384 must flow through prompt_preview."""
    import tempfile
    import uuid

    from harness.interfaces import AgentSpec, Event
    from harness.prompt_preview import compose_llm_context
    from harness.session_store.impl import SessionStore

    spec = AgentSpec(name="reserve-preview", description="preview",
                     model="deepseek-v4-flash", context_window=100_000,
                     max_tokens=None)
    assert spec.output_reserve_tokens == 16_384, spec.output_reserve_tokens

    sid = "reserve-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(base_path=tmp)
        store.create_session(sid, metadata={})
        store.append_event(Event(
            event_id="a0", session_id=sid, event_type="agent_started",
            timestamp=0.0, source="test", parent_event_id=None,
            payload={"input": "task"}))
        out = compose_llm_context(store, spec, sid)
    assert out["meta"]["max_tokens"] is None, out["meta"]
    assert out["meta"]["hard_limit"] == 100_000 - 16_384, out["meta"]
