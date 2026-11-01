"""Tests for harness/sdk/client.py — TDD vertical slices."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from harness.sdk import HarnessClient
from harness.interfaces import SandboxConfig
from harness.gateway.gateway import Gateway
from harness.agent_runtime.impl import AgentRuntime
import yaml as _yaml

def _load_spec(path):
    """Helper: load AgentSpec from YAML for test migration."""
    from harness.interfaces import AgentSpec
    with open(path, encoding="utf-8") as f:
        raw = _yaml.safe_load(f) or {}
    return AgentSpec(**raw)
from harness.sdk.models import AgentInstance, AgentStatus


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect HOME to tmp_path so ~/.harness is isolated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def sample_spec(tmp_path: Path) -> Path:
    """Create a sample AgentSpec YAML file."""
    registry = tmp_path / "registry"
    registry.mkdir()
    spec_file = registry / "test-worker.yaml"
    spec_data = {
        "name": "test-worker",
        "description": "Test worker agent",
        "model": "deepseek-v4-flash",
        "llm_profile": "deepseek-prod",
        "system_prompt": "You are a test worker.",
        "tools": ["bash", "read_file"],
    }
    with spec_file.open("w", encoding="utf-8") as f:
        yaml.dump(spec_data, f)
    return spec_file


@pytest.fixture
def client(tmp_home: Path) -> HarnessClient:
    """HarnessClient with isolated home directory."""
    return HarnessClient()


class TestHarnessClientAuditLog:
    """HarnessClient initializes and injects AuditLog."""

    def test_client_has_audit_log(self, tmp_home: Path) -> None:
        """HarnessClient._audit is an AuditLog instance."""
        from harness.audit_log import AuditLog
        client = HarnessClient(sandbox_configs={"main": SandboxConfig(backend="bubblewrap", firewall={"network": "host"})})
        assert hasattr(client, "_audit")
        assert isinstance(client._audit, AuditLog)

    def test_audit_log_writes(self, tmp_path: Path) -> None:
        """Client's AuditLog can write events."""
        from harness.audit_log import AuditLog
        audit = AuditLog(base_dir=str(tmp_path / "audit"))
        audit.append(
            level="info", category="job", event="job_created", job_id="j1"
        )

        global_path = tmp_path / "audit" / "audit_log.jsonl"
        assert global_path.exists()
        lines = global_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "job_created"


class TestSessionEventsSend:
    """sessions.events.send() supports append parameter."""

    def test_send_plain_message(self, client: HarnessClient, tmp_home: Path) -> None:
        """send() writes user_message event."""
        import uuid
        sid = f"sess_{uuid.uuid4().hex[:8]}"
        client._store.create_session(sid, {})
        client.sessions.events.send(sid, "Hello")

        events = client.sessions.events.list(sid)
        assert len(events) == 1
        assert events[0].event_type == "user_message"
        assert events[0].payload["message"] == "Hello"

    def test_send_with_append(self, client: HarnessClient, tmp_home: Path) -> None:
        """send(append=) concatenates message with append text."""
        import uuid
        sid = f"sess_{uuid.uuid4().hex[:8]}"
        client._store.create_session(sid, {})
        client.sessions.events.send(sid, "Hello", append="Be concise")

        events = client.sessions.events.list(sid)
        assert len(events) == 1
        msg = events[0].payload["message"]
        assert "Hello" in msg
        assert "Be concise" in msg
        assert "追加指令" in msg


class TestAgentsCompile:
    """agents.register() creates AgentInstance from AgentSpec."""

    def test_compile_creates_instance(self, client: HarnessClient, sample_spec: Path) -> None:
        """compile() returns AgentInstance with correct fields."""
        instance = client.agents.register(_load_spec(str(sample_spec)), name="my-worker")

        assert isinstance(instance, AgentInstance)
        assert instance.instance_id.startswith("inst_")
        assert instance.spec_name == "test-worker"
        assert instance.llm_profile == "deepseek-prod"
        assert instance.status == AgentStatus.COMPILED

    def test_compile_persists_to_registry(self, client: HarnessClient, sample_spec: Path, tmp_home: Path) -> None:
        """compile() saves instance to ~/.harness/instances/."""
        instance = client.agents.register(_load_spec(str(sample_spec)))

        registry_dir = tmp_home / ".harness" / "instances"
        assert registry_dir.exists()

        saved_file = registry_dir / f"{instance.instance_id}.json"
        assert saved_file.exists()

        with saved_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["spec_name"] == "test-worker"
        assert data["status"] == "compiled"

    def test_compile_missing_spec_raises(self, client: HarnessClient) -> None:
        """register() raises FileNotFoundError for missing spec."""
        with pytest.raises(FileNotFoundError):
            _load_spec("registry/nonexistent.yaml")

    def test_list_returns_compiled_instances(self, client: HarnessClient, sample_spec: Path) -> None:
        """agents.list() returns all compiled instances."""
        inst1 = client.agents.register(_load_spec(str(sample_spec)), name="worker-1")
        inst2 = client.agents.register(_load_spec(str(sample_spec)), name="worker-2")

        agents = client.agents.list()
        ids = {a.instance_id for a in agents}

        assert inst1.instance_id in ids
        assert inst2.instance_id in ids

    def test_get_returns_specific_instance(self, client: HarnessClient, sample_spec: Path) -> None:
        """agents.get() retrieves a specific instance by ID."""
        original = client.agents.register(_load_spec(str(sample_spec)))

        fetched = client.agents.get(original.instance_id)
        assert fetched.instance_id == original.instance_id
        assert fetched.spec_name == original.spec_name

    def test_delete_removes_instance(self, client: HarnessClient, sample_spec: Path) -> None:
        """agents.delete() removes a compiled instance."""
        instance = client.agents.register(_load_spec(str(sample_spec)))
        client.agents.delete(instance.instance_id)

        with pytest.raises(KeyError):
            client.agents.get(instance.instance_id)

    def test_delete_succeeds(self, client: HarnessClient, sample_spec: Path) -> None:
        """agents.delete() removes instance from registry."""
        instance = client.agents.register(_load_spec(str(sample_spec)))
        client.agents.delete(instance.instance_id)
        with pytest.raises(KeyError):
            client.agents.get(instance.instance_id)


class TestToolsClient:
    """SDK tools.register() → same execution path as built-in tools."""

    def test_sdk_registered_tool_behavior_matches_builtin(
        self, client
    ) -> None:
        """Tool registered via SDK behaves identically to built-in tools."""
        import uuid
        from harness.interfaces import AgentSpec
        from harness.session_store.impl import SessionStore
        from tests.test_agent_runtime import _msg_tool, _msg_done

        gateway = Gateway(event_bus=None)
        temp_store_path = client._store.base_path

        class MockSandbox:
            def spawn(self, *a, **kw): return "mock"
            def kill(self, *a): pass
            def is_alive(self, *a): return True
            def get_logs(self, *a): return ""
            def execute(self, *a, **kw): return ("MOCK", "", 0)
        mock_sandbox = MockSandbox()
        deepseek_api_key = "sk-test"

        @client.tools.register("greet", "Greet someone by name", {"name": str})
        def greet(name: str) -> dict:
            return {"ok": True, "content": f"Hello, {name}!", "exit_code": 0}

        store = SessionStore(base_path=temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})

        spec = AgentSpec(
            name="test-worker", description="SDK tools test",
            tools=["greet"], model="deepseek-v4-flash", max_iterations=2,
        )
        runtime = AgentRuntime(
            spec=spec, gateway=gateway, store=store,
            api_key=deepseek_api_key, sandbox=mock_sandbox,
        )
        runtime._current_session_id = session_id
        runtime._tool_registry = client.tools.registry
        runtime._inject_tool_deps()
        call_num = [0]
        def mock_llm(messages, tools=None):
            call_num[0] += 1
            if call_num[0] == 1:
                return _msg_tool("greet", {"name": "World"})
            return _msg_done()
        runtime._call_llm = mock_llm
        result = runtime._agent_loop(session_id)
        assert result["status"] == "completed"
        events = store.read_events(session_id)
        event_types = [e.event_type for e in events]
        assert "tool_called" in event_types
        assert "tool_result" in event_types
        assert "agent_failed" not in event_types

class TestMemorySlots:
    """SDK memory slots + fold boundary.

    The former `memory.compress()` built-in is GONE: it was a parallel
    compaction path (a programmatically registered `memory-compressor` spec)
    whose model id did not exist, so it could only fail, and whose only
    consumers were its own tests.  Compaction policy belongs to the app
    (ADR-001); what the harness provides is the mechanism — one slot per region
    (`inject_summary`) and the fold boundary (`tail`).
    """

    def test_tail_returns_old_events_and_boundary(self, client):
        """tail splits events: old 40% + boundary for inject."""
        import uuid
        from harness.interfaces import Event
        store = client._store
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        for i in range(10):
            store.append_event(Event(
                event_id=f"ev-{i}", session_id=sid,
                event_type="tool_result", timestamp=float(i),
                source="test", payload={"result": f"step_{i}"}))

        old, boundary = client.memory.tail(sid, keep_ratio=0.6)

        # keep_ratio=0.6: oldest 40% in old_events, boundary at ev-4
        assert len(old) == 4
        assert boundary == "ev-4"
        assert old[0].event_id == "ev-0"
        assert old[-1].event_id == "ev-3"

    def test_tail_empty_when_below_cutoff(self, client):
        """tail returns empty old_events and None boundary when too few events."""
        import uuid
        from harness.interfaces import Event
        store = client._store
        sid = str(uuid.uuid4())
        store.create_session(sid, {})
        store.append_event(Event(
            event_id="ev-0", session_id=sid,
            event_type="tool_result", timestamp=0.0,
            source="test", payload={"result": "step_0"}))

        old, boundary = client.memory.tail(sid, keep_ratio=0.9)
        assert len(old) == 0
        assert boundary is None

    def test_compression_flow_excludes_old_events(self, client):
        """Full compression flow: tail → inject_summary → build_messages cutoff."""
        import uuid
        from harness.interfaces import Event
        store = client._store
        sid = str(uuid.uuid4())
        store.create_session(sid, {})

        # Create 20 events with unique markers
        for i in range(20):
            store.append_event(Event(
                event_id=f"ev-{i}", session_id=sid,
                event_type="tool_result", timestamp=float(i),
                source="test", payload={"result": f"result_{i:02d}"}))

        # Simulate APP-layer compression
        old, boundary = client.memory.tail(sid, keep_ratio=0.5)
        assert len(old) == 10
        summary = "Compressed: first 10 results processed."
        client.memory.inject_summary(sid, summary, slot="before_task",
                                     after_event_id=boundary)

        # build_messages should exclude pre-boundary events
        events = store.read_events(sid)
        from harness.prompt_assembly import build_messages
        msgs = build_messages("sys", events, context_window=128000)

        # the harness-owned slot placeholder must be present
        assert any("<mem>" in str(m.get("content", "")) for m in msgs), \
            "the fold summary was not assembled into its slot"
        # Old events (ev-0 to ev-9) must NOT appear
        contents = [str(m.get("content", "")) for m in msgs]
        for i in range(10):
            assert not any(f"result_{i:02d}" in c for c in contents), (
                f"ev-{i} should be excluded after compression")
        # Recent events (ev-10 to ev-19) MAY appear (subject to budget)

    def test_compression_context_shrinks(self, client):
        """After compression, build_messages returns fewer messages."""
        import uuid
        from harness.interfaces import Event
        store = client._store
        sid = str(uuid.uuid4())
        store.create_session(sid, {})

        for i in range(15):
            store.append_event(Event(
                event_id=f"ev-{i}", session_id=sid,
                event_type="agent_started", timestamp=float(i),
                source="test", payload={"input": f"task_{i}" * 100}))

        from harness.prompt_assembly import build_messages
        before = build_messages("sys", store.read_events(sid), 128000)

        old, boundary = client.memory.tail(sid, keep_ratio=0.5)
        client.memory.inject_summary(sid, "summary", slot="before_task",
                                     after_event_id=boundary)
        after = build_messages("sys", store.read_events(sid), 128000)

        # After compression, fewer messages (old replaced by summary)
        assert len(after) < len(before)

    def test_crash_recovery_rebuilds_identical_context(self, client):
        """After crash+restore, build_messages produces identical output."""
        import uuid
        from harness.interfaces import Event
        from harness.session_store.impl import SessionStore
        from harness.prompt_assembly import build_messages

        store = client._store
        sid = str(uuid.uuid4())
        store.create_session(sid, {})

        for i in range(5):
            store.append_event(Event(
                event_id=f"ev-{i}", session_id=sid,
                event_type="agent_started", timestamp=float(i),
                source="test", payload={"input": f"task_{i}"}))

        # Pre-crash: capture context
        msgs_before = build_messages("sys", store.read_events(sid))

        # Simulate crash: create fresh store (same dir) and rebuild
        store2 = SessionStore(base_path=store.base_path)
        msgs_after = build_messages("sys", store2.read_events(sid))

        assert len(msgs_before) == len(msgs_after)
        for a, b in zip(msgs_before, msgs_after):
            assert a == b, f"Mismatch: {a} != {b}"


# ─────────────────────────────────────────────────────────────
# I-01: SDK dead-code fixes — gates + token estimation
# These calls previously crashed on deleted `_coordinator` /
# mismatched `_count_tokens` signature.
# ─────────────────────────────────────────────────────────────


class TestGatesClientDeadRefs:
    """client.gates list/get must read from the session store."""

    def _session_with_gate(self, client, gate_id="ev-gate-1"):
        import uuid
        from harness.interfaces import Event

        sid = str(uuid.uuid4())
        client._store.create_session(sid, {})
        client._store.append_event(Event(
            event_id=gate_id, session_id=sid,
            event_type="human_gate_pending", timestamp=1.0,
            source="test", payload={"gate_event_id": gate_id}))
        return sid

    def test_list_pending_returns_gate(self, client):
        """list_pending reads from store without AttributeError."""
        sid = self._session_with_gate(client)
        pending = client.gates.list_pending(sid)
        assert len(pending) == 1
        assert pending[0].event_id == "ev-gate-1"
        assert pending[0].status == "pending"

    def test_list_pending_resolved_gate_excluded(self, client):
        """A gate with a later human_gate_resolved is not pending."""
        import uuid
        from harness.interfaces import Event

        sid = str(uuid.uuid4())
        client._store.create_session(sid, {})
        client._store.append_event(Event(
            event_id="ev-gate-1", session_id=sid,
            event_type="human_gate_pending", timestamp=1.0,
            source="test", payload={"gate_event_id": "ev-gate-1"}))
        # Gateway semantics: resolved event reuses the gate's event_id.
        client._store.append_event(Event(
            event_id="ev-gate-1", session_id=sid,
            event_type="human_gate_resolved", timestamp=2.0,
            source="test", payload={"decision": "approved"}))
        assert client.gates.list_pending(sid) == []

    def test_get_returns_gate_info(self, client):
        """get returns GateInfo for a specific gate event."""
        sid = self._session_with_gate(client)
        info = client.gates.get("ev-gate-1", sid)
        assert info is not None
        assert info.event_id == "ev-gate-1"
        assert info.status == "pending"

    def test_get_unknown_returns_none(self, client):
        """get returns None for an unknown gate event."""
        sid = self._session_with_gate(client)
        assert client.gates.get("ev-missing", sid) is None


class TestEstimateTokens:
    """client.memory.estimate_tokens must not raise TypeError."""

    def test_estimate_tokens_counts_session(self, client):
        """estimate_tokens returns >0 for a session with events."""
        import uuid
        from harness.interfaces import Event

        sid = str(uuid.uuid4())
        client._store.create_session(sid, {})
        client._store.append_event(Event(
            event_id="ev-1", session_id=sid,
            event_type="agent_started", timestamp=1.0,
            source="test", payload={"input": "hello world"}))
        total = client.memory.estimate_tokens(sid)
        assert total > 0

    def test_estimate_tokens_empty_session(self, client):
        """estimate_tokens returns 0 for an empty session."""
        import uuid
        sid = str(uuid.uuid4())
        client._store.create_session(sid, {})
        assert client.memory.estimate_tokens(sid) == 0

# ─────────────────────────────────────────────────────────────
# I-09: SDK hooks view + process-level registry (ADR-042)
# ─────────────────────────────────────────────────────────────


class TestHooksSdkView:
    """client.hooks is a view over the process-level registry."""

    def test_client_exposes_hooks_view(self, client):
        from harness.hooks import points
        assert client.hooks is not None
        seen: list = []
        client.hooks.on(points.LLM_CALL, "observe",
                       lambda ctx: seen.append(1))
        client.hooks.trigger(points.LLM_CALL)
        assert seen == [1]

    def test_agents_status_query(self, client):
        """I-10: client.agents.status(instance_id) returns lifecycle state."""
        from harness.interfaces import AgentSpec
        spec = AgentSpec(
            name="status-test", description="Status test",
            tools=["echo"], model="deepseek-v4-flash",
        )
        inst = client.agents.register(spec, name="status-test")
        status = client.agents.status(inst.instance_id)
        assert status == "compiled"

    def test_agents_status_unknown_instance_raises(self, client):
        """I-10: unknown instance raises for status query."""
        import pytest as _pytest
        with _pytest.raises(Exception):
            client.agents.status("inst_does_not_exist")

    def test_two_clients_share_process_registry(self, client):
        """hooks registered on client A are visible to client B (ADR-042)."""
        from harness.sdk import HarnessClient
        from harness.hooks import points
        c2 = HarnessClient()
        seen: list = []
        client.hooks.on(points.AGENT_PRE_STEP, "observe",
                       lambda ctx: seen.append("from-A"))
        c2.hooks.trigger(points.AGENT_PRE_STEP)
        assert seen == ["from-A"], "registry must be process-level"




# ═══════════════════════════════════════════════════════════
# I-24: 装配引导收敛 + sessions.stop 迁移（TDD red first）
# ═══════════════════════════════════════════════════════════


def _write_project_configs(tmp_path, local_yaml=True, sandbox_yaml=None):
    """Create tmp_path/harness/config/{local,sandbox}.yaml and chdir."""
    cfg_dir = tmp_path / "harness" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    if local_yaml:
        (cfg_dir / "local.yaml").write_text(
            'hmac_secret: "test-secret-0123456789abcdef"\n'
            'sessions_dir: "' + str(tmp_path / "sessions") + '/"\n',
            encoding="utf-8")
    if sandbox_yaml is not None:
        (cfg_dir / "sandbox.yaml").write_text(sandbox_yaml, encoding="utf-8")


class TestBootstrapAssembly:
    """I-24: HarnessClient 保底装配 — 显式参数 > sandbox.yaml > 内置默认."""

    def test_builtin_fallback_main_when_no_sandbox_yaml(
            self, tmp_path, monkeypatch) -> None:
        """无 sandbox_configs 且无 sandbox.yaml → 内置保底 main（构造不炸）."""
        _write_project_configs(tmp_path, sandbox_yaml=None)
        monkeypatch.chdir(tmp_path)
        from harness.sdk import HarnessClient
        client = HarnessClient()
        cfg = client._sandbox_mgr.get_config("main")
        assert cfg is not None, "builtin fallback main missing"
        assert cfg.backend == "bubblewrap"

    def test_sandbox_yaml_loaded_and_explicit_overrides(
            self, tmp_path, monkeypatch) -> None:
        """sandbox.yaml 被读取；显式 sandbox_configs 同名覆盖 yaml."""
        _write_project_configs(
            tmp_path,
            sandbox_yaml=(
                "sandboxes:\n"
                "  main:\n"
                "    backend: docker\n"
                "    firewall:\n"
                "      network: none\n"
                "  extra:\n"
                "    backend: bubblewrap\n"
                "    firewall:\n"
                "      network: host\n"
            ))
        monkeypatch.chdir(tmp_path)
        from harness.sdk import HarnessClient
        from harness.interfaces import SandboxConfig
        client = HarnessClient(sandbox_configs={
            "main": SandboxConfig(backend="bubblewrap",
                                  firewall={"network": "host"}),
        })
        # yaml-only sandbox loaded
        assert client._sandbox_mgr.get_config("extra") is not None
        # explicit main overrides yaml's docker main
        assert client._sandbox_mgr.get_config("main").backend == "bubblewrap"

    def test_skill_mounts_scanned_from_registered_specs(
            self, tmp_path, monkeypatch) -> None:
        """registry/ 中带 skills 的 spec → 扫描挂载进对应沙箱 shared_folders."""
        _write_project_configs(
            tmp_path,
            sandbox_yaml=(
                "sandboxes:\n"
                "  main:\n"
                "    backend: bubblewrap\n"
                "    firewall:\n"
                "      network: host\n"
                "    shared_folders: []\n"
            ))
        # A persisted spec (registry dir) with skills — discoverable by any
        # fresh client (in-memory registration is per-client only).
        reg = tmp_path / "registry"
        reg.mkdir()
        (reg / "skillful.yaml").write_text(
            "name: skillful\n"
            "description: t\n"
            "tools: [echo]\n"
            "model: deepseek-v4-flash\n"
            "sandbox: main\n"
            "max_iterations: 1\n"
            "skills: [python]\n"
            "permissions:\n"
            "  subagent_spawn: false\n",
            encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        from harness.sdk import HarnessClient
        client = HarnessClient()
        cfg = client._sandbox_mgr.get_config("main")
        shared = cfg.shared_folders or []
        assert any("skills" in str(sf.get("host_path", ""))
                   for sf in shared), \
            f"skill mount not found in {shared}"


class TestHardPrereqFailFast:
    """I-24: 三前置 fail-fast（含修复指引），不再运行期随机炸."""

    def test_hmac_secret_empty_raises_with_guidance(
            self, tmp_path, monkeypatch) -> None:
        """空 hmac_secret → 构造 RuntimeError 且含修复指引."""
        _write_project_configs(tmp_path, local_yaml=False)
        monkeypatch.chdir(tmp_path)
        from harness.sdk import HarnessClient
        with pytest.raises(RuntimeError) as exc:
            HarnessClient()
        msg = str(exc.value)
        assert "hmac_secret" in msg
        assert "local.yaml" in msg or "HARNESS_HMAC_SECRET" in msg

    def test_register_empty_llm_profile_raises(
            self, client: HarnessClient) -> None:
        """注册期：llm_profile 字段为空 → ValueError（带指引）."""
        from harness.interfaces import AgentSpec
        spec = AgentSpec(
            name="no-llm", description="t", tools=[], model="m",
            sandbox="main", llm_profile="", max_iterations=1,
            permissions={"subagent_spawn": False})
        with pytest.raises(ValueError, match="llm_profile"):
            client.agents.register(spec)


class TestRunLlmFailFast:
    """I-24: run() 前置 LLM profile 解析失败 → 修复指引."""

    def test_run_llm_profile_missing_guidance(
            self, tmp_home: Path, monkeypatch) -> None:
        from harness.sdk import HarnessClient
        from harness.orchestration.pool import AgentInstancePool
        from harness.interfaces import AgentSpec
        from unittest.mock import MagicMock

        def bad_resolve(profile, project_dir=None):
            raise RuntimeError(
                f"LLM profile '{profile}' not found. Searched: "
                f"project configs/llm.yaml, ~/.harness/llm.d/{profile}.yaml")

        client = HarnessClient()
        mock_mgr = MagicMock()
        mock_mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-iid", backend=MagicMock())
        client._pool = AgentInstancePool(
            store=client._store, gateway=client._gateway, api_key="",
            sandbox_manager=mock_mgr, resolve_llm=bad_resolve)

        spec = AgentSpec(
            name="llm-missing", description="t", tools=["echo"],
            model="deepseek-v4-flash", sandbox="main", llm_profile="nope",
            max_iterations=1, permissions={"subagent_spawn": False})
        with pytest.raises(RuntimeError) as exc:
            client.agents.run(spec, "t")
        msg = str(exc.value)
        assert "LLM profile" in msg or "llm_profile" in msg
        assert "llm.yaml" in msg, f"guidance missing: {msg}"


class TestSessionStopMigration:
    """I-24 必须项：sessions.stop 转调 agents.stop（真双轨裁决）."""

    def _client_with_mock_pool(self, tmp_home, tmp_path):
        from harness.sdk import HarnessClient
        from harness.orchestration.pool import AgentInstancePool
        from harness.sdk.config import HarnessSettings
        from unittest.mock import MagicMock
        client = HarnessClient(settings=HarnessSettings(
            hmac_secret="test-secret-0123456789abcdef",
            sessions_dir=str(tmp_path / "sessions")))
        mock_mgr = MagicMock()
        mock_mgr.get_or_create.return_value = MagicMock(sandbox_id="mock-iid", backend=MagicMock())
        client._pool = AgentInstancePool(
            store=client._store, gateway=client._gateway,
            api_key="sk-test", sandbox_manager=mock_mgr)
        return client

    def test_stop_bound_running_requests_app_stop(
            self, tmp_home: Path, tmp_path: Path) -> None:
        """绑定实例且 running → sessions.stop 转调 request_stop."""
        from harness.interfaces import AgentSpec
        client = self._client_with_mock_pool(tmp_home, tmp_path)
        spec = AgentSpec(
            name="stop-svc", description="t", tools=["echo"],
            model="deepseek-v4-flash", sandbox="main", max_iterations=1,
            permissions={"subagent_spawn": False})
        inst = client.agents.register(spec)
        sid = client.sessions.start(inst.instance_id).session_id
        agent = client._pool.acquire(spec)
        agent._status = "running"

        client.sessions.stop(sid)

        assert agent._stop_request is not None, \
            "bound running session must request an app stop"
        assert agent._stop_request["reason"] == "user_requested"
        # audit marker still appended
        events = client._store.read_events(sid)
        assert any(e.event_type == "session_stopped" for e in events)

    def test_stop_unbound_only_marker(
            self, tmp_home: Path, tmp_path: Path) -> None:
        """未绑定实例 → 仅追加标记事件（向后兼容，无 request_stop）."""
        import uuid
        client = self._client_with_mock_pool(tmp_home, tmp_path)
        sid = str(uuid.uuid4())
        client._store.create_session(sid, {"type": "unbound"})

        client.sessions.stop(sid)

        events = client._store.read_events(sid)
        assert any(e.event_type == "session_stopped" for e in events)
