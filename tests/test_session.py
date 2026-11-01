"""SessionManager (I-31): stateless append-only log + agent-derived holder — tests."""

from __future__ import annotations

import uuid

import pytest

from harness.interfaces import Event
from harness.session_store.impl import SessionStore
from harness.session.impl import SessionManager


def _ev(sid, etype, payload=None, ev_id=None):
    return Event(event_id=ev_id or str(uuid.uuid4()), session_id=sid,
                 event_type=etype, timestamp=0.0, source="t",
                 parent_event_id=None, payload=payload or {})


@pytest.fixture
def store(tmp_path):
    return SessionStore(base_path=str(tmp_path / "s"))


@pytest.fixture
def mgr(store):
    return SessionManager(store)


class TestStatelessLog:
    def test_append_no_transition_validation(self, mgr, store):
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        mgr.append(_ev(sid, "agent_completed", {"output": "x"}))
        mgr.append(_ev(sid, "agent_started", {"input": "again"}))
        mgr.append(_ev(sid, "weird_new_type", {"a": 1}))
        view = mgr.wake(sid)
        assert [e.event_type for e in view] == [
            "agent_completed", "agent_started", "weird_new_type"]

    def test_rewind_marker_truncates_logical_view(self, mgr, store):
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        e1 = _ev(sid, "agent_started", {})
        store.append_event(e1)
        store.append_event(_ev(sid, "agent_thought", {}))
        mgr.rewind(sid, e1.event_id)
        view = mgr.wake(sid)
        assert [e.event_type for e in view] == ["agent_started",
                                                "session_rewound"]
        assert len(store.read_events(sid)) == 3

    def test_fork_remaps_ids_and_refs(self, mgr, store):
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        tc = _ev(sid, "tool_called", {"tool": "bash"})
        store.append_event(tc)
        tr = Event(event_id="t2", session_id=sid, event_type="tool_result",
                    timestamp=0.0, source="t", parent_event_id=tc.event_id,
                    payload={"result": "r"})
        store.append_event(tr)
        new = str(uuid.uuid4())
        mgr.fork(sid, new)
        forked = store.read_events(new)
        assert len(forked) == 2
        ids = {e.event_id for e in forked}
        assert len(ids) == 2 and tc.event_id not in ids
        tr_f = [e for e in forked if e.event_type == "tool_result"][0]
        tc_f = [e for e in forked if e.event_type == "tool_called"][0]
        assert tr_f.parent_event_id == tc_f.event_id

    def test_checkpoint_persists_event(self, mgr, store):
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        snap = mgr.checkpoint(sid, {"prompt": "p"})
        assert snap["task"]["prompt"] == "p"
        evs = store.read_events(sid)
        assert evs[-1].event_type == "checkpoint_created"
        assert evs[-1].payload["task"]["prompt"] == "p"


class FakeStore:
    def __init__(self, meta):
        self._meta = meta
        self._events = []

    def get_session_metadata(self, sid):
        return self._meta

    def read_events(self, sid):
        return list(self._events)

    def append_event(self, e):
        self._events.append(e)

    def create_session(self, sid, meta):
        pass

    def list_sessions(self):
        return []


class _Agent:
    def __init__(self, status):
        self.status = status


class _Pool:
    def __init__(self, agent):
        self._a = agent

    def get_by_instance_id(self, iid):
        return self._a


def _sess_client(meta, agent):
    from harness.sdk.client import _SessionsClient
    client = type("C", (), {})()
    client._store = FakeStore(meta)
    client._pool = _Pool(agent)
    return _SessionsClient(client)


class TestHolderAndGuard:
    def test_unbound_when_no_instance(self):
        sc = _sess_client({}, None)
        assert sc.holder("s")["holder_status"] == "unbound"
        sc._guard_not_running("s")

    def test_idle_held(self):
        sc = _sess_client({"instance_id": "i1"}, _Agent("idle"))
        h = sc.holder("s")
        assert h["holder_instance_id"] == "i1"
        assert h["holder_status"] == "idle"
        sc._guard_not_running("s")

    def test_running_blocks_guard(self):
        sc = _sess_client({"instance_id": "i1"}, _Agent("running"))
        assert sc.holder("s")["holder_status"] == "running"
        with pytest.raises(RuntimeError, match="running"):
            sc._guard_not_running("s")

    def test_stopped_derives_unbound(self):
        sc = _sess_client({"instance_id": "i1"}, _Agent("stopped"))
        assert sc.holder("s")["holder_status"] == "unbound"
        sc._guard_not_running("s")

