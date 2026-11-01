"""harness.hooks — unified hook registry + context (ADR-042)."""

from harness.hooks.context import HookContext
from harness.hooks.registry import HookMode, HookRegistry, TriggerResult
from harness.hooks import points

__all__ = ["HookContext", "HookMode", "HookRegistry", "TriggerResult", "points"]
