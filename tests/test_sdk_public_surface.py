"""ADR-015 (2026-10-19) — SDK public surface required by the app-layer pi CLI.

These tests lock the surface the `pi` package (ADR-002) relies on:

1. construction/registration classes are exported from `harness.sdk`;
2. `HarnessClient(project_dir=...)` actually drives profile resolution;
3. `HarnessClient(tool_registry=...)` wires app-registered tools into runtimes
   (the pre-existing bug: runtimes always loaded builtins), with per-runtime
   copies so dependency injection stays isolated;
4. `client.memory.event_cost/span_cost` measure what the assembler sends;
5. `client.hooks.points` / `HookMode` are reachable without importing internals;
6. `client.policy.enable(cfg=...)` configures a builtin preset and
   `client.policy.guard(...)` exposes the compiled restriction.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import harness.sdk as sdk
from harness.gateway.gateway import GateContext
from harness.hooks import HookRegistry
from harness.interfaces import Event
from harness.sdk import (
    AgentSpec,
    HarnessClient,
    HarnessSettings,
    HookMode,
    SandboxConfig,
    ToolRegistry,
    points,
)


def _settings(tmp_path) -> HarnessSettings:
    return HarnessSettings(
        hmac_secret="test-secret",
        sessions_dir=str(tmp_path / "sessions"),
        audit_log_dir=str(tmp_path / "sessions"),
        deepseek_api_key="",
    )


@pytest.fixture(autouse=True)
def _clean_hooks():
    """The hook registry is process-global (ADR-042); isolate each test."""
    reg = HookRegistry.get()
    reg.clear()
    yield
    reg.clear()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated project dir with a cloud LLM profile named `default`."""
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "llm.yaml").write_text(
        "default:\n"
        "  provider: deepseek\n"
        "  api_key: sk-test\n"
        "  base_url: http://127.0.0.1:9/v1\n"
        "  model: fake-model-from-project\n"
        "  backend: cloud\n"
        "  endpoint: ''\n"
        "  context_window: 3200\n"
        "  max_tokens: 256\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _client(project, tool_registry=None) -> HarnessClient:
    return HarnessClient(
        settings=_settings(project),
        project_dir=str(project),
        tool_registry=tool_registry,
    )


def _stub_sandbox(client: HarnessClient) -> None:
    """Avoid spawning bubblewrap in a pure wiring test."""
    client._sandbox_mgr.get_or_create = lambda name: SimpleNamespace(
        backend=SimpleNamespace(execute=lambda *a, **k: ("", "", 0)),
        sandbox_id="sb_test",
    )


class TestExports:
    def test_app_layer_construction_surface_is_public(self) -> None:
        for name in ("AgentSpec", "SandboxConfig", "ToolRegistry",
                     "HookMode", "points"):
            assert name in sdk.__all__, f"{name} missing from harness.sdk.__all__"
        assert sdk.AgentSpec is AgentSpec
        assert sdk.SandboxConfig is SandboxConfig
        assert sdk.ToolRegistry is ToolRegistry


class TestProjectDir:
    def test_project_dir_drives_profile_resolution(self, project) -> None:
        client = _client(project)
        # The repo cwd also has a `default` profile with a different model; the
        # explicit project_dir must win.
        assert client.config.profile("default").model == "fake-model-from-project"
        assert "default" in client.config.profiles()

    def test_memory_count_tokens_uses_project_provider(self, project) -> None:
        client = _client(project)
        # deepseek tokenizer treats CJK correctly (the 77a64b4 fix); 0 would
        # mean the fallback path regressed.
        assert client.memory.count_tokens("厦门朝日俱乐部") > 0


class TestToolRegistryWiring:
    def test_app_tools_reach_the_runtime(self, project) -> None:
        registry = ToolRegistry()

        @registry.register("pi_read", "Read a file", {"path": str},
                           deps=["execute"])
        def pi_read(path, execute=None):
            return execute(f"cat {path}")[0]

        client = _client(project, tool_registry=registry)
        _stub_sandbox(client)
        assert client.tools.list() == ["pi_read"]

        spec = AgentSpec(name="pi", description="d", tools=["pi_read"],
                         llm_profile="default", sandbox="main")
        inst = client.agents.register(spec)
        agent = client._pool.acquire(spec, instance_id=inst.instance_id)

        names = [d["function"]["name"] for d in agent._spec_tools()]
        assert names == ["pi_read"]
        assert agent._tool_registry is not client.tools.registry
        # the copy stays unbound (deps are injected per runtime, not now)
        assert (agent._tool_registry._tools["pi_read"]["fn"]
                is agent._tool_registry._tools["pi_read"]["_original"])

    def test_default_registry_keeps_builtins(self, project) -> None:
        client = _client(project)
        assert "bash" in client.tools.list()
        spec = AgentSpec(name="pi", description="d", tools=["bash"],
                         llm_profile="default", sandbox="main")
        inst = client.agents.register(spec)
        _stub_sandbox(client)
        agent = client._pool.acquire(spec, instance_id=inst.instance_id)
        assert [d["function"]["name"] for d in agent._spec_tools()] == ["bash"]


class TestMemoryCost:
    def test_event_and_span_cost_are_consistent(self, project) -> None:
        client = _client(project)
        ev1 = Event(event_id="e1", session_id="s", event_type="agent_thought",
                    timestamp=0.0, source="x", parent_event_id=None,
                    payload={"content": "hello world"})
        ev2 = Event(event_id="e2", session_id="s", event_type="tool_result",
                    timestamp=0.0, source="x", parent_event_id=None,
                    payload={"result": "some tool output"})
        total = client.memory.span_cost([ev1, ev2])
        assert total == (client.memory.event_cost(ev1)
                         + client.memory.event_cost(ev2))
        assert total > 0


class TestHooksSurface:
    def test_points_and_mode_without_internal_import(self, project) -> None:
        client = _client(project)
        assert client.hooks.points.AGENT_PRE_STEP == "agent.pre_step"
        seen: list[object] = []

        def obs(ctx):
            seen.append(ctx)

        client.hooks.on(client.hooks.points.AGENT_PRE_STEP,
                        HookMode.OBSERVE, obs)
        try:
            HookRegistry.get().trigger(points.AGENT_PRE_STEP, {"x": 1},
                                       agent="pi-test")
            assert seen, "hook registered through client.hooks never fired"
        finally:
            HookRegistry.get().clear()


class TestPolicySurface:
    def test_enable_cfg_configures_read_before_edit(self, project) -> None:
        client = _client(project)
        state = {"digest": "H1"}
        spec = SimpleNamespace(name="pi-policy-test")
        cfg = {
            "read_literal": {"read": "path"},
            "write_literal": {"edit": ("path", None)},
            "digest_provider": lambda p: state["digest"],
        }
        # The policy engine is a process singleton; restore the preset cfg after
        # this test so other suites see the default.
        from harness.policy import engine as _policy_engine
        original_cfg = dict(_policy_engine._defs["read_before_edit"]["cfg"])
        try:
            client.policy.enable("read_before_edit", agent=spec.name, cfg=cfg)
            gateway = client._gateway
            edit_ctx = lambda: GateContext(  # noqa: E731
                gate_type="", session_id="s1", spec=spec,
                tool_name="edit", tool_args={"path": "/workspace/a.py"})

            # 1) edit before read → denied
            assert gateway.authorize(edit_ctx()).decision == "denied"

            # 2) read (observed digest H1) → edit passes when the file is unchanged
            HookRegistry.get().trigger(
                points.TOOL_EXECUTE,
                {"tool_name": "read", "tool_args": {"path": "/workspace/a.py"},
                 "tool_result": {"output": "content"}, "session_id": "s1",
                 "ok": True},
                agent=spec.name,
            )
            assert gateway.authorize(edit_ctx()).decision == "approved"

            # 3) file changes underneath → stale edit denied
            state["digest"] = "H2"
            assert gateway.authorize(edit_ctx()).decision == "denied"
        finally:
            client.policy.disable("read_before_edit", agent=spec.name)
            _policy_engine._defs["read_before_edit"]["cfg"] = original_cfg

    def test_guard_query_returns_compiled_restriction(self, project) -> None:
        client = _client(project)
        agent = "pi-guard-test"
        client.policy.add("pi_guard_test", agent=agent,
                          guard={"paths": {"rw": ["/workspace", "/dev"]}},
                          description="test guard")
        try:
            restriction = client.policy.guard(agent)
            assert restriction is not None
            assert "/workspace" in restriction.paths["rw"]
            assert "/dev" in restriction.paths["rw"]
        finally:
            client.policy.disable("pi_guard_test", agent=agent)


# ── tokenizer relative-path fallback (I-60) ────────────────────────────

class TestTokenizerPathFallback:
    """A relative `tokenizer_dir` is a deployment artifact, not a cwd one.

    The pi CLI runs in a user workspace, so the fallback must find the checkout's
    tokenizer_dir (or HARNESS_TOKENIZER_DIR) rather than failing on cwd.
    """

    def _artifacts(self):
        from pathlib import Path
        return Path(__file__).resolve().parents[1] / "tokenizer_dir" / "deepseek"

    def test_env_override_wins(self, monkeypatch, tmp_path):
        from pathlib import Path
        import harness.prompt_assembly as pa

        art = self._artifacts()
        if not art.is_dir():
            pytest.skip("no tokenizer artifacts")
        tokroot = tmp_path / "tok"
        tokroot.mkdir()
        (tokroot / "deepseek").symlink_to(art, target_is_directory=True)
        monkeypatch.setenv("HARNESS_TOKENIZER_DIR", str(tokroot))
        monkeypatch.chdir(tmp_path)
        pa._TOKENIZERS.pop("deepseek", None)
        tk = pa._get_tokenizer("deepseek", "tokenizer_dir")
        assert len(tk.encode("厦门朝日俱乐部")) > 0

    def test_checkout_root_is_the_fallback(self, monkeypatch, tmp_path):
        import harness.prompt_assembly as pa

        if not self._artifacts().is_dir():
            pytest.skip("no tokenizer artifacts")
        monkeypatch.delenv("HARNESS_TOKENIZER_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        pa._TOKENIZERS.pop("deepseek", None)
        tk = pa._get_tokenizer("deepseek", "tokenizer_dir")
        assert len(tk.encode("厦门")) > 0


# ── profiles robustness (I-60 R1) ──────────────────────────────────────

class TestProfilesRobustness:
    def test_broken_project_yaml_does_not_raise(self, project):
        """A malformed configs/llm.yaml must not break client.config.profiles().

        OH I-56 R2 deliberately catches yaml.YAMLError; the profiles surface is
        read-only discovery and must degrade, not throw.
        """
        (project / "configs" / "llm.yaml").write_text(
            "broken: [unterminated", encoding="utf-8")
        c = _client(project)
        assert c.config.profiles() == []
