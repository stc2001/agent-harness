"""EventBus — thin wrapper around SessionManager for event persistence.

Gate events are emitted through the injected EventBus so they always
target the parent session.  No in-memory state — events are the single
truth (ADR-035: Gateway owns EventBus internally).
"""

from __future__ import annotations
import time
import uuid
from typing import Any

from harness.interfaces import Event


class EventBus:
    """Thin wrapper around SessionManager for event persistence.

    Provides a future-proof indirection point — swap to Kafka, message
    queue, etc. without touching Gateway internals.
    """

    def __init__(self, session_manager) -> None:
        self._session_manager = session_manager

    def wake(self, session_id: str) -> None:
        self._session_manager.wake(session_id)

    def append(self, session_id: str, event_type: str, source: str,
               event_id: str | None = None, **payload: Any) -> None:
        """Append a typed event to the session log."""
        self._session_manager.append(
            Event(
                event_id=event_id or str(uuid.uuid4()),
                session_id=session_id,
                event_type=event_type,
                timestamp=time.time(),
                source=source,
                payload=payload,
            )
        )

    def get_events(self) -> list[Event]:
        return self._session_manager.get_events()
