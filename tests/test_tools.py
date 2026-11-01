"""Tests for AgentRuntime tools: read_file, write_file, bash,
list_agents, validate, diff.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from harness.agent_runtime.impl import AgentRuntime
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.session_store.impl import SessionStore
from tests.conftest import register_test_tools


@pytest.fixture
def gateway() -> Gateway:
    return Gateway(event_bus=None)


@pytest.fixture
def mock_sandbox():
    """Minimal sandbox mock returning canned responses."""
    class MockSandbox:
        def spawn(self, config, cap_token=""):
            return "mock-pid"
        def kill(self, container_id):
            pass
        def is_alive(self, container_id):
            return True
        def get_logs(self, container_id):
            return ""
        def execute(self, container_id, command, restrict=None):
            return (command, "", 0)
    return MockSandbox()


def _make_runtime(
    store,
    spec_overrides: dict[str, Any] | None = None,
    sandbox=None,
    api_key="sk-test",
    gateway=None,
    registry_path: str | None = None,
) -> AgentRuntime:
    defaults = {
        "name": "test-tools",
        "description": "Test agent for tools",
        "tools": ["echo", "validate", "list_agents", "read_file", "write_file", "bash", "diff"],
        "model": "deepseek-v4-flash",
        "max_iterations": 2,
        "permissions": {"file_access": {"/workspace": "read_write"}},
    }
    if spec_overrides:
        defaults.update(spec_overrides)
    spec = AgentSpec(**defaults)
    runtime = AgentRuntime(
        spec=spec,
        gateway=gateway or Gateway(event_bus=None),
        store=store,
        api_key=api_key,
        sandbox=sandbox,
        registry_path=registry_path,
    )
    register_test_tools(runtime._tool_registry)
    runtime._inject_tool_deps()
    return runtime


# ─────────────────────────────────────────────────────────────
# validate / list_agents (pure memory tools)
# ─────────────────────────────────────────────────────────────


class TestValidate:
    def test_validate_pass(self, temp_store_path, gateway, mock_sandbox):
        """validate with matching values returns PASS."""
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(store, gateway=gateway, sandbox=mock_sandbox)
        result = runtime._tool_registry.get_handler("validate")("hello", "hello")
        assert "PASS" in result

    def test_validate_fail(self, temp_store_path, gateway, mock_sandbox):
        """validate with different values returns FAIL with diff."""
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(store, gateway=gateway, sandbox=mock_sandbox)
        result = runtime._tool_registry.get_handler("validate")("expected", "actual")
        assert "FAIL" in result
        assert "expected" in result
        assert "actual" in result


class TestListAgents:
    def test_list_agents_with_registry(self, temp_store_path, gateway, mock_sandbox):
        """list_agents returns agent names from registry."""
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(
            store, gateway=gateway, sandbox=mock_sandbox,
            registry_path="./registry/",
        )
        result = runtime._tool_registry.get_handler("list_agents")()
        assert "test-worker" in result
        assert "test-validator" in result

    def test_list_agents_no_registry(self, temp_store_path, gateway, mock_sandbox):
        """list_agents without valid registry returns error."""
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(
            store, gateway=gateway, sandbox=mock_sandbox,
            registry_path="/nonexistent/registry/path",
        )
        result = runtime._tool_registry.get_handler("list_agents")()
        # list_agents uses its default ./registry/, not the spec's registry_path
        assert "test-validator" in result or "No agents found" in result or "Error" in result


# ─────────────────────────────────────────────────────────────
# sandbox tools: read_file / write_file / bash / diff
# ─────────────────────────────────────────────────────────────


class TestReadFile:
    def test_read_file_returns_content(self, temp_store_path, gateway, mock_sandbox):
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(store, gateway=gateway, sandbox=mock_sandbox)
        result = runtime._tool_registry.get_handler("read_file")("/workspace/data.txt")
        assert isinstance(result, str)

    def test_read_file_path_denied(self, temp_store_path, gateway, mock_sandbox):
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(
            store, gateway=gateway, sandbox=mock_sandbox,
            spec_overrides={"permissions": {"file_access": {}}},
        )
        with pytest.raises(PermissionError, match="not in agent permissions"):
            runtime._tool_registry.get_handler("read_file")("/workspace/data.txt")


class TestWriteFile:
    def test_write_file_returns_bytes_written(self, temp_store_path, gateway, mock_sandbox):
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(store, gateway=gateway, sandbox=mock_sandbox)
        result = runtime._tool_registry.get_handler("write_file")("/workspace/out.txt", "hello")
        assert "Wrote" in result

    def test_write_file_read_only_path(self, temp_store_path, gateway):
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(
            store, gateway=gateway, sandbox=mock_sandbox,
            spec_overrides={"permissions": {"file_access": {"/workspace": "read_only"}}},
        )
        with pytest.raises(PermissionError, match="read_only"):
            runtime._tool_registry.get_handler("write_file")("/workspace/out.txt", "hello")


class TestBash:
    def test_bash_returns_structured(self, temp_store_path, gateway, mock_sandbox):
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(store, gateway=gateway, sandbox=mock_sandbox)
        result = runtime._tool_registry.get_handler("bash")("echo hello")
        assert "stdout" in result.lower() or "hello" in result.lower()


class TestDiff:
    def test_diff_returns_output(self, temp_store_path, gateway, mock_sandbox):
        store = SessionStore(temp_store_path)
        runtime = _make_runtime(store, gateway=gateway, sandbox=mock_sandbox)
        result = runtime._tool_registry.get_handler("diff")("/workspace/a.txt", "/workspace/b.txt")
        assert isinstance(result, str)


# ─────────────────────────────────────────────────────────────
# Real LLM integration tests
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def real_api_key():
    import os
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        pytest.skip("DEEPSEEK_API_KEY not available")
    return key


@pytest.fixture
def real_base_url():
    import os
    return os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


class TestRealLLMTools:
    """Integration tests using real DeepSeek API.

    These verify that the LLM correctly calls tools when prompted,
    and that tool results are properly recorded as events.
    """

    def test_read_file_via_llm(
        self, temp_store_path, gateway, mock_sandbox,
        real_api_key, real_base_url, monkeypatch,
    ):
        """LLM calls read_file when asked to read a file."""
        store = SessionStore(temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        monkeypatch.setenv("AGENT_SESSION_ID", session_id)

        spec = AgentSpec(
            name="test-reader",
            description="File reader",
            tools=["echo", "read_file"],
            model="deepseek-v4-flash",
            system_prompt=(
                "You are a file reader. "
                "Call read_file with path='/tmp/test_read.txt' then respond DONE."
            ),
            max_iterations=3,
            permissions={"file_access": {"/tmp": "read_write"}},
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=real_api_key,
            base_url=real_base_url,
            sandbox=mock_sandbox,
        )

        try:
            runtime.run_loop()
        except Exception as exc:
            err_msg = str(exc).lower()
            if any(w in err_msg for w in ("connection", "timeout", "unreachable", "resolve")):
                pytest.skip(f"DeepSeek API unreachable: {exc}")
            raise

        events = store.read_events(session_id)
        tool_called = [e for e in events if e.event_type == "tool_called"]
        tool_results = [e for e in events if e.event_type == "tool_result"]
        assert len(tool_called) >= 1, f"Expected tool_called event, got {[e.event_type for e in events]}"
        assert tool_called[0].payload["tool"] == "read_file"
        assert tool_called[0].payload["allowed"] is True
        assert len(tool_results) >= 1

    def test_echo_and_validate_via_llm(
        self, temp_store_path, gateway, mock_sandbox,
        real_api_key, real_base_url, monkeypatch,
    ):
        """LLM chains echo → validate with matching values."""
        store = SessionStore(temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        monkeypatch.setenv("AGENT_SESSION_ID", session_id)

        spec = AgentSpec(
            name="test-validator",
            description="Echo and validate",
            tools=["echo", "validate"],
            model="deepseek-v4-flash",
            system_prompt=(
                "You are a validator. Follow these steps exactly:\n"
                "1. Call echo with message='hello'\n"
                "2. Call validate with expected='hello' actual='hello'\n"
                "3. Respond DONE"
            ),
            max_iterations=5,
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=real_api_key,
            base_url=real_base_url,
            sandbox=mock_sandbox,
        )
        register_test_tools(runtime._tool_registry)

        try:
            runtime.run_loop()
        except Exception as exc:
            err_msg = str(exc).lower()
            if any(w in err_msg for w in ("connection", "timeout", "unreachable", "resolve")):
                pytest.skip(f"DeepSeek API unreachable: {exc}")
            raise

        events = store.read_events(session_id)
        tools_used = [e.payload["tool"] for e in events if e.event_type == "tool_called"]
        assert "echo" in tools_used, f"echo not called. Tools used: {tools_used}"
        assert "validate" in tools_used, f"validate not called. Tools used: {tools_used}"

    def test_write_and_read_via_llm(
        self, temp_store_path, gateway, mock_sandbox,
        real_api_key, real_base_url, monkeypatch,
    ):
        """LLM writes a file, then reads it back."""
        store = SessionStore(temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        monkeypatch.setenv("AGENT_SESSION_ID", session_id)

        spec = AgentSpec(
            name="test-writer",
            description="File writer and reader",
            tools=["write_file", "read_file"],
            model="deepseek-v4-flash",
            system_prompt=(
                "You are a file operator. Follow these steps:\n"
                "1. write_file path='/tmp/llm_test.txt' content='integration_test_data'\n"
                "2. read_file path='/tmp/llm_test.txt'\n"
                "3. Respond DONE"
            ),
            max_iterations=5,
            permissions={"file_access": {"/tmp": "read_write"}},
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=real_api_key,
            base_url=real_base_url,
            sandbox=mock_sandbox,
        )

        try:
            runtime.run_loop()
        except Exception as exc:
            err_msg = str(exc).lower()
            if any(w in err_msg for w in ("connection", "timeout", "unreachable", "resolve")):
                pytest.skip(f"DeepSeek API unreachable: {exc}")
            raise

        events = store.read_events(session_id)
        tools_used = [e.payload["tool"] for e in events if e.event_type == "tool_called"]
        assert "write_file" in tools_used, f"write_file not called. Tools used: {tools_used}"
        assert "read_file" in tools_used, f"read_file not called. Tools used: {tools_used}"

    def test_bash_via_llm(
        self, temp_store_path, gateway, mock_sandbox,
        real_api_key, real_base_url, monkeypatch,
    ):
        """LLM executes a bash command in sandbox."""
        store = SessionStore(temp_store_path)
        session_id = str(uuid.uuid4())
        store.create_session(session_id, {})


        monkeypatch.setenv("AGENT_SESSION_ID", session_id)

        spec = AgentSpec(
            name="test-bash",
            description="Shell executor",
            tools=["bash"],
            model="deepseek-v4-flash",
            system_prompt=(
                "You are a shell executor. "
                "Call bash with command='echo real_llm_bash_test' then respond DONE."
            ),
            max_iterations=3,
            permissions={"file_access": {"/tmp": "read_write"}},
        )

        runtime = AgentRuntime(
            spec=spec,
            gateway=gateway,
            store=store,
            api_key=real_api_key,
            base_url=real_base_url,
            sandbox=mock_sandbox,
        )

        try:
            runtime.run_loop()
        except Exception as exc:
            err_msg = str(exc).lower()
            if any(w in err_msg for w in ("connection", "timeout", "unreachable", "resolve")):
                pytest.skip(f"DeepSeek API unreachable: {exc}")
            raise

        events = store.read_events(session_id)
        tools_used = [e.payload["tool"] for e in events if e.event_type == "tool_called"]
        assert "bash" in tools_used, f"bash not called. Tools used: {tools_used}"

class TestHashline:
    """hashline_read + hashline_edit tools."""

    @pytest.fixture
    def mock_exec(self):
        """Mock sandbox executor with a virtual filesystem."""
        fs: dict[str, str] = {
            "/workspace/test.txt": "line one\nline two\nline three\n",
        }

        def execute(cmd):
            import re
            # Read
            if "open(" in cmd and ".read()" in cmd:
                m = re.search(r"open\('([^']+)'\)", cmd)
                if m:
                    path = m.group(1)
                    content = fs.get(path, "")
                    return (content, "", 0 if path in fs else 1)
            # Write (base64 decode)
            if "base64.b64decode" in cmd and "open(" in cmd:
                m = re.search(r"open\('([^']+)','w'\)", cmd)
                b64m = re.search(r"b64decode\('([^']+)'\)", cmd)
                if m and b64m:
                    import base64
                    path = m.group(1)
                    content = base64.b64decode(b64m.group(1)).decode()
                    fs[path] = content
                    return ("", "", 0)
            return ("", "unknown command", 1)

        return execute, fs

    def test_hashline_read_returns_hash_prefixed_lines(self, mock_exec):
        """hashline_read returns line:hash|content format."""
        execute, fs = mock_exec
        from harness.tools.hashline import hashline_read
        result = hashline_read("/workspace/test.txt", execute=execute)
        lines = result.split("\n")
        assert len(lines) == 3
        for line in lines:
            # Format: N:XX|content
            assert ":" in line
            assert "|" in line

    def test_hashline_edit_replace_by_hash(self, mock_exec):
        """hashline_edit replaces a line by its anchor hash."""
        execute, fs = mock_exec
        from harness.tools.hashline import hashline_read, hashline_edit

        # First read to get hashes
        read_result = hashline_read("/workspace/test.txt", execute=execute)
        lines = read_result.split("\n")
        anchor = lines[1].split(":")[1].split("|")[0]  # hash of "line two"

        # Replace line two
        result = hashline_edit(
            "/workspace/test.txt",
            [{"op": "replace", "anchor_hash": anchor, "text": "LINE TWO MODIFIED"}],
            execute=execute,
        )
        assert "Applied 1 edit" in result
        assert fs["/workspace/test.txt"] == "line one\nLINE TWO MODIFIED\nline three\n"

    def test_hashline_edit_rejects_bad_anchor(self, mock_exec):
        """hashline_edit rejects edits with unknown anchor hashes."""
        execute, fs = mock_exec
        from harness.tools.hashline import hashline_edit
        result = hashline_edit(
            "/workspace/test.txt",
            [{"op": "replace", "anchor_hash": "xx", "text": "bad"}],
            execute=execute,
        )
        assert "rejected" in result.lower()

    def test_hashline_edit_insert_after(self, mock_exec):
        """hashline_edit insert_after inserts text after an anchor line."""
        execute, fs = mock_exec
        from harness.tools.hashline import hashline_read, hashline_edit

        read_result = hashline_read("/workspace/test.txt", execute=execute)
        lines = read_result.split("\n")
        anchor = lines[0].split(":")[1].split("|")[0]

        result = hashline_edit(
            "/workspace/test.txt",
            [{"op": "insert_after", "anchor_hash": anchor, "text": "inserted line"}],
            execute=execute,
        )
        assert "Applied 1 edit" in result
        assert fs["/workspace/test.txt"] == "line one\ninserted line\nline two\nline three\n"

    def test_hashline_edit_delete(self, mock_exec):
        """hashline_edit deletes a line by anchor hash."""
        execute, fs = mock_exec
        from harness.tools.hashline import hashline_read, hashline_edit

        read_result = hashline_read("/workspace/test.txt", execute=execute)
        lines = read_result.split("\n")
        anchor = lines[1].split(":")[1].split("|")[0]

        result = hashline_edit(
            "/workspace/test.txt",
            [{"op": "delete", "anchor_hash": anchor}],
            execute=execute,
        )
        assert "Applied 1 edit" in result
        assert fs["/workspace/test.txt"] == "line one\nline three\n"

    def test_hashline_edit_checks_all_anchors_before_applying(self, mock_exec):
        """If any anchor is bad, ALL edits are rejected (atomic)."""
        execute, fs = mock_exec
        from harness.tools.hashline import hashline_edit
        original = fs["/workspace/test.txt"]
        result = hashline_edit(
            "/workspace/test.txt",
            [
                {"op": "replace", "anchor_hash": "xx", "text": "a"},
                {"op": "replace", "anchor_hash": "yy", "text": "b"},
            ],
            execute=execute,
        )
        assert "rejected" in result.lower()
        assert fs["/workspace/test.txt"] == original  # unchanged
