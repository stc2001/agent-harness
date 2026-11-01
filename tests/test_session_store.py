"""Tests for Module 2 — SessionStore."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from harness.interfaces import Event, SessionNotFoundError
from harness.session_store.impl import SessionStore


# ─────────────────────────────────────────────────────────────
# 1. create_session
# ─────────────────────────────────────────────────────────────


def test_create_session_creates_directory_tree(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("sess-001", metadata={"job_id": "job-001", "user": "alice"})

    session_dir = Path(temp_store_path) / "sess-001"
    assert session_dir.exists()
    assert (session_dir / "meta.json").exists()
    assert (session_dir / "events.jsonl").exists()
    assert (session_dir / "checkpoints").exists() and (session_dir / "checkpoints").is_dir()


def test_create_session_writes_meta_json(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    meta = {"job_id": "job-002", "tags": ["test", "demo"]}
    store.create_session("sess-002", metadata=meta)

    meta_path = Path(temp_store_path) / "sess-002" / "meta.json"
    with open(meta_path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == meta


def test_create_session_raises_file_exists(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("sess-003", metadata={})
    with pytest.raises(FileExistsError):
        store.create_session("sess-003", metadata={})


# ─────────────────────────────────────────────────────────────
# 2. append_event + read_events
# ─────────────────────────────────────────────────────────────


def test_append_and_read_events_roundtrip(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("sess-004", metadata={})

    event = Event(
        event_id="ev-1",
        session_id="sess-004",
        event_type="agent_started",
        timestamp=1700000000.0,
        source="agent-a",
        payload={"task": "hello"},
    )
    store.append_event(event)

    events = store.read_events("sess-004")
    assert len(events) == 1
    assert events[0].event_id == "ev-1"
    assert events[0].event_type == "agent_started"
    assert events[0].payload == {"task": "hello"}


def test_append_multiple_events(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("sess-005", metadata={})

    for i in range(5):
        store.append_event(
            Event(
                event_id=f"ev-{i}",
                session_id="sess-005",
                event_type="tool_called",
                timestamp=1700000000.0 + i,
                source="agent-b",
                payload={"index": i},
            )
        )

    events = store.read_events("sess-005")
    assert len(events) == 5
    for i, ev in enumerate(events):
        assert ev.event_id == f"ev-{i}"
        assert ev.payload["index"] == i


# ─────────────────────────────────────────────────────────────
# 3. read_events(after_event_id)
# ─────────────────────────────────────────────────────────────


def test_read_events_after_event_id(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("sess-006", metadata={})

    for i in range(10):
        store.append_event(
            Event(
                event_id=f"ev-{i}",
                session_id="sess-006",
                event_type="tick",
                timestamp=1700000000.0 + i,
                source="clock",
                payload={},
            )
        )

    assert len(store.read_events("sess-006")) == 10
    assert len(store.read_events("sess-006", after_event_id="ev-4")) == 5
    assert len(store.read_events("sess-006", after_event_id="ev-8")) == 1
    assert store.read_events("sess-006", after_event_id="ev-9") == []
    assert store.read_events("sess-006", after_event_id="no-such") == []


# ─────────────────────────────────────────────────────────────
# 4. write_checkpoint + read_checkpoint
# ─────────────────────────────────────────────────────────────


def test_write_and_read_checkpoint(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("sess-007", metadata={})

    cp1 = {"state": "running", "counter": 1}
    store.write_checkpoint("sess-007", cp1)
    time.sleep(0.01)  # ensure different timestamp

    cp2 = {"state": "completed", "counter": 2}
    store.write_checkpoint("sess-007", cp2)

    latest = store.read_checkpoint("sess-007")
    assert latest == cp2


def test_read_checkpoint_none_when_empty(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("sess-008", metadata={})
    assert store.read_checkpoint("sess-008") is None


# ─────────────────────────────────────────────────────────────
# 5. SessionNotFoundError
# ─────────────────────────────────────────────────────────────


def test_append_event_raises_session_not_found(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    event = Event(
        event_id="ev-x",
        session_id="no-such-session",
        event_type="agent_started",
        timestamp=1700000000.0,
        source="agent",
        payload={},
    )
    with pytest.raises(SessionNotFoundError):
        store.append_event(event)


def test_read_events_raises_session_not_found(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    with pytest.raises(SessionNotFoundError):
        store.read_events("no-such-session")


def test_write_checkpoint_raises_session_not_found(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    with pytest.raises(SessionNotFoundError):
        store.write_checkpoint("no-such-session", {"foo": "bar"})


def test_read_checkpoint_raises_session_not_found(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    with pytest.raises(SessionNotFoundError):
        store.read_checkpoint("no-such-session")


# ─────────────────────────────────────────────────────────────
# 6. list_sessions
# ─────────────────────────────────────────────────────────────


def test_list_sessions(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("alpha", metadata={})
    store.create_session("beta", metadata={})
    store.create_session("gamma", metadata={})

    sessions = store.list_sessions()
    assert sessions == ["alpha", "beta", "gamma"]


def test_list_sessions_empty_store(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    assert store.list_sessions() == []


def test_list_sessions_ignores_non_session_dirs(temp_store_path: str) -> None:
    store = SessionStore(base_path=temp_store_path)
    store.create_session("valid", metadata={})
    # Create a random directory without the required files
    rogue = Path(temp_store_path) / "rogue"
    rogue.mkdir()
    (rogue / "some_file.txt").write_text("not a session")

    sessions = store.list_sessions()
    assert sessions == ["valid"]


# ─────────────────────────────────────────────────────────────
# 7. Concurrent append_event
# ─────────────────────────────────────────────────────────────


def test_concurrent_append_events(temp_store_path: str) -> None:
    """Multiple threads appending to the same session must not lose or corrupt events.

    This is the regression guard for the per-session threading.Lock that
    protects JSONL writes.  Without it, two threads could interleave
    seeks/writes and truncate each other's lines.
    """
    store = SessionStore(base_path=temp_store_path)
    store.create_session("sess-concurrent", metadata={})

    num_threads = 8
    events_per_thread = 50
    errors: list[Exception] = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(events_per_thread):
                store.append_event(
                    Event(
                        event_id=f"t{thread_id}-e{i}",
                        session_id="sess-concurrent",
                        event_type="concurrent_tick",
                        timestamp=time.time(),
                        source=f"thread-{thread_id}",
                        payload={"thread": thread_id, "seq": i},
                    )
                )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(tid,))
        for tid in range(num_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Exceptions during concurrent append: {errors}"

    events = store.read_events("sess-concurrent")
    assert len(events) == num_threads * events_per_thread

    # Verify every expected event is present and no line is corrupted.
    event_ids = {ev.event_id for ev in events}
    expected_ids = {
        f"t{tid}-e{i}"
        for tid in range(num_threads)
        for i in range(events_per_thread)
    }
    assert event_ids == expected_ids

    # Verify raw file is valid JSONL (no partial/truncated lines).
    events_path = Path(temp_store_path) / "sess-concurrent" / "events.jsonl"
    with open(events_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            # This will raise if any line was truncated/corrupted.
            obj = json.loads(stripped)
            assert "event_id" in obj


# ─────────────────────────────────────────────────────────────
# S1 (I-32): update_session_metadata — merge+persist
# ─────────────────────────────────────────────────────────────


class TestUpdateMetadata:
    def test_merge_patch_persists(self, tmp_path) -> None:
        store = SessionStore(base_path=str(tmp_path / "s"))
        sid = str(uuid.uuid4())
        store.create_session(sid, {"instance_id": "i1", "spec_name": "a"})
        store.update_session_metadata(sid, {"sandbox_id": "sbx-1",
                                            "binding_history": [
                                                {"agent": "a",
                                                 "session_id": sid,
                                                 "sandbox_id": "sbx-1",
                                                 "first_ts": 1.0,
                                                 "last_ts": 1.0}]})
        meta = store.get_session_metadata(sid)
        assert meta["instance_id"] == "i1"  # 旧字段保留
        assert meta["sandbox_id"] == "sbx-1"
        assert meta["binding_history"][0]["agent"] == "a"

    def test_update_missing_session_raises(self, tmp_path) -> None:
        from harness.interfaces import SessionNotFoundError
        store = SessionStore(base_path=str(tmp_path / "s"))
        with pytest.raises(SessionNotFoundError):
            store.update_session_metadata("nope", {"sandbox_id": "x"})

