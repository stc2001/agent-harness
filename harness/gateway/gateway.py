"""Gateway — unified authorization engine.

Converges HumanGate (phase/node/atomic), custom rules, and audit
logging into a single entry point.  EventBus is
internal components for event persistence.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal

from harness.interfaces import GateInfo
from harness.gateway.gate_manager import EventBus


# ── Data types ─────────────────────────────────────────────────


@dataclass
class GateContext:
    """Context passed to Gateway.authorize() for every authorization check."""
    gate_type: str  # "phase" | "node" | "atomic" | "sandbox_spawn" | ""
    session_id: str
    spec: Any   # AgentSpec (avoid circular import)
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    phase_name: str | None = None
    node_id: str | None = None
    config: Any | None = None  # SandboxConfig for sandbox_spawn gate_type


@dataclass
class AuthorizeResult:
    """Returned by Gateway.authorize().  Caller inspects decision."""
    decision: Literal["approved", "modified", "denied"]
    amend_args: dict[str, Any] | None = None
    reason: str | None = None


@dataclass
class _GateEntry:
    """Internal: tracks a pending gate and its resolution."""
    event_id: str
    session_id: str
    gate_type: str
    phase_name: str | None
    node_id: str | None
    event: threading.Event
    result: str | None = None
    amend_input: dict[str, Any] | None = None


# ── Gateway ────────────────────────────────────────────────────


class Gateway:
    """Unified authorization engine.

    Usage:
        gateway = Gateway(event_bus=event_bus, parent_session_id="sid")
        ctx = GateContext("atomic", "sid", spec, tool_name="bash")
        result = gateway.authorize(ctx)
        if result.decision == "denied":
            raise PermissionError(result.reason)
    """

    def __init__(self, event_bus: EventBus | None = None,
                 parent_session_id: str = "",
                 gate_timeout: float = 60.0) -> None:
        self._event_bus = event_bus
        self._parent_session_id = parent_session_id
        self._gate_timeout = gate_timeout
        self._pending: dict[str, _GateEntry] = {}
        self._lock = threading.Lock()
        self._rules: list[tuple[str, Callable]] = []

    # ── Authorization ────────────────────────────────────

    def authorize(self, ctx: GateContext,
                  timeout: float | None = None) -> AuthorizeResult:
        """Authorize an agent action.

        Checks in order:
          0. Pre-hooks — if a hook blocks, post-hooks fire and return
          1. Custom rules — automated policy: deny immediately without waiting
          2. HumanGate — if rules pass AND gate is triggered, wait for human
          3. Post-hooks — fire regardless of outcome
          4. Returns AuthorizeResult
        """
        # 0. authorize.veto hooks (was pre-hooks, ADR-042)
        from harness.hooks import HookRegistry, points
        reg = HookRegistry.get()
        veto = reg.trigger(points.AUTHORIZE_VETO, ctx,
                           agent=getattr(ctx.spec, "name", None))
        if veto.vetoed:
            result = AuthorizeResult("denied", reason=str(veto.veto_reason))
            self._fire_authorize_observe(ctx, result)
            return result

        try:
            result = self._evaluate(ctx, timeout)
        except Exception:
            result = AuthorizeResult("denied", reason="gateway authorization error")

        # authorize.observe hooks — fire regardless of outcome (was post-hooks)
        self._fire_authorize_observe(ctx, result)
        return result

    def _evaluate(self, ctx: GateContext,
                  timeout: float | None = None) -> AuthorizeResult:

        # 1. Custom rules — automated policy
        result = None
        amend_args: dict[str, Any] = {}
        try:
            for name, rule_fn in self._rules:
                try:
                    verdict = rule_fn(ctx)
                    if verdict:
                        if isinstance(verdict, AuthorizeResult):
                            if verdict.decision == "denied":
                                result = verdict
                                break
                            elif verdict.decision == "modified":
                                # Rule wants to amend the context (e.g.,
                                # strip env vars).  Accumulate amendments
                                # and continue to next rule.
                                if verdict.amend_args:
                                    amend_args.update(verdict.amend_args)
                                continue
                            # "approved" — continue to next rule
                            continue
                        # Plain truthy value → deny
                        reason = str(verdict) if not isinstance(verdict, bool) \
                            else f"rule '{name}' denied"
                        result = AuthorizeResult("denied", reason=reason)
                        break
                except Exception as exc:
                    result = AuthorizeResult("denied",
                                             reason=f"rule '{name}' errored: {exc}")
                    break
        except Exception as exc:
            result = AuthorizeResult("denied",
                                     reason=f"gateway rules evaluation failed: {exc}")

        # 2. HumanGate — only if rules didn't already deny
        if result is None:
            if ctx.gate_type in ("phase", "node", "atomic"):
                entry = self._create_gate(ctx)
                waited = entry.event.wait(timeout=timeout)
                if not waited:
                    result = AuthorizeResult("denied", reason="human gate timed out")
                else:
                    decision = entry.result or "denied"
                    if decision == "reject":
                        result = AuthorizeResult("denied", reason="human gate rejected")
                    elif decision == "modify":
                        result = AuthorizeResult("modified", amend_args=entry.amend_input)
                    else:
                        result = AuthorizeResult("approved")

        if result is None:
            result = AuthorizeResult("approved")

        # Attach accumulated amendments from "modified" rules
        if amend_args and result.decision != "denied":
            result = AuthorizeResult("modified", amend_args=amend_args,
                                     reason=result.reason)

        return result

    def _fire_authorize_observe(self, ctx: GateContext,
                               result: AuthorizeResult) -> None:
        """Fire authorize.observe hooks (was post-hooks, ADR-042).

        Errors are swallowed by the registry (observe mode).
        """
        from harness.hooks import HookRegistry, points
        HookRegistry.get().trigger(
            points.AUTHORIZE_OBSERVE,
            {"ctx": ctx, "result": result},
            agent=getattr(ctx.spec, "name", None),
        )


    def create_gate(self, ctx: GateContext) -> str:
        """Create a pending gate and return its event_id. Does NOT block."""
        entry = self._create_gate(ctx)
        return entry.event_id

    def _create_gate(self, ctx: GateContext) -> _GateEntry:
        event_id = str(uuid.uuid4())
        if self._event_bus:
            self._event_bus.append(
                session_id=ctx.session_id,
                event_type="human_gate_pending",
                source="coordinator",
                event_id=event_id,
                gate_event_id=event_id,
                node_id=ctx.node_id or "",
            )
        entry = _GateEntry(
            event_id=event_id,
            session_id=ctx.session_id,
            gate_type=ctx.gate_type,
            phase_name=ctx.phase_name,
            node_id=ctx.node_id,
            event=threading.Event(),
        )
        with self._lock:
            self._pending[event_id] = entry
        return entry

    def resolve(self, event_id: str, decision: str,
                amend: dict[str, Any] | None = None) -> None:
        if decision not in {"approve", "reject", "modify"}:
            raise ValueError(
                f"Invalid decision '{decision}'. "
                f"Must be: approve, reject, modify"
            )
        entry = self._pending.get(event_id)
        if entry is None:
            return
        entry.result = decision
        if decision == "modify":
            entry.amend_input = amend or {}
        if self._event_bus:
            self._event_bus.append(
                session_id=entry.session_id,
                event_type="human_gate_resolved",
                source="human",
                event_id=event_id,
                decision=decision,
            )
            if decision == "modify" and amend:
                self._event_bus.append(
                    session_id=entry.session_id,
                    event_type="human_gate_amend",
                    source="human",
                    event_id=event_id,
                    gate_event_id=event_id,
                    amend_input=amend,
                )
        entry.event.set()
        with self._lock:
            del self._pending[event_id]

    # ── Query ─────────────────────────────────────────────

    def list_pending(self, session_id: str) -> list[GateInfo]:
        result: list[GateInfo] = []
        with self._lock:
            for eid, entry in self._pending.items():
                if entry.session_id == session_id and not entry.event.is_set():
                    result.append(GateInfo(
                        event_id=eid,
                        session_id=session_id,
                        status="pending",
                        phase_name=entry.phase_name,
                        node_id=entry.node_id,
                    ))
        if self._event_bus:
            stored = self._event_bus.get_events()
            pending_ids: set[str] = {g.event_id for g in result}
            resolved_ids: set[str] = set()
            for ev in stored:
                if ev.session_id != session_id:
                    continue
                if ev.event_type == "human_gate_pending":
                    eid = ev.payload.get("gate_event_id", ev.event_id)
                    if eid not in pending_ids:
                        pending_ids.add(eid)
                        result.append(GateInfo(
                            event_id=eid,
                            session_id=session_id,
                            status="pending",
                            phase_name=ev.payload.get("phase_name"),
                            node_id=ev.payload.get("node_id"),
                        ))
                elif ev.event_type == "human_gate_resolved":
                    resolved_ids.add(ev.event_id)
            result = [g for g in result
                      if g.event_id not in resolved_ids]
        return result

    def get_gate(self, event_id: str, session_id: str) -> GateInfo | None:
        for g in self.list_pending(session_id):
            if g.event_id == event_id:
                return g
        return None

    def wait_for_resolution(self, event_id: str,
                            timeout: float | None = None) -> AuthorizeResult:
        """Block until a previously created gate is resolved.

        Used by coordinator/orchestrator for node/phase gates.

        Args:
            event_id: Gate event ID returned by create_gate().
            timeout: Max seconds to wait. None = wait indefinitely.

        Returns:
            AuthorizeResult with decision (approved/modified/denied).
        """
        entry = self._pending.get(event_id)
        if entry is None:
            return AuthorizeResult("denied", reason="gate not found")
        waited = entry.event.wait(timeout=timeout or self._gate_timeout)
        if not waited:
            return AuthorizeResult("denied", reason="human gate timed out")
        decision = entry.result or "denied"
        if decision == "reject":
            return AuthorizeResult("denied", reason="human gate rejected")
        if decision == "modify":
            return AuthorizeResult("modified", amend_args=entry.amend_input)
        return AuthorizeResult("approved")

    # ── Rules ─────────────────────────────────────────────

    def register_rule(self, fn: Callable[[GateContext], bool | str],
                      name: str = "") -> None:
        """Register a custom authorization rule.

        Args:
            fn: Callable(GateContext) → truthy value means DENY.
            name: Rule identifier. If empty, auto-generated:
                  uses fn.__name__ (or 'anonymous-N' for lambdas).
        """
        if not name:
            if fn.__name__ == "<lambda>":
                name = f"anonymous-{len(self._rules) + 1}"
            else:
                name = fn.__name__
        self._rules.append((name, fn))

