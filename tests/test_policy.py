"""I-27: 策略中间件（gateway/hooks 定向规则 + preset）— TDD.

grill 2026-09-04/05 决策：
- 一切默认关；enable/add 必须显式 agent 定向（或 "*"）
- 生效集 = scope ∈ {agent, "*"} 的已启用条目；多规则=并集+deny-wins
- spec.policy 声明 = register 自动 enable；app 可 add 自定义规则
- 内置 preset：bash_whitelist / fs_paths / read_before_edit / net_policy
"""

from __future__ import annotations

import pytest

from harness.gateway.gateway import GateContext


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """Isolate ~/.harness per test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return str(home)


@pytest.fixture(autouse=True)
def _clean_hooks():
    """Policy hooks are process-global (ADR-042) — isolate per test."""
    from harness.hooks import HookRegistry
    reg = HookRegistry.get()
    reg.clear()
    yield
    reg.clear()


def _ctx(agent: str, tool: str, args: dict | None = None,
         session: str = "s1", config: object | None = None):
    class _Spec:
        name = agent
    return GateContext(gate_type="", session_id=session,
                       spec=_Spec(), tool_name=tool, tool_args=args or {},
                       config=config)


# ─────────────────────────────────────────────────────────────
# S1: PolicyEngine 默认全关 + enable/add 定向（RED）
# ─────────────────────────────────────────────────────────────


class TestEngine:
    def test_default_off_nothing_enabled(self) -> None:
        """未 enable/add → effective 为空（默认全关）."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        assert eng.effective("worker-a") == []
        assert eng.effective("*") == []

    def test_enable_targets_specific_agent(self) -> None:
        """enable(name, agent="a") → 只影响 a，不影响 b."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.define("p1", veto=lambda ctx: None)
        eng.enable("p1", agent="worker-a")
        assert eng.effective("worker-a") == ["p1"]
        assert eng.effective("worker-b") == []

    def test_enable_star_targets_all(self) -> None:
        """agent="*" 全局启用 → 对所有 agent 生效."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.define("p2", veto=lambda ctx: None)
        eng.enable("p2", agent="*")
        assert "p2" in eng.effective("anything")

    def test_enable_unknown_name_raises(self) -> None:
        """enable 未定义名 → ValueError."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        with pytest.raises(ValueError):
            eng.enable("no-such", agent="worker-a")

    def test_add_custom_rule_active_immediately(self) -> None:
        """add(name, veto, agent) 即生效（显式创建=激活）."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.add("custom", veto=lambda ctx: None, agent="worker-a")
        assert eng.effective("worker-a") == ["custom"]

# ─────────────────────────────────────────────────────────────
# S2: scoped hook 端到端（authorize 定向拒绝）
# ─────────────────────────────────────────────────────────────


class TestScopedEnforcement:
    def _gateway(self):
        from harness.gateway.gateway import Gateway
        return Gateway(event_bus=None)

    def test_veto_only_targets_enabled_agent(self) -> None:
        """enable(deny_evil, agent=a) → a 被拒，b 放行."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()

        def deny_evil(ctx, cfg=None):
            return "evil command" if ctx.tool_args.get("cmd") == "evil" else None

        eng.define("deny_evil", veto=deny_evil)
        eng.enable("deny_evil", agent="worker-a")

        gw = self._gateway()
        ctx_a = _ctx("worker-a", "bash", {"cmd": "evil"})
        ctx_b = _ctx("worker-b", "bash", {"cmd": "evil"})
        assert gw.authorize(ctx_a).decision == "denied"
        assert gw.authorize(ctx_b).decision != "denied"

    def test_disable_removes_effect(self) -> None:
        """disable 后同 agent 不再被该规则拦截."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.define("x", veto=lambda ctx, cfg=None: "no")
        eng.enable("x", agent="worker-a")
        gw = self._gateway()
        assert gw.authorize(_ctx("worker-a", "bash", {"cmd": "y"})).decision == "denied"
        eng.disable("x", agent="worker-a")
        assert gw.authorize(_ctx("worker-a", "bash", {"cmd": "y"})).decision != "denied"

# ─────────────────────────────────────────────────────────────
# S3: 内置 preset — bash_whitelist / fs_paths
# ─────────────────────────────────────────────────────────────


class TestPresetsBashFs:
    def _gw(self):
        from harness.gateway.gateway import Gateway
        return Gateway(event_bus=None)

    def test_bash_whitelist_denies_dangerous_cmd(self) -> None:
        """危险通配（rm -rf /）deny；无害命令放行."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.enable("bash_whitelist", agent="worker-a")
        gw = self._gw()

        assert gw.authorize(_ctx("worker-a", "bash",
                                 {"cmd": "rm -rf /"})).decision == "denied"
        assert gw.authorize(_ctx("worker-a", "bash",
                                 {"cmd": "echo hi"})).decision != "denied"

    def test_fs_paths_denies_system_dirs(self) -> None:
        """系统目录 deny；工作区放行."""
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.enable("fs_paths", agent="worker-a")
        gw = self._gw()

        assert gw.authorize(_ctx("worker-a", "read_file",
                                 {"path": "/etc/passwd"})).decision == "denied"
        assert gw.authorize(_ctx("worker-a", "read_file",
                                 {"path": "/workspace/x.py"})).decision != "denied"

# ─────────────────────────────────────────────────────────────
# S4: read_before_edit（DSH 版本化，hash 纯参数门 + literal 适配器）
# ─────────────────────────────────────────────────────────────


class TestReadBeforeEdit:
    def _gw(self):
        from harness.gateway.gateway import Gateway
        return Gateway(event_bus=None)

    def _exec_tool(self, agent, tool, args, result_out):
        """模拟工具执行完成 → 触发 engine 的 TOOL_EXECUTE observe."""
        from harness.hooks import HookMode, HookRegistry, points
        reg = HookRegistry.get()
        reg.trigger(points.TOOL_EXECUTE,
                    {"tool_name": tool, "tool_args": args,
                     "tool_result": {"output": result_out},
                     "session_id": "s1", "ok": True},
                    agent=agent)

    def test_hashedit_without_read_denied(self) -> None:
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.enable("read_before_edit", agent="worker-a")
        gw = self._gw()
        res = gw.authorize(_ctx("worker-a", "hashedit",
                                {"file_path": "/ws/a.py", "old_hash": "H1"},
                                session="s1"))
        assert res.decision == "denied"

    def test_hashread_then_hashedit_gate(self) -> None:
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.enable("read_before_edit", agent="worker-a")
        gw = self._gw()

        # hashread 完成 → stamp observed digest "H1"
        self._exec_tool("worker-a", "hashread", {"file_path": "/ws/a.py"},
                        "H1")

        ok = gw.authorize(_ctx("worker-a", "hashedit",
                               {"file_path": "/ws/a.py", "old_hash": "H1"},
                               session="s1"))
        stale = gw.authorize(_ctx("worker-a", "hashedit",
                                  {"file_path": "/ws/a.py",
                                   "old_hash": "H2"}, session="s1"))
        assert ok.decision != "denied"
        assert stale.decision == "denied"

    def test_literal_edit_uses_digest_provider(self) -> None:
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng.define("rbe", veto=None, observe_exec=None,
                   cfg={})
        # 用带 digest_provider 的 preset：先 enable 再重配 cfg（provider 注入）
        files = {"/ws/a.py": "D1"}
        eng._defs["read_before_edit"]["cfg"]["digest_provider"] =             files.__getitem__
        eng.enable("read_before_edit", agent="worker-a")
        gw = self._gw()
        self._exec_tool("worker-a", "read", {"file_path": "/ws/a.py"}, "")

        ok = gw.authorize(_ctx("worker-a", "edit",
                               {"file_path": "/ws/a.py"}, session="s1"))
        assert ok.decision != "denied"
        files["/ws/a.py"] = "D2"  # 外部改动
        stale = gw.authorize(_ctx("worker-a", "edit",
                                  {"file_path": "/ws/a.py"}, session="s1"))
        assert stale.decision == "denied"

# ─────────────────────────────────────────────────────────────
# S5: net_policy（目标 IP/CIDR/域名 deny）
# ─────────────────────────────────────────────────────────────


class TestNetPolicy:
    def _gw(self):
        from harness.gateway.gateway import Gateway
        return Gateway(event_bus=None)

    def test_denies_configured_targets(self) -> None:
        from harness.policy import PolicyEngine
        eng = PolicyEngine()
        eng._defs["net_policy"]["cfg"]["deny"] = ["192.168.0.1",
                                                  "example.com",
                                                  "10.0.0.0/8"]
        eng.enable("net_policy", agent="worker-a")
        gw = self._gw()

        assert gw.authorize(_ctx("worker-a", "bash", {
            "cmd": "curl http://192.168.0.1/admin"})).decision == "denied"
        assert gw.authorize(_ctx("worker-a", "bash", {
            "cmd": "curl http://gw.example.com/x"})).decision == "denied"
        assert gw.authorize(_ctx("worker-a", "bash", {
            "cmd": "curl http://10.1.2.3/x"})).decision == "denied"
        assert gw.authorize(_ctx("worker-a", "bash", {
            "cmd": "curl http://public.example.org/x"})).decision != "denied"

# ─────────────────────────────────────────────────────────────
# S6: spec.policy 声明 + client.policy（SDK）
# ─────────────────────────────────────────────────────────────


class TestSdkPolicySurface:
    def test_register_declares_policy(self, tmp_home) -> None:
        """spec.policy="bash_whitelist" → register 后 engine.effective 含该名."""
        from harness.sdk import HarnessClient
        from harness.interfaces import AgentSpec
        from harness.policy import engine
        client = HarnessClient()
        spec = AgentSpec(
            name="pol-worker", description="t", tools=["bash"],
            model="deepseek-v4-flash", sandbox="main", max_iterations=1,
            permissions={"subagent_spawn": False},
            policy="bash_whitelist")
        client.agents.register(spec)
        assert "bash_whitelist" in engine.effective("pol-worker")

    def test_client_policy_enable_and_list(self, tmp_home) -> None:
        """client.policy.enable/list/disable 代理全局引擎."""
        from harness.sdk import HarnessClient
        from harness.policy import engine
        client = HarnessClient()
        client.policy.enable("fs_paths", agent="w1")
        assert "fs_paths" in engine.effective("w1")
        entries = client.policy.list()
        assert any(e["name"] == "fs_paths" and "w1" in e["agents"]
                   for e in entries)
        client.policy.disable("fs_paths", agent="w1")
        assert "fs_paths" not in engine.effective("w1")

    def test_client_policy_add_custom(self, tmp_home) -> None:
        """client.policy.add 自定义规则 → 定向生效."""
        from harness.sdk import HarnessClient
        from harness.policy import engine
        from harness.gateway.gateway import Gateway
        client = HarnessClient()

        def block_grep(ctx, cfg=None):
            return "no grep" if ctx.tool_name == "bash" and "grep" in (ctx.tool_args or {}).get("cmd", "") else None

        client.policy.add("no_grep", agent="w2", veto=block_grep)
        assert "no_grep" in engine.effective("w2")
        gw = Gateway(event_bus=None)
        assert gw.authorize(_ctx("w2", "bash", {"cmd": "grep x /etc/passwd"})).decision == "denied"
        assert gw.authorize(_ctx("w2", "bash", {"cmd": "ls"})).decision != "denied"




