"""I-18: Program 执行器（SDK 执行面包装）— TDD.

grill 决策（2026-09-04）：
- 入口 = 独立 client.program.run(...)；输出写 session 事件为主；sandbox 可选（脚本自身写 FS）
- Program = agent 配置子集：不构造 AgentRuntime / 无 LLM / 无推理循环
- 重试语义（mission §2）：瞬时失败按次重试；超时/永久失败不重试
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture
def tmp_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """Isolate ~/.harness per test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return str(home)


# ─────────────────────────────────────────────────────────────
# S1: 重试策略 util（RED）
# ─────────────────────────────────────────────────────────────


class TestRetryPolicy:
    def test_retries_transient_then_succeeds(self) -> None:
        """瞬时失败（OSError）重试至成功；attempts 记录."""
        from harness.sdk.program import with_retry

        calls = []

        def flaky() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise OSError("transient")
            return "ok"

        out = with_retry(flaky, retries=3)
        assert out == "ok"
        assert len(calls) == 3

    def test_retries_exhausted_raises_last(self) -> None:
        """重试耗尽 → 抛最后一次异常."""
        from harness.sdk.program import with_retry

        calls = []

        def always_fail() -> None:
            calls.append(1)
            raise OSError("boom")

        with pytest.raises(OSError, match="boom"):
            with_retry(always_fail, retries=2)
        assert len(calls) == 3  # 1 次初试 + 2 次重试

    def test_timeout_not_retried(self) -> None:
        """超时（subprocess.TimeoutExpired）→ 不重试，直接抛."""
        from harness.sdk.program import with_retry

        calls = []

        def slow() -> None:
            calls.append(1)
            raise subprocess.TimeoutExpired(cmd=["sleep"], timeout=1)

        with pytest.raises(subprocess.TimeoutExpired):
            with_retry(slow, retries=3)
        assert len(calls) == 1

    def test_retryable_filter_respected(self) -> None:
        """自定义 retryable 判定：非白名单异常不重试."""
        from harness.sdk.program import with_retry

        calls = []

        def value_err() -> None:
            calls.append(1)
            raise ValueError("nope")

        with pytest.raises(ValueError):
            with_retry(value_err, retries=3,
                       retryable=lambda exc: isinstance(exc, OSError))
        assert len(calls) == 1

# ─────────────────────────────────────────────────────────────
# S2: 宿主直跑 runner（RED）
# ─────────────────────────────────────────────────────────────


class TestHostRunner:
    def test_echo_completed(self) -> None:
        """确定性脚本成功 → completed + output + exit 0."""
        from harness.sdk.program import run_script_host
        res = run_script_host("echo hello")
        assert res.status == "completed"
        assert "hello" in res.output
        assert res.exit_code == 0

    def test_nonzero_exit_is_failed_not_raise(self) -> None:
        """脚本失败（exit 1）→ failed + exit_code（不抛异常，不重试）."""
        from harness.sdk.program import run_script_host
        res = run_script_host("exit 3")
        assert res.status == "failed"
        assert res.exit_code == 3

    def test_timeout_returns_timed_out(self) -> None:
        """超时 → timed_out（快速返回，不挂起不重试）."""
        from harness.sdk.program import run_script_host
        res = run_script_host("sleep 5", timeout=0.5)
        assert res.status == "timed_out"

# ─────────────────────────────────────────────────────────────
# S3: sandbox 实体执行路径
# ─────────────────────────────────────────────────────────────


class StubBackend:
    """execute 返回预设 (stdout, stderr, rc)；记录脚本."""

    def __init__(self, stdout="", stderr="", rc=0):
        self.out, self.err, self.rc = stdout, stderr, rc
        self.calls: list[str] = []

    def spawn(self, config, sandbox_id=None):
        self.alive = True
        return sandbox_id or "pid"

    def kill(self, sandbox_id, keep_workspace=False):
        self.alive = False

    def is_alive(self, sandbox_id):
        return getattr(self, "alive", True)

    def execute(self, sandbox_id, command, restrict=None):
        self.calls.append(command)
        if command == "echo ready":
            return ("", "", 0)  # manager readiness probe
        return (self.out, self.err, self.rc)


def _mgr(monkeypatch, backend):
    from harness.sandbox import manager as mgr_mod
    from harness.sandbox.manager import SandboxManager
    from harness.interfaces import SandboxConfig
    monkeypatch.setattr(mgr_mod, "get_sandbox",
                        lambda backend_n="bubblewrap": backend)
    return SandboxManager(configs={"main": SandboxConfig(
        backend="bubblewrap", firewall={"network": "host"})})


class TestSandboxRunner:
    def test_runs_inside_canonical_entity(self, monkeypatch) -> None:
        """默认规范实体执行；completed + output + exit 0."""
        from harness.sdk.program import run_script_sandbox
        backend = StubBackend(stdout="hello")
        mgr = _mgr(monkeypatch, backend)

        res = run_script_sandbox("echo hello", "main", mgr)

        assert res.status == "completed"
        assert res.output == "hello"
        assert backend.calls[-1] == "echo hello"

    def test_pinned_sandbox_id_used(self, monkeypatch) -> None:
        """显式 sandbox_id → ensure 该实体执行."""
        from harness.sdk.program import run_script_sandbox
        backend = StubBackend(stdout="hi")
        mgr = _mgr(monkeypatch, backend)
        e = mgr.create("main")

        res = run_script_sandbox("true", "main", mgr,
                                 sandbox_id=e.sandbox_id)

        assert res.status == "completed"
        assert backend.calls[-1] == "true"  # probe 先于脚本

    def test_nonzero_rc_is_failed(self, monkeypatch) -> None:
        """沙箱内脚本失败（rc!=0）→ failed + exit_code."""
        from harness.sdk.program import run_script_sandbox
        backend = StubBackend(stdout="boom", rc=2)
        mgr = _mgr(monkeypatch, backend)

        res = run_script_sandbox("exit 2", "main", mgr)

        assert res.status == "failed"
        assert res.exit_code == 2

# ─────────────────────────────────────────────────────────────
# S4/S5: client.program.run — session 事件输出 + sandbox 可选
# ─────────────────────────────────────────────────────────────


class TestProgramClient:
    def test_run_host_writes_session_events(self, tmp_home) -> None:
        """宿主执行 → ProgramResult + session 事件（program_started/completed）."""
        from harness.sdk import HarnessClient
        client = HarnessClient()

        res = client.program.run("echo p1")

        assert res.status == "completed"
        assert "p1" in res.output
        assert res.session_id
        evs = client._store.read_events(res.session_id)
        types = [e.event_type for e in evs]
        assert "program_started" in types
        completed = [e for e in evs if e.event_type == "program_completed"][0]
        assert completed.payload["status"] == "completed"

    def test_run_reuses_given_session(self, tmp_home) -> None:
        """传入 session_id → 事件写入该 session."""
        import uuid as _uuid
        from harness.sdk import HarnessClient
        client = HarnessClient()
        sid = str(_uuid.uuid4())
        client._store.create_session(sid, {"type": "program"})
        res = client.program.run("echo x", session_id=sid)
        assert res.session_id == sid
        evs = client._store.read_events(sid)
        assert any(e.event_type == "program_completed" for e in evs)

    def test_run_optional_sandbox_end_to_end(self, tmp_home,
                                             monkeypatch) -> None:
        """给了 sandbox → 在 manager 实体上执行并写事件（脚本可写 FS）."""
        from harness.sdk import HarnessClient
        from harness.sandbox.manager import SandboxManager
        from harness.sandbox import manager as mgr_mod
        from harness.interfaces import SandboxConfig
        backend = StubBackend(stdout="from-sandbox")
        monkeypatch.setattr(mgr_mod, "get_sandbox",
                            lambda b="bubblewrap": backend)
        mgr = SandboxManager(configs={"main": SandboxConfig(
            backend="bubblewrap", firewall={"network": "host"})})

        client = HarnessClient()
        client._sandbox_mgr = mgr
        res = client.program.run("echo from-sandbox", sandbox_name="main")

        assert res.status == "completed"
        assert res.output == "from-sandbox"
        assert backend.calls[-1] == "echo from-sandbox"
        evs = client._store.read_events(res.session_id)
        assert any(e.event_type == "program_completed" for e in evs)

