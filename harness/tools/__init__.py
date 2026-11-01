"""Built-in agent tools — registered via ToolRegistry with DI support.

Each sub-module exposes a ``register(registry)`` function that registers
its tools on the given ToolRegistry instance.  ``load_builtins()`` creates
a fresh ToolRegistry pre-loaded with all production tools.

Production tools (7):
    read_file, write_file, diff, bash, sandbox_execute,
    spawn_subagent, list_agents

Test-only tools (echo, sum, validate) live in tests/ — not here.
"""

from harness.tool_registry import ToolRegistry


def load_builtins() -> ToolRegistry:
    """Create a ToolRegistry pre-loaded with all built-in tools."""
    registry = ToolRegistry()

    from harness.tools import file_io, sandbox_exec, agent_mgmt, declare_output, install_skill, hashline

    file_io.register(registry)
    sandbox_exec.register(registry)
    agent_mgmt.register(registry)
    declare_output.register(registry)
    install_skill.register(registry)
    hashline.register(registry)

    return registry
