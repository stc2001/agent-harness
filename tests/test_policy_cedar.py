"""I-35 S1: Cedar static decision core (permit/forbid/NoDecision) — TDD."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.policy import PolicyEngine


@pytest.fixture(autouse=True)
def _clean():
    eng = PolicyEngine()
    # tests use a fresh engine to avoid cross-test state
    import harness.policy as P
    saved = P.engine
    yield eng
    P.engine = saved


def _fresh():
    import harness.policy as P
    P.engine = PolicyEngine()
    return P.engine


BASH_POLICY = '''
@id("allow-ls")
permit(
  principal == Agent::"t",
  action == Action::"bash_exec",
  resource is Cmd
) when { resource.text like "ls*" };
@id("forbid-rm")
forbid(
  principal == Agent::"t",
  action == Action::"bash_exec",
  resource is Cmd
) when { resource.text like "rm*" };
'''


def test_allow_permit():
    eng = _fresh()
    eng.define("bash", cedar=BASH_POLICY, kind="cmd")
    eng.enable("bash", agent="t")
    decision, reason, matched = eng.check_static(
        "t", "bash_exec", {"command": "ls /tmp"})
    assert decision == "allow" and matched is True


def test_forbid_deny():
    eng = _fresh()
    eng.define("bash", cedar=BASH_POLICY, kind="cmd")
    eng.enable("bash", agent="t")
    decision, reason, matched = eng.check_static(
        "t", "bash_exec", {"command": "rm -rf /tmp"})
    assert decision == "deny" and reason


def test_nodedecision_not_matched():
    eng = _fresh()
    eng.define("bash", cedar=BASH_POLICY, kind="cmd")
    eng.enable("bash", agent="t")
    decision, reason, matched = eng.check_static(
        "t", "bash_exec", {"command": "echo hi"})
    assert decision is None and matched is False


def test_scope_only_enabled_agents():
    eng = _fresh()
    eng.define("bash", cedar=BASH_POLICY, kind="cmd")
    eng.enable("bash", agent="other")
    decision, _, matched = eng.check_static(
        "t", "bash_exec", {"command": "rm -rf /"})
    assert matched is False  # 't' has no enabled rule


def test_file_kind_uses_path():
    eng = _fresh()
    eng.define("fs", cedar='''
@id("no-etc")
forbid(
  principal == Agent::"t",
  action == Action::"read_file",
  resource is File
) when { resource.path like "/etc*" };
''', kind="file")
    eng.enable("fs", agent="t")
    d, r, m = eng.check_static("t", "read_file", {"path": "/etc/hostname"})
    assert d == "deny" and m is True



# ─────────────────────────────────────────────────────────────
# I-35 S2: definition unit channels (engine.add / client.policy.add)
# ─────────────────────────────────────────────────────────────


def test_engine_add_cedar_only_rule():
    eng = _fresh()
    eng.add("bash", agent="t", cedar=BASH_POLICY, kind="cmd",
            description="bash ls allowed / rm forbidden",
            reason="rm 命令被策略禁止")
    decision, reason, matched = eng.check_static(
        "t", "bash_exec", {"command": "rm -rf /"})
    assert decision == "deny"
    assert "rm 命令被策略禁止" in reason


def test_engine_add_requires_something():
    eng = _fresh()
    with pytest.raises(ValueError):
        eng.add("empty", agent="t")


def test_client_policy_add_cedar():
    from harness.sdk.client import _PolicyClient
    eng = _fresh()
    # stash engine so client add targets our fresh engine
    import harness.sdk.client as sc
    saved = sc._policy_engine
    try:
        sc._policy_engine = eng
        pc = _PolicyClient(client=None)
        pc.add("bash", agent="t", cedar=BASH_POLICY, kind="cmd",
               guard={"paths": {"rw": ["/workspace/sub"]},
                      "syscalls_deny": ["socket"]})
        d, reason, m = eng.check_static(
            "t", "bash_exec", {"command": "rm -rf /"})
        assert d == "deny"
        assert "t" in eng._enabled  # auto-enabled
        assert eng._defs["bash"]["guard"]["paths"]["rw"] == ["/workspace/sub"]
    finally:
        sc._policy_engine = saved




# ─────────────────────────────────────────────────────────────
# I-35 S3: guard data -> SandboxRestriction compilation
# ─────────────────────────────────────────────────────────────


def test_compile_guard_merges_rules():
    from harness.interfaces import SandboxRestriction, SandboxRestrictionError
    eng = _fresh()
    eng.add("g1", agent="t", guard={"paths": {"rw": ["/workspace/sub"]},
                                    "syscalls_deny": ["socket"]})
    eng.add("g2", agent="t", guard={"paths": {"ro": ["/workspace"]},
                                    "syscalls_deny": ["ptrace"]})
    r = eng.compile_guard("t")
    assert isinstance(r, SandboxRestriction)
    assert r.paths["rw"] == ["/workspace/sub"]
    assert r.paths["ro"] == ["/workspace"]
    assert set(r.syscalls_deny) == {"socket", "ptrace"}


def test_compile_guard_rw_wins_conflict():
    eng = _fresh()
    eng.add("a", agent="t", guard={"paths": {"rw": ["/ws"]}})
    eng.add("b", agent="t", guard={"paths": {"ro": ["/ws"]}})
    r = eng.compile_guard("t")
    assert "/ws" in r.paths["rw"] and "/ws" not in r.paths["ro"]


def test_compile_guard_empty_is_none():
    eng = _fresh()
    assert eng.compile_guard("nobody") is None


def test_compile_guard_invalid_raises():
    from harness.interfaces import SandboxRestrictionError
    eng = _fresh()
    eng.add("bad", agent="t", guard={"paths": {"rw": ["relative"]}})
    with pytest.raises(SandboxRestrictionError):
        eng.compile_guard("t")




# ─────────────────────────────────────────────────────────────
# I-35 S4: static decision wired into authorize chain
# ─────────────────────────────────────────────────────────────


def _gateway_ctx(agent_spec, tool, args):
    from harness.gateway.gateway import GateContext
    return GateContext(gate_type="", session_id="s1",
                       spec=agent_spec, tool_name=tool, tool_args=args)


@pytest.fixture
def _agent_t():
    from harness.interfaces import AgentSpec
    return AgentSpec(name="t", description="x", tools=["bash"],
                     model="deepseek-v4-flash", sandbox="main",
                     max_iterations=3, llm_profile="default",
                     permissions={"subagent_spawn": False})


def test_cedar_deny_through_authorize(_agent_t):
    from harness.gateway.gateway import Gateway
    from harness.hooks import HookRegistry
    eng = _fresh()
    HookRegistry.get().clear()
    eng.add("bash", agent="t", cedar=BASH_POLICY, kind="cmd",
            reason="bash 危险命令被拒")
    gw = Gateway(event_bus=None)
    result = gw.authorize(
        _gateway_ctx(_agent_t, "bash_exec", {"command": "rm -rf /"}))
    assert result.decision == "denied"
    assert "bash 危险命令被拒" in (result.reason or "")


def test_cedar_nodedecision_allows(_agent_t):
    from harness.gateway.gateway import Gateway
    from harness.hooks import HookRegistry
    eng = _fresh()
    HookRegistry.get().clear()
    eng.add("bash", agent="t", cedar=BASH_POLICY, kind="cmd")
    gw = Gateway(event_bus=None)
    result = gw.authorize(
        _gateway_ctx(_agent_t, "bash_exec", {"command": "echo hi"}))
    assert result.decision != "denied"


def test_overlay_veto_still_denies_after_static(_agent_t):
    from harness.gateway.gateway import Gateway
    from harness.hooks import HookRegistry
    eng = _fresh()
    HookRegistry.get().clear()
    # static allows ls; overlay denies anything touching home
    eng.add("ov", agent="t", cedar=BASH_POLICY, kind="cmd",
            veto=lambda gate, cfg: "overlay deny" if "keys" in str(
                (gate.tool_args or {}).get("command", "")) else None)
    gw = Gateway(event_bus=None)
    r = gw.authorize(_gateway_ctx(
        _agent_t, "bash_exec", {"command": "ls /root/.keys"}))
    assert r.decision == "denied" and "overlay deny" in (r.reason or "")




# ─────────────────────────────────────────────────────────────
# I-35 S5: guard data -> SandboxRestriction -> kernel enforcement
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def _bwrap(tmp_path):
    import shutil as _s
    if _s.which("bwrap") is None:
        pytest.skip("no bwrap")
    from harness.sandbox.impl import BubblewrapSandbox
    from harness.interfaces import SandboxConfig
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sub").mkdir()
    sb = BubblewrapSandbox()
    pid = sb.spawn(SandboxConfig(
        backend="bubblewrap", firewall={"network": "none"},
        shared_folders=[{"host_path": str(ws),
                         "sandbox_path": "/workspace", "mode": "rw"}],
        ttl_seconds=0))
    yield sb, pid
    sb.kill(pid)


@pytest.mark.skipif(not Path("/sys/kernel/security/lsm").exists()
                    or "landlock" not in
                    Path("/sys/kernel/security/lsm").read_text(),
                    reason="no landlock")
def test_guard_to_kernel_end_to_end(_bwrap):
    eng = _fresh()
    eng.add("g", agent="t",
            guard={"paths": {"rw": ["/workspace/sub"], "ro": ["/workspace"]},
                   "syscalls_deny": ["socket"]})
    sb, pid = _bwrap
    restrict = eng.compile_guard("t")
    out, err, rc = sb.execute(
        pid,
        "touch /workspace/sub/x && echo SUB_OK; "
        "touch /workspace/a 2>&1; "
        "python3 -c 'import socket; socket.socket()' 2>&1", restrict=restrict)
    text = out + err
    assert "SUB_OK" in text, text
    assert "Permission denied" in text, text
    assert "Operation not permitted" in text or "EPERM" in text, text




# ─────────────────────────────────────────────────────────────
# I-35 infra: _register_builtins carries full policy-unit fields
# ─────────────────────────────────────────────────────────────


def test_register_builtins_wires_cedar_guard_fields(monkeypatch):
    import harness.policy_presets as PP
    fake = {"name-marker": lambda: {
        "veto": None,
        "cedar": "@id('x')\nforbid(principal, action, resource);",
        "kind": "cmd",
        "guard": {"paths": {"ro": ["/workspace"]},
                  "syscalls_deny": ["socket"]},
        "description": "fake preset",
        "reason": "fake reason",
    }}
    monkeypatch.setattr(PP, "BUILTIN_PRESETS", fake)
    eng = PolicyEngine()  # __init__ -> _register_builtins reads patched map
    d = eng._defs["name-marker"]
    assert d["cedar"] and "forbid" in d["cedar"]
    assert d["kind"] == "cmd"
    assert d["guard"]["syscalls_deny"] == ["socket"]
    assert d["description"] == "fake preset"
    assert d["reason"] == "fake reason"




# ─────────────────────────────────────────────────────────────
# I-37 fs_paths: bash 命令访问系统路径也应被静态拦截（先 RED）
# ─────────────────────────────────────────────────────────────


def test_fs_paths_bash_command_denied():
    eng = _fresh()
    eng.enable("fs_paths", agent="t")
    decision, reason, matched = eng.check_static(
        "t", "bash_exec", {"command": "cat /etc/hostname"})
    assert decision == "deny", (decision, reason)


def test_fs_paths_file_tool_still_denied():
    eng = _fresh()
    eng.enable("fs_paths", agent="t")
    decision, _, matched = eng.check_static(
        "t", "read_file", {"path": "/etc/hostname"})
    assert decision == "deny"


def test_fs_paths_workspace_command_allowed():
    eng = _fresh()
    eng.enable("fs_paths", agent="t")
    decision, _, matched = eng.check_static(
        "t", "bash_exec", {"command": "ls /workspace"})
    assert decision is None  # 未纳管=放行（NoDecision 语义）




def test_fs_paths_kernel_read_deny_e2e(_bwrap):
    """fs_paths → guard.sys_read_deny → bash cat /etc EACCES（内核级）."""
    eng = _fresh()
    eng.enable("fs_paths", agent="t")
    r = eng.compile_guard("t")
    assert r is not None and "/etc" in r.sys_read_deny
    sb, pid = _bwrap
    out, err, rc = sb.execute(pid, "cat /etc/hostname 2>&1; echo rc=$?",
                              restrict=r)
    text = out + err
    assert "Permission denied" in text or "EACCES" in text, text




def test_bash_whitelist_is_veto_overlay_unit():
    """I-37b 决策：bash_whitelist 保留纯 veto overlay（正则不可 Cedar 化）."""
    import harness.policy_presets as PP
    unit = PP.BUILTIN_PRESETS["bash_whitelist"]()
    assert unit.get("cedar") is None
    assert callable(unit.get("veto"))
    assert unit.get("cfg") == {"allow": []}

