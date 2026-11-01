"""Agent Harness SDK — programmable interface for Agent Harness.

Usage:
    from harness.sdk import HarnessClient

    client = HarnessClient()

    # Register an agent from a pre-built spec
    spec = AgentSpec(name="worker", ...)
    instance = client.agents.register(spec, name="my-worker")

    # Start a session
    session = client.sessions.start(instance.instance_id)
    client.sessions.events.send(session.session_id, "List files")

    # Stream events
    for event in client.sessions.events.stream(session.session_id):
        print(event.event_type, event.payload)

    # Resolve a pending human gate
    client.gates.approve(gate_event_id)
"""

from __future__ import annotations

from .client import HarnessClient
from .config import HarnessSettings, LLMConfig, resolve_llm_profile
from .models import AgentInstance, AgentStatus, EventInfo, SessionInfo, SessionStatus
from harness.hooks import HookMode, points
from harness.interfaces import AgentSpec, GateInfo, SandboxConfig
from harness.tool_registry import ToolRegistry

__all__ = [
    "GateInfo",
    "HarnessClient",
    "HarnessSettings",
    "LLMConfig",
    "resolve_llm_profile",
    "AgentInstance",
    "AgentStatus",
    "EventInfo",
    "SessionInfo",
    "SessionStatus",
    # ADR-015 (2026-10-19): app-layer construction / registration surfaces.
    "AgentSpec",
    "SandboxConfig",
    "ToolRegistry",
    "HookMode",
    "points",
]
