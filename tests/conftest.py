"""Shared test fixtures for Agent Harness.

Provides:
- Temporary session store path (isolated per test)
- DeepSeek API client fixture
- Sample AgentSpec / Event fixtures
- CapTokenInterface implementation (if available)
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest


# ─────────────────────────────────────────────────────────────
# Path & Environment
# ─────────────────────────────────────────────────────────────

# Prefer a local key file when present (gitignored, not shipped); fall back for offline runs.
import pathlib as _pl
_KEYFILE = _pl.Path(__file__).resolve().parents[1] / "examples" / ".keys" / "auth"
if _KEYFILE.exists():
    TEST_DEEPSEEK_API_KEY = _KEYFILE.read_text(encoding="utf-8").strip()
else:
    TEST_DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "invalid-hermetic-key")
TEST_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


@pytest.fixture(autouse=True)
def set_deepseek_env():
    """Ensure DeepSeek API key is set for all tests."""
    old_key = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = TEST_DEEPSEEK_API_KEY
    os.environ["DEEPSEEK_BASE_URL"] = TEST_DEEPSEEK_BASE_URL
    yield
    if old_key:
        os.environ["DEEPSEEK_API_KEY"] = old_key
    else:
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ.pop("DEEPSEEK_BASE_URL", None)


@pytest.fixture
def temp_store_path():
    """Temporary directory for session data, cleaned after test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def deepseek_api_key():
    """Return the DeepSeek API key for tests."""
    return TEST_DEEPSEEK_API_KEY


@pytest.fixture
def deepseek_base_url():
    """Return the DeepSeek base URL for tests."""
    return TEST_DEEPSEEK_BASE_URL


# ─────────────────────────────────────────────────────────────
# Shared Data Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def sample_session_id() -> str:
    """Generate a unique session ID for each test."""
    return str(uuid.uuid4())


@pytest.fixture
def sample_event(sample_session_id) -> dict[str, Any]:
    """Return a sample Event dict."""
    return {
        "event_id": str(uuid.uuid4()),
        "session_id": sample_session_id,
        "event_type": "agent_started",
        "timestamp": 1700000000.0,
        "source": "test-agent",
        "payload": {"message": "Hello, world"},
    }


@pytest.fixture
def sample_agent_spec_dict() -> dict[str, Any]:
    """Return a sample AgentSpec-compatible dict."""
    return {
        "name": "test-worker",
        "description": "Test worker agent for unit tests",
        "sandbox": "main",
        "capabilities": ["toolcall"],
        "tools": ["echo", "sum", "sandbox_execute"],
        "model": "deepseek-v4-flash",
        "system_prompt": "You are a helpful test assistant.",
        "max_iterations": 3,
        "permissions": {
            "subagent_spawn": False,
            "file_access": {"/workspace": "read_write", "/outputs": "read_write"},
        },
    }


@pytest.fixture
def sample_dag_nodes() -> list[dict[str, Any]]:
    """Return sample DAG node dicts."""
    return [
        {"id": "execute", "agent": "test-worker", "phase": "execute", "depends_on": []},
        {"id": "verify", "agent": "test-validator", "phase": "verify", "depends_on": ["execute"]},
    ]


@pytest.fixture
def sample_config_path(tmp_path):
    """Create a temporary config directory with a minimal local.yaml."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "local.yaml"
    config_file.write_text("""
sandbox:
  backend: docker
  default_image: python:3.11-slim
  network: none
orchestrator:
  max_agents: 5
  default_ttl: 600
session:
  store_path: "./sessions/"
security:
  capability_token_ttl: 600
  hmac_secret: "test-secret"
""")
    return str(config_file)


@pytest.fixture
def sandbox_yaml_path(tmp_path):
    """Create a temporary sandbox.yaml for test isolation."""
    sandbox_yaml = tmp_path / "sandbox.yaml"
    sandbox_yaml.write_text("""
sandboxes:
  main:
    backend: bubblewrap
    firewall:
      network: "host"
    shared_folders:
      - host_path: "/tmp"
        sandbox_path: "/tmp"
  airgapped:
    backend: bubblewrap
    firewall:
      network: "none"
    shared_folders: []
""")
    return str(sandbox_yaml)


@pytest.fixture
def sandbox_manager():
    """Create a SandboxManager for testing."""
    from harness.sandbox.manager import SandboxManager
    from harness.interfaces import SandboxConfig
    return SandboxManager(configs={
        "main": SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[{"host_path": "/tmp", "sandbox_path": "/tmp"}],
        ),
        "airgapped": SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "none"},
        ),
    })


# ── Test-only tools (echo, sum, validate) ──
# These are not production tools — used only in tests to verify
# the tool-calling roundtrip without real side effects.


def register_test_tools(registry):
    """Register echo, sum, validate tools for testing.

    Usage in tests::

        runtime._tool_registry = load_builtins()
        register_test_tools(runtime._tool_registry)
        runtime._inject_tool_deps()
    """
    from harness.tool_registry import ToolRegistry

    @registry.register("echo", "Returns the message back unchanged.",
                       {"message": str})
    def echo(message: str) -> str:
        return message

    @registry.register("sum", "Returns the sum of two numbers.",
                       {"a": float, "b": float})
    def sum_(a: float, b: float) -> str:
        return str(a + b)

    @registry.register("validate",
                       "Compare expected vs actual. Returns PASS/FAIL.",
                       {"expected": str, "actual": str})
    def validate(expected: str, actual: str) -> str:
        if expected == actual:
            return "PASS: values match"
        return f"FAIL: expected '{expected}', got '{actual}'"


# ═══════════════════════════════════════════════════════════
# Shared mock sandbox for pool/sandbox tests
# ═══════════════════════════════════════════════════════════

from harness.interfaces import SandboxConfig as _SandboxConfig
from harness.interfaces import SandboxInterface as _SandboxInterface


class AlwaysAliveSandbox(_SandboxInterface):
    """Mock sandbox that never dies — exposes pool caching bugs.

    Used by test_pool.py and test_pool_config_change.py.
    """

    def __init__(self):
        self.spawned: list[str] = []  # host_paths from spawn configs
        self.killed: list[str] = []
        self._counter = 0

    def spawn(self, config: _SandboxConfig, sandbox_id: str | None = None) -> str:
        self._counter += 1
        host_path = config.shared_folders[0]["host_path"]
        pid = f"pid-{self._counter}"
        self.spawned.append(host_path)
        return pid

    def kill(self, instance_id: str, keep_workspace: bool = False) -> None:
        self.killed.append(instance_id)

    def is_alive(self, instance_id: str) -> bool:
        return True  # sandbox never naturally dies

    def execute(self, instance_id: str, cmd: str, *args, **kwargs):
        return (f"[{instance_id}] {cmd}", "", 0)

    def get_logs(self, instance_id: str) -> str:
        return ""


