"""Agent Instance Pool — manages agent lifecycle for reuse.

Decouples agent instances from sandbox containers. An agent instance is a
long-lived Python object (AgentRuntime) that can execute multiple tasks.
The pool supports acquire/release semantics. Instances are keyed by
(name, instance_id) to support multiple concurrent instances of the same agent.

Phase 2 implementation: dict with lazy creation + concurrent support.
Phase 3 will add health checks, TTL, and auto-scaling.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable
import threading

import openai  # I-14/Q9: rebuild the agent client on endpoint update

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import GateContext
from harness.interfaces import (
    AgentSpec,
    SandboxInterface as SandboxManagerInterface,
    SessionStoreInterface,
)


class AgentInstancePool:
    """Manages a pool of agent instances keyed by (name, instance_id).

    Supports concurrent instances of the same agent via create_new=True.
    Supports per-agent LLM configuration via llm_profile resolution.
    """

    def __init__(
        self,
        store: SessionStoreInterface,
        gateway,
        api_key: str,
        sandbox_manager: SandboxManagerInterface,
        base_url: str = "https://api.deepseek.com",
        resolve_llm: Callable[[str, Path | None], Any] | None = None,
        project_dir: Path | None = None,
        audit_log=None,
        model_pool=None,  # I-14: ModelServicePool for local-backend routing
        tool_registry=None,  # ADR-015: client-level registry (copied per runtime)
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._api_key = api_key
        self._base_url = base_url
        self._sandbox_manager = sandbox_manager
        self._resolve_llm = resolve_llm
        self._project_dir = project_dir
        self._audit_log = audit_log
        self._model_pool = model_pool
        self._tool_registry = tool_registry
        self._pool: dict[tuple[str, str], AgentRuntime] = {}
        self._specs: dict[str, AgentSpec] = {}
        self._lock = threading.Lock()

    def acquire(self, spec: AgentSpec, create_new: bool = False,
                spawner=None,
                api_key: str | None = None,
                base_url: str | None = None,
                instance_id: str | None = None,
                sandbox_id: str | None = None) -> AgentRuntime:
        """Get an agent instance by spec, creating it if necessary.

        Always verifies sandbox freshness via sandbox_manager.get_or_create().
        If the sandbox was respawned (config changed), updates cached agent's
        sandbox references in-place.

        I-25/D1: ``instance_id`` given -> exact (name, instance_id) keying —
        one runtime per registered instance (never a random-uuid runtime, no
        by-name collapse).  ``instance_id=None`` keeps the legacy random-uuid
        keyed behavior.

        Args:
            api_key: Override the default API key for this instance.
            base_url: Override the default base URL for this instance.
            instance_id: Registered instance id to pin this runtime to.
        """
        name = spec.name
        # I-25/D1: preserve the requested instance id — the sandbox
        # get_or_create() below reuses the ``instance_id`` name locally.
        requested_instance_id = instance_id

        # ── Gateway: sandbox_spawn authorization ──
        # Allow rules to inspect and modify sandbox config (e.g., strip
        # sensitive env vars) before sandbox_manager creates the sandbox.
        sandbox_config = self._sandbox_manager.get_config(spec.sandbox)
        if sandbox_config is not None:
            ctx = GateContext(
                gate_type="sandbox_spawn",
                session_id="",
                spec=spec,
                config=sandbox_config,
            )
            result = self._gateway.authorize(ctx)
            if result.decision == "denied":
                raise RuntimeError(
                    f"Sandbox spawn denied for {spec.sandbox}: {result.reason}"
                )
            if result.decision == "modified" and result.amend_args:
                # Record rule amendments in audit log generically.
                # Do NOT modify sandbox config — the rule expresses
                # intent (which keys to watch) but tools need real values.
                if self._audit_log is not None:
                    self._audit_log.append(
                        level="info", category="gateway",
                        event="sandbox_spawn_modified",
                        detail={
                            "sandbox": spec.sandbox,
                            "amend_args": result.amend_args,
                        },
                    )

        # I-26: resolve the sandbox ENTITY — canonical by config name, or a
        # pinned sandbox_id (the manager re-materialises it when missing).
        # Manager owns respawn/alive detection; uuid identity is stable.
        if sandbox_id is not None:
            sandbox_entity = self._sandbox_manager.ensure(
                spec.sandbox, sandbox_id)
        else:
            sandbox_entity = self._sandbox_manager.get_or_create(spec.sandbox)
        sandbox_backend = sandbox_entity.backend
        resolved_sandbox_id = sandbox_entity.sandbox_id

        # Resolve per-agent LLM configuration from spec.llm_profile.
        # I-13: resolve the FULL LLMConfig (model/backend/adapter), not just
        # credentials; also resolve the fallback profile when declared.
        resolved_api_key = api_key if api_key is not None else self._api_key
        resolved_base_url = base_url if base_url is not None else self._base_url
        llm_config: Any = None
        llm_fallback_config: Any = None
        if not resolved_api_key and self._resolve_llm is not None:
            # I-24: do NOT swallow profile-resolution failures — a missing
            # profile would otherwise surface as a confusing
            # "Missing credentials" OpenAIError at runtime construction.
            # Fail fast here so the caller can add guidance (see
            # _AgentsClient.run).
            llm_config = self._resolve_llm(
                spec.llm_profile, project_dir=self._project_dir)
            if llm_config.api_key:
                resolved_api_key = llm_config.api_key
            if llm_config.base_url:
                resolved_base_url = llm_config.base_url

            # I-13/Q1: explicit spec.backend must match the profile backend.
            declared = getattr(spec, "backend", "auto") or "auto"
            if declared != "auto" and declared != llm_config.backend:
                raise RuntimeError(
                    f"spec.backend={declared!r} conflicts with profile "
                    f"{spec.llm_profile!r} backend={llm_config.backend!r} — "
                    "fix configs/llm.yaml (or ~/.harness/llm.d) or set "
                    "spec.backend=auto")

            # I-13: fallback profile (single-level).
            fallback_name = getattr(spec, "llm_fallback", "") or ""
            if fallback_name:
                llm_fallback_config = self._resolve_llm(
                    fallback_name, project_dir=self._project_dir)

        # I-14/Q5-A: local backends route through the model service pool —
        # resolve a healthy endpoint once at acquire; _call_llm stays unchanged.
        # When no pool is configured, fall back to the I-13 behavior (direct
        # profile base_url).
        model_service_url = None
        if (llm_config is not None and llm_config.backend == "local"
                and self._model_pool is not None):
            # ADR-054 §5: the pool key and the request model must come from the
            # SAME resolution, otherwise a spec/profile mismatch silently looks
            # up one model and sends another.
            from harness.interfaces import resolve_spec_model
            service = (getattr(llm_config, "model_service", "")
                       or resolve_spec_model(getattr(spec, "model", ""),
                                             getattr(llm_config, "model", "")))
            model_service_url, _loaded = self._model_pool._acquire(
                service, adapter=getattr(spec, "adapter", "") or "")
            llm_config.base_url = model_service_url
            resolved_base_url = model_service_url

        with self._lock:
            if not create_new:
                for (pool_name, _iid), agent in list(self._pool.items()):
                    if pool_name == name and (
                            requested_instance_id is None
                            or _iid == requested_instance_id):
                        # I-10/ADR-043: stopped or idle-expired instances are
                        # not reusable — dispose them (join the wake worker
                        # thread, I-11) and let a new one spawn.
                        if agent.status == "stopped" or (
                            agent.status == "idle"
                            and agent.is_idle_expired(agent._idle_timeout)
                        ):
                            agent.dispose()
                            self._pool.pop((pool_name, _iid), None)
                            continue
                        if agent._sandbox_id != resolved_sandbox_id:
                            agent._sandbox_id = resolved_sandbox_id
                            agent._sandbox = sandbox_backend
                            if self._audit_log is not None:
                                self._audit_log.append(
                                    level="info", category="agent",
                                    event="agent_sandbox_updated",
                                    detail={
                                        "spec_name": name,
                                        "sandbox_id": resolved_sandbox_id,
                                    },
                                )
                        agent._sandbox_config = (
                            self._sandbox_manager.get_config(spec.sandbox)
                        )
                        # I-14/Q9: engine restart on a new port → point the
                        # reused agent at the current registered endpoint
                        # (audit the internal auto-update).
                        if model_service_url is not None and \
                                agent._model_service_url != model_service_url:
                            old = agent._model_service_url
                            agent._model_service_url = model_service_url
                            agent.base_url = model_service_url
                            agent._client = openai.OpenAI(
                                api_key=resolved_api_key,
                                base_url=model_service_url)
                            if self._audit_log is not None:
                                self._audit_log.append(
                                    level="info", category="model",
                                    event="model_endpoint_updated",
                                    detail={
                                        "model_service": pool_name,
                                        "old": old, "new": model_service_url,
                                    },
                                )
                        if spawner:
                            agent.set_spawner(spawner)
                        if self._audit_log is not None:
                            self._audit_log.append(
                                level="info", category="agent", event="agent_reused",
                                detail={"spec_name": name, "instance_id": _iid},
                            )
                        return agent

            agent = AgentRuntime(
                spec=spec,
                gateway=self._gateway,
                store=self._store,
                api_key=resolved_api_key,
                base_url=resolved_base_url,
                sandbox=sandbox_backend,
                sandbox_instance_id=resolved_sandbox_id,
                audit_log=self._audit_log,
                llm_config=llm_config,
                llm_fallback_config=llm_fallback_config,
                tool_registry=self._tool_registry,
            )
            # I-14: record the model-service endpoint for Q9 reuse linkage.
            agent._model_service_url = model_service_url
            agent._sandbox_config = self._sandbox_manager.get_config(spec.sandbox)
            if spawner:
                agent.set_spawner(spawner)
            # I-25/D1: instance-keyed runtime — pin to the registered
            # instance id so run/stop/status/wake can locate it exactly.
            key = ((name, requested_instance_id)
                   if requested_instance_id is not None
                   else (name, str(uuid.uuid4())))
            agent.instance_id = key[1]
            agent._idle_timeout = getattr(spec, "idle_timeout", 0) or 0
            self._pool[key] = agent
            self._specs[name] = spec
            if self._audit_log is not None:
                self._audit_log.append(
                    level="info", category="agent", event="agent_created",
                    detail={"spec_name": name, "instance_id": key[1]},
                )
            return agent

    def release(self, agent: AgentRuntime) -> None:
        """Mark an agent instance as available for reuse.

        Removes the instance from the pool (actual cleanup on next acquire).
        """
        for key, inst in list(self._pool.items()):
            if inst is agent:
                self._pool.pop(key, None)
                return

    def destroy(self, name: str) -> None:
        """Remove all agent instances of a given name from the pool.

        I-11/ADR-043: disposes each instance (joins wake worker threads).
        """
        destroyed = 0
        for key in list(self._pool.keys()):
            if key[0] == name:
                self._pool[key].dispose()
                self._pool.pop(key, None)
                destroyed += 1
        self._specs.pop(name, None)
        if self._audit_log and destroyed:
            self._audit_log.append(
                level="info", category="agent", event="agent_destroyed",
                detail={"spec_name": name, "count": destroyed},
            )

    def destroy_all(self) -> None:
        """Remove all agent instances from the pool (disposing each)."""
        for agent in list(self._pool.values()):
            agent.dispose()
        self._pool.clear()
        self._specs.clear()

    def get_status(self, instance_id: str) -> str:
        """I-10/ADR-043: status of a pooled instance ('' if unknown)."""
        for (_, iid), agent in self._pool.items():
            if iid == instance_id:
                return agent.status
        return ""

    def stop_instance(self, instance_id: str) -> bool:
        """I-10/ADR-043: explicit stop of a pooled instance.

        I-11: stop() disposes the instance (joins the wake worker).
        """
        for key, agent in list(self._pool.items()):
            if key[1] == instance_id:
                agent.stop()
                self._pool.pop(key, None)
                return True
        return False

    def get_by_instance_id(self, instance_id: str) -> AgentRuntime | None:
        """I-11/ADR-043: return the pooled instance by id, or None."""
        for (_, iid), agent in self._pool.items():
            if iid == instance_id:
                return agent
        return None

    def list_instances(self, name: str) -> list[AgentRuntime]:
        """Return all active instances of a given agent name."""
        return [agent for (pool_name, _), agent in self._pool.items()
                if pool_name == name]

    @property
    def active_count(self) -> int:
        return len(self._pool)

    def list_active(self) -> list[str]:
        return sorted({key[0] for key in self._pool})
