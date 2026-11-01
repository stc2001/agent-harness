"""I-27: 策略中间件 — gateway/hooks 定向规则引擎 + preset 规则集.

All rules default OFF.  Enable/add must name an agent (or "*") explicitly.
Effective set for agent A = enabled entries whose scope ∈ {A, "*"}.
Rules compose as union + deny-wins (no priority layer).  Enforcement rides
the existing global ``authorize.veto/observe`` hooks (ADR-042 §8): a hook
is registered once and filters by ctx.spec.name — zero-config behaviour
is unchanged for everyone else.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


VetoFn = Callable[[Any], str | None]

# Tools whose arguments carry a shell command (I-35 kind="auto").
CMD_TOOLS = {"bash", "bash_exec", "execute", "run_script"}
ObserveFn = Callable[[Any], None]


class PolicyEngine:
    """Process-level policy registry keyed by rule name and agent scope."""

    def __init__(self) -> None:
        self._defs: dict[str, dict[str, Any]] = {}
        self._enabled: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._hooks_registered = False
        self._register_builtins()

    # ── rule definition ──
    def define(self, name: str, veto: VetoFn | None = None,
               observe: ObserveFn | None = None,
               observe_exec: Callable | None = None,
               cfg: dict | None = None,
               cedar: str | None = None,
               kind: str | None = None,
               guard: dict | None = None,
               description: str | None = None,
               reason: str | None = None) -> None:
        """Register a rule (I-35).

        cedar: Cedar policy text (static decision core).
        kind: 'cmd' (resource Cmd{text}) or 'file' (resource File{path}).
        guard: {paths: {rw/ro: [...]}, syscalls_deny/allow: [...]} data
            compiled into SandboxRestriction at execution (no translation).
        description: optional human/agent-facing explanation.
        reason: required for rules that can deny via cedar forbid
            (dynamic python veto returns its own reason string).
        """
        with self._lock:
            self._defs[name] = {"veto": veto, "observe": observe,
                                "observe_exec": observe_exec,
                                "cfg": dict(cfg or {}),
                                "cedar": cedar,
                                "kind": kind,
                                "guard": dict(guard or {}),
                                "description": description,
                                "reason": reason}

    def add(self, name: str, agent: str,
            veto: VetoFn | None = None,
            observe: ObserveFn | None = None,
            cfg: dict | None = None,
            cedar: str | None = None,
            kind: str | None = None,
            guard: dict | None = None,
            description: str | None = None,
            reason: str | None = None) -> None:
        """App-defined rule: define + immediately enable (agent-scoped).

        I-35: cedar-only rules need neither veto nor observe.
        """
        if (veto is None and observe is None and cedar is None
                and not guard):
            raise ValueError(
                f"policy {name!r} needs a veto/observe fn, cedar text or guard")
        self.define(name, veto=veto, observe=observe, cfg=cfg,
                    cedar=cedar, kind=kind, guard=guard,
                    description=description, reason=reason)
        self.enable(name, agent=agent)

    def enable(self, name: str, agent: str, *,
               cfg: dict | None = None) -> None:
        """Explicitly enable a rule for an agent (or "*"). Unknown → ValueError.

        ADR-015 (2026-10-19): ``cfg`` is merged over the rule's default cfg so an
        app can configure a builtin preset (e.g. ``read_before_edit``'s
        ``digest_provider`` / parameter-name mapping) without re-implementing it.
        The merged cfg lives on the rule's GLOBAL definition — this architecture
        has no per-agent cfg, so use one rule per distinct configuration.
        """
        with self._lock:
            if name not in self._defs:
                raise ValueError(f"Unknown policy {name!r}: define/add it first")
            if cfg:
                merged = dict(self._defs[name].get("cfg") or {})
                merged.update(cfg)
                self._defs[name]["cfg"] = merged
            self._enabled.setdefault(agent, set()).add(name)
        self._ensure_hooks()

    def disable(self, name: str, agent: str) -> None:
        with self._lock:
            self._enabled.get(agent, set()).discard(name)

    def effective(self, agent: str) -> list[str]:
        """Rule names in scope for an agent (union of agent and "*")."""
        with self._lock:
            names = set(self._enabled.get(agent, set()))
            names |= set(self._enabled.get("*", set()))
            return sorted(names)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            by_rule: dict[str, set[str]] = {}
            for agent, names in self._enabled.items():
                for n in names:
                    by_rule.setdefault(n, set()).add(agent)
            return [{"name": n, "agents": sorted(agents)}
                    for n, agents in sorted(by_rule.items())]

    # ── I-35: Cedar static decision core ──
    def check_static(self, agent_name: str, tool_name: str,
                     args: dict) -> tuple[str | None, str | None, bool]:
        """Static (Cedar) decision for one tool call.

        Returns (decision, reason, matched): decision in {'allow','deny'}
        or None (no enabled rule covered the call = NoDecision), reason
        non-None only when denied.  deny-wins across enabled rules.
        """
        cmd_keys = ("command", "cmd", "script")
        file_keys = ("path", "file_path", "file")
        covered = False
        enabled = self.effective(agent_name)
        import cedarpy
        for name in enabled:
            d = self._defs.get(name) or {}
            text = d.get("cedar")
            kind = d.get("kind")
            if kind == "auto":
                kind = "cmd" if tool_name in CMD_TOOLS else "file"
            if not text or kind not in ("cmd", "file"):
                continue
            if kind == "cmd":
                value = next((args.get(k) for k in cmd_keys
                              if isinstance(args.get(k), str) and args.get(k)),
                             None)
                if value is None:
                    continue
                entities = [{"uid": {"type": "Cmd", "id": "c"},
                             "attrs": {"text": value}, "parents": []}]
            else:
                value = next((args.get(k) for k in file_keys
                              if isinstance(args.get(k), str) and args.get(k)),
                             None)
                if value is None:
                    continue
                entities = [{"uid": {"type": "File", "id": "f"},
                             "attrs": {"path": value}, "parents": []}]
            request = {
                "principal": 'Agent::"%s"' % agent_name,
                "action": 'Action::"%s"' % tool_name,
                "resource": 'Cmd::"c"' if kind == "cmd" else 'File::"f"',
                "context": {},
            }
            try:
                result = cedarpy.is_authorized(request=request,
                                               policies=text,
                                               entities=entities)
            except Exception as exc:  # fail-closed on eval error
                return ("deny",
                        "policy engine error in %r: %s" % (name, exc), True)
            # cedarpy returns Deny for BOTH explicit forbid matches and the
            # implicit-deny of unmatched policies.  diagnostics.reasons is
            # non-empty only when a policy with @id actually matched — that
            # is the true forbid/coverage signal (I-35 NoDecision semantics:
            # unmatched = not governed -> allow + warn upstream).
            matched_ids = list(getattr(result.diagnostics, "reasons", None)
                               or [])
            if result.decision == cedarpy.Decision.Deny and matched_ids:
                reason = d.get("reason") or (
                    "denied by policy %r" % name)
                return ("deny", reason, True)
            if result.decision == cedarpy.Decision.Allow and matched_ids:
                covered = True
        if covered:
            return ("allow", None, True)
        return (None, None, False)

    def compile_guard(self, agent_name: str):
        """I-35: merge enabled rules' guard data into SandboxRestriction.

        paths: union of rw/ro allow sets; a path declared both ways resolves
        to rw (wins, with a warning).  syscalls_deny union.  Returns None
        when no enabled rule contributes guard data.
        """
        from harness.interfaces import (SandboxRestriction,
                                        SandboxRestrictionError)
        mode_of: dict[str, str] = {}
        syscalls: list = []
        seen_sys: set = set()
        read_deny: list = []
        seen_rd: set = set()
        for name in self.effective(agent_name):
            g = (self._defs.get(name) or {}).get("guard") or {}
            for mode in ("rw", "ro"):
                for p in g.get("paths", {}).get(mode, []):
                    if p in mode_of and mode_of[p] != mode:
                        if mode_of[p] != "rw" and mode == "rw":
                            mode_of[p] = "rw"
                            logger.warning(
                                "guard %s: path %s rw wins over ro",
                                name, p)
                    else:
                        mode_of.setdefault(p, mode)
            for s in g.get("syscalls_deny", []):
                if s not in seen_sys:
                    seen_sys.add(s)
                    syscalls.append(s)
            for p in g.get("sys_read_deny", []):
                if p not in seen_rd:
                    seen_rd.add(p)
                    read_deny.append(p)
        if not mode_of and not syscalls and not read_deny:
            return None
        paths = {"rw": [], "ro": []}
        for p, mode in mode_of.items():
            paths[mode].append(p)
        try:
            return SandboxRestriction(paths=paths,
                                      sys_read_deny=read_deny,
                                      syscalls_deny=syscalls)
        except ValueError as exc:
            raise SandboxRestrictionError(str(exc)) from exc

    # ── hooks ──
    def _ensure_hooks(self) -> None:
        # Resilient registration: another suite may clear the global registry
        # (ADR-042); re-register a point whenever it has no hooks.
        from harness.hooks import HookMode, HookRegistry, points
        reg = HookRegistry.get()

        def veto_hook(hc):
            # hc is a HookContext; payload carries the GateContext.
            gate = getattr(hc, "payload", None)
            agent_name = getattr(hc, "agent", None) or (
                getattr(getattr(gate, "spec", None), "name", None))
            if not agent_name:
                return None
            # I-35: Cedar static decision runs first (deny-wins over overlay).
            if any((self._defs.get(n) or {}).get("cedar")
                   for n in self.effective(agent_name)):
                try:
                    decision, reason, _matched = self.check_static(
                        agent_name,
                        getattr(gate, "tool_name", "") or "",
                        getattr(gate, "tool_args", None) or {})
                except Exception as exc:  # fail-closed on engine error
                    return ("policy engine error: %s" % exc)[:500]
                if decision == "deny":
                    return reason
            for name in self.effective(agent_name):
                fn = self._defs[name]["veto"]
                if fn is None:
                    continue
                reason = fn(gate, self._defs[name]["cfg"])
                if reason:
                    return reason
            return None

        def observe_hook(hc):
            payload = getattr(hc, "payload", None) or {}
            gate = payload.get("ctx")
            result = payload.get("result")
            agent_name = getattr(hc, "agent", None) or (
                getattr(getattr(gate, "spec", None), "name", None))
            if not agent_name:
                return
            for name in self.effective(agent_name):
                fn = self._defs[name]["observe"]
                if fn is not None:
                    try:
                        fn(gate, result)
                    except Exception:
                        pass

        if not reg.has_hooks(points.AUTHORIZE_VETO):
            reg.on(points.AUTHORIZE_VETO, HookMode.VETO, veto_hook)
        if not reg.has_hooks(points.AUTHORIZE_OBSERVE):
            reg.on(points.AUTHORIZE_OBSERVE, HookMode.OBSERVE, observe_hook)

        def tool_exec_hook(hc):
            """Post-tool-use stamping (payload: tool_name/args/result/session)."""
            payload = getattr(hc, "payload", None) or {}
            agent_name = getattr(hc, "agent", None)
            if not agent_name:
                return
            for name in self.effective(agent_name):
                fn = self._defs[name].get("observe_exec")
                if fn is not None:
                    try:
                        fn(payload, agent_name, self._defs[name]["cfg"])
                    except Exception:
                        pass

        if not reg.has_hooks(points.TOOL_EXECUTE):
            reg.on(points.TOOL_EXECUTE, HookMode.OBSERVE, tool_exec_hook)

    # ── builtin presets ──
    def _register_builtins(self) -> None:
        from harness.policy_presets import BUILTIN_PRESETS
        for name, maker in BUILTIN_PRESETS.items():
            entry = maker()
            self.define(name, veto=entry.get("veto"),
                        observe=entry.get("observe"),
                        observe_exec=entry.get("observe_exec"),
                        cfg=entry.get("cfg"),
                        cedar=entry.get("cedar"),
                        kind=entry.get("kind"),
                        guard=entry.get("guard"),
                        description=entry.get("description"),
                        reason=entry.get("reason"))


engine = PolicyEngine()