"""Tests for harness/audit_log.py — TDD vertical slice.

Verify that AuditLog writes security events to both global and per-session logs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from harness.audit_log import AuditLog
from harness.gateway.gateway import Gateway
from harness.interfaces import AgentSpec
from harness.sandbox.manager import SandboxManager
from harness.session_store.impl import SessionStore


class TestAuditLogGlobal:
    """AuditLog writes events to global audit_log.jsonl."""

    def test_append_creates_global_file(self, tmp_path: Path) -> None:
        """append() creates audit_log.jsonl if it doesn't exist."""
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(
            level="info",
            category="job",
            event="job_created",
            job_id="job_001",
        )

        global_path = tmp_path / "audit_log.jsonl"
        assert global_path.exists()

    def test_append_writes_valid_jsonl(self, tmp_path: Path) -> None:
        """Each append produces one valid JSON line."""
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(
            level="warn",
            category="permission",
            event="file_access_deny",
            session_id="sess_001",
            detail={"path": "/etc/passwd", "reason": "not_allowed"},
        )

        lines = (tmp_path / "audit_log.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["level"] == "warn"
        assert entry["category"] == "permission"
        assert entry["event"] == "file_access_deny"
        assert entry["session_id"] == "sess_001"
        assert entry["detail"]["path"] == "/etc/passwd"

    def test_append_includes_timestamp(self, tmp_path: Path) -> None:
        """Each entry has a ts field close to now."""
        before = time.time()
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(level="info", category="agent", event="agent_created")

        after = time.time()
        lines = (tmp_path / "audit_log.jsonl").read_text().strip().split("\n")
        entry = json.loads(lines[0])

        assert before <= entry["ts"] <= after

    def test_null_fields_when_not_provided(self, tmp_path: Path) -> None:
        """job_id and session_id are null when omitted."""
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(level="info", category="sandbox", event="sandbox_created")

        lines = (tmp_path / "audit_log.jsonl").read_text().strip().split("\n")
        entry = json.loads(lines[0])

        assert entry["job_id"] is None
        assert entry["session_id"] is None
        assert entry["detail"] == {}

    def test_multiple_appends(self, tmp_path: Path) -> None:
        """Multiple appends produce multiple lines."""
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(level="info", category="job", event="job_created", job_id="j1")
        audit.append(level="info", category="job", event="job_completed", job_id="j1")

        lines = (tmp_path / "audit_log.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "job_created"
        assert json.loads(lines[1])["event"] == "job_completed"


class TestAuditLogPerSession:
    """AuditLog writes per-session audit.jsonl when session_id is provided."""

    def test_creates_per_session_file(self, tmp_path: Path) -> None:
        """append(session_id=...) creates sessions/{sid}/audit.jsonl."""
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(
            level="warn",
            category="permission",
            event="file_access_deny",
            session_id="sess_001",
            detail={"path": "/etc/passwd"},
        )

        session_path = tmp_path / "sess_001" / "audit.jsonl"
        assert session_path.exists()

    def test_no_session_file_without_session_id(self, tmp_path: Path) -> None:
        """append without session_id does not create per-session file."""
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(level="info", category="agent", event="agent_created")

        # Only global file should exist
        assert (tmp_path / "audit_log.jsonl").exists()
        assert len(list(tmp_path.glob("*/audit.jsonl"))) == 0

    def test_per_session_appends(self, tmp_path: Path) -> None:
        """Multiple appends to same session produce multiple lines."""
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(
            level="warn", category="permission", event="file_access_deny",
            session_id="sess_002", detail={"path": "/a"},
        )
        audit.append(
            level="warn", category="permission", event="file_access_deny",
            session_id="sess_002", detail={"path": "/b"},
        )

        lines = (tmp_path / "sess_002" / "audit.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["detail"]["path"] == "/a"
        assert json.loads(lines[1])["detail"]["path"] == "/b"

    def test_both_global_and_session_written(self, tmp_path: Path) -> None:
        """One append with session_id writes to both files."""
        audit = AuditLog(base_dir=str(tmp_path))

        audit.append(
            level="info", category="gate", event="gate_approved",
            session_id="sess_003", job_id="job_003",
        )

        global_lines = (tmp_path / "audit_log.jsonl").read_text().strip().split("\n")
        session_lines = (tmp_path / "sess_003" / "audit.jsonl").read_text().strip().split("\n")

        assert len(global_lines) == 1
        assert len(session_lines) == 1
        assert json.loads(global_lines[0]) == json.loads(session_lines[0])


class TestSandboxManagerAudit:
    """SandboxManager records sandbox lifecycle events."""

    def test_sandbox_created_audit(self, tmp_path: Path) -> None:
        """get_or_create writes sandbox_created audit event."""
        audit = AuditLog(base_dir=str(tmp_path))
        from harness.interfaces import SandboxConfig
        mgr = SandboxManager(configs={"main": SandboxConfig(backend="bubblewrap", firewall={"network": "host"})}, audit_log=audit)

        # Use main sandbox instance
        entity = mgr.get_or_create("main")  # I-26: SandboxEntity
        assert entity.sandbox_id

        lines = (tmp_path / "audit_log.jsonl").read_text().strip().split("\n")
        events = [json.loads(line) for line in lines]
        sandbox_events = [e for e in events if e["event"] == "sandbox_created"]
        assert len(sandbox_events) >= 1
        assert sandbox_events[0]["category"] == "sandbox"

    def test_sandbox_destroyed_audit(self, tmp_path: Path) -> None:
        """destroy writes sandbox_destroyed audit event."""
        audit = AuditLog(base_dir=str(tmp_path))
        from harness.interfaces import SandboxConfig
        mgr = SandboxManager(configs={"main": SandboxConfig(backend="bubblewrap", firewall={"network": "host"})}, audit_log=audit)

        entity = mgr.get_or_create("main")
        mgr.destroy(entity.sandbox_id)

        lines = (tmp_path / "audit_log.jsonl").read_text().strip().split("\n")
        events = [json.loads(line) for line in lines]
        destroy_events = [e for e in events if e["event"] == "sandbox_destroyed"]
        assert len(destroy_events) >= 1


class TestAgentRuntimeAudit:
    """AgentRuntime records permission denial events."""

    def test_file_access_deny_audit(self, tmp_path: Path) -> None:
        """_read_file on unpermitted path writes file_access_deny audit."""
        from harness.agent_runtime.impl import AgentRuntime

        audit = AuditLog(base_dir=str(tmp_path))

        # Mock sandbox for testing
        class MockSandbox:
            def spawn(self, config, cap_token=""):
                return "mock-pid"
            def kill(self, container_id):
                pass
            def is_alive(self, container_id):
                return True
            def get_logs(self, container_id):
                return ""
            def execute(self, container_id, command):
                return (f"MOCK: {command}", "", 0)

        # Create a minimal spec with no file_access permissions
        spec = AgentSpec(
            name="test-agent",
            description="test agent",
            model="deepseek-v4-flash",
            system_prompt="test",
            tools=["read_file"],
            permissions={"file_access": {}},
            max_iterations=1,
        )

        cap = Gateway(event_bus=None)
        store = SessionStore(base_path=str(tmp_path / "sessions"))
        store.create_session("sess_denied", {})

        # Trigger file access denial — use a plain list to verify audit entries
        audit_list: list[dict] = []
        runtime = AgentRuntime(
            spec=spec,
            gateway=cap,
            store=store,
            api_key="fake",
            sandbox=MockSandbox(),
            sandbox_instance_id="mock-pid",
            audit_log=audit_list,
        )
        runtime._current_session_id = "sess_denied"
        runtime._inject_tool_deps()
        with pytest.raises(PermissionError, match="not in agent permissions"):
            runtime._tool_registry.get_handler("read_file")("/etc/passwd")

        assert len(audit_list) == 1
        assert audit_list[0]["event"] == "file_access_deny"
        assert audit_list[0]["detail"]["path"] == "/etc/passwd"
