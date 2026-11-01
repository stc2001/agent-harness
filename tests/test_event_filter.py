"""Tests for P2-4 event filtering API — events.filter() + events.find()."""

from __future__ import annotations

from harness.sdk.models import EventInfo


def _make_event(event_id="e1", event_type="tool_called", payload=None):
    return EventInfo(
        event_id=event_id,
        event_type=event_type,
        source="test",
        payload=payload or {},
        timestamp=0.0,
    )



def _fake_client(fake_store):
    """FakeClient 桩：提供 I-42 语义读面接缝（真实客户端由 HarnessClient 提供）。"""
    from harness.session.impl import logical_view

    def _logical_events(session_id, after_event_id=None):
        events = logical_view(fake_store.read_events(session_id))
        if after_event_id is None:
            return events
        out, found = [], False
        for ev in events:
            if found:
                out.append(ev)
            elif ev.event_id == after_event_id:
                found = True
        return out

    return type("FakeClient", (), {"_store": fake_store,
                                  "_logical_events": staticmethod(_logical_events)})()


class TestEventFilter:
    """events.filter(session_id, event_type=...)"""

    def test_filter_by_event_type_returns_matches(self):
        """filter() returns only events of the requested type."""
        from harness.sdk.client import _SessionEventsClient

        events = [
            _make_event("e1", "agent_started"),
            _make_event("e2", "tool_called", {"tool": "read_file"}),
            _make_event("e3", "tool_result", {"tool": "read_file"}),
            _make_event("e4", "tool_called", {"tool": "declare_output_path",
                                              "args": {"path": "/workspace/results"}}),
            _make_event("e5", "agent_completed"),
        ]

        fake_store = _FakeStore(events)
        client = _fake_client(fake_store)
        sec = _SessionEventsClient(client)

        result = sec.filter("sess_1", event_type="tool_called")
        assert len(result) == 2
        assert all(e.event_type == "tool_called" for e in result)
        assert result[0].payload["tool"] == "read_file"
        assert result[1].payload["tool"] == "declare_output_path"

    def test_filter_returns_empty_when_no_match(self):
        """filter() returns empty list when no events match."""
        from harness.sdk.client import _SessionEventsClient

        events = [_make_event("e1", "agent_started")]
        fake_store = _FakeStore(events)
        client = _fake_client(fake_store)
        sec = _SessionEventsClient(client)

        result = sec.filter("sess_1", event_type="tool_called")
        assert result == []

    def test_filter_empty_session_returns_empty(self):
        """filter() on session with no events returns empty list."""
        from harness.sdk.client import _SessionEventsClient

        fake_store = _FakeStore([])
        client = _fake_client(fake_store)
        sec = _SessionEventsClient(client)

        result = sec.filter("sess_1", event_type="tool_called")
        assert result == []

    def test_filter_respects_after_event_id(self):
        """filter() after_event_id skips earlier events."""
        from harness.sdk.client import _SessionEventsClient

        events = [
            _make_event("e1", "tool_called"),
            _make_event("e2", "agent_started"),
            _make_event("e3", "tool_called"),
        ]
        fake_store = _FakeStore(events)
        client = _fake_client(fake_store)
        sec = _SessionEventsClient(client)

        result = sec.filter("sess_1", event_type="tool_called",
                            after_event_id="e1")
        assert len(result) == 1
        assert result[0].event_id == "e3"


class TestEventFind:
    """events.find(session_id, predicate=...)"""

    def test_find_returns_first_match(self):
        """find() returns the first event matching the predicate."""
        from harness.sdk.client import _SessionEventsClient

        events = [
            _make_event("e1", "agent_started"),
            _make_event("e2", "tool_called", {"tool": "read_file"}),
            _make_event("e3", "tool_called", {"tool": "declare_output_path",
                                              "args": {"path": "/workspace/results"}}),
            _make_event("e4", "tool_called", {"tool": "bash"}),
        ]
        fake_store = _FakeStore(events)
        client = _fake_client(fake_store)
        sec = _SessionEventsClient(client)

        found = sec.find(
            "sess_1",
            predicate=lambda e: e.payload.get("tool") == "declare_output_path",
        )
        assert found is not None
        assert found.event_id == "e3"
        assert found.payload["args"]["path"] == "/workspace/results"

    def test_find_returns_none_when_no_match(self):
        """find() returns None when no event matches."""
        from harness.sdk.client import _SessionEventsClient

        events = [_make_event("e1", "agent_started")]
        fake_store = _FakeStore(events)
        client = _fake_client(fake_store)
        sec = _SessionEventsClient(client)

        found = sec.find("sess_1", predicate=lambda e: False)
        assert found is None

    def test_find_respects_after_event_id(self):
        """find() with after_event_id skips earlier events."""
        from harness.sdk.client import _SessionEventsClient

        events = [
            _make_event("e1", "tool_called", {"tool": "declare_output_path"}),
            _make_event("e2", "tool_called", {"tool": "bash"}),
            _make_event("e3", "tool_called", {"tool": "declare_output_path"}),
        ]
        fake_store = _FakeStore(events)
        client = _fake_client(fake_store)
        sec = _SessionEventsClient(client)

        found = sec.find(
            "sess_1",
            predicate=lambda e: "declare_output_path" in str(e.payload),
            after_event_id="e1",
        )
        assert found is not None
        assert found.event_id == "e3"


class _FakeStore:
    """Minimal store that returns pre-configured events."""

    def __init__(self, events):
        self._events = events

    def read_events(self, session_id, after_event_id=None):
        return self._events
