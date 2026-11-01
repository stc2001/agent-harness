"""HookRegistry — process-level registry with four-mode semantics (ADR-042).

Semantics (non-negotiable):
- observe:  return value ignored; errors swallowed (logged)
- transform: return value replaces payload; errors fall back to original
- veto:     non-None return short-circuits; errors fail closed (deny)
- around:   wraps the rest of the chain; errors propagate

Order: around (priority desc, registration order) wraps the chain of
veto -> transform -> observe hooks, each sorted by priority desc then
registration order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from harness.hooks.context import (
    HookContext,
    reset_context,
    set_context_fields,
)

logger = logging.getLogger(__name__)


class HookMode(str, Enum):
    OBSERVE = "observe"
    TRANSFORM = "transform"
    VETO = "veto"
    AROUND = "around"


@dataclass
class _RegisteredHook:
    point: str
    mode: HookMode
    fn: Callable
    priority: int
    seq: int


@dataclass
class TriggerResult:
    """Outcome of a trigger dispatch."""

    payload: Any = None
    vetoed: bool = False
    veto_reason: Any = None


class HookRegistry:
    """Process-level hook registry (module-level singleton via get())."""

    _instance: "HookRegistry | None" = None

    def __init__(self) -> None:
        self._hooks: list[_RegisteredHook] = []
        self._seq = 0

    @classmethod
    def get(cls) -> "HookRegistry":
        """Return the process-wide registry singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def on(
        self,
        point: str,
        mode: HookMode | str,
        fn: Callable,
        priority: int = 0,
    ) -> None:
        """Register a hook.  Same point+priority runs in registration order."""
        mode = mode if isinstance(mode, HookMode) else HookMode(mode)
        self._hooks.append(_RegisteredHook(point, mode, fn, priority, self._seq))
        self._seq += 1

    def _for_point(self, point: str) -> list[_RegisteredHook]:
        hooks = [h for h in self._hooks if h.point == point]
        hooks.sort(key=lambda h: (-h.priority, h.seq))
        return hooks

    def has_hooks(self, point: str) -> bool:
        """Whether any hook is registered for a point (cheap guard)."""
        return any(h.point == point for h in self._hooks)

    def trigger(
        self,
        point: str,
        payload: Any = None,
        *,
        final: Callable[[Any], Any] | None = None,
        **fields: Any,
    ) -> TriggerResult:
        """Dispatch hooks for a point.

        When ``final`` is provided (business call), around hooks wrap it:
        the innermost around next_fn invokes ``final`` and its return
        value becomes the TriggerResult payload.  Without ``final`` the
        around chain ends in the ordinary veto/transform/observe chain.
        """
        token = set_context_fields(**fields)
        try:
            hooks = self._for_point(point)
            arounds = [h for h in hooks if h.mode == HookMode.AROUND]
            others = [h for h in hooks if h.mode != HookMode.AROUND]

            def chain(current: Any) -> TriggerResult:
                result = TriggerResult(payload=current)
                for h in others:
                    ctx = HookContext.build(point, result.payload, **fields)
                    if h.mode == HookMode.VETO:
                        try:
                            verdict = h.fn(ctx)
                        except Exception as exc:
                            logger.exception("veto hook error (fail-closed)")
                            result.vetoed = True
                            result.veto_reason = f"hook-error: {exc}"
                            return result
                        if verdict is not None:
                            result.vetoed = True
                            result.veto_reason = verdict
                            return result
                    elif h.mode == HookMode.TRANSFORM:
                        try:
                            new_value = h.fn(ctx)
                        except Exception:
                            logger.exception("transform hook error (fallback)")
                            new_value = None
                        if new_value is not None:
                            result.payload = new_value
                    elif h.mode == HookMode.OBSERVE:
                        try:
                            h.fn(ctx)
                        except Exception:
                            logger.exception("observe hook error (swallowed)")
                # final (business call) runs innermost: hooks shaped the
                # request payload, its return value becomes the result.
                if final is not None:
                    result.payload = final(result.payload)
                return result

            def wrap(index: int, current: Any) -> TriggerResult:
                if index >= len(arounds):
                    return chain(current)
                h = arounds[index]
                ctx = HookContext.build(point, current, **fields)

                def next_fn(inner_payload: Any = current) -> TriggerResult:
                    return wrap(index + 1, inner_payload)

                out = h.fn(ctx, next_fn)
                if isinstance(out, TriggerResult):
                    return out
                return TriggerResult(payload=out)

            return wrap(0, payload)
        finally:
            reset_context(token)

    def clear(self) -> None:
        """Remove all hooks (tests / teardown)."""
        self._hooks.clear()
        self._seq = 0


__all__ = ["HookMode", "HookRegistry", "TriggerResult"]
