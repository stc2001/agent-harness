"""_fire_post_tool_hook: hook_result only when a hook rewrote the payload — TDD."""

from __future__ import annotations

import pytest

from harness.gateway.gateway import Gateway
from harness.hooks import HookMode, HookRegistry, points
from harness.interfaces import AgentSpec
from harness.agent_runtime.impl import AgentRuntime


class _Store:
    def __init__(self):
        self.events = []

    def append_event(self, event) -> None:
        self.events.append(event)

    def read_events(self, sid):
        return list(self.events)


@pytest.fixture(autouse=True)
def _clean():
    reg = HookRegistry.get()
    reg.clear()
    yield
    reg.clear()


@pytest.fixture
def rt():
    spec = AgentSpec(name="h", description="t", tools=["bash"],
                     model="deepseek-v4-flash", sandbox="main",
                     max_iterations=1, llm_profile="default",
                     permissions={"subagent_spawn": False})
    from unittest.mock import MagicMock
    return AgentRuntime(spec=spec, gateway=Gateway(event_bus=None),
                        store=_Store(), api_key="x", sandbox=MagicMock())


def _types(store):
    return [e.event_type for e in store.events]


def test_no_hook_no_hook_result(rt) -> None:
    """无 tool.execute hook → 不发 hook_result（原 payload 恒真不再误发）."""
    rt._fire_post_tool_hook("bash", {"command": "ls"}, "out", "s1", True)
    assert _types(rt.store) == []


def test_observe_hook_no_emit(rt) -> None:
    """仅 observe hook（返回 None）→ 不发."""
    seen = []

    def obs(ctx):
        seen.append(ctx.payload)

    HookRegistry.get().on(points.TOOL_EXECUTE, HookMode.OBSERVE, obs)
    rt._fire_post_tool_hook("bash", {"command": "ls"}, "out", "s1", True)
    assert _types(rt.store) == []
    assert len(seen) == 1


def test_around_replacing_payload_emits(rt) -> None:
    """around hook 替换载荷 → hook_result 携带新载荷（有审计价值）."""
    def around(ctx, next_fn):
        return {"altered": True, "by": "test"}

    HookRegistry.get().on(points.TOOL_EXECUTE, HookMode.AROUND, around)
    rt._fire_post_tool_hook("bash", {"command": "ls"}, "out", "s1", True)
    hooks = [e for e in rt.store.events
             if e.event_type == "hook_result"]
    assert len(hooks) == 1
    assert hooks[0].payload["result"]["altered"] is True
