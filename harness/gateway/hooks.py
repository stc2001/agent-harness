"""Gateway built-in hooks — installed into the unified hooks registry (ADR-042).

Authorization hooks live in the process-level HookRegistry:
- authorize.veto  (was PreHook): return non-None to block authorization
- authorize.observe (was PostHook): fire after authorize, errors swallowed

The built-in SandboxMutexHook is now installed via install_sandbox_mutex().
"""

from __future__ import annotations

import threading

from harness.hooks import HookMode, HookRegistry, points


def install_sandbox_mutex(registry: HookRegistry | None = None,
                          timeout: float = 10.0) -> HookRegistry:
    """Serialize access to shared sandboxes across agents.

    Registers two hooks on the (default process-level) registry:
    - authorize.veto: acquire per-sandbox RLock before authorization
    - authorize.observe: release the lock after authorization

    Returns the registry the hooks were installed on.
    """
    registry = registry or HookRegistry.get()

    _locks: dict[str, threading.RLock] = {}
    _locks_lock = threading.Lock()

    def veto(ctx):
        sandbox = getattr(ctx.payload.spec, "sandbox", "default") or "default"
        with _locks_lock:
            lock = _locks.setdefault(sandbox, threading.RLock())
        acquired = lock.acquire(timeout=timeout)
        if not acquired:
            return f"sandbox '{sandbox}' occupied (timeout={timeout}s)"
        ctx._sandbox_mutex_lock = lock
        return None

    def observe(ctx):
        gctx = (ctx.payload or {}).get("ctx") if isinstance(ctx.payload, dict) else None
        lock = getattr(gctx, "_sandbox_mutex_lock", None)
        if lock is not None:
            lock.release()
            if gctx is not None:
                gctx._sandbox_mutex_lock = None

    registry.on(points.AUTHORIZE_VETO, HookMode.VETO, veto)
    registry.on(points.AUTHORIZE_OBSERVE, HookMode.OBSERVE, observe)
    return registry


__all__ = ["install_sandbox_mutex"]
