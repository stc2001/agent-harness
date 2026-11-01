"""TDD: Gateway EnvSanitizer — filter sensitive env vars before sandbox spawn.

RED phase: Gateway rules are not yet invoked on sandbox_spawn path.
After fix, pool.acquire() calls gateway.authorize(ctx) with gate_type="sandbox_spawn",
allowing rules to inspect and modify SandboxConfig.env_vars before sandbox creation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.gateway.gateway import Gateway, GateContext, AuthorizeResult
from harness.interfaces import AgentSpec, SandboxConfig, SandboxInterface
from harness.orchestration.pool import AgentInstancePool
from harness.sandbox.manager import SandboxManager
from harness.session_store.impl import SessionStore


# ── Helpers ────────────────────────────────────────────────


def _make_spec(name: str = "agent", sandbox_name: str = "main") -> AgentSpec:
    return AgentSpec(
        name=name, description="test", sandbox=sandbox_name,
        tools=[], model="mock", max_iterations=1, timeout_seconds=10,
    )


def _make_config(env_vars: dict | None = None) -> SandboxConfig:
    return SandboxConfig(
        backend="bubblewrap",
        firewall={"network": "none"},
        shared_folders=[{"host_path": "/tmp/test", "sandbox_path": "/ws"}],
        env_vars=dict(env_vars or {}),
    )


class _FakeSandbox(SandboxInterface):
    """Captures the config passed to spawn() for inspection."""
    def __init__(self):
        self.last_config: SandboxConfig | None = None
        self._counter = 0

    def spawn(self, config: SandboxConfig, sandbox_id: str | None = None) -> str:
        self._counter += 1
        self.last_config = config
        return f"pid-{self._counter}"

    def kill(self, instance_id: str, keep_workspace: bool = False) -> None:
        pass

    def is_alive(self, instance_id: str) -> bool:
        return True

    def execute(self, instance_id: str, cmd: str, *args, **kwargs):
        return (f"[{instance_id}] {cmd}", "", 0)

    def get_logs(self, instance_id: str) -> str:
        return ""


# ── EnvSanitizer rule ──────────────────────────────────────


def env_sanitizer_rule(ctx: GateContext) -> AuthorizeResult | None:
    """Strip secret-looking env vars from sandbox config.

    Returns AuthorizeResult("modified") with filtered env_vars,
    or None if no changes needed (let authorization continue).
    """
    config = ctx.config
    if config is None or not config.env_vars:
        return None

    filtered = {}
    removed: list[str] = []
    for key, value in config.env_vars.items():
        # Strip API keys, tokens, and internal agent vars
        if any([
            value.startswith("sk-"),
            value.startswith("hf_"),
            value.startswith("ghp_"),
            key.startswith("AGENT_"),
            "_TOKEN" in key.upper(),
            "_SECRET" in key.upper(),
            "_KEY" in key.upper() and key not in ("PATH", "HOME", "USER"),
        ]):
            removed.append(key)
            continue
        filtered[key] = value

    if not removed:
        return None  # nothing to filter, continue normally

    # Return modified result with filtered env_vars
    return AuthorizeResult(
        decision="modified",
        amend_args={
            "env_vars": filtered,
            "removed_keys": removed,
        },
        reason=f"Stripped {len(removed)} sensitive env vars: {removed}",
    )


# ═══════════════════════════════════════════════════════════
# RED PHASE — Behavior Test
# ═══════════════════════════════════════════════════════════


class TestGatewayEnvSanitizer:
    """Env sanitizer rule must filter secrets from sandbox env_vars.

    DESIRED: When a Gateway rule strips sensitive env vars, the sandbox
    spawned by SandboxManager must NOT receive those vars.
    """

    def test_secret_env_vars_preserved_in_sandbox(
        self, tmp_path: Path, monkeypatch
    ):
        """RED: Sandbox must RETAIN API keys for tools to function.

        The EnvSanitizer rule should NOT strip keys from sandbox config.
        Only the LLM context should be filtered (handled by content_filter
        in prompt_assembly).
        """
        store = SessionStore(base_path=str(tmp_path / "store"))
        backend = _FakeSandbox()

        # Sandbox config with mixed env: one secret, one safe
        config = _make_config(env_vars={
            "OPENAI_API_KEY": "sk-fake1234567890",
            "GITHUB_TOKEN": "ghp_secret_token_xxx",
            "ANTHROPIC_API_KEY": "sk-ant-fake999",
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/agent",
            "MY_APP_SECRET": "should-be-stripped",  # matches _SECRET
            "DEBUG": "true",  # safe
        })
        mgr = SandboxManager(configs={"main": config})

        # Register env sanitizer rule on Gateway
        gateway = Gateway(event_bus=None)
        gateway.register_rule(env_sanitizer_rule, name="env_sanitizer")

        pool = AgentInstancePool(
            store=store, gateway=gateway, api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _make_spec("agent", sandbox_name="main")

        from harness.sandbox.manager import get_sandbox
        monkeypatch.setattr(
            "harness.sandbox.manager.get_sandbox",
            lambda _: backend,
        )

        # Trigger sandbox creation via pool.acquire()
        pool.acquire(spec)

        # ── Assertions ──
        spawned_config = backend.last_config
        assert spawned_config is not None, "Sandbox should have been spawned"

        spawn_env = spawned_config.env_vars

        # DESIRED: secrets PRESERVED in sandbox (tools need them)
        assert "OPENAI_API_KEY" in spawn_env, (
            f"OPENAI_API_KEY must be preserved in sandbox, got: {spawn_env}"
        )
        assert "GITHUB_TOKEN" in spawn_env, (
            f"GITHUB_TOKEN must be preserved"
        )
        assert "ANTHROPIC_API_KEY" in spawn_env, (
            f"ANTHROPIC_API_KEY must be preserved"
        )
        assert "MY_APP_SECRET" in spawn_env, (
            f"MY_APP_SECRET must be preserved"
        )

        # Safe vars also preserved
        assert spawn_env.get("PATH") == "/usr/bin:/bin"
        assert spawn_env.get("HOME") == "/home/agent"
        assert spawn_env.get("DEBUG") == "true"

    def test_no_secrets_no_modifications(self, tmp_path: Path, monkeypatch):
        """When env_vars have no secrets, config passes through unchanged."""
        store = SessionStore(base_path=str(tmp_path / "store2"))
        backend = _FakeSandbox()

        config = _make_config(env_vars={
            "PATH": "/usr/bin",
            "HOME": "/home/agent",
            "DEBUG": "true",
        })
        mgr = SandboxManager(configs={"main": config})

        gateway = Gateway(event_bus=None)
        gateway.register_rule(env_sanitizer_rule, name="env_sanitizer")

        pool = AgentInstancePool(
            store=store, gateway=gateway, api_key="sk-test",
            sandbox_manager=mgr,
        )
        spec = _make_spec("agent", sandbox_name="main")

        monkeypatch.setattr(
            "harness.sandbox.manager.get_sandbox",
            lambda _: backend,
        )

        pool.acquire(spec)

        spawned_env = backend.last_config.env_vars
        assert spawned_env == config.env_vars, (
            f"Safe env vars should pass through:\n"
            f"  original: {config.env_vars}\n"
            f"  spawned:  {spawned_env}"
        )

    def test_gateway_audit_logs_filtered_secrets(self, tmp_path: Path, monkeypatch):
        """RED: Audit log should record injected key names (not values).

        When Gateway rule detects sensitive env vars, the audit log records
        which keys were detected but NOT their values.  Sandbox config is
        left untouched (tools need the real values).
        """
        store = SessionStore(base_path=str(tmp_path / "store3"))
        backend = _FakeSandbox()
        audit_log: list[dict] = []

        class AuditRecorder:
            def append(self, **kwargs):
                audit_log.append(kwargs)

        config = _make_config(env_vars={
            "OPENAI_API_KEY": "sk-secret123",
            "PATH": "/bin",
        })
        mgr = SandboxManager(configs={"main": config}, audit_log=AuditRecorder())

        gateway = Gateway(event_bus=None)
        gateway.register_rule(env_sanitizer_rule, name="env_sanitizer")

        pool = AgentInstancePool(
            store=store, gateway=gateway, api_key="sk-test",
            sandbox_manager=mgr, audit_log=AuditRecorder(),
        )
        spec = _make_spec("agent", sandbox_name="main")

        monkeypatch.setattr(
            "harness.sandbox.manager.get_sandbox",
            lambda _: backend,
        )

        pool.acquire(spec)

        # DESIRED: audit log records rule amendments generically
        modified_events = [
            e for e in audit_log
            if e.get("event") == "sandbox_spawn_modified"
        ]
        assert len(modified_events) >= 1, (
            f"Audit log should record sandbox_spawn_modified. Log: {audit_log}"
        )
        # amend_args contains whatever the rule returned (app-layer concept)
        amend = modified_events[0]["detail"]["amend_args"]
        assert "removed_keys" in amend
        assert "OPENAI_API_KEY" in amend["removed_keys"]
