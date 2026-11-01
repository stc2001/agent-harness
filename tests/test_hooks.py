"""I-08 (ADR-042): unified hooks module — four modes, priority, error isolation.

Red tests first: observe / transform / veto / around semantics, then
priority determinism, error isolation, and contextvars propagation.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from harness.hooks import HookMode, HookRegistry, HookContext
from harness.hooks import points


class TestObserveMode:
    """observe hooks see payload but cannot change it."""

    def test_observe_records_but_keeps_payload(self):
        reg = HookRegistry()
        seen: list[Any] = []
        reg.on(points.TOOL_RESULT, HookMode.OBSERVE, lambda ctx: seen.append(ctx.payload))
        result = reg.trigger(points.TOOL_RESULT, "ORIGINAL")
        assert seen == ["ORIGINAL"]
        assert result.payload == "ORIGINAL"
        assert not result.vetoed


class TestTransformMode:
    """transform hooks replace the payload with their return value."""

    def test_transform_replaces_payload(self):
        reg = HookRegistry()
        reg.on(points.TOOL_RESULT, HookMode.TRANSFORM,
               lambda ctx: str(ctx.payload).upper())
        result = reg.trigger(points.TOOL_RESULT, "lower")
        assert result.payload == "LOWER"

    def test_transform_chains_in_order(self):
        reg = HookRegistry()
        reg.on(points.TOOL_RESULT, HookMode.TRANSFORM, lambda ctx: ctx.payload + "-a")
        reg.on(points.TOOL_RESULT, HookMode.TRANSFORM, lambda ctx: ctx.payload + "-b")
        result = reg.trigger(points.TOOL_RESULT, "x")
        assert result.payload == "x-a-b"  # registration order; later sees earlier result


class TestVetoMode:
    """veto hooks short-circuit when they return a non-None value."""

    def test_veto_short_circuits_later_hooks(self):
        reg = HookRegistry()
        called: list[str] = []
        reg.on(points.AGENT_STOP_FRAMEWORK, HookMode.VETO,
               lambda ctx: called.append("first") or "DENIED")
        reg.on(points.AGENT_STOP_FRAMEWORK, HookMode.OBSERVE,
               lambda ctx: called.append("second"))
        result = reg.trigger(points.AGENT_STOP_FRAMEWORK)
        assert result.vetoed
        assert result.veto_reason == "DENIED"
        assert called == ["first"], "later hooks must not run after veto"

    def test_veto_returning_none_continues(self):
        reg = HookRegistry()
        reg.on(points.AGENT_STOP_FRAMEWORK, HookMode.VETO, lambda ctx: None)
        result = reg.trigger(points.AGENT_STOP_FRAMEWORK)
        assert not result.vetoed


class TestAroundMode:
    """around hooks wrap the rest of the chain (pre/post/exception)."""

    def test_around_wraps_call(self):
        reg = HookRegistry()
        trace: list[str] = []

        def around(ctx, next_fn):
            trace.append("pre")
            out = next_fn(ctx.payload)
            trace.append("post")
            return out

        reg.on(points.LLM_CALL, HookMode.AROUND, around)
        reg.on(points.LLM_CALL, HookMode.OBSERVE, lambda ctx: trace.append("body"))

        result = reg.trigger(points.LLM_CALL, "req")
        assert trace == ["pre", "body", "post"]
        assert result.payload == "req"

    def test_around_can_replace_result(self):
        reg = HookRegistry()
        reg.on(points.LLM_CALL, HookMode.AROUND,
               lambda ctx, next_fn: "WRAPPED")
        result = reg.trigger(points.LLM_CALL, "req")
        assert result.payload == "WRAPPED"


class TestPriority:
    """priority desc; same priority keeps registration order."""

    def test_priority_desc_order(self):
        reg = HookRegistry()
        order: list[int] = []
        reg.on(points.AGENT_PRE_STEP, HookMode.OBSERVE,
               lambda ctx: order.append(10), priority=10)
        reg.on(points.AGENT_PRE_STEP, HookMode.OBSERVE,
               lambda ctx: order.append(50), priority=50)
        reg.on(points.AGENT_PRE_STEP, HookMode.OBSERVE,
               lambda ctx: order.append(1), priority=1)
        reg.trigger(points.AGENT_PRE_STEP)
        assert order == [50, 10, 1]

    def test_same_priority_registration_order(self):
        reg = HookRegistry()
        order: list[int] = []
        for i in range(3):
            reg.on(points.AGENT_PRE_STEP, HookMode.OBSERVE,
                   lambda ctx, i=i: order.append(i))
        reg.trigger(points.AGENT_PRE_STEP)
        assert order == [0, 1, 2]


class TestErrorIsolation:
    """observe swallow / transform fallback / veto fail-closed / around raise."""

    def test_observe_error_swallowed(self):
        reg = HookRegistry()
        reg.on(points.TOOL_RESULT, HookMode.OBSERVE,
               lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
        result = reg.trigger(points.TOOL_RESULT, "data")
        assert result.payload == "data"

    def test_transform_error_falls_back_to_original(self):
        reg = HookRegistry()
        reg.on(points.TOOL_RESULT, HookMode.TRANSFORM,
               lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
        result = reg.trigger(points.TOOL_RESULT, "data")
        assert result.payload == "data"

    def test_veto_error_fails_closed(self):
        reg = HookRegistry()
        reg.on(points.AGENT_STOP_FRAMEWORK, HookMode.VETO,
               lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
        result = reg.trigger(points.AGENT_STOP_FRAMEWORK)
        assert result.vetoed, "veto hook error must fail closed (deny)"

    def test_around_error_propagates(self):
        reg = HookRegistry()
        reg.on(points.LLM_CALL, HookMode.AROUND,
               lambda ctx, next_fn: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            reg.trigger(points.LLM_CALL)


class TestContextVars:
    """contextvars carry fields across thread boundaries (I-08)."""

    def test_context_fields_visible_in_hook(self):
        reg = HookRegistry()
        seen: list[dict] = []
        reg.on(points.LLM_CALL, HookMode.OBSERVE,
               lambda ctx: seen.append({
                   "point": ctx.point,
                   "trace_id": ctx.trace_id,
                   "mission_id": ctx.mission_id,
               }))
        reg.trigger(points.LLM_CALL, "p",
                    trace_id="tr-1", mission_id="m-1")
        assert seen[0]["point"] == points.LLM_CALL
        assert seen[0]["trace_id"] == "tr-1"
        assert seen[0]["mission_id"] == "m-1"

    def test_context_propagates_to_thread(self):
        reg = HookRegistry()
        seen: list[str] = []

        def hook(ctx):
            seen.append(ctx.trace_id)

        reg.on(points.LLM_CALL, HookMode.OBSERVE, hook)

        def run_trigger():
            reg.trigger(points.LLM_CALL, "p", trace_id="tr-thread")

        t = threading.Thread(target=run_trigger)
        t.start()
        t.join(timeout=2.0)
        assert seen == ["tr-thread"], "contextvars must reach the worker thread"


class TestLeafDependency:
    """hooks module must not import business modules (leaf dependency)."""

    def test_module_imports_only_stdlib(self):
        import subprocess, sys, os
        root = os.path.join(os.path.dirname(__file__), "..")
        hooks_dir = os.path.join(os.path.dirname(__file__), "..", "harness", "hooks")
        # Static check: hooks/*.py must not import harness.* except harness.hooks
        bad: list[str] = []
        for name in os.listdir(hooks_dir):
            if not name.endswith(".py"):
                continue
            path = os.path.join(hooks_dir, name)
            text = open(path, encoding="utf-8").read()
            for line in text.splitlines():
                stripped = line.strip()
                if (stripped.startswith("import harness.") or
                        stripped.startswith("from harness.")) and \
                        "harness.hooks" not in stripped:
                    bad.append(f"{name}: {stripped}")
        assert not bad, f"hooks module is not a leaf: {bad}"
