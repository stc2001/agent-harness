"""Model Service Pool (I-14) — model endpoints registry + health probing.

I-14 / grill 13 decisions:
- Q1-B: keyed by model_name; one logical service per model with an in-process
  set of loaded adapters (request-level switching stays in the engine).
- Q2 C+thin-B: no process spawning — external engines self-register; liveness
  = GET /health (fallback /v1/models), mirroring ADR-040 §2 readiness probes
  ("probe availability, not process liveness").
- Q3/Q13: single-endpoint MVP, multi-endpoint failover candidates; all-dead →
  clear error; NO acquire-level downgrade (fallback stays in I-13 _call_llm).
- Q4-C: models.yaml baseline (loaded at client construction) + dynamic
  client.models.register (runtime override, memory-only).
- Q6-A: adapter warmup is best-effort (loaded_adapters + audit; failure does
  not block routing — final adapter success/failure is the engine's contract).
- Q7-A: acquire-time probe is the routing guarantee; low-frequency background
  polling feeds observability; explicit unregister; no TTL eviction (process
  lifecycle belongs to external orchestration).
- Q10: short lock guards the registry; probing happens OUTSIDE the lock;
  concurrent acquires share one endpoint (idempotent).
- Q11: structured validation; same-name+url re-register keeps loaded_adapters;
  same-name+different-url adds a failover candidate.
- Q12: baseline services start in state=unknown (engine may still be booting).
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


@dataclass
class ModelService:
    """One registered model-service endpoint (engine process)."""

    name: str
    url: str
    health_path: str = "/health"
    model: str = ""
    load_adapter_path: str = ""          # optional warmup endpoint
    preloaded_adapters: list[str] = field(default_factory=list)
    state: str = "unknown"               # unknown | healthy | unhealthy
    loaded_adapters: set[str] = field(default_factory=set)
    last_checked: float = 0.0


@dataclass
class ModelServiceStatus:
    """Read-only SDK view of a model service."""

    name: str
    urls: list[str]
    state: str
    loaded_adapters: list[str]
    last_checked: float


def validate_model_service(name: str, url: str) -> None:
    """Q11: structured validation — url must be an absolute http(s) URL."""
    if not name or not name.strip():
        raise ValueError("model_service name must be non-empty")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"model service url must be http(s)://host:port, got {url!r}")


class ModelServicePool:
    """Model endpoints registry + health probing (no process spawning).

    ``services`` maps model_name → list[ModelService] (failover candidates).
    ``http_get``/``http_post`` are injectable for tests (default: httpx).
    ``poll_interval > 0`` starts a daemon health-polling thread (Q7).
    """

    def __init__(
        self,
        services: dict[str, list[ModelService]] | None = None,
        audit_log=None,
        http_get: Callable | None = None,
        http_post: Callable | None = None,
        poll_interval: float = 30.0,
        probe_timeout: float = 2.0,
    ) -> None:
        self._services: dict[str, list[ModelService]] = dict(services or {})
        self._audit_log = audit_log
        self._http_get = http_get or httpx.get
        self._http_post = http_post or httpx.post
        self._probe_timeout = probe_timeout
        self._lock = threading.Lock()
        self._disposed = False
        self._poll_interval = poll_interval
        if poll_interval > 0:
            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True,
                name="model-pool-health")
            self._poll_thread.start()
        else:
            self._poll_thread = None

    # ── registry (Q4/Q11/Q12) ───────────────────────────────

    def register(self, name: str, url: str, *,
                 health_path: str = "/health", model: str = "",
                 load_adapter_path: str = "",
                 preloaded_adapters: list[str] | None = None) -> None:
        """Register/replace an endpoint. Same name+url → update (keep loaded
        adapters); same name+different url → add a failover candidate."""
        validate_model_service(name, url)
        with self._lock:
            existing = self._services.get(name, [])
            for svc in existing:
                if svc.url == url:
                    svc.health_path = health_path or svc.health_path
                    svc.model = model or svc.model
                    svc.load_adapter_path = load_adapter_path or \
                        svc.load_adapter_path
                    for a in (preloaded_adapters or []):
                        svc.loaded_adapters.add(a)  # keep existing set
                    self._audit("info", "model_service_registered",
                                {"model_service": name, "url": url,
                                 "action": "updated"})
                    return
            svc = ModelService(
                name=name, url=url, health_path=health_path, model=model,
                load_adapter_path=load_adapter_path,
                preloaded_adapters=list(preloaded_adapters or []))
            for a in (preloaded_adapters or []):
                svc.loaded_adapters.add(a)
            existing.append(svc)
            self._services[name] = existing
            self._audit("info", "model_service_registered",
                        {"model_service": name, "url": url, "action": "added"})

    def unregister(self, name: str) -> None:
        with self._lock:
            self._services.pop(name, None)
        self._audit("info", "model_service_unregistered",
                    {"model_service": name})

    def status(self, name: str) -> ModelServiceStatus | None:
        with self._lock:
            svcs = self._services.get(name)
            if not svcs:
                return None
            return ModelServiceStatus(
                name=name,
                urls=[s.url for s in svcs],
                state=svcs[0].state,
                loaded_adapters=sorted(set().union(
                    *(s.loaded_adapters for s in svcs))),
                last_checked=max(s.last_checked for s in svcs),
            )

    def list(self) -> list[ModelServiceStatus]:
        with self._lock:
            return [self.status(n) for n in self._services]  # type: ignore[list-item]

    def _current_url(self, name: str) -> str | None:
        """First registered url for a model (state-agnostic — Q9 reuse check)."""
        with self._lock:
            svcs = self._services.get(name)
            return svcs[0].url if svcs else None

    # ── probing (Q2/Q7/Q10) ─────────────────────────────────

    def _probe(self, svc: ModelService) -> bool:
        """Readiness probe: GET /health (fallback /v1/models)."""
        base = svc.url.rstrip("/")
        try:
            resp = self._http_get(base + svc.health_path,
                                  timeout=self._probe_timeout)
            if resp.status_code == 200:
                return True
            resp2 = self._http_get(base + "/v1/models",
                                   timeout=self._probe_timeout)
            return resp2.status_code == 200
        except Exception:
            return False

    def _poll_loop(self) -> None:
        while not self._disposed:
            time.sleep(self._poll_interval)
            with self._lock:
                all_svcs = [s for lst in self._services.values() for s in lst]
            for svc in all_svcs:
                ok = self._probe(svc)
                with self._lock:
                    svc.state = "healthy" if ok else "unhealthy"
                    svc.last_checked = time.time()
                if not ok:
                    self._audit("warn", "model_service_unhealthy",
                                {"model_service": svc.name, "url": svc.url})

    # ── acquire (Q3/Q6/Q10/Q13) ─────────────────────────────

    def _acquire(self, name: str, adapter: str = "") -> tuple[str, set[str]]:
        """Resolve a healthy endpoint for a model; warm up the adapter
        (best-effort). Probing happens OUTSIDE the lock (Q10)."""
        with self._lock:
            candidates = list(self._services.get(name, []))
        if not candidates:
            raise RuntimeError(
                f"model service '{name}' not registered — add it to "
                "harness/config/models.yaml or client.models.register(...)")

        failures: list[str] = []
        for svc in candidates:
            ok = self._probe(svc)
            with self._lock:
                svc.state = "healthy" if ok else "unhealthy"
                svc.last_checked = time.time()
            if ok:
                self._ensure_adapter(svc, adapter)
                with self._lock:
                    return svc.url, set(svc.loaded_adapters)
            failures.append(f"{svc.url} (unhealthy)")
            self._audit("warn", "model_service_unhealthy",
                        {"model_service": name, "url": svc.url})

        raise RuntimeError(
            f"model service '{name}' unavailable: "
            + ", ".join(failures)
            + " — check the engine process or re-register a healthy endpoint; "
              "or switch the spec to a cloud profile (I-13 fallback)")

    def _ensure_adapter(self, svc: ModelService, adapter: str) -> None:
        """Q6-A: best-effort warmup — record loaded set + audit; failure does
        not block routing (final adapter success is the engine's contract)."""
        if not adapter or adapter in svc.loaded_adapters:
            return
        if not svc.load_adapter_path:
            return  # no warmup endpoint — request-level passthrough only
        ok = False
        reason = ""
        try:
            resp = self._http_post(
                svc.url.rstrip("/") + svc.load_adapter_path,
                json={"adapter": adapter}, timeout=10.0)
            ok = resp.status_code < 400
            if not ok:
                reason = f"http {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 — best-effort contract
            reason = str(exc)[:300]
        with self._lock:
            if ok:
                svc.loaded_adapters.add(adapter)
        self._audit(
            "info" if ok else "warn", "model_adapter_load",
            {"model_service": svc.name, "url": svc.url, "adapter": adapter,
             "success": ok, "reason": reason or None})

    # ── teardown ────────────────────────────────────────────

    def dispose(self) -> None:
        self._disposed = True
        if self._poll_thread is not None and \
                self._poll_thread is not threading.current_thread():
            self._poll_thread.join(timeout=2.0)

    def _audit(self, level: str, event: str, detail: dict[str, Any]) -> None:
        if self._audit_log is not None:
            try:
                self._audit_log.append(
                    level=level, category="model", event=event,
                    detail=detail)
            except Exception:
                pass


__all__ = ["ModelService", "ModelServiceStatus", "ModelServicePool",
           "validate_model_service"]
