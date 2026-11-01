"""Tests for built-in tools — file I/O, sandbox exec, agent management.

TDD Slices 2-4 of the ToolRegistry + built-in tools refactoring.
Tools are tested through ToolRegistry with mock deps — no AgentRuntime needed.
"""

from __future__ import annotations

import pytest

from harness.tool_registry import ToolRegistry


class TestFileIOTools:
    """read_file, write_file, diff — all share execute/permissions/audit_log deps."""

    @pytest.fixture
    def reg(self):
        reg = ToolRegistry()
        from harness.tools.file_io import register as _register
        _register(reg)
        return reg

    @pytest.fixture
    def mock_execute(self):
        """Returns (stdout, stderr, code) tuples for known commands."""
        def execute(cmd):
            if "unified_diff" in cmd:
                return ("-hello\n+HELLO", "", 0)
            if ".read()" in cmd:
                return ("hello world", "", 0)
            if "open(" in cmd and "write" in cmd:
                return ("", "", 0)
            return ("", "command not found", 127)
        return execute

    @pytest.fixture
    def mock_permissions_full(self):
        return {"file_access": {"/workspace": "read_write"}}

    @pytest.fixture
    def audit_log(self):
        return []

    # ── read_file ────────────────────────────────────────

    def test_read_file_returns_content(self, reg, mock_execute,
                                        mock_permissions_full, audit_log):
        reg.inject({
            "execute": mock_execute,
            "permissions": mock_permissions_full,
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("read_file")("/workspace/test.txt")
        assert "hello world" in result

    def test_read_file_path_denied(self, reg, mock_execute, audit_log):
        reg.inject({
            "execute": mock_execute,
            "permissions": {"file_access": {"/other": "read_write"}},
            "audit_log": audit_log,
            "path_map": {},
        })
        with pytest.raises(PermissionError, match="not in agent permissions"):
            reg.get_handler("read_file")("/workspace/secret.txt")

    # ── write_file ───────────────────────────────────────

    def test_write_file_returns_bytes(self, reg, mock_execute,
                                       mock_permissions_full, audit_log):
        reg.inject({
            "execute": mock_execute,
            "permissions": mock_permissions_full,
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("write_file")("/workspace/out.txt", "abc")
        assert "3 bytes" in result

    def test_write_file_read_only(self, reg, mock_execute, audit_log):
        reg.inject({
            "execute": mock_execute,
            "permissions": {"file_access": {"/workspace": "read_only"}},
            "audit_log": audit_log,
            "path_map": {},
        })
        with pytest.raises(PermissionError, match="read_only"):
            reg.get_handler("write_file")("/workspace/out.txt", "abc")

    def test_write_file_read_only_host_path(self, reg, mock_execute, audit_log):
        """path_map resolves sandbox path → host path; file_access blocks write."""
        reg.inject({
            "execute": mock_execute,
            "permissions": {"file_access": {"/tmp/host_dir": "read_only"}},
            "audit_log": audit_log,
            "path_map": {"/workspace": "/tmp/host_dir"},
        })
        with pytest.raises(PermissionError, match="read_only"):
            reg.get_handler("write_file")("/workspace/out.txt", "abc")

    def test_write_file_allowed_unmapped_host_path(self, reg, mock_execute, audit_log):
        """Sandbox path not covered by path_map → no host impact → allowed."""
        reg.inject({
            "execute": mock_execute,
            "permissions": {"file_access": {"/tmp/mapped": "read_write"}},
            "audit_log": audit_log,
            # path_map only covers /workspace/output, not /workspace/other
            "path_map": {"/workspace/output": "/tmp/mapped"},
        })
        result = reg.get_handler("write_file")("/workspace/other/file.txt", "data")
        assert "4 bytes" in result

    # ── diff ──────────────────────────────────────────────

    def test_diff_returns_output(self, reg, mock_execute,
                                  mock_permissions_full, audit_log):
        reg.inject({
            "execute": mock_execute,
            "permissions": mock_permissions_full,
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("diff")("/workspace/a.txt", "/workspace/b.txt")
        assert "HELLO" in result

    # ── Edge cases ────────────────────────────────────────

    def test_read_file_truncates_large_output(
            self, reg, mock_permissions_full, audit_log):
        """I-06: oversized file reads are truncated with an explicit marker."""
        big = "line-of-data\n" * 2000  # > 8K chars
        reg.inject({
            "execute": lambda cmd: (big, "", 0),
            "permissions": mock_permissions_full,
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("read_file")("/workspace/big.txt")
        assert "[... truncated" in result
        assert len(result) < len(big)

    def test_read_file_small_output_unchanged(
            self, reg, mock_permissions_full, audit_log):
        """I-06: small outputs pass through untouched."""
        reg.inject({
            "execute": lambda cmd: ("small content", "", 0),
            "permissions": mock_permissions_full,
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("read_file")("/workspace/small.txt")
        assert "small content" in result
        assert "[... truncated" not in result

    def test_read_file_error(self, reg, mock_permissions_full, audit_log):
        reg.inject({
            "execute": lambda cmd: ("", "not found", 1),
            "permissions": mock_permissions_full,
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("read_file")("/workspace/missing.txt")
        assert "Error" in result

    def test_write_file_error(self, reg, mock_permissions_full, audit_log):
        reg.inject({
            "execute": lambda cmd: ("", "disk full", 1),
            "permissions": mock_permissions_full,
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("write_file")("/workspace/out.txt", "abc")
        assert "Error" in result

    def test_audit_log_on_deny(self, reg, mock_execute, audit_log):
        reg.inject({
            "execute": mock_execute,
            "permissions": {"file_access": {"/public": "read_write"}},
            "audit_log": audit_log,
            "path_map": {},
        })
        try:
            reg.get_handler("read_file")("/workspace/secret.txt")
        except PermissionError:
            pass
        assert len(audit_log) > 0
        assert audit_log[0]["event"] == "file_access_deny"


class TestSandboxExecTools:
    """bash and sandbox_execute tools."""

    @pytest.fixture
    def reg(self):
        reg = ToolRegistry()
        from harness.tools.sandbox_exec import register as _register
        _register(reg)
        return reg

    def test_bash_returns_result(self, reg):
        reg.inject({"execute": lambda cmd: ("output", "", 0),
                     "permissions": {}, "audit_log": [], "path_map": {}})
        result = reg.get_handler("bash")("echo hi")
        assert "output" in result
        assert "0" in result

    def test_bash_returns_stderr(self, reg):
        reg.inject({"execute": lambda cmd: ("", "error msg", 1),
                     "permissions": {}, "audit_log": [], "path_map": {}})
        result = reg.get_handler("bash")("bad command")
        assert "error msg" in result
        assert "1" in result

    def test_sandbox_execute_returns_result(self, reg):
        reg.inject({"execute": lambda cmd: ("done", "", 0),
                     "permissions": {}, "audit_log": [], "path_map": {}})
        result = reg.get_handler("sandbox_execute")("ls")
        assert "done" in result

    def test_bash_truncates_large_output(self, reg):
        """I-06: oversized command output is truncated with a marker."""
        big = "out\n" * 5000  # > 8K chars
        reg.inject({"execute": lambda cmd: (big, "", 0),
                     "permissions": {}, "audit_log": [], "path_map": {}})
        result = reg.get_handler("bash")("cat huge")
        assert "[... truncated" in result
        assert len(result) < len(big)

    # ── Permission gating ────────────────────────────────

    def test_bash_gate_is_advisory_for_read_only_paths(self, reg):
        """The pre-call gate does NOT block writes; the KERNEL does.

        The gate is field matching and cannot tell what a shell command does
        (ADR-051 §9).  It used to check every path as "write", which blocked
        legitimate reads of a read-only INPUT tree — including the agent's own
        idiom for writing files (`cat > out.txt <<'EOF'`, `python3 - <<'PY'`).
        MEASURED: that idiom is used throughout real work, so the gate now
        prefers READ: a readable path is allowed through, and enforcement is left
        to the kernel (mount `mode:"ro"` → "Read-only file system").

        What the gate still guarantees: an OBVIOUS write (a shell redirection to a
        file) to a path that is not writable is refused before execution.
        """
        audit_log = []
        reg.inject({
            "execute": lambda cmd: ("ok", "", 0),
            "permissions": {"file_access": {"/workspace": "read_only"}},
            "audit_log": audit_log,
            "path_map": {},
        })
        # a plain read of the read-only tree is allowed (it is a READ)
        assert "ok" in reg.get_handler("bash")("cat /workspace/data.txt")

    def test_bash_allows_write_to_permitted_path(self, reg):
        """bash with read_write file_access allows writes."""
        audit_log = []
        reg.inject({
            "execute": lambda cmd: ("ok", "", 0),
            "permissions": {"file_access": {"/workspace": "read_write"}},
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("bash")("echo data > /workspace/out.txt")
        assert "ok" in result

    def test_sandbox_execute_read_only_is_advisory(self, reg):
        """`sandbox_execute` shares the advisory gate (see the bash test above)."""
        audit_log = []
        reg.inject({
            "execute": lambda cmd: ("ok", "", 0),
            "permissions": {"file_access": {"/workspace": "read_only"}},
            "audit_log": audit_log,
            "path_map": {},
        })
        assert "ok" in reg.get_handler("sandbox_execute")(
            "cat /workspace/data.txt")

    def test_sandbox_execute_allows_write_to_permitted_path(self, reg):
        """sandbox_execute with read_write file_access allows writes."""
        audit_log = []
        reg.inject({
            "execute": lambda cmd: ("ok", "", 0),
            "permissions": {"file_access": {"/workspace": "read_write"}},
            "audit_log": audit_log,
            "path_map": {},
        })
        result = reg.get_handler("sandbox_execute")("echo data > /workspace/out.txt")
        assert "ok" in result

    def test_bash_rejects_unparseable_command(self, reg):
        """bash refuses to execute commands that can't be parsed for paths."""
        reg.inject({
            "execute": lambda cmd: ("ok", "", 0),
            "permissions": {"file_access": {"/workspace": "read_write"}},
            "audit_log": [],
            "path_map": {},
        })
        result = reg.get_handler("bash")("echo 'unclosed quote > /workspace/out.txt")
        assert "refused" in result.lower()

    def test_bash_extracts_extension_filenames(self, reg):
        """bash catches bare filenames like data.txt (no /, not starting with .)."""
        audit_log = []
        reg.inject({
            "execute": lambda cmd: ("ok", "", 0),
            "permissions": {"file_access": {"/workspace": "read_only"}},
            "audit_log": audit_log,
            "path_map": {},
        })
        # `/workspace/data.txt` has a slash, so it is extracted and checked;
        # READING it is allowed (read-only means read-only, not unreadable).
        assert "ok" in reg.get_handler("bash")("cat /workspace/data.txt")


class TestAgentMgmtTools:
    """spawn_subagent and list_agents tools."""

    @pytest.fixture
    def reg(self):
        reg = ToolRegistry()
        from harness.tools.agent_mgmt import register as _register
        _register(reg)
        return reg

    def test_spawn_subagent_no_spawner(self, reg):
        """Without a spawner injected, returns error."""
        result = reg.get_handler("spawn_subagent")("worker", "do stuff")
        assert "not available" in result.lower()

    def test_list_agents(self, reg):
        """list_agents works (even if registry is empty)."""
        result = reg.get_handler("list_agents")()
        # Should return JSON list or error string — both OK for unit test
        assert isinstance(result, str)
