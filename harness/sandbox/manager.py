"""Module 4 — SandboxManager: sandbox entity (uuid) lifecycle.

I-26 (2026-09-04): namespace-level decoupling — sandbox identity is a
manager-minted ``sandbox_id`` (uuid); config names are only a template.
A config may have many entities (distinct uuids). OS handles (bwrap PID /
docker container id) never leave the backend; SandboxInterface addresses
sandboxes by sandbox_id.

File access control is enforced by AgentRuntime, not at the sandbox level.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from harness.interfaces import (
    SandboxConfig,
    SandboxInterface,
    SandboxSpawnError,
)
from harness.sandbox.backends import get_sandbox


@dataclass
class SandboxEntity:
    """A sandbox instance identity: uuid + its config template + backend.

    I-26/D2: the ONLY sandbox identity visible above the manager is
    ``sandbox_id`` (uuid).  The backend object holds OS handles internally.
    """

    sandbox_id: str
    config_name: str
    backend: SandboxInterface
    config_fingerprint: str = ""
    created_at: float = field(default_factory=lambda: __import__("time").time())


class SandboxManager:
    """Manages sandbox entities (lazy creation, uuid lifecycle).

    - one config template -> many entities (distinct sandbox_id)
    - canonical entity per config name (``_default_by_config``) keeps the
      legacy ``get_or_create(name)`` behaviour stable
    - respawn keeps the same sandbox_id (entity identity stable);
      a pinned sandbox_id missing from this process is re-materialised
      from its config on demand
    """

    def __init__(self, configs: dict[str, SandboxConfig] | None = None,
                 audit_log=None) -> None:
        self._configs: dict[str, SandboxConfig] = dict(configs or {})
        self._entities: dict[str, SandboxEntity] = {}
        self._default_by_config: dict[str, str] = {}
        self._lock = threading.Lock()
        self._audit_log = audit_log

    # ─────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────

    def get_or_create(self, name: str, create_new: bool = False) -> SandboxEntity:
        """Name-level coarse API: canonical entity per config name.

        Default (``create_new=False``) reuses the canonical entity — exact
        pre-I-26 behaviour.  ``create_new=True`` mints a NEW entity with a
        distinct sandbox_id even though the name is identical (canonical
        pointer is NOT moved).
        """
        config = self._configs.get(name)
        if config is None:
            available = list(self._configs.keys())
            raise ValueError(
                f"Sandbox {name!r} is not defined. Available: {available}")

        with self._lock:
            if not create_new:
                sid = self._default_by_config.get(name)
                entity = self._entities.get(sid) if sid else None
                if entity is not None and self._ensure_alive(entity, config):
                    return entity

            entity = self._materialize(name, config, str(uuid.uuid4()))
            if not create_new and name not in self._default_by_config:
                self._default_by_config[name] = entity.sandbox_id
            return entity

    def ensure(self, name: str, sandbox_id: str) -> SandboxEntity:
        """Resolve a pinned sandbox_id under a config template.

        I-26/D5: returns the live entity when present (same config), else
        re-materialises it — spawns under the SAME sandbox_id from this
        config.  Makes a pinned binding survive process restarts and an
        explicit destroy (destroy = tear down now; next reference provisions
        again).  Cross-config reuse of a sandbox_id raises ValueError.
        """
        config = self._configs.get(name)
        if config is None:
            raise ValueError(f"Sandbox {name!r} is not defined.")
        with self._lock:
            entity = self._entities.get(sandbox_id)
            if entity is not None:
                if entity.config_name != name:
                    raise ValueError(
                        f"sandbox_id {sandbox_id} belongs to config "
                        f"{entity.config_name!r}, not {name!r}")
                if not self._ensure_alive(entity, config):
                    # respawn failed and entity dropped — fall through to mint
                    entity = None
            if entity is None:
                entity = self._materialize(name, config, sandbox_id)
            return entity

    def create(self, name: str) -> SandboxEntity:
        """Fine-grained API: always mint a NEW entity (distinct uuid).

        Raises if the config is unknown or the spawn/probe fails.
        """
        config = self._configs.get(name)
        if config is None:
            raise ValueError(f"Sandbox {name!r} is not defined.")
        with self._lock:
            return self._materialize(name, config, str(uuid.uuid4()))

    def get_entity(self, sandbox_id: str) -> SandboxEntity:
        """Return the entity for a sandbox_id, or raise KeyError."""
        with self._lock:
            entity = self._entities.get(sandbox_id)
            if entity is None:
                raise KeyError(f"No sandbox entity with sandbox_id={sandbox_id}")
            return entity

    def destroy(self, sandbox_id: str) -> None:
        """Kill and remove the entity identified by sandbox_id.

        Legacy config-name lookup is honoured via the canonical pointer.
        No-op when the id does not exist.
        """
        with self._lock:
            sid = sandbox_id
            if sid not in self._entities and sid in self._default_by_config:
                sid = self._default_by_config[sandbox_id]
            entity = self._entities.pop(sid, None)
            if entity is None:
                return
            for name, d in list(self._default_by_config.items()):
                if d == sid:
                    del self._default_by_config[name]
            entity.backend.kill(entity.sandbox_id)
            if self._audit_log is not None:
                self._audit_log.append(
                    level="info", category="sandbox", event="sandbox_destroyed",
                    detail={"config_name": entity.config_name,
                            "sandbox_id": entity.sandbox_id},
                )

    def destroy_all(self) -> None:
        """Kill and remove all active sandbox entities."""
        for sandbox_id in list(self._entities.keys()):
            self.destroy(sandbox_id)

    def list(self) -> list[SandboxEntity]:
        """Return all live entities (uuid first-class)."""
        with self._lock:
            return list(self._entities.values())

    def reap_orphans(self) -> int:
        """Reap leftover anchor trees from hosts that died without cleanup."""
        import harness.sandbox.impl as impl
        return impl.reap_orphan_anchors()

    def get_config(self, name: str) -> SandboxConfig | None:
        """Return the SandboxConfig template for a named sandbox."""
        return self._configs.get(name)

    def set_ttl(self, name: str, ttl_seconds: int, idle_timeout: int = 0) -> None:
        """Update TTL config for a sandbox template (future spawns)."""
        config = self._configs.get(name)
        if config is None:
            raise ValueError(f"Sandbox {name!r} not defined")
        config.ttl_seconds = ttl_seconds
        config.idle_timeout = idle_timeout

    # ─────────────────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────────────────

    def _ensure_alive(self, entity: SandboxEntity,
                      config: SandboxConfig) -> bool:
        """Reuse the entity when alive + config unchanged; else respawn
        under the SAME sandbox_id (I-26/D4).  Returns False when the
        caller must mint a fresh entity instead."""
        fingerprint = str(config)
        if (entity.backend.is_alive(entity.sandbox_id)
                and fingerprint == entity.config_fingerprint):
            if self._audit_log is not None:
                self._audit_log.append(
                    level="info", category="sandbox",
                    event="sandbox_reused",
                    detail={"config_name": entity.config_name,
                            "sandbox_id": entity.sandbox_id},
                )
            return True

        # Dead or config changed — destroy the OS handle, respawn under the
        # same sandbox_id (entity identity survives; backend re-registers).
        reason = ("config_changed"
                  if fingerprint != entity.config_fingerprint
                  else "process_not_alive")
        keep_ws = reason == "process_not_alive"
        try:
            entity.backend.kill(entity.sandbox_id, keep_workspace=keep_ws)
        except Exception:
            pass
        if self._audit_log is not None:
            self._audit_log.append(
                level="warn", category="sandbox", event="sandbox_respawned",
                detail={"config_name": entity.config_name,
                        "sandbox_id": entity.sandbox_id, "reason": reason},
            )
        try:
            entity.backend.spawn(config, entity.sandbox_id)
        except Exception:
            # respawn failed — drop the entity so the caller mints fresh
            self._entities.pop(entity.sandbox_id, None)
            for nm, d in list(self._default_by_config.items()):
                if d == entity.sandbox_id:
                    del self._default_by_config[nm]
            return False
        entity.config_fingerprint = fingerprint
        return True

    def _materialize(self, name: str, config: SandboxConfig,
                     sandbox_id: str) -> SandboxEntity:
        """Spawn + readiness-probe a new entity under sandbox_id."""
        backend = get_sandbox(config.backend)
        try:
            backend.spawn(config, sandbox_id)
        except Exception as exc:
            raise SandboxSpawnError(
                f"Sandbox {name!r} spawn failed: {exc}") from exc
        try:
            _out, err, rc = backend.execute(sandbox_id, "echo ready")
        except Exception as exc:
            raise SandboxSpawnError(
                f"Sandbox {name!r} readiness probe failed: {exc}") from exc
        if rc != 0:
            try:
                backend.kill(sandbox_id, keep_workspace=False)
            except Exception:
                pass
            raise SandboxSpawnError(
                f"Sandbox {name!r} readiness probe failed: {err.strip()}")
        entity = SandboxEntity(
            sandbox_id=sandbox_id,
            config_name=name,
            backend=backend,
            config_fingerprint=str(config),
        )
        self._entities[sandbox_id] = entity
        if self._audit_log is not None:
            self._audit_log.append(
                level="info", category="sandbox", event="sandbox_created",
                detail={"config_name": name, "sandbox_id": sandbox_id,
                        "backend": config.backend},
            )
        return entity