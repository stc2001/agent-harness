"""Integration tests: AgentRuntime + bwrap sandbox + shared folders.

Verifies the full call chain from Agent tools through real bwrap
sandbox execution to the shared filesystem, bridging the gap between
unit tests (mock sandbox) and E2E tests (full stack + real LLM).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from typing import Any

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec, SandboxConfig
from harness.sandbox.impl import BubblewrapSandbox
from harness.session_store.impl import SessionStore


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def gateway():
    return Gateway(event_bus=None)


@pytest.fixture
def shared_temp_dir() -> str:
    """A temp directory bind-mounted into the sandbox at /workspace."""
    d = tempfile.mkdtemp(prefix="harness-integration-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def agent_spec_worker() -> AgentSpec:
    """Worker agent spec with read_write to /workspace."""
    return AgentSpec(
        name="integration-worker",
        description="Integration test worker",
        tools=["write_file", "read_file", "bash"],
        model="deepseek-v4-flash",
        max_iterations=1,
        system_prompt="You write files to /workspace/.",
        permissions={
            "subagent_spawn": False,
            "file_access": {"/workspace": "read_write"},
        },
    )


@pytest.fixture
def agent_spec_reader() -> AgentSpec:
    """Reader agent spec with read_only to /workspace."""
    return AgentSpec(
        name="integration-reader",
        description="Integration test reader",
        tools=["read_file", "bash"],
        model="deepseek-v4-flash",
        max_iterations=1,
        system_prompt="You read files from /workspace/.",
        permissions={
            "subagent_spawn": False,
            "file_access": {"/workspace": "read_only"},
        },
    )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _build_runtime(
    spec: AgentSpec,
    gateway: Gateway,
    shared_temp_dir: str,
    temp_store_path: str,
    api_key: str = "sk-test",
) -> tuple[AgentRuntime, BubblewrapSandbox, str]:
    """Build AgentRuntime wired to a real bwrap sandbox.

    Spawns a fresh bwrap with ``shared_temp_dir`` bind-mounted at
    ``/workspace``.  Returns (runtime, sandbox, instance_id) so the
    caller is responsible for cleanup via ``sandbox.kill(instance_id)``.
    """
    store = SessionStore(base_path=temp_store_path)
    session_id = str(uuid.uuid4())
    store.create_session(session_id, {"test": "integration"})

    caps: dict[str, object] = {"toolcall": spec.tools, "subagentspawn": False}

    sandbox = BubblewrapSandbox()
    config = SandboxConfig(
        backend="bubblewrap",
        firewall={"network": "host"},
        shared_folders=[
            {"host_path": shared_temp_dir, "sandbox_path": "/workspace"},
        ],
        command=["/bin/sh", "-c", "sleep 3600"],
    )
    instance_id = sandbox.spawn(config)

    runtime = AgentRuntime(
        spec=spec,
        gateway=gateway,
        store=store,
        api_key=api_key,
        sandbox=sandbox,
    )
    runtime._current_session_id = session_id
    runtime._sandbox_id = instance_id
    runtime._inject_tool_deps()
    return runtime, sandbox, instance_id


def _wait_ready(sandbox: BubblewrapSandbox, instance_id: str, timeout: float = 5.0) -> None:
    """Poll until the sandbox process is alive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sandbox.is_alive(instance_id):
            return
        time.sleep(0.1)
    raise TimeoutError("Sandbox did not become ready")


# ─────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────


class TestAgentFileIOThroughSandbox:
    """Agent tool → bwrap execute → shared filesystem roundtrip."""

    def test_write_then_read_same_file(
        self,
        gateway: Gateway,
        shared_temp_dir: str,
        agent_spec_worker: AgentSpec,
        temp_store_path: str,
    ) -> None:
        """write_file then read_file returns the same content.

        Writes via AgentRuntime._write_file (which shell-execs Python
        inside the sandbox) and reads back via _read_file.  Also
        checks the file exists on the host in shared_temp_dir.
        """
        runtime, sandbox, instance_id = _build_runtime(
            agent_spec_worker, gateway, shared_temp_dir, temp_store_path
        )
        _wait_ready(sandbox, instance_id)

        content = f"integration-test-{uuid.uuid4().hex[:8]}"
        path = "/workspace/test_output.txt"

        write_result = runtime._tool_registry.get_handler("write_file")(path, content)
        assert "Wrote" in write_result, f"Write failed: {write_result}"

        read_result = runtime._tool_registry.get_handler("read_file")(path)
        assert content in read_result, f"Read mismatch: {read_result}"

        # Verify on host side — shared_temp_dir is bind-mounted
        host_path = os.path.join(shared_temp_dir, "test_output.txt")
        assert os.path.isfile(host_path)
        with open(host_path) as f:
            assert f.read() == content

        sandbox.kill(instance_id)

    def test_write_file_permission_denied_read_only(
        self,
        gateway: Gateway,
        shared_temp_dir: str,
        agent_spec_reader: AgentSpec,
        temp_store_path: str,
    ) -> None:
        """A read_only agent cannot write to /workspace.

        The AgentRuntime path-level permission check runs *before* the
        tool command ever reaches bwrap, so this is a code-level gate
        rather than a filesystem restriction.
        """
        runtime, sandbox, instance_id = _build_runtime(
            agent_spec_reader, gateway, shared_temp_dir, temp_store_path
        )
        _wait_ready(sandbox, instance_id)

        with pytest.raises(PermissionError, match="read_only"):
            runtime._tool_registry.get_handler("write_file")("/workspace/should_fail.txt", "nope")

        sandbox.kill(instance_id)

    def test_bash_lists_shared_directory(
        self,
        gateway: Gateway,
        shared_temp_dir: str,
        agent_spec_worker: AgentSpec,
        temp_store_path: str,
    ) -> None:
        """bash ls inside sandbox sees files written by agent tools.

        Writes two files through write_file, then lists the directory
        via bash (which shell-execs inside the same bwrap namespace).
        """
        runtime, sandbox, instance_id = _build_runtime(
            agent_spec_worker, gateway, shared_temp_dir, temp_store_path
        )
        _wait_ready(sandbox, instance_id)

        runtime._tool_registry.get_handler("write_file")("/workspace/a.txt", "aaa")
        runtime._tool_registry.get_handler("write_file")("/workspace/b.txt", "bbb")

        stdout = runtime._tool_registry.get_handler("bash")("ls /workspace/")

        assert "a.txt" in stdout
        assert "b.txt" in stdout

        sandbox.kill(instance_id)


class TestTwoAgentsShareSandbox:
    """Two AgentRuntimes sharing one bwrap instance for DAG-style file passing."""

    def test_worker_writes_reader_reads(
        self,
        gateway: Gateway,
        shared_temp_dir: str,
        agent_spec_worker: AgentSpec,
        agent_spec_reader: AgentSpec,
        temp_store_path: str,
    ) -> None:
        """Worker writes a file; reader in the same sandbox reads it back.

        This simulates a Producer→Validator DAG where both agents
        operate on the same shared filesystem.
        """
        worker, sandbox, instance_id = _build_runtime(
            agent_spec_worker, gateway, shared_temp_dir, temp_store_path
        )
        _wait_ready(sandbox, instance_id)

        secret = f"shared-secret-{uuid.uuid4().hex[:6]}"
        worker._tool_registry.get_handler("write_file")("/workspace/shared_data.txt", secret)

        # Reader reuses the same bwrap instance
        store2 = SessionStore(base_path=temp_store_path)
        sid2 = str(uuid.uuid4())
        store2.create_session(sid2, {"test": "integration-reader"})
        caps2: dict[str, object] = {"toolcall": ["read_file", "bash"], "subagentspawn": False}

        reader = AgentRuntime(
            spec=agent_spec_reader,
            gateway=gateway,
            store=store2,
            api_key="sk-test",
            sandbox=sandbox,
        )
        reader._current_session_id = sid2
        reader._sandbox_id = instance_id
        reader._inject_tool_deps()

        result = reader._tool_registry.get_handler("read_file")("/workspace/shared_data.txt")
        assert secret in result, f"Reader could not see worker's file: {result}"

        # Verify on host
        host_path = os.path.join(shared_temp_dir, "shared_data.txt")
        assert os.path.isfile(host_path)
        with open(host_path) as f:
            assert f.read() == secret

        sandbox.kill(instance_id)

    def test_permission_boundary_enforced(
        self,
        gateway: Gateway,
        shared_temp_dir: str,
        agent_spec_worker: AgentSpec,
        agent_spec_reader: AgentSpec,
        temp_store_path: str,
    ) -> None:
        """Path-level permissions are enforced even on a shared sandbox.

        The filesystem allows writes (single bwrap namespace), but
        AgentRuntime's file_access check blocks the read_only agent.
        """
        worker, sandbox, instance_id = _build_runtime(
            agent_spec_worker, gateway, shared_temp_dir, temp_store_path
        )
        _wait_ready(sandbox, instance_id)
        worker._tool_registry.get_handler("write_file")("/workspace/data.txt", "hello")

        # Reader shares sandbox but has read_only permission
        store2 = SessionStore(base_path=temp_store_path)
        sid2 = str(uuid.uuid4())
        store2.create_session(sid2, {"test": "integration-reader"})
        caps2: dict[str, object] = {"toolcall": ["read_file"], "subagentspawn": False}

        reader = AgentRuntime(
            spec=agent_spec_reader,
            gateway=gateway,
            store=store2,
            api_key="sk-test",
            sandbox=sandbox,
        )
        reader._current_session_id = sid2
        reader._sandbox_id = instance_id
        reader._inject_tool_deps()

        # Reader can read (read_only permitted)
        read_result = reader._tool_registry.get_handler("read_file")("/workspace/data.txt")
        assert "hello" in read_result

        # Reader cannot write (file_access: read_only enforced)
        with pytest.raises(PermissionError, match="read_only"):
            reader._tool_registry.get_handler("write_file")("/workspace/data.txt", "overwrite")

        sandbox.kill(instance_id)


class TestSandboxManagerLazyLoading:
    """SandboxManager.get_or_create lazy loading and cache behavior."""

    def test_second_get_or_create_returns_cached_instance(
        self, sandbox_manager
    ) -> None:
        """Second get_or_create("main") returns the SAME instance without spawning."""
        mgr = sandbox_manager

        e1 = mgr.get_or_create("main")
        e2 = mgr.get_or_create("main")

        # Same backend object, same entity (sandbox_id stable)
        assert e1.backend is e2.backend
        assert e1.sandbox_id == e2.sandbox_id

        # Verify the sandbox is actually alive (not a stale cache)
        assert e1.backend.is_alive(e1.sandbox_id)

        mgr.destroy(e1.sandbox_id)

    def test_destroy_then_get_or_create_respawns(
        self, sandbox_manager
    ) -> None:
        """After destroy, get_or_create spawns a NEW instance."""
        mgr = sandbox_manager

        e1 = mgr.get_or_create("main")
        old_sid = e1.sandbox_id
        mgr.destroy(old_sid)
        assert not e1.backend.is_alive(old_sid), "Destroyed sandbox should be dead"

        # I-26: destroy removed the entity; default get_or_create mints a
        # FRESH canonical entity (old uuid not re-materialised on this path).
        e2 = mgr.get_or_create("main")
        assert e2.backend.is_alive(e2.sandbox_id)
        assert e2.sandbox_id != old_sid

        mgr.destroy(e2.sandbox_id)


class TestSkillsInSandbox:
    """Skills are bind-mounted into sandbox and readable by Agent."""

    def test_skills_bind_mounted_and_readable(
        self, gateway, shared_temp_dir,
        agent_spec_worker, temp_store_path,
    ):
        """Agent can read_file SKILL.md from within bwrap sandbox."""
        import os
        from harness.agent_runtime.impl import AgentRuntime
        from harness.interfaces import AgentSpec, SandboxConfig
        from harness.session_store.impl import SessionStore
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.tools import load_builtins
        from tests.conftest import register_test_tools

        # Augment spec to include skills
        spec = AgentSpec(
            name="test-skills-agent",
            description="Integration test agent with skills",
            tools=["read_file", "bash"],
            model="deepseek-v4-flash",
            max_iterations=3,
            permissions={"file_access": {"/workspace": "read_write"}},
            skills=["bash-expert"],
        )

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[
                {"host_path": shared_temp_dir, "sandbox_path": "/workspace"},
                {"host_path": os.path.abspath("skills/bash-expert"),
                 "sandbox_path": "/workspace/.skills/bash-expert"},
            ],
            command=["/bin/sh", "-c", "sleep 3600"],
        )

        store = SessionStore(base_path=temp_store_path)
        session_id = "test-skills-sandbox"
        store.create_session(session_id, {})
        caps = {"toolcall": ["read_file", "bash"]}

        instance_id = sandbox.spawn(config)

        from tests.test_integration import _wait_ready
        _wait_ready(sandbox, instance_id)

        runtime = AgentRuntime(
            spec=spec, gateway=gateway,
            store=store, api_key="sk-test",
            sandbox=sandbox, sandbox_instance_id=instance_id,
        )
        runtime._current_session_id = session_id
        runtime._inject_tool_deps()

        # ── Verify metadata in prompt (via prompts.assemble + sandbox scan) ──
        from harness.sdk.client import _PromptsClient, _SkillsClient
        pc = _PromptsClient(None)
        sc = _SkillsClient(None)
        metas = sc.scan(sandbox, instance_id)
        prompt = pc.assemble(
            base_prompt="You are an agent runtime assistant.",
            skills=[s for s in metas if s.name in ("bash-expert",)]
        )
        messages, _meta = runtime.llm_messages(session_id, prompt)
        content = messages[0]["content"]
        assert "Available skills" in content, \
            f"Skills metadata missing from prompt: {content[:200]}"
        assert "bash-expert" in content

        # ── Verify agent can read SKILL.md from within sandbox ──
        handler = runtime._tool_registry.get_handler("read_file")
        result = handler(path="/workspace/.skills/bash-expert/SKILL.md")
        assert "Bash Expert" in result, \
            f"Could not read SKILL.md in sandbox: {result}"
        assert "set -euo pipefail" in result

        sandbox.kill(instance_id)

    def test_selective_mount_only_specified_skills(
        self, gateway, shared_temp_dir,
        agent_spec_worker, temp_store_path,
    ):
        """Only skills in AgentSpec.skills are visible in sandbox .skills/."""
        import os
        from harness.interfaces import AgentSpec, SandboxConfig
        from harness.session_store.impl import SessionStore
        from harness.sandbox.impl import BubblewrapSandbox

        spec = AgentSpec(
            name="test-selective-skills",
            description="Integration test — selective mount",
            tools=["bash"],
            model="deepseek-v4-flash",
            max_iterations=1,
            permissions={"file_access": {"/workspace": "read_write"}},
            skills=["bash-expert"],
        )

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[
                {"host_path": shared_temp_dir, "sandbox_path": "/workspace"},
                {"host_path": os.path.abspath("skills/bash-expert"),
                 "sandbox_path": "/workspace/.skills/bash-expert"},
            ],
            command=["/bin/sh", "-c", "sleep 3600"],
        )

        store = SessionStore(base_path=temp_store_path)
        session_id = "test-selective-skills"
        store.create_session(session_id, {})
        instance_id = sandbox.spawn(config)

        from tests.test_integration import _wait_ready
        _wait_ready(sandbox, instance_id)

        out, _, rc = sandbox.execute(instance_id, "ls /workspace/.skills/")
        assert rc == 0
        assert "bash-expert" in out
        assert "rdkit" not in out, f"rdkit should not be visible: {out}"
        if "test-agent-skill" in out:
            pytest.fail(f"Only bash-expert should be visible, got: {out}")

        sandbox.kill(instance_id)

    def test_union_mount_two_agents_see_all(
        self, gateway, shared_temp_dir,
        agent_spec_worker, temp_store_path,
    ):
        """When two agents' skills union-mounted, both visible."""
        import os
        from harness.interfaces import SandboxConfig
        from harness.session_store.impl import SessionStore
        from harness.sandbox.impl import BubblewrapSandbox

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[
                {"host_path": shared_temp_dir, "sandbox_path": "/workspace"},
                {"host_path": os.path.abspath("skills/bash-expert"),
                 "sandbox_path": "/workspace/.skills/bash-expert"},
                {"host_path": os.path.abspath("skills/test-agent-skill"),
                 "sandbox_path": "/workspace/.skills/test-agent-skill"},
            ],
            command=["/bin/sh", "-c", "sleep 3600"],
        )

        store = SessionStore(base_path=temp_store_path)
        session_id = "test-union-skills"
        store.create_session(session_id, {})
        instance_id = sandbox.spawn(config)

        from tests.test_integration import _wait_ready
        _wait_ready(sandbox, instance_id)

        out, _, rc = sandbox.execute(instance_id, "ls /workspace/.skills/")
        assert rc == 0
        assert "bash-expert" in out
        assert "test-agent-skill" in out

        sandbox.kill(instance_id)

    def test_agent_discovers_skills_via_ls_and_read_file(
        self, gateway, shared_temp_dir,
        agent_spec_worker, temp_store_path,
    ):
        """Agent uses bash ls + read_file to discover and load skills."""
        import os
        from harness.agent_runtime.impl import AgentRuntime
        from harness.interfaces import AgentSpec, SandboxConfig
        from harness.session_store.impl import SessionStore
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.tools import load_builtins
        from tests.conftest import register_test_tools

        spec = AgentSpec(
            name="test-discover-skills",
            description="Agent discovers skills via ls + read_file",
            tools=["read_file", "bash"],
            model="deepseek-v4-flash",
            max_iterations=3,
            permissions={"file_access": {"/workspace": "read_write"}},
            skills=["bash-expert"],
        )

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[
                {"host_path": shared_temp_dir, "sandbox_path": "/workspace"},
                {"host_path": os.path.abspath("skills/bash-expert"),
                 "sandbox_path": "/workspace/.skills/bash-expert"},
            ],
            command=["/bin/sh", "-c", "sleep 3600"],
        )

        store = SessionStore(base_path=temp_store_path)
        session_id = "test-discover-skills"
        store.create_session(session_id, {})
        caps = {"toolcall": ["read_file", "bash"]}
        instance_id = sandbox.spawn(config)

        from tests.test_integration import _wait_ready
        _wait_ready(sandbox, instance_id)

        runtime = AgentRuntime(
            spec=spec, gateway=gateway,
            store=store, api_key="sk-test",
            sandbox=sandbox, sandbox_instance_id=instance_id,
        )
        runtime._current_session_id = session_id
        runtime._inject_tool_deps()

        # Agent discovers via bash ls
        ls_handler = runtime._tool_registry.get_handler("bash")
        ls_result = ls_handler(command="ls /workspace/.skills/")
        assert "bash-expert" in str(ls_result)

        # Agent reads SKILL.md
        read_handler = runtime._tool_registry.get_handler("read_file")
        content = read_handler(path="/workspace/.skills/bash-expert/SKILL.md")
        assert "Bash Expert" in content

        sandbox.kill(instance_id)


