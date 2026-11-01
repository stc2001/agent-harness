"""Memory slots are fixed insertion points around the Task anchor (ADR-053 rev).

WHY THIS SHAPE
  The harness's job is now narrow: guarantee the request fits, and hold two
  POSITIONS where the app may put its folded history.  Which region a summary
  describes is the APP's business (ADR-001's state machine), so the harness no
  longer infers it from `after_event_id` relative to the anchor.

  What the harness still guarantees:
    * the positions, relative to the Task anchor: one BEFORE, one AFTER
    * the message shape: exactly one `user` message per slot, content wrapped in
      the harness-owned `<mem>…</mem>` placeholder, so the harness can recognise
      its own slots and can never confuse them with the Task anchor
    * SYS and Task are never dropped

  `user` is not arbitrary: `assistant` would break the tool-call pairing
  contract, and a `system` message would shift the cached prefix (SYS sits at
  position 0) and lose KV reuse.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.interfaces import Event  # noqa: E402
from harness.prompt_assembly import build_messages  # noqa: E402

PROVIDER = "deepseek"


def _ev(eid: str, et: str, payload: dict) -> Event:
    return Event(event_id=eid, session_id="s", event_type=et, timestamp=0.0,
                 source="t", parent_event_id=None, payload=payload)


def _task_session() -> list:
    """One task anchor plus one tool round, so the shape is realistic."""
    return [
        _ev("a0", "agent_started", {"input": "translate the book"}),
        _ev("t1", "agent_thought", {"content": "", "tool_calls": [
            {"id": "c1", "name": "bash", "arguments": "{}"}]}),
        _ev("r1", "tool_result", {"tool": "bash", "result": "ok"}),
    ]


def _roles_and_text(events) -> list[tuple[str, str]]:
    msgs = build_messages("SYS", events, context_window=10**9,
                          provider=PROVIDER)
    return [(m["role"], str(m.get("content") or "")) for m in msgs]


def test_slots_sit_immediately_before_and_after_the_task():
    """`slot_before` precedes the Task; `slot_after` follows it; nothing else
    sits between them and the Task."""
    before = _ev("m_before", "memory_summary",
                 {"content": "older history folded", "slot": "before_task",
                  "after_event_id": None})
    after = _ev("m_after", "memory_summary",
                {"content": "current task middle folded",
                 "slot": "after_task", "after_event_id": None})
    events = [before] + _task_session() + [after]

    seq = _roles_and_text(events)
    texts = [t for _, t in seq]

    task_i = next(i for i, t in enumerate(texts) if t.startswith("Task: "))
    before_i = next(i for i, t in enumerate(texts) if "older history folded" in t)
    after_i = next(i for i, t in enumerate(texts) if "current task middle folded" in t)

    assert before_i == task_i - 1, (
        f"slot_before is not immediately before the Task: {seq}")
    assert after_i == task_i + 1, (
        f"slot_after is not immediately after the Task: {seq}")


def test_slots_render_as_user_messages_wrapped_in_the_harness_placeholder():
    """One `user` message per slot, content wrapped in `<mem>…</mem>`.

    The placeholder is harness-owned so the slots are self-identifying: without
    it, a replacement pass could not tell a slot from the Task anchor.
    """
    before = _ev("m_before", "memory_summary",
                 {"content": "PINNED-CONTENT", "slot": "before_task"})
    events = [before] + _task_session()
    seq = _roles_and_text(events)

    slot = [t for _, t in seq if "PINNED-CONTENT" in t]
    assert len(slot) == 1, seq
    assert slot[0] == "<mem>PINNED-CONTENT</mem>", (
        f"the harness must own the placeholder, got {slot[0]!r}")
    role = next(r for r, t in seq if "PINNED-CONTENT" in t)
    assert role == "user", "slot must be a user message (tool pairing + KV)"

    # and the Task anchor is NOT swallowed into a slot
    assert any(t.startswith("Task: ") for _, t in seq), seq


# ── replacement: one slot, newest wins (ADR-001 §4) ─────────────────────

def _isolate_home(tmp_path, monkeypatch):
    """The harness instance registry uses Path.home(); keep it in tmp_path."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def _client(tmp_path, monkeypatch):
    """A real HarnessClient with its session store redirected to tmp_path.

    Uses the public surfaces only: `sessions.start`, `memory.inject_summary`,
    `memory.tail`, and `prompts.build_messages`.
    """
    import os
    from harness.sdk import HarnessClient
    from harness.interfaces import AgentSpec, SandboxConfig

    _isolate_home(tmp_path, monkeypatch)
    os.environ["HARNESS_HOME"] = str(tmp_path)
    c = HarnessClient(sandbox_configs={
        "main": SandboxConfig(backend="bubblewrap",
                              firewall={"network": "host"},
                              shared_folders=[])})
    c._settings.sessions_dir = str(tmp_path / "sessions")
    from harness.session_store.impl import SessionStore
    from harness.session.impl import SessionManager
    c._store = SessionStore(base_path=str(tmp_path / "sessions"))
    c._session_mgr = SessionManager(c._store)
    spec = AgentSpec(name="slot-probe", description="d", tools=[],
                     sandbox="main", model="", max_iterations=1,
                     system_prompt="SYS",
                     permissions={"subagent_spawn": False, "file_access": {}})
    inst = c.agents.register(spec)
    sid = c.sessions.start(inst.instance_id).session_id
    return c, sid


def _assembled_texts(c, sid):
    events = c.session_mgr_logical(sid)
    msgs = c.prompts.build_messages("SYS", events, 10**9)
    return [str(m.get("content") or "") for m in msgs]


def test_injecting_the_same_slot_twice_keeps_only_the_newest(tmp_path, monkeypatch):
    """A slot holds at most ONE summary; a new injection supersedes the old.

    ADR-001 §4: prefix and suffix each have exactly one placeholder.  The harness
    provides the replacement mechanism (the app has no way to delete events), and
    the superseded event stays in the log for auditability.
    """
    c, sid = _client(tmp_path, monkeypatch)
    c.memory.inject_summary(sid, "FIRST-VERSION", slot="before_task")
    c.memory.inject_summary(sid, "SECOND-VERSION", slot="before_task")

    texts = "\n".join(_assembled_texts(c, sid))
    assert "SECOND-VERSION" in texts, texts
    assert "FIRST-VERSION" not in texts, (
        f"the superseded summary is still assembled:\n{texts}")
    assert texts.count("<mem>") == 1, f"slot must hold one message:\n{texts}"


# ── W3: the two truncation outcomes are DIFFERENT events ────────────────

class _RecordingAudit:
    def __init__(self):
        self.entries = []

    def append(self, **kw):
        self.entries.append(kw)

    def named(self, event):
        return [e for e in self.entries if e.get("event") == event]


def _runtime_with_audit(tmp_path):
    """A bare AgentRuntime plus a recording audit log.

    Constructed directly: `pool.acquire` health-checks the model service, which
    is irrelevant to the audit decision under test.
    """
    from harness.interfaces import AgentSpec
    from harness.agent_runtime.impl import AgentRuntime
    from harness.session_store.impl import SessionStore

    store = SessionStore(base_path=str(tmp_path / "sessions"))
    spec = AgentSpec(name="audit-probe", description="d", tools=[],
                     sandbox="main", model="", max_iterations=1,
                     system_prompt="SYS",
                     permissions={"subagent_spawn": False, "file_access": {}})
    rt = AgentRuntime(spec=spec, gateway=object(), store=store,
                      api_key="sk-test", sandbox=object())
    audit = _RecordingAudit()
    rt._audit_log = audit
    return store, rt, audit


def test_dropping_history_is_a_warning_not_an_error(tmp_path):
    """When history is dropped and the request still fits, that is the FALLBACK
    working as designed ⇒ `history_context_truncated` at level `warn`.

    The name says what was truncated: the history.  SYS and the Task anchor are
    never truncated, so a generic "prompt_truncated" would misdescribe it.
    """
    store, rt, audit = _runtime_with_audit(tmp_path)
    sid = "audit-session"

    rt._audit_truncation(sid, {"truncated": True, "dropped_rounds": 12,
                               "dropped_event_count": 24, "kept_rounds": 3,
                               "budget_tokens": 1200, "hard_limit": 1200})
    hits = audit.named("history_context_truncated")
    assert hits, [e.get("event") for e in audit.entries]
    assert hits[0]["level"] == "warn", hits[0]
    assert hits[0]["detail"]["preserved"] == ["system", "task", "memory_slots"]
    assert not audit.named("prompt_truncated"), "old umbrella event still emitted"


def test_unresolvable_overflow_is_an_error(tmp_path):
    """No history left to drop and still over the limit ⇒ the app's fixed
    overhead is too large.  That is a CONFIG error, not a fallback: level
    `error`, distinct event name.
    """
    store, rt, audit = _runtime_with_audit(tmp_path)
    sid = "audit-session"

    rt._audit_truncation(sid, {"over_budget_unresolvable": True,
                               "truncated": True, "dropped_rounds": 0,
                               "dropped_event_count": 0, "kept_rounds": 0,
                               "budget_tokens": 1200, "hard_limit": 1200})
    hits = audit.named("over_budget_unresolvable")
    assert hits, [e.get("event") for e in audit.entries]
    assert hits[0]["level"] == "error", hits[0]


# ── W4: the public surfaces an app needs (no private reach-through) ─────

def test_app_can_read_the_logical_event_view_without_private_names(tmp_path, monkeypatch):
    """`client.sessions.events.logical(sid)` — not the private `_logical_events`.

    SessionManager.logical_view is already the ONE documented read face
    (ADR-050/I-31: every read rebuilds the logical view, and callers must not
    consume the physical log).  The SDK wrapper simply exposes it.
    """
    import os
    from harness.interfaces import AgentSpec, SandboxConfig
    from harness.sdk import HarnessClient

    _isolate_home(tmp_path, monkeypatch)
    os.environ["HARNESS_HOME"] = str(tmp_path)
    c = HarnessClient(sandbox_configs={
        "main": SandboxConfig(backend="bubblewrap",
                              firewall={"network": "host"},
                              shared_folders=[])})
    c._settings.sessions_dir = str(tmp_path / "sessions")
    from harness.session_store.impl import SessionStore
    from harness.session.impl import SessionManager
    c._store = SessionStore(base_path=str(tmp_path / "sessions"))
    c._session_mgr = SessionManager(c._store)
    spec = AgentSpec(name="events-probe", description="d", tools=[],
                     sandbox="main", model="", max_iterations=1,
                     system_prompt="SYS",
                     permissions={"subagent_spawn": False, "file_access": {}})
    inst = c.agents.register(spec)
    sid = c.sessions.start(inst.instance_id).session_id

    # put one event in the log, then read it back through the public face
    from harness.interfaces import Event
    c._store.append_event(Event(
        event_id="ev1", session_id=sid, event_type="agent_started",
        timestamp=0.0, source="test", parent_event_id=None,
        payload={"input": "hello"}))

    events = c.sessions.events.logical(sid)
    assert isinstance(events, list)
    assert [e.event_id for e in events] == ["ev1"], events


def test_app_can_count_tokens_without_reaching_into_prompt_assembly(tmp_path, monkeypatch):
    """`client.memory.count_tokens(text)` — not `prompt_assembly._count_tokens`.

    Token counting is a harness mechanism (it must know how big a request is),
    so it belongs on the public surface rather than being imported privately by
    an app.
    """
    import os
    from harness.sdk import HarnessClient
    from harness.interfaces import SandboxConfig

    _isolate_home(tmp_path, monkeypatch)
    os.environ["HARNESS_HOME"] = str(tmp_path)
    c = HarnessClient(sandbox_configs={
        "main": SandboxConfig(backend="bubblewrap",
                              firewall={"network": "host"},
                              shared_folders=[])})
    n = c.memory.count_tokens("hello world")
    assert isinstance(n, int) and n > 0, n


def test_app_can_resolve_a_profile_without_importing_sdk_internals(tmp_path, monkeypatch):
    """`client.config.profile(name)` — not `harness.sdk.config.resolve_llm_profile`."""
    import os
    from harness.sdk import HarnessClient
    from harness.interfaces import SandboxConfig

    _isolate_home(tmp_path, monkeypatch)
    os.environ["HARNESS_HOME"] = str(tmp_path)
    c = HarnessClient(sandbox_configs={
        "main": SandboxConfig(backend="bubblewrap",
                              firewall={"network": "host"},
                              shared_folders=[])})
    prof = c.config.profile("default")
    assert prof is not None
    assert hasattr(prof, "base_url"), prof


# ── R1/P2: slot boundaries must never swallow the Task anchor ──────────

def _task_present(events) -> bool:
    seq = _roles_and_text(events)
    return any(t.startswith("Task: ") for _, t in seq)


def test_before_slot_boundary_keeps_task_anchor():
    events = [_ev("p_old", "agent_thought", {"content": "old"}),
              _ev("p0", "agent_thought", {"content": ""})] + _task_session()
    events.insert(0, _ev("m_before", "memory_summary",
                         {"content": "old folded", "slot": "before_task",
                          "after_event_id": "p0"}))
    assert _task_present(events)
    texts = [t for _, t in _roles_and_text(events)]
    assert any("Task: " in t for t in texts)
    assert not any(t == "old" for t in texts), "prefix before boundary was not folded"


def test_after_slot_boundary_keeps_task_anchor():
    events = _task_session() + [_ev("s1", "agent_thought", {"content": "mid"})]
    events.append(_ev("m_after", "memory_summary",
                      {"content": "mid folded", "slot": "after_task",
                       "after_event_id": "s1"}))
    assert _task_present(events)


def test_before_slot_boundary_after_anchor_still_keeps_task():
    events = _task_session() + [_ev("m_before", "memory_summary",
                                    {"content": "x", "slot": "before_task",
                                     "after_event_id": "t1"})]
    assert _task_present(events)


def test_after_slot_boundary_before_anchor_still_keeps_task():
    events = [_ev("p0", "agent_thought", {"content": "old"}),
              _ev("m_after", "memory_summary",
                  {"content": "x", "slot": "after_task",
                   "after_event_id": "p0"})] + _task_session()
    assert _task_present(events)


def test_two_slots_with_boundaries_keep_task_and_both_summaries():
    events = [_ev("p0", "agent_thought", {"content": "old"}),
              _ev("m_before", "memory_summary",
                  {"content": "BEFORE", "slot": "before_task",
                   "after_event_id": "p0"})] + _task_session() + [
              _ev("s1", "agent_thought", {"content": "mid"}),
              _ev("m_after", "memory_summary",
                  {"content": "AFTER", "slot": "after_task",
                   "after_event_id": "s1"})]
    seq = _roles_and_text(events)
    texts = [t for _, t in seq]
    assert any(t.startswith("Task: ") for t in texts)
    assert any("<mem>BEFORE</mem>" == t for t in texts), texts
    assert any("<mem>AFTER</mem>" == t for t in texts), texts


# ── R1/P5: legacy summary without slot is ignored, but reported ────────

def test_legacy_summary_without_slot_is_ignored_and_reported():
    legend = _ev("m_legacy", "memory_summary", {"content": "LEGACY"})
    events = [legend] + _task_session()
    meta: dict = {}
    msgs = build_messages("SYS", events, context_window=10**9,
                          provider=PROVIDER, meta_out=meta)
    texts = [str(m.get("content") or "") for m in msgs]
    assert all("LEGACY" not in t for t in texts), texts
    assert meta.get("ignored_legacy_summary_count") == 1, meta
    assert any(t.startswith("Task: ") for t in texts), "Task lost"


# ── R1/P3: client.config.profiles() works ──────────────────────────────

def test_config_profiles_lists_project_profiles(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    project = tmp_path / "project"
    (project / "configs").mkdir(parents=True)
    (project / "configs" / "llm.yaml").write_text(
        "alpha:\n  api_key: a\nbeta:\n  api_key: b\n", encoding="utf-8")
    # I-60: SDK surfaces resolve against client.project_dir (not cwd)
    c._project_dir = project
    assert c.config.profiles() == ["alpha", "beta"]

# ── A1/A2: the harness keeps BOTH positions (mechanism, not policy) ─────

def test_both_slot_positions_are_supported_mechanism(tmp_path):
    """The harness offers TWO positions around the Task anchor, and both work.

    This is deliberately a MECHANISM lock: the app currently writes only
    `before_task` (the consolidated history checkpoint, as DSH does), but the
    harness must keep `after_task` available for future needs.  Removing it would
    be a capability cut, not a simplification — what ADR-038 says the harness must
    NOT hold is the unbounded *inference* of which region a summary describes
    (that is the app's call), not the bounded *positions* themselves.
    """
    before = _ev("m_before", "memory_summary",
                 {"content": "OLDER", "slot": "before_task"})
    after = _ev("m_after", "memory_summary",
                {"content": "MIDDLE", "slot": "after_task"})
    seq = _roles_and_text([before] + _task_session() + [after])
    texts = [t for _, t in seq]

    task_i = next(i for i, t in enumerate(texts) if t.startswith("Task: "))
    assert texts[task_i - 1] == "<mem>OLDER</mem>", seq
    assert texts[task_i + 1] == "<mem>MIDDLE</mem>", seq


def test_inject_summary_accepts_both_positions_over_the_public_surface(tmp_path, monkeypatch):
    """Both slot values are accepted by `client.memory.inject_summary`."""
    import os
    from harness.interfaces import AgentSpec, SandboxConfig
    from harness.sdk import HarnessClient
    from harness.session.impl import SessionManager
    from harness.session_store.impl import SessionStore

    _isolate_home(tmp_path, monkeypatch)
    os.environ["HARNESS_HOME"] = str(tmp_path)
    c = HarnessClient(sandbox_configs={
        "main": SandboxConfig(backend="bubblewrap",
                              firewall={"network": "host"},
                              shared_folders=[])})
    c._settings.sessions_dir = str(tmp_path / "sessions")
    c._store = SessionStore(base_path=str(tmp_path / "sessions"))
    c._session_mgr = SessionManager(c._store)
    spec = AgentSpec(name="slot-mechanism", description="d", tools=[],
                     sandbox="main", model="", max_iterations=1,
                     system_prompt="SYS",
                     permissions={"subagent_spawn": False, "file_access": {}})
    inst = c.agents.register(spec)
    sid = c.sessions.start(inst.instance_id).session_id

    c.memory.inject_summary(sid, "A", slot="before_task")
    c.memory.inject_summary(sid, "B", slot="after_task")

    from harness.interfaces import Event
    c._store.append_event(Event(
        event_id="a0", session_id=sid, event_type="agent_started",
        timestamp=0.0, source="test", parent_event_id=None,
        payload={"input": "the task"}))
    msgs = c.prompts.build_messages("SYS", c.sessions.events.logical(sid),
                                    10**9)
    texts = [str(m.get("content") or "") for m in msgs]
    ti = next(i for i, t in enumerate(texts) if t.startswith("Task: "))
    assert texts[ti - 1] == "<mem>A</mem>", texts
    assert texts[ti + 1] == "<mem>B</mem>", texts


def test_unknown_slot_is_refused(tmp_path, monkeypatch):
    """A slot the harness does not know is refused, not guessed into a position."""
    import os
    import pytest
    from harness.interfaces import AgentSpec, SandboxConfig
    from harness.sdk import HarnessClient
    from harness.session.impl import SessionManager
    from harness.session_store.impl import SessionStore

    _isolate_home(tmp_path, monkeypatch)
    os.environ["HARNESS_HOME"] = str(tmp_path)
    c = HarnessClient(sandbox_configs={
        "main": SandboxConfig(backend="bubblewrap",
                              firewall={"network": "host"},
                              shared_folders=[])})
    c._settings.sessions_dir = str(tmp_path / "sessions")
    c._store = SessionStore(base_path=str(tmp_path / "sessions"))
    c._session_mgr = SessionManager(c._store)
    spec = AgentSpec(name="slot-unknown", description="d", tools=[],
                     sandbox="main", model="", max_iterations=1,
                     system_prompt="SYS",
                     permissions={"subagent_spawn": False, "file_access": {}})
    inst = c.agents.register(spec)
    sid = c.sessions.start(inst.instance_id).session_id

    with pytest.raises(ValueError):
        c.memory.inject_summary(sid, "X", slot="middle")

