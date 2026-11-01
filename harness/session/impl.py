"""Module 5 — Session: stateless append-only log with log-level ops.

I-31 (2026-09-07): Session is a pure append-only log over SessionStore;
no in-memory status machine.  Lifecycle authority lives on the AGENT
(ADR-043); session holding/guards derive from it.  rewind/fork/checkpoint
remain log operations here.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from harness.interfaces import (
    Event,
    SessionInterface,
    SessionStateError,
    SessionNotFoundError,
)
from harness.session_store.impl import SessionStore


_EVENT_REF_KEYS = {"parent_event_id", "gate_event_id"}


def _remap_event_refs(
    payload: dict[str, Any], id_map: dict[str, str]
) -> dict[str, Any]:
    """Return a copy of payload with event-ID references remapped."""

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (id_map.get(v, v) if k in _EVENT_REF_KEYS
                     and isinstance(v, str) else walk(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(dict(payload))


class SessionManager(SessionInterface):
    """Stateless log operations over SessionStore.

    Methods take an explicit session_id; every read rebuilds the logical
    view from physical events (rewind markers replay).  No status is kept.
    """

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    # ── reads ──
    def wake(self, session_id: str) -> list[Event]:
        """Load a session and return its LOGICAL event view (no status)."""
        physical = self._store.read_events(session_id)
        return self._rebuild_logical_events(physical)

    def get_events(self, session_id: str) -> list[Event]:
        """Logical events for a session (alias of wake)."""
        return self.wake(session_id)

    def list_sessions(self) -> list[str]:
        return self._store.list_sessions()

    # ── writes (append-only) ──
    def append(self, event: Event) -> None:
        """Append an event to the log (no transition validation)."""
        self._store.append_event(event)

    def rewind(self, session_id: str, to_event_id: str) -> None:
        """Rewind: append a session_rewound marker targeting to_event_id.

        Logical view replays markers (see _rebuild_logical_events); the
        physical log stays append-only.  Callers guard against a RUNNING
        bound agent (I-31).
        """
        events = self.wake(session_id)
        if not any(e.event_id == to_event_id for e in events):
            raise SessionStateError(
                f"Event {to_event_id!r} not found in session {session_id}.")
        marker = Event(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            event_type="session_rewound",
            timestamp=time.time(),
            source="session_manager",
            payload={"target_event_id": to_event_id},
        )
        self._store.append_event(marker)

    def fork(self, session_id: str, new_session_id: str) -> str:
        """Fork the logical view into a new session (fresh event ids)."""
        if new_session_id in self._store.list_sessions():
            raise SessionStateError(
                f"Session {new_session_id!r} already exists.")
        self._store.create_session(new_session_id,
                                  {"forked_from": session_id})
        events = self.wake(session_id)
        id_map = {ev.event_id: str(uuid.uuid4()) for ev in events}
        for ev in events:
            self._store.append_event(Event(
                event_id=id_map[ev.event_id],
                session_id=new_session_id,
                event_type=ev.event_type,
                timestamp=ev.timestamp,
                source=ev.source,
                parent_event_id=(id_map.get(ev.parent_event_id)
                                  if ev.parent_event_id else None),
                payload=_remap_event_refs(ev.payload, id_map),
            ))
        return new_session_id

    def checkpoint(self, session_id: str, task: dict | None = None) -> dict:
        """Record a checkpoint_created event (crash-recovery marker).

        I-11/ADR-039 task boundary snapshot as an append-only log event.
        """
        events = self.wake(session_id)
        snapshot = {
            "session_id": session_id,
            "event_count": len(events),
            "task": task or {},
        }
        self._store.append_event(Event(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            event_type="checkpoint_created",
            timestamp=time.time(),
            source="session_manager",
            payload=snapshot,
        ))
        return snapshot

    def logical_view(self, session_id: str) -> list[Event]:
        """I-41: 日志层公开读面 — 物理日志的**逻辑视图**（rewind 标记重放）。

        ADR-050/I-31 声明「每次读取都重建逻辑视图」；本方法是该契约的
        唯一入口：调用方（runtime / SDK 读面）不得直接消费物理日志。
        """
        return logical_view(self._store.read_events(session_id))

    # ── helpers ──
    @staticmethod
    def _rebuild_logical_events(physical: list[Event]) -> list[Event]:
        """Back-compat 别名：见模块级 ``logical_view``。"""
        return logical_view(physical)


def logical_view_of(store, session_id: str) -> list[Event]:
    """I-42: 语义读面（store 版）——日志层实现，供 runtime/编排组合使用。

    物理日志读取只允许出现在日志层（此处）与审计读面；调用方不得直接
    消费 ``store.read_events()``（见 ADR-050 I-42 注记）。
    """
    return logical_view(store.read_events(session_id))


def logical_view(physical: list[Event]) -> list[Event]:
    """I-41: 纯函数形式的逻辑视图重建（rewind 标记截断到目标事件）。

    无状态模块操作，供 SessionManager 与需要组合日志投影的调用方复用。
    """
    logical: list[Event] = []
    for ev in physical:
        if ev.event_type == "session_rewound":
            target_id = ev.payload.get("target_event_id")
            for i, e in enumerate(logical):
                if e.event_id == target_id:
                    logical = logical[: i + 1]
                    break
            logical.append(ev)
        else:
            logical.append(ev)
    return logical