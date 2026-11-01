"""HookContext — data class + contextvars carrier (ADR-042).

Trace/attribution fields ride a contextvars ContextVar so nested hooks,
worker threads and async boundaries see the same values.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any


_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "harness_hook_ctx", default={},
)


def current_context() -> dict[str, Any]:
    """Return the trace/attribution fields for the current context."""
    return _CTX.get()


def set_context_fields(**fields: Any):
    """Set context fields for the duration of a trigger; returns a token."""
    merged = dict(_CTX.get())
    merged.update({k: v for k, v in fields.items() if v is not None})
    return _CTX.set(merged)


def reset_context(token) -> None:
    """Restore the previous context after a trigger."""
    _CTX.reset(token)


@dataclass
class HookContext:
    """Payload + attribution passed to every hook callback."""

    point: str
    payload: Any = None

    # Attribution / trace fields (contextvars-backed when available)
    trace_id: str | None = None
    span_id: str | None = None
    mission_id: str | None = None
    wave: str | None = None
    agent: str | None = None
    # Session this hook fired for.  App-layer memory policy needs it: measuring
    # the assembled context cost is a PUBLIC API that takes a session id
    # (`client.sessions.preview_llm_messages(sid, instance_id)`), and
    # `agent.pre_step` is the documented intervention point for compaction
    # (ADR-034/ADR-053).  Without it the app cannot identify what to compact and
    # is forced to wrap `llm.call` just to capture the id by closure.
    session_id: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, point: str, payload: Any, **fields: Any) -> "HookContext":
        """Construct a context; contextvars fields override call-site kwargs."""
        merged = dict(current_context())
        merged.update({k: v for k, v in fields.items() if v is not None})
        known = {"trace_id", "span_id", "mission_id", "wave", "agent",
                 "session_id", "tokens_in", "tokens_out"}
        extra = {k: v for k, v in merged.items() if k not in known}
        return cls(
            point=point,
            payload=payload,
            trace_id=merged.get("trace_id"),
            span_id=merged.get("span_id"),
            mission_id=merged.get("mission_id"),
            wave=merged.get("wave"),
            agent=merged.get("agent"),
            session_id=merged.get("session_id"),
            tokens_in=merged.get("tokens_in"),
            tokens_out=merged.get("tokens_out"),
            extra=extra,
        )


__all__ = ["HookContext", "current_context", "set_context_fields", "reset_context"]
