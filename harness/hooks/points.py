"""Canonical hook point names (ADR-042)."""

from __future__ import annotations

# Gateway authorization (migrated from gateway.register_hook)
AUTHORIZE_VETO = "authorize.veto"
AUTHORIZE_OBSERVE = "authorize.observe"

# Tool execution / result shaping
TOOL_EXECUTE = "tool.execute"
TOOL_RESULT = "tool.result"

# LLM call (routing / metering / tracing)
LLM_CALL = "llm.call"

# Agent lifecycle
AGENT_PRE_STEP = "agent.pre_step"
AGENT_LIFECYCLE = "agent.lifecycle"

# Stop dual-track (I-12)
AGENT_STOP_FRAMEWORK = "agent.stop.framework"
AGENT_STOP_APP = "agent.stop.app"


__all__ = [
    "AUTHORIZE_VETO",
    "AUTHORIZE_OBSERVE",
    "TOOL_EXECUTE",
    "TOOL_RESULT",
    "LLM_CALL",
    "AGENT_PRE_STEP",
    "AGENT_LIFECYCLE",
    "AGENT_STOP_FRAMEWORK",
    "AGENT_STOP_APP",
]
