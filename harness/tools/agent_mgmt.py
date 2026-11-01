"""Built-in tools: agent management — spawn_subagent, list_agents.

- ``spawn_subagent`` — spawner is injected at AgentRuntime level (not via DI)
- ``list_agents`` — queries AgentSpecService registry
"""

import json


def register(registry):
    """Register agent management tools on the given ToolRegistry."""

    @registry.register(
        "spawn_subagent",
        "Spawn a subagent to handle a delegated task. "
        "Returns immediately (non-blocking). "
        "Pull results via read_subagent (ADR-036).",
        {"agent_name": str, "task": str},
    )
    def spawn_subagent(agent_name, task, spawner=None):
        if spawner is None:
            return "Error: spawn_subagent not available — no spawner configured"
        try:
            child_id = spawner(agent_name, task)
            return f"Subagent spawned: {child_id}. Pull results via read_subagent."
        except Exception as exc:
            return f"Error spawning subagent: {exc}"

    @registry.register(
        "list_agents",
        "List all available agent names in the registry.",
        {},
    )
    def list_agents():
        try:
            from harness.agent_spec.impl import AgentSpecService
            svc = AgentSpecService()
            names = svc.list_all()
            return json.dumps(names)
        except Exception as exc:
            return f"Error listing agents: {exc}"
