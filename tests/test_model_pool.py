"""I-14: 模型服务池（双池化）— TDD.

决策（grill 13 项闭合）：
- Q1-B: 池键 = model_name；每 model 一逻辑服务 + 进程内多 adapter 加载集
- Q2 C+薄B: 不 spawn；探活 = /health（回退 /v1/models），镜像 ADR-040 §2 探针
- Q3: 单端点 MVP + 多端点候选预留；unhealthy → 清晰报错；回退靠 I-13
- Q4-C: models.yaml 基线 + client.models 动态注册
- Q5-A: LLMConfig.model_service 池键；acquire 路由一次
- Q6-A: adapter 惰性预热尽力而为，失败不阻断（成败归引擎）
- Q7-A: acquire 探活硬保证 + 低频轮询观测 + unregister；不做 TTL 驱逐
- Q9: acquire 复用路径端点更新（审计 model_endpoint_updated）
- Q10: 短锁 + 锁外探活 + 并发共享端点
- Q11: 结构化校验；同名同 url 覆盖保留 loaded_adapters；同名字不同 url = 候选
- Q12: yaml 基线初始 unknown；动态仅内存
- Q13: 多候选故障转移 + 全败清晰报错；无 acquire 层降级
"""

from __future__ import annotations

import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.orchestration.model_pool import (
    ModelService, ModelServicePool, validate_model_service,
)
from harness.orchestration.pool import AgentInstancePool
from harness.sdk.config import LLMConfig
from harness.session_store.impl import SessionStore


class FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _pool(services=None, audit_log=None, http_get=None, http_post=None,
          poll_interval: float = 0.0):
    return ModelServicePool(
        services=services or {}, audit_log=audit_log,
        http_get=http_get, http_post=http_post, poll_interval=poll_interval)


def _mock_sandbox_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-iid", backend=MagicMock())
    return mgr


def _svc(name="rwkv-6", url="http://127.0.0.1:8000/v1",
         health_path="/health", model="RWKV-6-7B",
         load_adapter_path="") -> ModelService:
    return ModelService(name=name, url=url, health_path=health_path,
                        model=model, load_adapter_path=load_adapter_path)


# ─────────────────────────────────────────────────────────────
# Q11/Q12: 校验 / yaml 基线 / 动态注册
# ─────────────────────────────────────────────────────────────


class TestRegistration:
    def test_validate_rejects_bad_url(self):
        with pytest.raises(ValueError, match="url"):
            validate_model_service("x", "not-a-url")

    def test_register_initial_state_unknown(self):
        pool = _pool()
        pool.register("rwkv-6", "http://127.0.0.1:8000/v1")
        assert pool.status("rwkv-6").state == "unknown"

    def test_register_same_url_keeps_loaded_adapters(self):
        pool = _pool()
        pool.register("m", "http://a/v1", preloaded_adapters=["l1"])
        pool._services["m"][0].loaded_adapters.add("l1")
        pool.register("m", "http://a/v1", preloaded_adapters=["l2"])
        svc = pool._services["m"][0]
        assert "l1" in svc.loaded_adapters, "re-register must keep loaded set"
        assert "l2" in svc.loaded_adapters

    def test_register_same_name_different_url_adds_candidate(self):
        pool = _pool()
        pool.register("m", "http://a/v1")
        pool.register("m", "http://b/v1")
        assert len(pool._services["m"]) == 2, "second url = failover candidate"

    def test_unregister_removes_all_endpoints(self):
        pool = _pool()
        pool.register("m", "http://a/v1")
        pool.unregister("m")
        assert pool.status("m") is None


# ─────────────────────────────────────────────────────────────
# Q2/Q7: 探活
# ─────────────────────────────────────────────────────────────


class TestProbe:
    def test_health_ok_marks_healthy(self):
        pool = _pool(http_get=lambda url, timeout: FakeResp(200))
        pool.register("m", "http://a/v1")
        assert pool._probe(pool._services["m"][0]) is True

    def test_health_404_falls_back_to_models(self):
        calls: list[str] = []

        def fake_get(url, timeout):
            calls.append(url)
            return FakeResp(404 if url.endswith("/health") else 200)

        pool = _pool(http_get=fake_get)
        svc = _svc()
        assert pool._probe(svc) is True
        assert any("/v1/models" in u for u in calls)

    def test_probe_exception_false(self):
        pool = _pool(http_get=lambda url, timeout: (_ for _ in ()).throw(
            RuntimeError("conn refused")))
        assert pool._probe(_svc()) is False


# ─────────────────────────────────────────────────────────────
# Q3/Q13/Q10: acquire
# ─────────────────────────────────────────────────────────────


class TestAcquire:
    def test_acquire_healthy_returns_url(self):
        pool = _pool(http_get=lambda url, timeout: FakeResp(200))
        pool.register("m", "http://a/v1")
        url, _ = pool._acquire("m")
        assert url == "http://a/v1"
        assert pool.status("m").state == "healthy"

    def test_acquire_missing_service_raises(self):
        pool = _pool()
        with pytest.raises(RuntimeError, match="not registered"):
            pool._acquire("nope")

    def test_acquire_unhealthy_single_endpoint_raises_guidance(self):
        pool = _pool(http_get=lambda url, timeout: FakeResp(503))
        pool.register("m", "http://a/v1")
        with pytest.raises(RuntimeError) as exc:
            pool._acquire("m")
        msg = str(exc.value)
        assert "unavailable" in msg and "http://a/v1" in msg

    def test_acquire_failover_to_second_candidate(self):
        def fake_get(url, timeout):
            return FakeResp(200 if "b" in url else 503)

        pool = _pool(http_get=fake_get)
        pool.register("m", "http://a/v1")
        pool.register("m", "http://b/v1")
        url, _ = pool._acquire("m")
        assert url == "http://b/v1", "dead first candidate must fail over"

    def test_concurrent_acquire_shared_endpoint(self):
        pool = _pool(http_get=lambda url, timeout: FakeResp(200))
        pool.register("m", "http://a/v1")
        results: list = []
        lock = threading.Lock()

        def worker():
            url, _ = pool._acquire("m")
            with lock:
                results.append(url)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert results == ["http://a/v1"] * 8, "concurrent acquire must share endpoint"


# ─────────────────────────────────────────────────────────────
# Q6: adapter 预热（尽力而为）
# ─────────────────────────────────────────────────────────────


class TestAdapterWarmup:
    def test_adapter_loaded_and_recorded(self):
        pool = _pool(
            http_get=lambda url, timeout: FakeResp(200),
            http_post=lambda url, json, timeout: FakeResp(200))
        pool.register("m", "http://a/v1", load_adapter_path="/load")
        _, loaded = pool._acquire("m", adapter="lora-math")
        assert "lora-math" in loaded
        assert "lora-math" in pool._services["m"][0].loaded_adapters

    def test_adapter_load_failure_does_not_block(self):
        audit = MagicMock()
        pool = _pool(
            http_get=lambda url, timeout: FakeResp(200),
            http_post=lambda url, json, timeout: (_ for _ in ()).throw(
                RuntimeError("load failed")),
            audit_log=audit)
        pool.register("m", "http://a/v1", load_adapter_path="/load")
        url, loaded = pool._acquire("m", adapter="lora-x")
        assert url == "http://a/v1", "load failure must not block routing"
        assert "lora-x" not in loaded
        events = [c.kwargs.get("event") for c in audit.append.call_args_list]
        assert "model_adapter_load" in events

    def test_no_load_endpoint_skips_warmup(self):
        pool = _pool(http_get=lambda url, timeout: FakeResp(200))
        pool.register("m", "http://a/v1")  # no load_adapter_path
        url, loaded = pool._acquire("m", adapter="lora-x")
        assert url == "http://a/v1"
        assert "lora-x" not in loaded  # request-level passthrough only


# ─────────────────────────────────────────────────────────────
# 集成：agent 池路由（Q5/Q9）
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def gateway() -> Gateway:
    return Gateway(event_bus=None)


@pytest.fixture
def store(temp_store_path: str) -> SessionStore:
    return SessionStore(base_path=temp_store_path)


def _spec(name="local-agent", **kw) -> AgentSpec:
    base = dict(name=name, description="t", tools=["echo"],
                model="rwkv-6", sandbox="main", max_iterations=1,
                permissions={"subagent_spawn": False})
    base.update(kw)
    return AgentSpec(**base)


class TestPoolRouting:
    def test_local_backend_routes_via_model_pool(self, store, gateway):
        """local backend → acquire 用池端点覆盖 base_url."""
        model_pool = _pool(http_get=lambda url, timeout: FakeResp(200))
        model_pool.register("rwkv-6", "http://127.0.0.1:8000/v1")

        def resolve(profile, project_dir=None):
            return LLMConfig(backend="local", api_key="k",
                             base_url="http://stale/v1", model_service="rwkv-6")

        pool = AgentInstancePool(store=store, gateway=gateway, api_key="",
                                 sandbox_manager=_mock_sandbox_manager(),
                                 resolve_llm=resolve, model_pool=model_pool)
        agent = pool.acquire(_spec())
        assert agent.base_url == "http://127.0.0.1:8000/v1", \
            f"base_url {agent.base_url}"

    def test_local_backend_no_pool_entry_raises(self, store, gateway):
        model_pool = _pool(http_get=lambda url, timeout: FakeResp(503))
        # register nothing — service absent

        def resolve(profile, project_dir=None):
            return LLMConfig(backend="local", api_key="k",
                             base_url="http://stale/v1", model_service="ghost")

        pool = AgentInstancePool(store=store, gateway=gateway, api_key="",
                                 sandbox_manager=_mock_sandbox_manager(),
                                 resolve_llm=resolve, model_pool=model_pool)
        with pytest.raises(RuntimeError, match="not registered|unavailable"):
            pool.acquire(_spec())

    def test_reuse_updates_endpoint_on_reregister(self, store, gateway):
        """Q9: 引擎换端口重启 → 旧端点死 → failover 新端点 → 复用实例更新 + audit."""
        audit = MagicMock()
        state = {"old_alive": True}

        def fake_get(url, timeout):
            if "old:8000" in url:
                return FakeResp(200 if state["old_alive"] else 503)
            return FakeResp(200)

        model_pool = _pool(http_get=fake_get, audit_log=audit)
        model_pool.register("rwkv-6", "http://old:8000/v1")

        def resolve(profile, project_dir=None):
            return LLMConfig(backend="local", api_key="k",
                             base_url="http://old:8000/v1",
                             model_service="rwkv-6")

        pool = AgentInstancePool(store=store, gateway=gateway, api_key="",
                                 sandbox_manager=_mock_sandbox_manager(),
                                 resolve_llm=resolve, model_pool=model_pool,
                                 audit_log=audit)
        spec = _spec()
        spec.idle_timeout = 300
        agent = pool.acquire(spec)
        assert agent.base_url == "http://old:8000/v1"

        # engine restarts on a new port: old endpoint dies, new one registers
        state["old_alive"] = False
        model_pool.register("rwkv-6", "http://new:9000/v1")
        agent2 = pool.acquire(spec)  # reuse
        assert agent2 is agent
        assert agent2.base_url == "http://new:9000/v1", \
            "reuse must point the agent at the new endpoint"
        events = [c.kwargs.get("event") for c in audit.append.call_args_list]
        assert "model_endpoint_updated" in events
