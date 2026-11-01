"""SDK data models — AgentInstance, RuntimeConfig, Session info.

These are user-facing types returned by HarnessClient methods.
They are NOT the internal AgentSpec/Event types from harness.interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AgentStatus(str, Enum):
    """Agent instance lifecycle (ADR-043)."""

    COMPILED = "compiled"       # Instance exists in registry
    IDLE = "idle"               # Task loop exited; reusable (wake-able)
    RUNNING = "running"         # Executing a task
    STOPPED = "stopped"         # Explicitly stopped; resources released


class SessionStatus(str, Enum):
    """Runtime status of a session."""

    CREATED = "created"
    RUNNING = "running"
    WAITING_GATE = "waiting_gate"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentInstance:
    """A compiled agent instance — the result of `agents.register()`.

    Lightweight identity record.  Lifecycle is managed by Session, not
    by AgentInstance.  LLM profile is stored for session start resolution.
    """

    instance_id: str
    spec_name: str
    llm_profile: str
    created_at: str
    status: AgentStatus = AgentStatus.COMPILED
    sandbox_id: str | None = None  # I-26: pinned sandbox entity (uuid); None = canonical

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "spec_name": self.spec_name,
            "llm_profile": self.llm_profile,
            "created_at": self.created_at,
            "status": self.status.value,
            "sandbox_id": self.sandbox_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentInstance":
        return cls(
            instance_id=data["instance_id"],
            spec_name=data["spec_name"],
            llm_profile=data["llm_profile"],
            created_at=data["created_at"],
            sandbox_id=data.get("sandbox_id"),
            status=AgentStatus(data.get("status", "compiled")),
        )


@dataclass
class SessionInfo:
    """Runtime session information."""

    session_id: str
    instance_id: str
    status: SessionStatus
    started_at: str
    ended_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventInfo:
    """Lightweight event wrapper for SDK consumers."""

    event_id: str
    event_type: str
    source: str
    payload: dict[str, Any]
    timestamp: float

    @classmethod
    def from_internal(cls, event: Any) -> "EventInfo":
        """Convert internal Event to SDK EventInfo."""
        return cls(
            event_id=event.event_id,
            event_type=event.event_type,
            source=event.source,
            payload=event.payload,
            timestamp=event.timestamp,
        )


@dataclass
class RunResult:
    """I-23 (ADR-015): result of client.agents.run() — a sync task run.

    status: completed | failed | stopped
    output: terminal output from the event stream (agent_completed.output,
        falling back to the last agent_thought content); "" if none.
    error:  failure detail when status == failed.
    stop_reason: ADR-044 reason only when status == stopped (I-12).
    """

    session_id: str
    instance_id: str
    status: str
    output: str = ""
    error: str | None = None
    stop_reason: str | None = None
    instance_status: str | None = None  # I-39: 返回瞬间的实例状态快照
