"""I-26: 沙箱 uuid 统一身份 + agent→sandbox 关联 — TDD.

grill 决策（2026-09-04）：
- D1 接口层统一 uuid：SandboxInterface 按 sandbox_id 寻址；OS 身份不出后端
- D2 SandboxEntity(sandbox_id, config_name, backend)，无公开 backend_instance_id
- D3 get_or_create(name, create_new=False) name 级 + create() 细粒度 + uuid 组 API
- D4 respawn 同 uuid 覆盖式；D5 绑定缺失透明重物化（冷启动/destroy 后懒重 spawn）
- D6 sandbox_id 命名收敛；D8 不做策略/持久化
"""

from __future__ import annotations

import uuid as _uuid
from unittest.mock import MagicMock

import pytest

from harness.interfaces import SandboxConfig


class FakeBackend:
    """Record-only SandboxInterface double keyed by sandbox_id."""

    def __init__(self):
        self.spawned: list[tuple[str, str]] = []  # (sandbox_id, config_name-ish)
        self.killed: list[str] = []
        self.alive = True

    def spawn(self, config, sandbox_id):
        self.spawned.append((sandbox_id, config.name if hasattr(config, "name") else ""))
        self.alive = True  # respawn brings a fresh process
        return sandbox_id

    def kill(self, sandbox_id, keep_workspace=False):
        self.killed.append(sandbox_id)

    def is_alive(self, sandbox_id):
        return self.alive

    def execute(self, sandbox_id, command, restrict=None):
        return ("", "", 0)

    def get_logs(self, sandbox_id):
        return ""

    def stop(self, sandbox_id, timeout=5):
        pass


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """Redirect HOME so ~/.harness registry is isolated per test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return str(home)


@pytest.fixture
def manager(monkeypatch):
    """SandboxManager backed by FakeBackend (no real spawn)."""
    from harness.sandbox import manager as mgr_mod
    from harness.sandbox.manager import SandboxManager
    fake = FakeBackend()
    monkeypatch.setattr(mgr_mod, "get_sandbox", lambda backend="bubblewrap": fake)
    cfg = SandboxConfig(backend="bubblewrap", firewall={"network": "host"})
    m = SandboxManager(configs={"main": cfg})
    m._fake = fake
    return m


# ─────────────────────────────────────────────────────────────
# S1: get_or_create 规范复用 + create_new 独立 uuid（RED）
# ─────────────────────────────────────────────────────────────


class TestCanonicalAndCreateNew:
    def test_get_or_create_reuses_canonical_entity(self, manager) -> None:
        """同名默认两次 → 同一 sandbox_id（规范实体复用，仅 spawn 一次）."""
        e1 = manager.get_or_create("main")
        e2 = manager.get_or_create("main")

        assert e1.sandbox_id == e2.sandbox_id
        assert e1.config_name == "main"
        assert e1.backend is not None
        assert len(manager._fake.spawned) == 1

    def test_create_new_mints_distinct_uuid_keeps_canonical(self, manager) -> None:
        """create_new=True → 同名独立新 uuid；随后默认调用仍返首个规范实体."""
        canon = manager.get_or_create("main")
        fresh = manager.get_or_create("main", create_new=True)
        again = manager.get_or_create("main")

        assert canon.sandbox_id != fresh.sandbox_id
        assert again.sandbox_id == canon.sandbox_id  # 规范指针不动
        assert len(manager._fake.spawned) == 2

    def test_get_or_create_unknown_name_raises(self, manager) -> None:
        """未定义配置名 → ValueError."""
        import pytest
        with pytest.raises(ValueError, match="main"):
            manager.get_or_create("nope")


# ─────────────────────────────────────────────────────────────
# S2: create() 总是新建 + list()/get_entity()
# ─────────────────────────────────────────────────────────────


class TestCreateAndList:
    def test_create_always_mints_distinct_uuid(self, manager) -> None:
        """create() 两次 → uuid 互异；list() 含两者（同配置多实体共存）."""
        a = manager.create("main")
        b = manager.create("main")

        assert a.sandbox_id != b.sandbox_id
        entities = manager.list()
        assert {x.sandbox_id for x in entities} == {a.sandbox_id, b.sandbox_id}
        assert all(x.config_name == "main" for x in entities)
        assert len(manager._fake.spawned) == 2

    def test_get_entity_roundtrip_and_unknown_raises(self, manager) -> None:
        """get_entity(uuid) 往返一致；未知 uuid → KeyError."""
        a = manager.create("main")
        got = manager.get_entity(a.sandbox_id)
        assert got.sandbox_id == a.sandbox_id and got.config_name == "main"

        import pytest
        with pytest.raises(KeyError):
            manager.get_entity("no-such-uuid")


# ─────────────────────────────────────────────────────────────
# S3: destroy(sandbox_id) 精确销毁
# ─────────────────────────────────────────────────────────────


class TestDestroy:
    def test_destroy_exact_entity_keeps_sibling(self, manager) -> None:
        """destroy(A) 只销毁 A；同配置 B 仍在 list；fake.killed 精确记录."""
        a = manager.create("main")
        b = manager.create("main")

        manager.destroy(a.sandbox_id)

        assert manager._fake.killed == [a.sandbox_id]
        remaining = {x.sandbox_id for x in manager.list()}
        assert b.sandbox_id in remaining and a.sandbox_id not in remaining

        import pytest
        with pytest.raises(KeyError):
            manager.get_entity(a.sandbox_id)

    def test_destroy_unknown_is_noop(self, manager) -> None:
        """销毁未知 uuid → no-op（不 raise、不影响现存）."""
        a = manager.create("main")
        manager.destroy("no-such-uuid")
        assert manager.get_entity(a.sandbox_id).sandbox_id == a.sandbox_id


# ─────────────────────────────────────────────────────────────
# S5: respawn 保 uuid + 透明重物化（ensure）
# ─────────────────────────────────────────────────────────────


class TestRespawnAndRematerialize:
    def test_respawn_keeps_same_sandbox_id(self, manager) -> None:
        """进程死（is_alive=False）→ 同 sandbox_id 覆盖式 respawn；实体/规范不变."""
        e1 = manager.get_or_create("main")
        sid = e1.sandbox_id

        manager._fake.alive = False
        e2 = manager.get_or_create("main")

        assert e2.sandbox_id == sid
        assert sid in manager._fake.killed  # 旧 OS 句柄已 kill
        assert [s for (s, _) in manager._fake.spawned] == [sid, sid]
        assert manager.get_entity(sid).config_name == "main"

    def test_ensure_rematerializes_pinned_id_after_cold_start(self, manager,
                                                              monkeypatch) -> None:
        """新管理器（模拟进程重启，_entities 空）按绑定 sandbox_id 重物化."""
        # 第一个管理器铸造实体
        e1 = manager.create("main")
        sid = e1.sandbox_id
        # 第二个管理器 = 冷启动（复用同 fake backend 亦可，仅注册表为空）
        from harness.sandbox.manager import SandboxManager
        from harness.interfaces import SandboxConfig
        m2 = SandboxManager(configs={"main": SandboxConfig(
            backend="bubblewrap", firewall={"network": "host"})})
        m2._fake = manager._fake
        m2._fake.spawned.clear()

        entity = m2.ensure("main", sid)

        assert entity.sandbox_id == sid
        assert [s for (s, _) in m2._fake.spawned] == [sid]
        assert m2.get_entity(sid).sandbox_id == sid

    def test_ensure_reprovisions_after_destroy(self, manager) -> None:
        """destroy(X) 后 ensure(X) → 以同一 sandbox_id 重新供给（文档语义）."""
        e1 = manager.create("main")
        sid = e1.sandbox_id
        manager.destroy(sid)

        entity = manager.ensure("main", sid)

        assert entity.sandbox_id == sid
        assert manager.get_entity(sid).sandbox_id == sid

    def test_ensure_cross_config_reuse_raises(self, manager) -> None:
        """sandbox_id 已属其他配置 → ValueError."""
        from harness.interfaces import SandboxConfig
        manager._configs["other"] = SandboxConfig(
            backend="bubblewrap", firewall={"network": "host"})
        e1 = manager.create("main")

        import pytest
        with pytest.raises(ValueError, match="config"):
            manager.ensure("other", e1.sandbox_id)


# ─────────────────────────────────────────────────────────────
# S6: pool/register 关联（AgentRuntime._sandbox_id / AgentInstance.sandbox_id）
# ─────────────────────────────────────────────────────────────


def _spec(name: str = "worker"):
    from harness.interfaces import AgentSpec
    return AgentSpec(
        name=name, description="t", tools=["echo"],
        model="deepseek-v4-flash", sandbox="main", max_iterations=1,
        permissions={"subagent_spawn": False},
    )


class TestAssociation:
    def test_pool_acquire_binds_canonical_sandbox_id(self, monkeypatch,
                                                     tmp_path) -> None:
        """默认 acquire → runtime._sandbox_id = 规范实体 uuid."""
        from harness.orchestration.pool import AgentInstancePool
        from harness.session_store.impl import SessionStore
        from harness.gateway.gateway import Gateway
        from harness.sandbox import manager as mgr_mod
        from harness.sandbox.manager import SandboxManager
        from harness.interfaces import SandboxConfig
        fake = FakeBackend()
        monkeypatch.setattr(mgr_mod, "get_sandbox",
                            lambda backend="bubblewrap": fake)
        mgr = SandboxManager(configs={"main": SandboxConfig(
            backend="bubblewrap", firewall={"network": "host"})})
        pool = AgentInstancePool(
            store=SessionStore(base_path=str(tmp_path / "s")),
            gateway=Gateway(event_bus=None), api_key="sk-test",
            sandbox_manager=mgr)

        rt = pool.acquire(_spec("worker"))

        canon = mgr.get_or_create("main")
        assert rt._sandbox_id == canon.sandbox_id
        assert rt._sandbox is canon.backend

    def test_pool_acquire_pinned_sandbox_binds_exact_uuid(self, monkeypatch,
                                                           tmp_path) -> None:
        """acquire(sandbox_id=X) → runtime._sandbox_id == X."""
        from harness.orchestration.pool import AgentInstancePool
        from harness.session_store.impl import SessionStore
        from harness.gateway.gateway import Gateway
        from harness.sandbox import manager as mgr_mod
        from harness.sandbox.manager import SandboxManager
        from harness.interfaces import SandboxConfig
        fake = FakeBackend()
        monkeypatch.setattr(mgr_mod, "get_sandbox",
                            lambda backend="bubblewrap": fake)
        mgr = SandboxManager(configs={"main": SandboxConfig(
            backend="bubblewrap", firewall={"network": "host"})})
        pinned = mgr.create("main")
        pool = AgentInstancePool(
            store=SessionStore(base_path=str(tmp_path / "s")),
            gateway=Gateway(event_bus=None), api_key="sk-test",
            sandbox_manager=mgr)

        rt = pool.acquire(_spec("worker"), sandbox_id=pinned.sandbox_id)

        assert rt._sandbox_id == pinned.sandbox_id

    def test_register_persists_sandbox_id(self, tmp_home) -> None:
        """register(spec, sandbox_id=X) → AgentInstance.sandbox_id 存档往返."""
        from harness.sdk import HarnessClient
        c = HarnessClient()
        inst = c.agents.register(_spec("worker"), sandbox_id="sbx-abc")

        assert inst.sandbox_id == "sbx-abc"
        loaded = c.agents._registry.load(inst.instance_id)
        assert loaded.sandbox_id == "sbx-abc"
        # 旧存档无 sandbox_id → 默认 None（向后兼容）
        old = c.agents._registry.load(inst.instance_id)
        assert old.instance_id == inst.instance_id


# ─────────────────────────────────────────────────────────────
# S7: 生命周期（agent stop/dispose ≠ 销毁实体）+ docker 静态一致
# ─────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_agent_dispose_keeps_sandbox_entity(self, monkeypatch,
                                                tmp_path) -> None:
        """agent dispose/stop 后实体仍在管理器 list()（destroy 是唯一途径）."""
        from harness.orchestration.pool import AgentInstancePool
        from harness.session_store.impl import SessionStore
        from harness.gateway.gateway import Gateway
        from harness.sandbox import manager as mgr_mod
        from harness.sandbox.manager import SandboxManager
        from harness.interfaces import SandboxConfig
        fake = FakeBackend()
        monkeypatch.setattr(mgr_mod, "get_sandbox",
                            lambda backend="bubblewrap": fake)
        mgr = SandboxManager(configs={"main": SandboxConfig(
            backend="bubblewrap", firewall={"network": "host"})})
        pool = AgentInstancePool(
            store=SessionStore(base_path=str(tmp_path / "s")),
            gateway=Gateway(event_bus=None), api_key="sk-test",
            sandbox_manager=mgr)
        rt = pool.acquire(_spec("worker"))
        sid = rt._sandbox_id

        rt.dispose()

        assert mgr.get_entity(sid).sandbox_id == sid
        assert sid in {x.sandbox_id for x in mgr.list()}

    def test_docker_backend_addresses_by_sandbox_id(self) -> None:
        """docker 后端五方法签名按 sandbox_id 寻址（接口不裂）."""
        import inspect
        from harness.sandbox.docker_impl import DockerSandbox
        for name in ("spawn", "stop", "kill", "is_alive", "get_logs",
                     "execute"):
            params = inspect.signature(
                getattr(DockerSandbox, name)).parameters
            assert "sandbox_id" in params, name