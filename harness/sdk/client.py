"""Harness SDK Client — the single entry point for all Agent Harness operations.

Architecture:
    CLI / TUI / Scripts → HarnessClient → internal modules

This module orchestrates all internal services (Orchestrator, Coordinator,
SessionStore, etc.) and exposes a clean, stable API surface.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from harness.agent_spec.impl import AgentSpecService
from harness.audit_log import AuditLog
from harness.gateway.gateway import Gateway
from harness.interfaces import (
    Event,
    SandboxConfig,
    SessionNotFoundError,
)
from harness.policy import engine as _policy_engine
from harness.sdk.program import ProgramResult
from harness.orchestration.model_pool import (
    ModelService, ModelServicePool, ModelServiceStatus,
)
from harness.orchestration.pool import AgentInstancePool
from harness.sandbox.manager import SandboxManager
from harness.session.impl import SessionManager
from harness.session_store.impl import SessionStore

from .config import HarnessSettings, LLMConfig, resolve_llm_profile
from .models import AgentInstance, AgentStatus, EventInfo, RunResult, SessionInfo, SessionStatus


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────


class _InstanceRegistry:
    """Filesystem-backed registry for compiled agent instances.

    Stores lightweight references (no credentials) in:
        ~/.harness/instances/{instance_id}.json
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._dir = base_dir or (Path.home() / ".harness" / "instances")
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, instance_id: str) -> Path:
        return self._dir / f"{instance_id}.json"

    def save(self, instance: AgentInstance) -> None:
        with self._path(instance.instance_id).open("w", encoding="utf-8") as f:
            json.dump(instance.to_dict(), f, indent=2, ensure_ascii=False)

    def load(self, instance_id: str) -> AgentInstance:
        path = self._path(instance_id)
        if not path.exists():
            raise KeyError(f"Agent instance not found: {instance_id}")
        with path.open("r", encoding="utf-8") as f:
            return AgentInstance.from_dict(json.load(f))

    def list_all(self) -> list[AgentInstance]:
        instances = []
        for path in sorted(self._dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as f:
                instances.append(AgentInstance.from_dict(json.load(f)))
        return instances

    def delete(self, instance_id: str) -> None:
        path = self._path(instance_id)
        if path.exists():
            path.unlink()


# ─────────────────────────────────────────────────────────────
# HarnessClient
# ─────────────────────────────────────────────────────────────


class HarnessClient:
    """Single entry point for Agent Harness operations.

    Usage:
        client = HarnessClient()
        instance = client.agents.register(AgentSpec(name="worker", ...), name="my-worker")
        session = client.sessions.start(instance.instance_id)
        client.sessions.events.send(session.session_id, "Hello")
    """

    def __init__(self, settings: HarnessSettings | None = None,
                 sandbox_configs: dict | None = None,
                 project_dir: str | Path | None = None,
                 tool_registry=None) -> None:
        self._settings = settings or HarnessSettings.from_yaml()
        # ADR-015 (2026-10-19): the project root is the CALLER's decision.  An
        # app launched in a user workspace still resolves its LLM profiles and
        # tokenizer provider from its own home, not from the process cwd.
        self._project_dir = Path(project_dir) if project_dir else Path.cwd()

        # Security: reject empty hmac_secret to prevent unsafe defaults
        if not self._settings.hmac_secret:
            raise RuntimeError(
                "hmac_secret is not configured. "
                "Set it in harness/config/local.yaml or via HARNESS_HMAC_SECRET env var."
            )

        # Internal services (initialized lazily where possible)
        self._store = SessionStore(base_path=self._settings.sessions_dir)
        self._spec_svc = AgentSpecService()
        # I-24: source assembly (explicit > sandbox.yaml > builtin fallback
        # "main") converges here — previously scattered in CLI _client().
        self._sandbox_mgr = SandboxManager(
            configs=self._load_sandbox_configs(sandbox_configs))
        self._gateway = Gateway()
        self._session_mgr = SessionManager(self._store)
        self._audit = AuditLog(base_dir=self._settings.audit_log_dir)

        # I-14: model service pool (models.yaml baseline + dynamic register).
        self._model_pool = ModelServicePool(
            services=self._load_model_services(), audit_log=self._audit)

        # ADR-015 (2026-10-19): the tool registry is CLIENT-level so an app can
        # register its own tools via `client.tools.register(...)`.  The runtime
        # receives a per-instance COPY (tool deps are injected per runtime, so a
        # shared registry would cross-talk between instances).  Default remains
        # `load_builtins()` — every existing caller sees exactly the same tools.
        self.tools = _ToolsClient(self, tool_registry)

        self._pool = AgentInstancePool(
            store=self._store,
            gateway=self._gateway,
            api_key=self._settings.deepseek_api_key or "",
            sandbox_manager=self._sandbox_mgr,
            resolve_llm=resolve_llm_profile,
            project_dir=self._project_dir,
            audit_log=self._audit,
            model_pool=self._model_pool,
            tool_registry=self.tools.registry,
        )

        # User-facing sub-clients
        self.agents = _AgentsClient(self)
        self.sessions = _SessionsClient(self)
        self.gates = _GatesClient(self)
        self.gateway = _GatewayPublicClient(self._gateway)  # Safe subset of Gateway API
        self.sandboxes = _SandboxClient(self)
        self.models = _ModelsClient(self)
        self.skills = _SkillsClient(self)
        self.prompts = _PromptsClient(self)
        self.memory = _MemoryClient(self)
        self.llm = _LLMClient(self)  # ADR-055: driver registry view
        self.program = _ProgramClient(self)  # I-18: deterministic script runner
        self.policy = _PolicyClient(self)  # I-27: scoped rule middleware
        self.config = _ConfigClient(self)
        self.hooks = _HooksClient(self)  # ADR-042: process-level registry view


    # ─── Internal helpers exposed for sub-clients ───

    # ── I-42: 语义读面接缝 ────────────────────────────────────
    # 一切语义读（SDK/CLI/编排）都必须经过这里；物理日志只允许出现在
    # _SessionsClient.audit_events()（审计读面，见 ADR-050 I-42 注记）。
    def session_mgr_logical(self, session_id: str):
        """Public read face for the logical event view (SessionManager).

        `_logical_events` wraps exactly this; apps must not reach the private
        name, and must never consume the physical log directly
        (ADR-050/I-31: every read rebuilds the logical view).
        """
        return self._session_mgr.logical_view(session_id)

    def _logical_events(self, session_id: str, after_event_id: str | None = None):
        """语义读面：逻辑视图（rewind 重放），可选 after 切片。"""
        events = self._session_mgr.logical_view(session_id)
        if after_event_id is None:
            return events
        out, found = [], False
        for ev in events:
            if found:
                out.append(ev)
            elif ev.event_id == after_event_id:
                found = True
        return out
    def _resolve_llm(self, profile_name: str) -> LLMConfig:
        return resolve_llm_profile(profile_name, project_dir=self._project_dir)

    def _load_sandbox_configs(
        self, explicit: dict | None = None
    ) -> dict[str, SandboxConfig]:
        """I-24: source assembly — explicit > sandbox.yaml > builtin fallback.

        Merges the CLI bootstrap that previously lived in ``cli/main.py``:
        1. Explicit ``sandbox_configs`` parameter wins on same-name conflicts.
        2. ``<project_dir>/harness/config/sandbox.yaml`` supplies base configs.
        3. Skill mounts from every registered AgentSpec are appended per
           sandbox name (union), via ``compute_skill_mounts``.
        4. A builtin ``main`` (bubblewrap / network=host) is guaranteed so
           ``pool.acquire`` never fails for a missing sandbox.
        """
        from harness.interfaces import SandboxConfig
        configs: dict[str, SandboxConfig] = dict(explicit or {})

        sandbox_yaml = self._project_dir / "harness" / "config" / "sandbox.yaml"
        if sandbox_yaml.exists():
            import yaml as _yaml
            try:
                raw = _yaml.safe_load(sandbox_yaml.read_text()) or {}
            except Exception:
                raw = {}
            for name, cfg in raw.get("sandboxes", {}).items():
                if name in configs:
                    continue  # explicit wins
                shared = []
                for sf in cfg.get("shared_folders", []) or []:
                    shared.append({
                        "host_path": sf.get("host_path", ""),
                        "sandbox_path": sf.get("sandbox_path", ""),
                    })
                configs[name] = SandboxConfig(
                    backend=cfg.get("backend", "bubblewrap"),
                    firewall=cfg.get("firewall", {"network": "host"}),
                    shared_folders=shared,
                    command=cfg.get("command") or None,
                )

        # Skill mounts from registered specs (union per sandbox name).
        # Scans BOTH the registry directory (persisted YAML — old CLI
        # behavior) and specs registered in-memory on this client.
        try:
            from harness.skills import compute_skill_mounts
            sandbox_skills: dict[str, set[str]] = {}
            services = [self._spec_svc]
            try:
                from harness.agent_spec.impl import AgentSpecService
                services.append(
                    AgentSpecService(str(self._spec_svc.registry_path)))
            except Exception:
                pass
            for svc in services:
                for name in svc.list_all():
                    try:
                        spec = svc.load(name)
                    except Exception:
                        continue
                    if spec.skills and spec.sandbox:
                        sandbox_skills.setdefault(spec.sandbox,
                                                  set()).update(spec.skills)
            for sb_name, skill_names in sandbox_skills.items():
                if sb_name in configs:
                    configs[sb_name].shared_folders.extend(
                        compute_skill_mounts(list(skill_names)))
        except Exception:
            pass  # No registered specs yet — ok on fresh start

        # Builtin fallback "main" — never undefined
        if "main" not in configs:
            configs["main"] = SandboxConfig(
                backend="bubblewrap",
                firewall={"network": "host"},
                shared_folders=[],
            )
        return configs

    def _load_model_services(self) -> dict[str, list]:
        """I-14/Q4-C: models.yaml static baseline for the model service pool.

        Each entry: ``{name, url, health_path?, model?, load_adapter_path?,
        preloaded_adapters?}``.  Services start in state=unknown (the engine
        process may still be booting — Q12).  Dynamic register() overrides.
        """
        from harness.orchestration.model_pool import ModelService
        services: dict[str, list[ModelService]] = {}
        models_yaml = self._project_dir / "harness" / "config" / "models.yaml"
        if models_yaml.exists():
            import yaml as _yaml
            try:
                raw = _yaml.safe_load(models_yaml.read_text()) or {}
            except Exception:
                raw = {}
            for name, cfg in raw.get("models", {}).items():
                try:
                    services[name] = [ModelService(
                        name=name,
                        url=cfg["url"],
                        health_path=cfg.get("health_path", "/health"),
                        model=cfg.get("model", ""),
                        load_adapter_path=cfg.get("load_adapter_path", ""),
                        preloaded_adapters=cfg.get("preloaded_adapters", []),
                    )]
                except (KeyError, TypeError):
                    continue  # malformed entry — skip (sandbox.yaml 同款容错)
        return services

    def _wake_session(self, session_id: str, message: str) -> None:
        """I-11/ADR-043: wake (or first-run) the pooled agent bound to a
        session, after a message was injected.

        Best-effort: if the session is not bound to a registered instance,
        or the instance/spec is gone, the event stays appended (audit) and
        nothing wakes.  Exceptions are swallowed so send() keeps its
        contract (returns the event_id of the injected message).
        """
        try:
            meta = self._store.get_session_metadata(session_id)
            instance_id = (meta or {}).get("instance_id")
            if not instance_id:
                return
            inst = self.agents._registry.load(instance_id)
            spec = self._spec_svc.load(inst.spec_name)
        except Exception:
            return
        try:
            # I-25/D2: wake the instance bound to this session — exact
            # (name, instance_id) keying, lazily creating its runtime.
            # I-26: carry the pinned sandbox entity id.
            agent = self._pool.acquire(
                spec, instance_id=instance_id,
                sandbox_id=getattr(inst, "sandbox_id", None))
            # I-32: keep the session→sandbox binding edge current.
            _record_binding(self._store, session_id, spec.name,
                            instance_id,
                            getattr(agent, "_sandbox_id", None) or "",
                            time.time())
            agent.wake(message, session_id)
        except Exception:
            # Wake is best-effort; the injected event remains in the log.
            pass


# ─────────────────────────────────────────────────────────────
# Sub-clients
# ─────────────────────────────────────────────────────────────


def _record_binding(store, session_id, agent_name, instance_id, sandbox_id,
                    ts):
    """I-32: append/merge an agent→(session,sandbox) binding edge.

    Same (instance, sandbox) pair refreshes last_ts; a new pair appends.
    """
    meta = store.get_session_metadata(session_id) or {}
    hist = list(meta.get("binding_history") or [])
    hit = next((e for e in hist
                if e.get("agent") == agent_name
                and e.get("instance_id") == instance_id
                and e.get("sandbox_id") == sandbox_id), None)
    if hit is not None:
        hit["last_ts"] = ts
    else:
        hist.append({"agent": agent_name, "instance_id": instance_id,
                     "session_id": session_id, "sandbox_id": sandbox_id,
                     "first_ts": ts, "last_ts": ts})
    store.update_session_metadata(session_id, {
        "sandbox_id": sandbox_id,
        "binding_history": hist,
    })


class _AgentsClient:
    """Agent compilation and lifecycle management."""

    def __init__(self, client: HarnessClient) -> None:
        self._client = client
        self._registry = _InstanceRegistry()

    def register(self, spec, name: str | None = None,
                 sandbox_id: str | None = None) -> AgentInstance:
        """Register a pre-built AgentSpec as a runnable instance.

        Preferred over compile() — no YAML involved.  The CLI layer
        reads YAML, validates, and passes AgentSpec objects here.

        I-26: ``sandbox_id`` pins this instance to a specific sandbox entity
        (uuid).  None = canonical entity of ``spec.sandbox`` config.
        """
        from harness.interfaces import AgentSpec

        errors = self._client._spec_svc.validate(spec)
        if errors:
            raise ValueError(f"Spec validation failed: {'; '.join(errors)}")

        # I-24: fail-fast on LLM profile shape (resolution happens at run()).
        profile = getattr(spec, "llm_profile", None)
        if not profile:
            raise ValueError(
                "spec.llm_profile is empty — set it to a profile defined in "
                "configs/llm.yaml or ~/.harness/llm.d/<profile>.yaml "
                "(e.g. 'default')")

        # I-13: backend field shape (AgentSpecService.validate covers values).
        backend = getattr(spec, "backend", "auto") or "auto"
        if backend not in ("auto", "cloud", "local"):
            raise ValueError(
                f"spec.backend must be auto|cloud|local, got {backend!r}")

        # Store spec in memory for later load()
        self._client._spec_svc.register(spec)

        profile_name = getattr(spec, "llm_profile", None) or "default"
        fallback_name = getattr(spec, "llm_fallback", None)

        instance_id = f"inst_{uuid.uuid4().hex[:12]}"
        instance = AgentInstance(
            instance_id=instance_id,
            spec_name=spec.name,
            llm_profile=profile_name,
            created_at=_iso_now(),
            status=AgentStatus.COMPILED,
            sandbox_id=sandbox_id,
        )
        self._registry.save(instance)
        # I-27: spec.policy 声明 → register 即自动 enable（未知名 warn 忽略）
        declared = getattr(spec, "policy", None)
        if declared:
            import warnings
            names = declared if isinstance(declared, list) else [declared]
            for pname in names:
                try:
                    _policy_engine.enable(pname, agent=spec.name)
                except ValueError as exc:
                    warnings.warn(f"register: {exc}", stacklevel=2)
        return instance


    def history_sessions(self, agent_name: str) -> list[dict]:
        """I-32: sessions this agent bound, newest-first (by latest edge)."""
        rows = []
        for sid in self._client._store.list_sessions():
            meta = self._client._store.get_session_metadata(sid) or {}
            edges = [e for e in (meta.get("binding_history") or [])
                     if e.get("agent") == agent_name]
            if not edges:
                continue
            latest = max(edges, key=lambda e: e.get("last_ts", 0))
            rows.append({"session_id": sid,
                         "instance_id": latest.get("instance_id"),
                         "sandbox_id": latest.get("sandbox_id"),
                         "first_ts": min(e.get("first_ts", 0) for e in edges),
                         "last_ts": latest.get("last_ts", 0)})
        rows.sort(key=lambda r: r["last_ts"], reverse=True)
        return rows

    def history_sandboxes(self, agent_name: str) -> list[dict]:
        """I-32: distinct sandboxes this agent bound, newest-first."""
        agg: dict[str, dict] = {}
        for sid in self._client._store.list_sessions():
            meta = self._client._store.get_session_metadata(sid) or {}
            for e in (meta.get("binding_history") or []):
                if e.get("agent") != agent_name:
                    continue
                sb = e.get("sandbox_id") or ""
                if not sb:
                    continue
                prev = agg.get(sb)
                if prev is None:
                    agg[sb] = {"sandbox_id": sb, "first_ts": e.get("first_ts", 0),
                               "last_ts": e.get("last_ts", 0),
                               "session_count": 1}
                else:
                    prev["first_ts"] = min(prev["first_ts"], e.get("first_ts", 0))
                    prev["last_ts"] = max(prev["last_ts"], e.get("last_ts", 0))
                    prev["session_count"] += 1
        rows = list(agg.values())
        rows.sort(key=lambda r: r["last_ts"], reverse=True)
        return rows

    def history_bindings(self, agent_name: str,
                         session_id: str) -> list[dict]:
        """I-32: edges of THIS agent in a session (others excluded)."""
        meta = self._client._store.get_session_metadata(session_id) or {}
        edges = [e for e in (meta.get("binding_history") or [])
                 if e.get("agent") == agent_name]
        edges.sort(key=lambda e: e.get("last_ts", 0), reverse=True)
        return edges
    def list(self) -> list[AgentInstance]:
        """List all compiled agent instances."""
        return self._registry.list_all()

    def get(self, instance_id: str) -> AgentInstance:
        """Get a specific agent instance by ID."""
        return self._registry.load(instance_id)

    def delete(self, instance_id: str) -> None:
        """Delete an agent instance from the registry.

        Sessions are independent of instances — deleting an instance
        does not affect existing or running sessions.
        """
        self._registry.delete(instance_id)

    def status(self, instance_id: str) -> str:
        """I-10/ADR-043: lifecycle status of a registered instance.

        I-11: pool-aware — if the spec has a live pooled instance, its
        real running/idle/stopped status wins; otherwise the registry-level
        status (compiled by default) is returned.
        """
        # I-25/D2: per-instance status — pooled[0] would return a sibling
        # same-template instance's state.
        inst = self._registry.load(instance_id)
        agent = self._client._pool.get_by_instance_id(instance_id)
        if agent is not None:
            return agent.status
        return inst.status.value

    def stop(self, instance_id: str, reason: str = "user_requested",
             session_id: str | None = None) -> bool:
        """I-12/ADR-044: request an app stop for a pooled running instance.

        Stop 是请求不是结论：请求被接受（等待 loop 步窗口裁决）返回 True；
        被拒绝（实例未运行 / reason 非法 fail-closed）返回 False。

        Args:
            instance_id: Registered agent instance id.
            reason: ADR-044 §5 合法 reason（objective_met/verify_passed/
                user_requested/parent_cancelled/client_disconnected/redirect）
                或需确认 reason（budget_policy/quality_unsatisfied）。
            session_id: Optional session context for the verdict hook.
        """
        # I-25/D2: stop the exact instance — list_instances(spec_name) would
        # collapse same-template siblings onto whichever is running first.
        inst = self._registry.load(instance_id)
        agent = self._client._pool.get_by_instance_id(instance_id)
        if agent is not None and agent.status == "running":
            return agent.request_stop(reason, requester="app",
                                      session_id=session_id)
        return False

    def run(
        self,
        spec_or_instance_id: Any,
        task: str | dict,
        session_id: str | None = None,
        config: dict | None = None,
        system_prompt: str = "",
        tools: list[dict] | None = None,
    ) -> RunResult:
        """I-23 (ADR-015): execute a task synchronously via public API.

        One-step assembly: register (if spec) → acquire (reuse guard) →
        session (new or continued) → execute_task → RunResult.

        Semantics (grill-closed):
        - spec | instance_id dual form; result carries instance_id.
        - ``session_id=None`` → new session (bound to instance);
          ``session_id=...`` → continue that session (light check: exists).
        - Assembly errors raise (fail-fast); task outcomes return.
        - Busy (RUNNING) instance → RuntimeError (no block, no queue).
        - tools=None → spec whitelist (see AgentRuntime._spec_tools).
        """
        # 1. Resolve instance identity
        from harness.interfaces import AgentSpec
        if isinstance(spec_or_instance_id, str):
            instance_id = spec_or_instance_id
            inst = self._registry.load(instance_id)
            spec = self._client._spec_svc.load(inst.spec_name)
        elif isinstance(spec_or_instance_id, AgentSpec):
            inst = self.register(spec_or_instance_id)
            instance_id = inst.instance_id
            spec = spec_or_instance_id
        else:
            raise TypeError(
                "run() expects an AgentSpec or an instance_id str, got "
                f"{type(spec_or_instance_id).__name__}")

        # 2. Acquire + busy guard (I-10 reuse guard; running → raise)
        # I-24: LLM profile resolution failures surface here with guidance
        # instead of blowing up mid-loop.
        try:
            # I-25/D2: dispatch to THIS registered instance's runtime
            # (never a by-name collapse onto a sibling instance).
            # I-26: pass the pinned sandbox entity id when bound.
            agent = self._client._pool.acquire(
                spec, instance_id=inst.instance_id,
                sandbox_id=inst.sandbox_id)
        except RuntimeError as exc:
            if "LLM profile" in str(exc) or "llm_profile" in str(exc):
                raise RuntimeError(
                    f"LLM profile '{getattr(spec, 'llm_profile', '')}' "
                    "cannot be resolved: " + str(exc) +
                    " — define it in configs/llm.yaml or ~/.harness/llm.d/, "
                    "or pass api_key/base_url explicitly") from exc
            raise
        if agent.status == "running":
            raise RuntimeError(
                f"agent '{spec.name}' is busy (running); use agents.stop or "
                "wait, or events.send for queued follow-up")

        # 3. Session: new (bind) or continue (light existence check)
        if session_id is None:
            session_id = self._client.sessions.start(instance_id).session_id
        else:
            from harness.interfaces import SessionNotFoundError
            try:
                self._client._logical_events(session_id)
            except SessionNotFoundError:
                raise ValueError(f"session '{session_id}' not found") from None

        # I-32: record agent→(session,sandbox) binding edge in metadata.
        _record_binding(self._client._store, session_id, spec.name,
                        inst.instance_id,
                        getattr(agent, "_sandbox_id", None) or "",
                        time.time())

        # 4. Execute (sync)
        result = agent.execute_task(task, session_id,
                                    config=config,
                                    system_prompt=system_prompt,
                                    tools=tools)

        # 5. Assemble RunResult from the event stream (terminal output)
        status = result.get("status", "failed")
        if status == "completed":
            output = self._terminal_output(session_id)
            return RunResult(session_id=session_id, instance_id=instance_id,
                             status="completed", output=output,
                             instance_status=result.get("instance_status"))
        if status == "stopped":
            return RunResult(session_id=session_id, instance_id=instance_id,
                             status="stopped",
                             stop_reason=result.get("stop_reason"),
                             instance_status=result.get("instance_status"))
        error = self._terminal_error(session_id) or result.get("error")
        return RunResult(session_id=session_id, instance_id=instance_id,
                         status="failed", error=error,
                         instance_status=result.get("instance_status"))

    def _terminal_output(self, session_id: str) -> str:
        """Last agent_completed.output, else last agent_thought content."""
        output = ""
        for ev in self._client._logical_events(session_id):
            if ev.event_type == "agent_completed":
                out = ev.payload.get("output", "")
                if isinstance(out, str) and out.strip():
                    output = out
            elif ev.event_type == "agent_thought" and not output:
                content = ev.payload.get("content") or ""
                if isinstance(content, str) and content.strip():
                    output = content
        return output

    def _terminal_error(self, session_id: str) -> str | None:
        error = None
        for ev in self._client._logical_events(session_id):
            if ev.event_type == "agent_failed":
                err = ev.payload.get("error")
                if err:
                    error = str(err)
        return error


class _SessionsClient:
    """Session runtime management."""

    def __init__(self, client: HarnessClient) -> None:
        self._client = client
        self._registry = _InstanceRegistry()

    def start(self, instance_id: str) -> SessionInfo:
        """Create a new session for an agent instance.

        Like 'docker create' — allocates a session ID and metadata,
        but does not start execution.  Use events.send() to inject
        messages and drive the agent loop.
        """
        inst = self._registry.load(instance_id)  # validate exists

        session_id = str(uuid.uuid4())
        self._client._store.create_session(
            session_id,
            metadata={
                "instance_id": instance_id,
                "spec_name": inst.spec_name,
                "llm_profile": inst.llm_profile,
            },
        )

        return SessionInfo(
            session_id=session_id,
            instance_id=instance_id,
            status=SessionStatus.CREATED,
            started_at=_iso_now(),
        )

    # ── I-42: 审计读面（物理日志）────────────────────────────
    def audit_events(self, session_id: str) -> list[EventInfo]:
        """审计读面：物理日志全量（含 session_rewound 与被回退内容）。

        与语义读面（sessions.events.list，逻辑视图）配对使用，用于调试对照：
        「模型为什么没看到 X」→ 先看语义视图，再看审计视图确认 X 是否被 rewind。
        """
        return [EventInfo.from_internal(e)
                for e in self._client._store.read_events(session_id)]

    def audit_path(self, session_id: str) -> str:
        """审计读面：会话事件文件（events.jsonl）路径。"""
        return self._client._store.events_path(session_id)

    # ── I-43: 组装产物预览（只读、与 runtime 单源同函数）──────────
    def preview_llm_messages(
        self,
        session_id: str,
        instance_id: str,
        event_id: str | None = None,
        task: str | None = None,
        system_prompt: str | None = None,
        overrides: dict | None = None,
    ) -> dict:
        """预览「发送给 LLM 的上下文」（组装产物面，读面三分之三）。

        与 runtime 调用**同一个函数**（prompt_preview.compose_llm_context），
        因此预览结果 == 实际请求；且**只读**：不落盘、不 acquire、不调用 LLM。

        Args:
            session_id: 会话 id。
            instance_id: **必填**——context_window / budget_ratio /
                reasoning_passback / system prompt 均来自该实例的 spec。
            event_id: 若给定，预览「以该事件为界」的历史上下文。
            task: 若给定，预览「当前视图 + 这条 Task」的上下文（不落盘）。
            system_prompt: 覆盖 system prompt（事件日志不存 system prompt，
                默认用**当前 spec 值**，见 meta.system_prompt_source）。
            overrides: context_window / budget_ratio / reasoning_passback /
                provider / system_prompt 显式覆盖。

        Returns:
            dict(messages, model, params, meta)；meta 含 truncated /
            dropped_event_count / folded_summary / stripped_dangling /
            as_of_event_id / synthetic_task / system_prompt_source。
        """
        try:
            inst = self._registry.load(instance_id)
        except KeyError:
            raise ValueError(
                f"unknown instance {instance_id!r} — preview 需要实例 spec "
                "来解析上下文窗口/预算/system prompt") from None
        spec = self._client._spec_svc.load(inst.spec_name)
        self._client._logical_events(session_id)  # 存在性检查（语义读面）

        from harness.prompt_preview import compose_llm_context
        return compose_llm_context(
            self._client._store, spec, session_id,
            system_prompt=system_prompt, task=task, event_id=event_id,
            overrides=overrides, agent_name=inst.spec_name)


    def stop(self, session_id: str) -> None:
        """Stop a session.

        I-24: the audit marker (session_stopped) is always appended; when the
        session is bound to a registered instance with a pooled RUNNING
        agent, this additionally requests an app stop (ADR-044 dual-track,
        reason=user_requested).  Unbound / not-running sessions keep the
        legacy marker-only behavior (backward compatible).
        """
        self._client._store.append_event(
            Event(
                event_id=f"ev_{uuid.uuid4().hex[:8]}",
                session_id=session_id,
                event_type="session_stopped",
                timestamp=time.time(),
                source="sdk",
                payload={},
            )
        )
        try:
            meta = self._client._store.get_session_metadata(session_id) or {}
            instance_id = meta.get("instance_id")
            if not instance_id:
                return
            inst = self._client.agents._registry.load(instance_id)
            pooled = self._client._pool.list_instances(inst.spec_name)
            running = [a for a in pooled if a.status == "running"]
            if running:
                running[0].request_stop("user_requested", requester="app",
                                        session_id=session_id)
        except Exception:
            pass  # best-effort: marker already appended

    def list(self) -> list[SessionInfo]:
        """List all sessions from the store."""
        sessions = []
        for sid in self._client._store.list_sessions():
            meta_path = Path(self._client._store.base_path) / sid / "meta.json"
            meta = {}
            if meta_path.exists():
                with meta_path.open("r", encoding="utf-8") as f:
                    meta = json.load(f)

            sessions.append(
                SessionInfo(
                    session_id=sid,
                    instance_id=meta.get("instance_id", ""),
                    status=SessionStatus.RUNNING,  # Simplified
                    started_at=meta.get("created_at", _iso_now()),
                    metadata=meta,
                )
            )
        return sessions

    def show(self, session_id: str) -> dict[str, Any]:
        """Return session details (I-31): log view + derived holder."""
        from harness.interfaces import SessionNotFoundError

        session_mgr = self._client._session_mgr
        try:
            events = session_mgr.wake(session_id)
        except SessionNotFoundError:
            raise KeyError(f"Session not found: {session_id}")
        holder_id, holder_status = self._holder(session_id)
        return {
            "session_id": session_id,
            "event_count": len(events),
            "events": events[-20:],
            "holder_instance_id": holder_id,
            "holder_status": holder_status,
        }

    def _holder(self, session_id: str):
        """Derive the session holder from the bound agent runtime.

        Returns (instance_id, status) where status is running|idle|unbound.
        Stopped/disposed/missing agents derive to unbound (I-31).
        """
        meta = self._client._store.get_session_metadata(session_id) or {}
        inst_id = meta.get("instance_id")
        if not inst_id:
            return None, "unbound"
        agent = self._client._pool.get_by_instance_id(inst_id)
        if agent is None or agent.status == "stopped":
            return inst_id, "unbound"
        return inst_id, agent.status

    def holder(self, session_id: str) -> dict:
        """Public holder query (I-31)."""
        holder_id, status = self._holder(session_id)
        return {"holder_instance_id": holder_id,
                "holder_status": status}

    def _guard_not_running(self, session_id: str) -> None:
        """Rewind/fork/checkpoint guard: bound agent RUNNING blocks."""
        _id, status = self._holder(session_id)
        if status == "running":
            raise RuntimeError(
                f"session {session_id} is held by running agent {_id}; "
                "stop it or wait before rewind/fork/checkpoint")

    def rewind(self, session_id: str, event_id: str) -> None:
        """Rewind a session to a specific event."""
        from harness.interfaces import SessionNotFoundError

        session_mgr = self._client._session_mgr
        try:
            session_mgr.wake(session_id)
        except SessionNotFoundError:
            raise KeyError(f"Session not found: {session_id}")

        self._guard_not_running(session_id)
        session_mgr.rewind(session_id, event_id)

    def fork(self, session_id: str, new_session_id: str) -> str:
        """Fork a session into a new session, copying all events."""
        from harness.interfaces import SessionNotFoundError

        session_mgr = self._client._session_mgr
        try:
            session_mgr.wake(session_id)
        except SessionNotFoundError:
            raise KeyError(f"Session not found: {session_id}")

        self._guard_not_running(session_id)
        return session_mgr.fork(session_id, new_session_id)


class _SessionEventsClient:
    """Event listing, sending, and streaming for a specific session."""

    def __init__(self, client: HarnessClient) -> None:
        self._client = client

    def logical(self, session_id: str):
        """The session's LOGICAL event view (rebuilt on every read).

        Distinct from `list()`/`filter()`/`find()`, which expose the PHYSICAL
        log: ADR-050/I-31 requires every semantic read to rebuild the view so
        rewind markers are honoured.  Apps (e.g. an app-layer memory policy) must
        use this rather than the private `client._logical_events`.
        """
        return self._client._session_mgr.logical_view(session_id)

    def list(self, session_id: str, after_event_id: str | None = None) -> list[EventInfo]:
        """List events for a session, optionally after a specific event."""
        events = self._client._logical_events(session_id)
        result = []
        found = after_event_id is None
        for ev in events:
            if not found:
                if ev.event_id == after_event_id:
                    found = True
                continue
            result.append(EventInfo.from_internal(ev))
        return result

    def filter(
        self,
        session_id: str,
        event_type: str,
        after_event_id: str | None = None,
    ) -> list[EventInfo]:
        """List events of a specific type.

        Args:
            session_id: Session to query.
            event_type: Event type to filter by (e.g. "tool_called").
            after_event_id: Return events after this ID, or all if None.

        Returns:
            Events matching the type. Empty list if none found.
        """
        all_events = self.list(session_id, after_event_id=after_event_id)
        return [e for e in all_events if e.event_type == event_type]

    def find(
        self,
        session_id: str,
        predicate: "Callable[[EventInfo], bool]",
        after_event_id: str | None = None,
    ) -> EventInfo | None:
        """Find the first event matching a predicate."""
        all_events = self.list(session_id, after_event_id=after_event_id)
        for ev in all_events:
            if predicate(ev):
                return ev
        return None

    def get(self, session_id: str, event_id: str) -> EventInfo | None:
        """Return a single event by its event_id."""
        events = self._client._logical_events(session_id)
        for ev in events:
            if ev.event_id == event_id:
                return EventInfo.from_internal(ev)
        return None

    def send(
        self,
        session_id: str,
        user_message: str,
        append: str | None = None,
    ) -> str:
        """Send a user message event to a session. Returns the event_id.

        I-11/ADR-043: after appending the audit event, wakes the pooled
        agent bound to this session (first message = first run, follow-ups
        = continuation round).  Sessions without a bound instance just
        append, as before.

        Args:
            user_message: The primary message content.
            append: Additional text appended after the message.

        Returns:
            The event_id of the injected message.
        """
        message = user_message
        if append:
            message = f"{message}\n\n[追加指令]\n{append}"

        event_id = f"ev_{uuid.uuid4().hex[:8]}"
        self._client._store.append_event(
            Event(
                event_id=event_id,
                session_id=session_id,
                event_type="user_message",
                timestamp=time.time(),
                source="user",
                payload={"message": message},
            )
        )
        # I-11/ADR-043: wake the bound agent (best-effort, never raises).
        self._client._wake_session(session_id, message)
        return event_id

    def stream(self, session_id: str, poll_interval: float = 0.5) -> Iterator[EventInfo]:
        """Block and yield new events as they arrive (polling generator).

        Usage:
            for event in client.sessions.events.stream("sess_xxx"):
                print(event)
        """
        last_event_id: str | None = None
        while True:
            events = self._client._logical_events(
                session_id, after_event_id=last_event_id)
            for ev in events:
                yield EventInfo.from_internal(ev)
                last_event_id = ev.event_id
            time.sleep(poll_interval)


# Attach events sub-client to SessionsClient
_SessionsClient.events = property(lambda self: _SessionEventsClient(self._client))


class _LLMClient:
    """LLM driver registry view (ADR-055).

    The framework owns the SEMANTICS of a request (ADR-054: an explicit
    AgentSpec value wins over the profile); a *driver* owns the FIELD NAMES a
    backend actually reads.  This facade is how an APP adds a backend without
    touching the framework:

        client.llm.register_driver("my-vllm", MyVllmDriver())

    Drivers are process-level (a module registry), so every HarnessClient sees
    the same set — like the hook registry (ADR-042).
    """

    def __init__(self, client: "HarnessClient") -> None:
        self._client = client

    def register_driver(self, name: str, driver: Any) -> None:
        """Register (or replace) a driver under `name`.

        A driver must expose `name`, `build_extra_body(spec, cfg, thinking)` and
        `reasoning_of(message)`.  Malformed drivers are rejected here, not
        mid-run.
        """
        from harness.llm_driver import register_driver
        register_driver(name, driver)

    def list_drivers(self) -> list[str]:
        """Names of all registered drivers."""
        from harness.llm_driver import registered_drivers
        return registered_drivers()

    def resolve(self, spec: Any = None, cfg: Any = None) -> str:
        """Which driver name would serve this spec/profile pair (for diagnostics)."""
        from harness.llm_driver import driver_for
        return driver_for(spec, cfg).name

    def register_builtin(self) -> None:
        """Re-register the shipped drivers (idempotent)."""
        from harness.llm_driver import (LlamaCppDriver, PassthroughDriver,
                                        register_driver)
        register_driver(PassthroughDriver.name, PassthroughDriver())
        register_driver(LlamaCppDriver.name, LlamaCppDriver())


class _HooksClient:
    """Process-level hook registry view (ADR-042).

    A thin facade over the module-level HookRegistry singleton — every
    HarnessClient sees the same hooks (client A's registration is visible
    to client B).
    """

    def __init__(self, client: HarnessClient) -> None:
        self._client = client
        from harness.hooks import HookRegistry
        from harness.hooks import points as _points
        self._registry = HookRegistry.get()
        self._points = _points

    @property
    def points(self):
        """Canonical hook point names (ADR-042).

        Exposed so an app can write `client.hooks.on(client.hooks.points.AGENT_PRE_STEP, …)`
        without importing `harness.hooks` directly (ADR-015).
        """
        return self._points

    def on(self, point: str, mode: str, fn, priority: int = 0) -> None:
        """Register a hook (modes: observe / transform / veto / around)."""
        self._registry.on(point, mode, fn, priority=priority)

    def trigger(self, point: str, payload=None, **kwargs):
        """Dispatch hooks for a point (see HookRegistry.trigger)."""
        return self._registry.trigger(point, payload, **kwargs)

    @property
    def registry(self):
        """Expose the underlying registry for advanced use."""
        return self._registry


class _GatesClient:
    """Human gate resolution — uses Gateway directly."""

    def __init__(self, client: HarnessClient) -> None:
        self._client = client

    def approve(self, gate_event_id: str) -> None:
        self._client._gateway.resolve(gate_event_id, "approve")

    def reject(self, gate_event_id: str, reason: str | None = None) -> None:
        self._client._gateway.resolve(gate_event_id, "reject", amend={"reason": reason} if reason else None)

    def modify(self, gate_event_id: str, amend_input: dict[str, Any]) -> None:
        self._client._gateway.resolve(gate_event_id, "modify", amend=amend_input)

    def list_pending(self, session_id: str) -> list:
        """Return all pending (unresolved) gates for a session.

        Reads from the session event log.  A gate is pending if a
        human_gate_pending event exists without a later
        human_gate_resolved event for the same event_id.
        """
        from harness.interfaces import GateInfo

        store = self._client._store
        events = self._client._logical_events(session_id)

        # Collect pending gates: event_id with pending but no resolved
        pending_ids: set[str] = set()
        resolved_ids: set[str] = set()
        gate_info: dict[str, dict] = {}
        for ev in events:
            if ev.event_type == "human_gate_pending":
                eid = ev.payload.get("gate_event_id", ev.event_id)
                pending_ids.add(eid)
                gate_info[eid] = {
                    "session_id": session_id,
                    "phase_name": None,  # Phase gates removed with DAG (ADR-038)
                    "node_id": ev.payload.get("node_id"),
                    "status": "pending",
                }
            elif ev.event_type == "human_gate_resolved":
                resolved_ids.add(ev.event_id)
                if ev.event_id in gate_info:
                    gate_info[ev.event_id]["status"] = ev.payload.get("decision", "resolved")

        result = []
        for eid in pending_ids - resolved_ids:
            info = gate_info.get(eid, {})
            result.append(GateInfo(
                event_id=eid,
                session_id=session_id,
                status="pending",
                phase_name=info.get("phase_name"),
                node_id=info.get("node_id"),
            ))
        return result

    def get(self, gate_event_id: str, session_id: str):
        """Return GateInfo for a specific gate event.

        Returns None if no matching gate event is found.
        """
        from harness.interfaces import GateInfo

        store = self._client._store
        events = self._client._logical_events(session_id)

        info: dict[str, Any] = {"status": "unknown"}
        for ev in events:
            if ev.event_type == "human_gate_pending":
                eid = ev.payload.get("gate_event_id", ev.event_id)
                if eid == gate_event_id:
                    info.update({
                        "session_id": session_id,
                        "phase_name": None,  # Phase gates removed with DAG (ADR-038)
                        "node_id": ev.payload.get("node_id"),
                        "status": "pending",
                    })
            elif ev.event_type == "human_gate_resolved" and ev.event_id == gate_event_id:
                info["status"] = ev.payload.get("decision", "resolved")

        if info.get("status") == "unknown":
            return None
        return GateInfo(
            event_id=gate_event_id,
            session_id=session_id,
            status=info["status"],
            phase_name=info.get("phase_name"),
            node_id=info.get("node_id"),
        )


class _SandboxClient:
    """Sandbox lifecycle management.

    Exposes SandboxManager operations (destroy, list, set_ttl) through the
    SDK public API.
    """

    def __init__(self, client: HarnessClient) -> None:
        self._client = client

    def destroy(self, sandbox_id: str) -> None:
        """Force-kill and remove a sandbox entity by its sandbox_id (uuid).

        Legacy config-name lookup is honoured via the canonical entity.
        No-op if the entity does not exist.
        """
        self._client._sandbox_mgr.destroy(sandbox_id)

    def destroy_all(self) -> None:
        """Force-kill all active sandbox instances.

        Use with caution — this terminates every running sandbox
        in the current process.
        """
        self._client._sandbox_mgr.destroy_all()

    def list(self):
        """Return all live sandbox entities (uuid first-class, I-26)."""
        return self._client._sandbox_mgr.list()

    def set_ttl(self, name: str, ttl_seconds: int, idle_timeout: int = 0) -> None:
        """Set TTL and idle timeout for a named sandbox instance.

        Applies to the next spawn of this sandbox.  A running sandbox
        must be destroyed and recreated for the new TTL to take effect.

        Args:
            name: Sandbox instance name (from sandbox.yaml).
            ttl_seconds: Maximum lifetime since spawn (0 = disabled).
            idle_timeout: Maximum idle time since last execute() (0 = disabled).
        """
        self._client._sandbox_mgr.set_ttl(name, ttl_seconds, idle_timeout)



class _ProgramClient:
    """I-18: deterministic zero-token script runner (SDK execution surface).

    Program = configuration subset of the agent class — no AgentRuntime,
    no LLM, no inference loop.  Output is written to session events
    (primary); a sandbox is optional (scripts persist FS artifacts there).
    """

    def __init__(self, client: HarnessClient) -> None:
        self._client = client

    def run(self, script: str, session_id: str | None = None, *,
            sandbox_name: str | None = None,
            sandbox_id: str | None = None,
            timeout: float = 60.0,
            retries: int = 0) -> ProgramResult:
        """Run a deterministic script; returns a ProgramResult."""
        import time as _time
        import uuid as _uuid
        from harness.interfaces import Event
        from harness.sdk.program import (
            ProgramResult, run_script_host, run_script_sandbox,
        )

        if session_id is None:
            session_id = str(_uuid.uuid4())
            self._client._store.create_session(session_id, {"type": "program"})

        def _emit(event_type: str, payload: dict) -> None:
            self._client._store.append_event(Event(
                event_id=str(_uuid.uuid4()),
                session_id=session_id,
                event_type=event_type,
                timestamp=_time.time(),
                source="program",
                parent_event_id=None,
                payload=payload,
            ))

        _emit("program_started", {"script": script,
                                  "sandbox_id": sandbox_id})

        if sandbox_name is not None or sandbox_id is not None:
            name = sandbox_name or "main"
            result = run_script_sandbox(
                script, name, self._client._sandbox_mgr,
                sandbox_id=sandbox_id, retries=retries)
        else:
            result = run_script_host(script, timeout=timeout,
                                     retries=retries)
        result.session_id = session_id
        _emit("program_completed", {
            "status": result.status,
            "output": result.output,
            "exit_code": result.exit_code,
            "attempts": result.attempts,
        })
        return result


class _ConfigClient:
    """Deployment configuration (LLM profiles).

    Exposes profile resolution so an app can see which engine/endpoint a
    summariser or sub-agent would bind to, without importing
    `harness.sdk.config` directly.
    """

    def __init__(self, client: HarnessClient) -> None:
        self._client = client

    def profile(self, name: str = "default"):
        """Resolve an LLM profile (base_url, model, provider, backend, …)."""
        from harness.sdk.config import resolve_llm_profile
        return resolve_llm_profile(name, project_dir=self._client._project_dir)

    def profiles(self) -> list[str]:
        """Names of the profiles available to this deployment."""
        from harness.sdk.config import available_llm_profiles
        return list(available_llm_profiles(project_dir=self._client._project_dir))


class _PolicyClient:
    """I-27: scoped rule middleware surface.

    enable/add MUST name an agent (or "*").  All presets default OFF.
    """

    def __init__(self, client: HarnessClient) -> None:
        self._client = client

    def enable(self, name: str, agent: str, *,
               cfg: dict | None = None) -> None:
        """Enable a defined rule (builtin preset or added) for an agent.

        ADR-015 (2026-10-19): ``cfg`` is merged over the rule's default cfg so an
        app can configure a builtin preset (e.g. ``read_before_edit``'s
        ``digest_provider`` / parameter-name mapping) without re-implementing it.
        NOTE: the cfg lives on the rule's global definition (this architecture
        has no per-agent cfg); use one rule per distinct configuration.
        """
        _policy_engine.enable(name, agent=agent, cfg=cfg)

    def guard(self, agent: str):
        """Return the compiled `SandboxRestriction` for an agent, or None.

        ADR-015 (2026-10-19): replaces `from harness.policy import engine` for
        apps that want to assert/inspect what the kernel guard actually compiled.
        """
        return _policy_engine.compile_guard(agent)

    def add(self, name: str, agent: str, *,
            veto=None, observe=None, cfg: dict | None = None,
            cedar: str | None = None, kind: str | None = None,
            guard: dict | None = None,
            description: str | None = None,
            reason: str | None = None) -> None:
        """Insert an app-defined rule, immediately active for the agent.

        I-35: cedar/guard/kind/description/reason define the policy unit.
        """
        _policy_engine.add(name, agent=agent, veto=veto, observe=observe,
                           cfg=cfg, cedar=cedar, kind=kind, guard=guard,
                           description=description, reason=reason)

    def disable(self, name: str, agent: str) -> None:
        _policy_engine.disable(name, agent=agent)

    def list(self) -> list:
        return _policy_engine.list()


class _ModelsClient:
    """I-14 (Q8-A): model service pool — public management surface.

    register/unregister/status/list are public; _acquire is internal routing
    (consumed by the agent pool; not part of the documented execution API).
    """

    def __init__(self, client: HarnessClient) -> None:
        self._client = client

    def register(self, model_service: str, url: str, *,
                 health_path: str = "/health", model: str = "",
                 load_adapter_path: str = "",
                 preloaded_adapters: list[str] | None = None) -> None:
        """Register (or update) an endpoint for a model service."""
        self._client._model_pool.register(
            model_service, url, health_path=health_path, model=model,
            load_adapter_path=load_adapter_path,
            preloaded_adapters=preloaded_adapters)

    def unregister(self, model_service: str) -> None:
        self._client._model_pool.unregister(model_service)

    def status(self, model_service: str) -> ModelServiceStatus | None:
        return self._client._model_pool.status(model_service)

    def list(self) -> list[ModelServiceStatus]:
        return self._client._model_pool.list()

    def _acquire(self, model_service: str, adapter: str = "") -> tuple[str, set]:
        """Internal: resolve a healthy endpoint (+ best-effort adapter warmup)."""
        return self._client._model_pool._acquire(model_service, adapter=adapter)


class _SkillsClient:
    """Skills discovery and management.

    Scans the sandbox filesystem for installed skills and returns
    metadata suitable for prompt assembly via prompts.assemble().
    """

    def __init__(self, client):
        self._client = client

    def scan(self, sandbox, sandbox_id: str = "") -> list:
        """Scan sandbox .skills/ directory for installed skills.

        Reads .skills/ subdirectories, parses SKILL.md frontmatter,
        returns SkillMeta list sorted alphabetically by name for KV
        cache stability.
        """
        from harness.skills import SkillMeta

        metas: dict[str, SkillMeta] = {}
        stdout, stderr, code = sandbox.execute(
            sandbox_id,
            "ls -1 .skills/ 2>/dev/null || true"
        )
        if code != 0 or not stdout.strip():
            return []

        for name in stdout.strip().split("\n"):
            name = name.strip()
            if not name:
                continue
            raw, _, _ = sandbox.execute(
                sandbox_id,
                f"head -20 .skills/{name}/SKILL.md"
            )
            try:
                import yaml
                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        n = fm.get("name", "")
                        d = fm.get("description", "")
                        if n and d:
                            metas[n] = SkillMeta(name=n, description=d)
            except Exception:
                continue

        return sorted(metas.values(), key=lambda m: m.name)


class _PromptsClient:
    """System prompt assembly and message construction.

    Takes over prompt assembly responsibilities formerly in
    AgentRuntime._build_messages: vars substitution, prompt_append,
    skills metadata injection, and tool description extraction.
    """

    def __init__(self, client):
        self._client = client

    # ── Prompt assembly ──────────────────────────────────

    def assemble(
        self,
        base_prompt: str,
        vars: dict[str, str] | None = None,
        skills: list | None = None,
        append: str | None = None,
    ) -> str:
        """Assemble a system prompt from components.  Delegates to prompt_assembly."""
        from harness.prompt_assembly import assemble_prompt
        return assemble_prompt(base_prompt, vars=vars, skills=skills, append=append)

    # ── Message construction ────────────────────────────

    def build_messages(
        self,
        system_prompt: str,
        events: list,
        context_window: int = 128000,
        budget_ratio: float = 1.0,
        content_filter=None,
    ) -> list[dict]:
        """Convert events to OpenAI messages with token-aware truncation.

        Delegates to prompt_assembly.build_messages.
        """
        from harness.prompt_assembly import build_messages as _build
        return _build(system_prompt, events, context_window, budget_ratio,
                      content_filter=content_filter)

    # ── Tool descriptions ───────────────────────────────

    def describe_tools(self, registry, filter_names: list[str] | None = None):
        """Return OpenAI-format tool definitions, optionally filtered.

        Delegates to prompt_assembly.describe_tools.
        """
        from harness.prompt_assembly import describe_tools as _desc
        return _desc(registry, filter_names)

class _GatewayPublicClient:
    """Public-safe subset of Gateway exposed to SDK users.

    Exposes register_rule, list_pending, get_gate, create_gate but NOT
    resolve (resolution goes through the SDK gates client / Gateway
    directly).  Authorization hooks now live in the unified hooks
    registry (ADR-042) via client.hooks.
    """

    def __init__(self, gateway) -> None:
        self._gateway = gateway
        self._raw = gateway  # Internal: raw Gateway for testing/advanced use

    def register_rule(self, fn, name: str = "") -> None:
        self._gateway.register_rule(fn, name)

    def list_pending(self, session_id: str) -> list:
        return self._gateway.list_pending(session_id)

    def get_gate(self, event_id: str, session_id: str):
        return self._gateway.get_gate(event_id, session_id)

    def create_gate(self, ctx) -> str:
        return self._gateway.create_gate(ctx)

    @property
    def gate_timeout(self) -> float:
        return self._gateway._gate_timeout

    @gate_timeout.setter
    def gate_timeout(self, value: float) -> None:
        self._gateway._gate_timeout = value


class _ToolsClient:
    """Tool registration — thin wrapper around the client's ToolRegistry.

    The registry is CLIENT-level and is wired into every runtime this client
    creates (each runtime gets a copy so dependency injection stays per-instance).
    The default registry is `load_builtins()`; pass `tool_registry=ToolRegistry()`
    to `HarnessClient(...)` to start from an empty set for an app-owned agent.

    Usage::

        @client.tools.register("check", "Validate results", {"path": str})
        def check(path: str) -> dict:
            return {"ok": True, "content": "all good", "exit_code": 0}

        client.tools.registry  # → ToolRegistry instance
        client.tools.list()    # → ["check", ...]
    """

    def __init__(self, client: HarnessClient | None = None,
                 registry=None) -> None:
        from harness.tool_registry import ToolRegistry
        from harness.tools import load_builtins
        self._client = client
        self.registry = registry if registry is not None else load_builtins()

    def register(self, name: str, description: str,
                 parameters: dict[str, type],
                 deps: list[str] | None = None):
        """Decorator: register a tool function."""
        return self.registry.register(name, description, parameters, deps)

    def list(self) -> list[str]:
        """Return registered tool names."""
        return self.registry.list_tools()

    def definitions(self) -> list[dict]:
        """Return OpenAI function-calling compatible tool definitions."""
        return self.registry.get_definitions()


class _MemoryClient:
    """Memory slots and token accounting.

    Compaction POLICY belongs to the app layer (ADR-001).  The harness provides
    only the mechanism: two fixed insertion slots around the Task anchor
    (`inject_summary`), plus token counting so an app can measure what it is
    about to send.

    NOTE: the former `compress()` built-in is GONE.  It was a parallel compaction
    path (a programmatically-registered `memory-compressor` agent spec) whose
    model id, `deepseek-v4-flash`, DOES NOT EXIST — so it could only ever fail —
    and whose only consumers were its own tests.  With the app owning compaction
    it had no role.
    """

    def __init__(self, client: HarnessClient) -> None:
        self._client = client

    # The two fixed insertion points around the Task anchor.  The harness owns
    # the POSITIONS and the placeholder; the app owns the text (ADR-001).
    SLOTS = ("before_task", "after_task")

    def inject_summary(self, session_id: str, content: str = "", *,
                       slot: str, after_event_id: str | None = None) -> str:
        """Place `content` in one of the two memory slots.

        Args:
            slot: **required** — ``"before_task"`` or ``"after_task"``.  The
                harness cannot guess which region a summary describes (that is
                the app's decision, ADR-001), so placement is explicit.
            after_event_id: fold boundary.  Events older than this id stop being
                assembled (they are what the summary replaces).  Purely a
                boundary now — it no longer influences slot placement.
            content: the summary text.  The harness wraps it in ``<mem>…</mem>``,
                which makes slots self-identifying so a replacement pass can
                never confuse one with the Task anchor.

        Replacement: a slot holds at most ONE summary.  The log is append-only
        (the store has no rewrite primitive, and keeping history is the point),
        so "replacement" is a READ rule the harness applies at assembly time:
        for each slot the NEWEST event wins and older ones are ignored.  That is
        why this method need not — and must not — try to delete anything: the app
        has no way to remove events either.

        Returns the new event id.
        """
        import time
        import uuid

        from harness.interfaces import Event

        if slot not in self.SLOTS:
            raise ValueError(
                f"slot must be one of {self.SLOTS}, got {slot!r} — the harness "
                "does not infer placement (ADR-001)")

        payload: dict = {"content": content, "slot": slot}
        if after_event_id:
            payload["after_event_id"] = after_event_id
        eid = f"mem_{uuid.uuid4().hex[:8]}"
        self._client._store.append_event(Event(
            event_id=eid, session_id=session_id, event_type="memory_summary",
            timestamp=time.time(), source="memory", payload=payload))
        if self._client._audit:
            self._client._audit.append(
                level="info", category="memory", event="memory_summary_injected",
                session_id=session_id,
                detail={"slot": slot, "content_length": len(content),
                        "after_event_id": after_event_id})
        return eid

    def _provider(self, provider: str | None = None) -> str:
        """Tokenizer provider: explicit > client-project profile > 'deepseek'."""
        if provider:
            return provider
        try:
            from harness.sdk.config import resolve_llm_profile as _resolve
            return _resolve("default", project_dir=self._client._project_dir).provider
        except Exception:                                   # noqa: BLE001
            return "deepseek"

    def count_tokens(self, text: str, provider: str | None = None) -> int:
        """Tokens in `text`, using the harness's counting mechanism.

        Exposed so an app can measure what it is about to send without importing
        `prompt_assembly._count_tokens` (a private name).  `provider` selects the
        fallback tokenizer directory; omit it to use the deployment default.
        """
        from harness.prompt_assembly import _count_tokens
        return _count_tokens(text, self._provider(provider), "tokenizer_dir")

    def event_cost(self, event, provider: str | None = None,
                   reasoning_passback: bool = True) -> int:
        """Tokens ONE event contributes to the assembled prompt.

        ADR-015 (2026-10-19): public replacement for the app importing
        `prompt_assembly._cost` (ADR-001 water marks / size guard).  Mirrors the
        assembler's own accounting: content (+ reasoning when passed back) +
        tool-call arguments, and the CLIPPED tool result.
        """
        from harness.prompt_assembly import _cost
        return _cost(event, self._provider(provider), "tokenizer_dir",
                     reasoning_passback=reasoning_passback)

    def span_cost(self, events, provider: str | None = None,
                  reasoning_passback: bool = True) -> int:
        """Sum of `event_cost` over a span (ADR-001 §4.4 size-guard baseline)."""
        provider = self._provider(provider)
        return sum(self.event_cost(ev, provider, reasoning_passback)
                   for ev in events)

    def estimate_tokens(self, session_id: str) -> int:
        """Legacy rough estimate over a session's raw payloads.

        APP layer should prefer `span_cost` over the events it is about to drop
        (this method stringifies payloads and is NOT what the assembler sends).
        """
        from harness.prompt_assembly import _count_tokens
        events = self._client._logical_events(session_id)
        provider = self._provider()
        total = 0
        for ev in events:
            total += _count_tokens(str(ev.payload), provider)
        return total

    def tail(self, session_id: str, keep_ratio: float = 0.6):
        """Split session events at keep_ratio boundary.

        Returns (old_events, boundary_id) where:
        - old_events: oldest (1-keep_ratio) events → to compress
        - boundary_id: first retained event's ID → inject(after_event_id=)

        Returns ([], None) if keep_ratio covers all events.
        """
        events = self._client._logical_events(session_id)
        cut = int(len(events) * (1 - keep_ratio))
        if cut <= 0:
            return [], None
        old = events[:cut]
        boundary = events[cut].event_id if cut < len(events) else None
        return old, boundary


# ─────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────


def _iso_now() -> str:
    """Return current ISO 8601 timestamp string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
