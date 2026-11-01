"""Tests for Module 4 — Bubblewrap Sandbox."""

from __future__ import annotations

import json
import os
import signal
import time

import pytest

from harness.interfaces import SandboxConfig, SandboxSpawnError
from harness.sandbox.impl import BubblewrapSandbox


class ProbeSandbox:
    """Stub backend with a controllable readiness probe.

    is_alive is always True (the anchor process exists), so the only way
    get_or_create can detect a broken sandbox is the readiness probe
    (execute).  probe_ok=False simulates a sandbox whose namespace/mounts
    are broken after spawn.
    """

    def __init__(self, probe_ok: bool = True) -> None:
        self.probe_ok = probe_ok
        self.spawn_count = 0
        self.probe_calls = 0
        self.kill_count = 0

    def spawn(self, config: SandboxConfig, sandbox_id: str | None = None) -> str:
        self.spawn_count += 1
        return f"stub-{self.spawn_count}"

    def is_alive(self, container_id: str) -> bool:
        return True

    def execute(self, container_id: str, command: str) -> tuple[str, str, int]:
        self.probe_calls += 1
        if self.probe_ok:
            return ("ready", "", 0)
        return ("", "broken sandbox", 1)

    def kill(self, container_id: str, keep_workspace: bool = False) -> None:
        self.kill_count += 1

    def stop(self, container_id: str, timeout: int = 5) -> None:
        pass


class TestBuildBwrapCmdFlags:
    """Anchor container vs execute command must differ in lifecycle flags.

    Regression: the anchor no-op container carried --die-with-parent, whose
    PDEATHSIG chain is misfired under concurrent spawns — the anchor bwrap
    process gets SIGKILL'd and a concurrent caller respawns it (double
    spawn).  The anchor must keep the whole tree in one process group
    (Popen start_new_session) instead, so killpg() can reap it fully.
    """

    def _cmd(self, extra_command, anchor_container=False):
        from harness.sandbox.impl import _build_bwrap_cmd
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
        )
        return _build_bwrap_cmd(config, None, extra_command, anchor_container)

    def test_anchor_container_omits_pdeathsig_and_new_session(self):
        """The no-op anchor (sleep infinity) must not use --die-with-parent
        or --new-session: both fragment the process tree and let PDEATHSIG
        kill the anchor under concurrency."""
        cmd = self._cmd(["/bin/sh", "-c", "sleep infinity"], anchor_container=True)
        assert "--die-with-parent" not in cmd
        assert "--new-session" not in cmd

    def test_execute_command_keeps_isolated_session(self):
        """execute() commands keep the original flags: each command runs in
        its own session and is cleaned up by PDEATHSIG on parent exit."""
        cmd = self._cmd(["/bin/sh", "-c", "echo ready"])
        assert "--die-with-parent" in cmd
        assert "--new-session" in cmd


class TestOrphanReaper:
    """Orphan anchor cleanup: hosts that died leave anchor trees behind.

    Anchors are tagged with HARNESS_SANDBOX_ANCHOR=1 so a later process
    can find and reap them.  The reaper scans /proc for tagged bwrap trees
    whose parent is dead (reparented to init/systemd), and reaps the whole
    tree by its process group.
    """

    def _fake_proc_lines(self, env_tag):
        """Build fake /proc/<pid>/environ style content for a tagged process."""
        return f"\x00HARNESS_SANDBOX_ANCHOR={env_tag}\x00PATH=/bin\x00"

    def test_anchor_cmd_injects_env_tag(self):
        """spawn()'s anchor command carries HARNESS_SANDBOX_ANCHOR so the
        reaper can find it later.  execute() commands must NOT carry it
        (they are short-lived, not anchors)."""
        from harness.sandbox.impl import _build_bwrap_cmd
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
        )
        anchor_cmd = _build_bwrap_cmd(config, None, ["/bin/sh", "-c", "sleep infinity"], anchor_container=True)
        exec_cmd = _build_bwrap_cmd(config, None, ["/bin/sh", "-c", "echo ready"])
        # bwrap uses --setenv KEY VALUE (two args), not KEY=VALUE
        assert "HARNESS_SANDBOX_ANCHOR" in anchor_cmd
        assert "1" in anchor_cmd
        assert "HARNESS_SANDBOX_ANCHOR" not in exec_cmd

    def test_reap_orphans_kills_tagged_tree(self, monkeypatch):
        """reap_orphan_anchors() reaps every tagged tree whose parent is dead.

        Fake the whole /proc scan + killpg so the test is deterministic and
        doesn't depend on real bwrap leftovers.
        """
        import harness.sandbox.impl as impl
        killed = []

        # The env tag lives on the sandbox-layer process (child of the anchor
        # root), while the reap target is its process group (the tree root).
        # Here: pid 4242 is the tagged child, pgid 4240 is its tree root;
        # 4343 is a live tagged tree.  4444 represents an untagged process
        # that _scan_tagged_processes already filtered out (not in result).
        fake_procs = {
            4242: {"ppid": 4240, "env": self._fake_proc_lines("1"), "pgid": 4240},
            4343: {"ppid": 5555, "env": self._fake_proc_lines("1"), "pgid": 4343},
        }
        monkeypatch.setattr(impl, "_scan_tagged_processes", lambda: fake_procs)
        monkeypatch.setattr(
            impl, "_killpg", lambda pgid, sig: killed.append(pgid))
        # pgid 4240's leader was reparented (orphan, ppid != us); 4343's
        # leader is still owned by the current harness process; 4444 is
        # untagged and its leader ppid must not matter.
        monkeypatch.setattr(
            impl, "_pgid_leader_ppid",
            lambda pgid: 1166 if pgid == 4240 else os.getpid() if pgid == 4343 else 4444)

        impl.reap_orphan_anchors()

        assert 4240 in killed, "orphan tree must be reaped by pgid"
        assert 4343 not in killed, "live tree must survive"
        assert len(killed) == 1, "only the orphan tree is reaped"

    def test_reap_orphans_end_to_end(self):
        """Real flow: a child process spawns an anchor then exits; the parent
        reaps the leftover tree.

        This is the core health-check scenario the reaper exists for: a host
        that died without cleanup leaves anchor trees behind, and the next
        process cleans them up.
        """
        import subprocess
        import sys
        import time

        # Child: spawn an anchor, print its pid, then exit without cleanup
        # (simulating a host crash).  Keep the anchor alive briefly so the
        # parent can observe it reparented to init/systemd.
        code = """
import time
from harness.sandbox.impl import BubblewrapSandbox
from harness.interfaces import SandboxConfig
sb = BubblewrapSandbox()
cfg = SandboxConfig(backend="bubblewrap", firewall={"network": "host"}, shared_folders=[], ttl_seconds=0)
pid = sb.spawn(cfg)
time.sleep(0.1)
print(pid, flush=True)
time.sleep(3)
"""
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30,
        )
        anchor_pid = int(out.stdout.strip().splitlines()[-1])
        time.sleep(0.5)

        # Confirm it is now an orphan: parent is no longer the child python.
        from harness.sandbox.impl import _pgid_leader_ppid, reap_orphan_anchors
        pgid = anchor_pid
        root_ppid = _pgid_leader_ppid(pgid)
        assert root_ppid != os.getpid(), "anchor should be reparented by now"

        n = reap_orphan_anchors()
        assert n >= 1, "reaper should find and reap the orphan tree"
        time.sleep(0.3)

        try:
            os.kill(anchor_pid, 0)
            raise AssertionError(f"anchor {anchor_pid} still alive after reap")
        except ProcessLookupError:
            pass  # reaped

        # And the anchor's sleep child is gone too (whole tree killed by pgid).
        left = subprocess.run(
            ["ps", "-eo", "pid,cmd"], capture_output=True, text=True).stdout
        assert not any(
            "sleep infinity" in l and str(anchor_pid) in l
            for l in left.splitlines()
        ), "anchor's sleep child must be reaped with the tree"

    def test_manager_reap_orphans_delegates_to_impl(self, monkeypatch):
        """SandboxManager.reap_orphans() delegates to the impl-level reaper
        and returns the number of trees reaped."""
        import harness.sandbox.manager as sm
        from harness.sandbox.manager import SandboxManager

        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
        )
        manager = SandboxManager(configs={"test": config})
        import harness.sandbox.impl as impl
        monkeypatch.setattr(impl, "reap_orphan_anchors", lambda: 3)

        assert manager.reap_orphans() == 3


@pytest.fixture
def sandbox() -> BubblewrapSandbox:
    return BubblewrapSandbox()


@pytest.fixture
def sandbox_config() -> SandboxConfig:
    return SandboxConfig(
        backend="bubblewrap",
        firewall={"network": "host"},
        shared_folders=[{"host_path": "/tmp", "sandbox_path": "/tmp"}],
    )


# ─────────────────────────────────────────────────────────────
# Sandbox existence check
# ─────────────────────────────────────────────────────────────


def test_bwrap_available():
    """Verify bwrap is installed and accessible."""
    import shutil
    assert shutil.which("bwrap") is not None, "bwrap must be installed"


# ─────────────────────────────────────────────────────────────
# Spawn tests
# ─────────────────────────────────────────────────────────────


class TestSpawn:
    def test_spawn_returns_pid(self, sandbox, sandbox_config):
        """Spawn should return a non-empty PID string."""
        pid = sandbox.spawn(sandbox_config)
        assert pid
        assert pid.isdigit()
        # Cleanup
        sandbox.kill(pid)

    def test_spawn_injects_env_vars(self, sandbox, sandbox_config):
        """AGENT_CAP_TOKEN / AGENT_SESSION_ID no longer injected (Phase 2: host runtime)."""
        import uuid
        sid = f"test-bwrap-{uuid.uuid4().hex[:8]}"
        config = SandboxConfig(
            backend="bubblewrap", firewall={"network": "host"},
            shared_folders=[{"host_path": "/tmp", "sandbox_path": "/workspace"}],
            session_id=sid,
        )

        pid = sandbox.spawn(config)
        time.sleep(0.3)

        stdout, _, code = sandbox.execute(
            pid,
            "echo CAP=${AGENT_CAP_TOKEN:-ABSENT} SESSION=${AGENT_SESSION_ID:-ABSENT}"
        )
        assert code == 0
        assert "CAP=ABSENT" in stdout, (
            f"AGENT_CAP_TOKEN should not leak into sandbox, got: {stdout!r}"
        )
        assert "SESSION=ABSENT" in stdout, (
            f"AGENT_SESSION_ID should not leak into sandbox, got: {stdout!r}"
        )

        sandbox.kill(pid)

    def test_spawn_with_no_session_id(self, sandbox):
        """Spawn should work even without a session directory."""
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
        )
        pid = sandbox.spawn(config)
        assert pid
        assert sandbox.is_alive(pid)
        sandbox.kill(pid)

    def test_spawn_with_empty_shared_folders(self, sandbox):
        """Spawn should work with empty shared_folders (no project root mount needed)."""
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
        )
        pid = sandbox.spawn(config)
        assert pid
        assert sandbox.is_alive(pid)
        sandbox.kill(pid)

    def test_spawn_process_actually_runs(self, sandbox, sandbox_config):
        """The spawned process should be alive immediately after spawn."""
        pid = sandbox.spawn(sandbox_config)
        assert sandbox.is_alive(pid)
        sandbox.kill(pid)

    def test_system_mounts_present_with_shared_folders(self, sandbox):
        """System dirs (/usr, /bin, /lib) must be mounted even when
        shared_folders is non-empty (regression test for mutual exclusion bug).
        """
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[{"host_path": "/tmp", "sandbox_path": "/workspace"}],
        )
        pid = sandbox.spawn(config)
        time.sleep(0.3)

        # /usr/bin/python and /bin/sh should both exist
        stdout, _, code = sandbox.execute(pid, "ls /usr/bin/python* /bin/sh")
        sandbox.kill(pid)

        assert code == 0, f"System mounts missing: {stdout}"
        assert "/bin/sh" in stdout

    def test_session_dir_not_double_bound_when_workspace_in_shared_folders(self, sandbox):
        """If shared_folders already maps to /workspace, session_dir should
        not create a conflicting second bind (regression test).
        """
        import tempfile, uuid
        shared_dir = tempfile.mkdtemp(prefix="bwrap-ws-")
        sid = f"test-bwrap-{uuid.uuid4().hex[:8]}"

        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[{"host_path": shared_dir, "sandbox_path": "/workspace"}],
            session_id=sid,
        )
        pid = sandbox.spawn(config)
        time.sleep(0.3)

        # Write through shared_folders mount, verify visible
        marker = f"marker-{uuid.uuid4().hex[:6]}"
        stdout, _, code = sandbox.execute(pid, f"echo {marker} > /workspace/test.txt && cat /workspace/test.txt")
        sandbox.kill(pid)

        assert code == 0, f"Double bind conflict: {stdout}"
        assert marker in stdout


# ─────────────────────────────────────────────────────────────
# Lifecycle tests
# ─────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_is_alive_after_kill(self, sandbox, sandbox_config):
        """After kill, is_alive should return False."""
        pid = sandbox.spawn(sandbox_config)
        sandbox.kill(pid)
        time.sleep(0.2)
        assert not sandbox.is_alive(pid)

    def test_kill_idempotent(self, sandbox, sandbox_config):
        """Killing twice should not raise."""
        pid = sandbox.spawn(sandbox_config)
        sandbox.kill(pid)
        sandbox.kill(pid)  # Should not raise

    def test_get_logs_from_killed_process(self, sandbox, sandbox_config):
        """Logs should be retrievable after process is killed — even if empty."""
        pid = sandbox.spawn(sandbox_config)
        time.sleep(1.0)
        sandbox.kill(pid)
        logs = sandbox.get_logs(pid)
        # get_logs returns stdout/stderr; AgentRuntime writes to SessionStore, 
        # not stdout, so empty logs is valid. Just verify no error.
        assert isinstance(logs, str)

    def test_is_alive_nonexistent(self, sandbox):
        """Non-existent PID should return False."""
        assert not sandbox.is_alive("99999999")

    def test_get_logs_nonexistent(self, sandbox):
        """Non-existent PID should return empty logs."""
        assert sandbox.get_logs("99999999") == ""

    def test_kill_nonexistent(self, sandbox):
        """Killing non-existent PID should not raise."""
        sandbox.kill("99999999")  # Should not raise


# ─────────────────────────────────────────────────────────────
# I-02: Docker backend kill signature must match SandboxInterface
# (manager.py calls backend.kill(id, keep_workspace=...) on config
# change / respawn — a missing parameter crashes that path).
# ─────────────────────────────────────────────────────────────


class TestDockerKillSignature:
    """DockerSandbox.kill must accept keep_workspace (interface contract)."""

    def test_kill_accepts_keep_workspace(self, monkeypatch):
        """kill(id, keep_workspace=...) must not raise TypeError."""
        import types
        from harness.sandbox.docker_impl import DockerSandbox

        sb = DockerSandbox()

        class FakeContainer:
            def stop(self, timeout=10):
                pass

            def remove(self, force=False):
                pass

        class FakeClient:
            def __init__(self):
                self.containers = types.SimpleNamespace(
                    get=lambda cid: FakeContainer())

        monkeypatch.setattr("harness.sandbox.docker_impl.docker.from_env",
                            lambda: FakeClient())

        # Must not raise: manager passes keep_workspace on config-change path
        sb.kill("container-1", keep_workspace=True)
        sb.kill("container-2", keep_workspace=False)

    def test_kill_signature_has_keep_workspace(self):
        """Interface-level check: keep_workspace is part of the signature."""
        import inspect
        from harness.sandbox.docker_impl import DockerSandbox
        params = inspect.signature(DockerSandbox.kill).parameters
        assert "keep_workspace" in params


# ─────────────────────────────────────────────────────────────
# Execute tests
# ─────────────────────────────────────────────────────────────


class TestExecute:
    def test_execute_echo(self, sandbox, sandbox_config):
        """Execute should run a command inside the sandbox."""
        pid = sandbox.spawn(sandbox_config)
        time.sleep(0.3)

        stdout, stderr, exit_code = sandbox.execute(pid, "echo hello-from-sandbox")
        sandbox.kill(pid)

        assert exit_code == 0
        assert "hello-from-sandbox" in stdout

    def test_execute_python(self, sandbox, sandbox_config):
        """Execute should be able to run Python."""
        pid = sandbox.spawn(sandbox_config)
        time.sleep(0.3)

        stdout, stderr, exit_code = sandbox.execute(
            pid, "python -c 'print(1+1)'"
        )
        sandbox.kill(pid)

        assert exit_code == 0
        assert "2" in stdout

    def test_execute_failing_command(self, sandbox, sandbox_config):
        """A failing command should return non-zero exit code."""
        pid = sandbox.spawn(sandbox_config)
        time.sleep(0.3)

        stdout, stderr, exit_code = sandbox.execute(
            pid, "exit 42"
        )
        sandbox.kill(pid)

        assert exit_code == 42

    def test_shared_folder_accessible_in_sandbox(self, sandbox):
        """Files created on the host in a shared folder are visible inside."""
        import tempfile, uuid

        # Create a temp dir that both host and sandbox can see
        shared_dir = tempfile.mkdtemp(prefix="bwrap-shared-")
        marker_file = os.path.join(shared_dir, "marker.txt")
        marker_content = f"secret-{uuid.uuid4().hex[:6]}"

        with open(marker_file, "w") as f:
            f.write(marker_content)

        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[{"host_path": shared_dir, "sandbox_path": shared_dir}],
        )

        pid = sandbox.spawn(config)
        time.sleep(0.5)

        stdout, stderr, exit_code = sandbox.execute(
            pid, f"cat {marker_file}"
        )
        sandbox.kill(pid)

        assert exit_code == 0, f"stderr: {stderr}"
        assert marker_content in stdout


# ─────────────────────────────────────────────────────────────
# SandboxManager tests
# ─────────────────────────────────────────────────────────────

class TestSandboxManager:
    """SandboxManager get_or_create reuses / respawns logic."""

    def test_get_or_create_reuses_alive_instance(self):
        """When instance is alive, get_or_create returns the same one."""
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.sandbox.manager import SandboxManager

        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
        )
        manager = SandboxManager(configs={"test": config})

        e1 = manager.get_or_create("test")
        assert isinstance(e1.backend, BubblewrapSandbox)
        assert e1.backend.is_alive(e1.sandbox_id)

        e2 = manager.get_or_create("test")
        assert e2.backend is e1.backend
        assert e2.sandbox_id == e1.sandbox_id  # canonical reused

        manager.destroy_all()

    def test_get_or_create_respawns_dead_instance(self):
        """When instance is killed externally, get_or_create spawns a new one."""
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.sandbox.manager import SandboxManager

        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
        )
        manager = SandboxManager(configs={"test": config})

        e1 = manager.get_or_create("test")
        assert e1.backend.is_alive(e1.sandbox_id)

        # Kill the sandbox externally (simulate OOM / crash)
        e1.backend.kill(e1.sandbox_id)
        time.sleep(0.2)
        assert not e1.backend.is_alive(e1.sandbox_id)

        # I-26: manager respawns under the SAME sandbox_id (uuid stable)
        e2 = manager.get_or_create("test")
        assert isinstance(e2.backend, BubblewrapSandbox)
        assert e2.sandbox_id == e1.sandbox_id
        assert e2.backend.is_alive(e2.sandbox_id)

        manager.destroy_all()

    def test_get_or_create_logs_respawn_event_to_audit_log(self):
        """When respawning a dead instance, audit_log gets sandbox_respawned."""
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.sandbox.manager import SandboxManager
        from harness.audit_log import AuditLog
        import tempfile

        tmpdir = tempfile.mkdtemp()
        audit = AuditLog(base_dir=tmpdir)
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
        )
        manager = SandboxManager(configs={"test": config}, audit_log=audit)

        e1 = manager.get_or_create("test")
        # Kill externally (simulate OOM/crash) — don't call destroy()
        e1.backend.kill(e1.sandbox_id)
        time.sleep(0.2)

        # get_or_create detects death and respawns (same uuid)
        e2 = manager.get_or_create("test")
        assert e2.sandbox_id == e1.sandbox_id

        # audit should contain: created, respawned
        with open(audit._global_path, "r") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        events = [l["event"] for l in lines]
        assert "sandbox_respawned" in events
        assert events[-1] == "sandbox_respawned"

        manager.destroy_all()

    def test_get_or_create_thread_safe(self):
        """Concurrent get_or_create for same name spawns exactly one sandbox."""
        import threading
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.sandbox.manager import SandboxManager

        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
        )
        manager = SandboxManager(configs={"test": config})
        spawn_count = [0]
        orig_spawn = BubblewrapSandbox.spawn

        def counting_spawn(self, cfg, sandbox_id=None):
            spawn_count[0] += 1
            return orig_spawn(self, cfg, sandbox_id)

        BubblewrapSandbox.spawn = counting_spawn
        try:
            results = []
            def create():
                results.append(manager.get_or_create("test"))

            threads = [threading.Thread(target=create) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # All threads got a result
            assert len(results) == 4
            # Only ONE sandbox was spawned
            assert spawn_count[0] == 1, f"Expected 1 spawn, got {spawn_count[0]}"
            # All returned the same canonical entity (same sandbox_id)
            assert len({r.sandbox_id for r in results}) == 1
        finally:
            BubblewrapSandbox.spawn = orig_spawn
            manager.destroy_all()

    def test_get_or_create_probe_failure_is_spawn_failure(self, monkeypatch):
        """A sandbox whose readiness probe fails must not be registered.

        is_alive() only proves the anchor process was spawned; it cannot
        detect a broken namespace.  The readiness probe (execute) is the
        ground truth: probe failure = spawn failure = SandboxSpawnError.
        """
        import harness.sandbox.manager as sm
        from harness.sandbox.manager import SandboxManager

        stub = ProbeSandbox(probe_ok=False)
        config = SandboxConfig(
            backend="stub",
            firewall={"network": "host"},
            shared_folders=[],
        )
        monkeypatch.setattr(sm, "get_sandbox", lambda _: stub)
        manager = SandboxManager(configs={"test": config})

        with pytest.raises(SandboxSpawnError):
            manager.get_or_create("test")

        # Failed probe → no registration, spawned instance cleaned up.
        assert stub.spawn_count == 1
        assert stub.probe_calls == 1
        assert stub.kill_count == 1

        # Manager is still consistent: retry spawns again, not a stale instance.
        with pytest.raises(SandboxSpawnError):
            manager.get_or_create("test")
        assert stub.spawn_count == 2

    def test_get_or_create_registers_only_after_probe_succeeds(self, monkeypatch):
        """Successful readiness probe is the precondition for registration.

        The probe result is the contract: get_or_create returns an
        instance that can actually execute commands.
        """
        import harness.sandbox.manager as sm
        from harness.sandbox.manager import SandboxManager

        stub = ProbeSandbox(probe_ok=True)
        config = SandboxConfig(
            backend="stub",
            firewall={"network": "host"},
            shared_folders=[],
        )
        monkeypatch.setattr(sm, "get_sandbox", lambda _: stub)
        manager = SandboxManager(configs={"test": config})

        e = manager.get_or_create("test")

        assert stub.spawn_count == 1
        assert stub.probe_calls == 1
        assert stub.kill_count == 0
        assert e.backend.is_alive(e.sandbox_id)

        manager.destroy_all()


# ─────────────────────────────────────────────────────────────
# Stop + TTL tests
# ─────────────────────────────────────────────────────────────


    def test_workspace_files_survive_unexpected_death(self):
        """Workspace files persist after sandbox crash and re-spawn."""
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.sandbox.manager import SandboxManager
        import tempfile, shutil

        ws_dir = tempfile.mkdtemp()
        try:
            config = SandboxConfig(
                backend="bubblewrap", firewall={"network": "host"},
                shared_folders=[{"host_path": ws_dir, "sandbox_path": "/workspace"}],
                ttl_seconds=0,
            )
            manager = SandboxManager(configs={"test": config})
            e1 = manager.get_or_create("test")
            e1.backend.execute(e1.sandbox_id,
                               "echo recovery_test > /workspace/marker.txt")

            # Kill sandbox preserving workspace
            e1.backend.kill(e1.sandbox_id, keep_workspace=True)
            # I-26: respawn under the same sandbox_id
            e2 = manager.get_or_create("test")
            assert e2.sandbox_id == e1.sandbox_id
            stdout, _, _ = e2.backend.execute(
                e2.sandbox_id, "cat /workspace/marker.txt")
            assert "recovery_test" in stdout
            manager.destroy_all()
        finally:
            shutil.rmtree(ws_dir, ignore_errors=True)

    def test_workspace_deleted_on_config_change(self):
        """Config change re-spawn deletes old workspace."""
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.sandbox.manager import SandboxManager
        import tempfile, shutil

        ws_dir = tempfile.mkdtemp()
        try:
            config = SandboxConfig(
                backend="bubblewrap", firewall={"network": "host"},
                shared_folders=[{"host_path": ws_dir, "sandbox_path": "/workspace"}],
                ttl_seconds=0,
            )
            manager = SandboxManager(configs={"test": config})
            e1 = manager.get_or_create("test")
            e1.backend.execute(e1.sandbox_id, "echo stale > /workspace/old.txt")

            # Change config
            manager._configs["test"] = SandboxConfig(
                backend="bubblewrap", firewall={"network": "none"},
                shared_folders=[], ttl_seconds=0,
            )
            e2 = manager.get_or_create("test")  # respawn same uuid, new config
            assert e2.sandbox_id == e1.sandbox_id
            stdout, _, _ = e2.backend.execute(
                e2.sandbox_id, "ls /workspace/old.txt 2>&1 || echo NOT_FOUND")
            assert "NOT_FOUND" in stdout
            manager.destroy_all()
        finally:
            shutil.rmtree(ws_dir, ignore_errors=True)

class TestStop:
    """Sandbox stop() — graceful shutdown."""

    def test_stop_terminates_process_gracefully(self, sandbox, sandbox_config):
        """stop() should send SIGTERM and wait for process to exit."""
        pid = sandbox.spawn(sandbox_config)
        assert sandbox.is_alive(pid)

        sandbox.stop(pid)
        time.sleep(0.5)

        assert not sandbox.is_alive(pid)

    def test_stop_is_idempotent(self, sandbox, sandbox_config):
        """stop() twice should not raise."""
        pid = sandbox.spawn(sandbox_config)
        sandbox.stop(pid)
        sandbox.stop(pid)  # should not raise


class TestTTL:
    """Sandbox TTL — automatic termination after timeout."""

    def test_ttl_expires_and_kills_sandbox(self, sandbox):
        """Sandbox with short TTL should be killed automatically."""
        # Speed up reaper for test
        sandbox._REAPER_INTERVAL = 0.5
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
            ttl_seconds=1,  # 1 second TTL
        )
        pid = sandbox.spawn(config)
        assert sandbox.is_alive(pid)

        # Wait for TTL + scan interval
        time.sleep(2.5)

        assert not sandbox.is_alive(pid)

    def test_execute_resets_idle_timer(self, sandbox):
        """Calling execute() should reset the idle timer."""
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
            ttl_seconds=2,
            idle_timeout=1,
        )
        pid = sandbox.spawn(config)

        # Keep calling execute() to reset idle timer
        for _ in range(5):
            time.sleep(0.5)
            stdout, _, code = sandbox.execute(pid, "echo ping")
            assert code == 0
            assert sandbox.is_alive(pid)

        sandbox.kill(pid)

    def test_sandbox_without_ttl_never_expires(self, sandbox):
        """Sandbox with ttl_seconds=0 should never auto-expire."""
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
            ttl_seconds=0,  # disabled
        )
        pid = sandbox.spawn(config)
        time.sleep(1)
        assert sandbox.is_alive(pid)
        sandbox.kill(pid)


# ─────────────────────────────────────────────────────────────
# Session / Workspace isolation tests
# ─────────────────────────────────────────────────────────────

class TestWorkspaceIsolation:
    """Session store must be invisible inside sandbox; workspace must be writable."""

    def test_session_files_not_visible_in_sandbox(self, sandbox):
        """meta.json and events.jsonl must NOT be visible inside the sandbox."""
        import uuid
        sid = f"test-isolation-{uuid.uuid4().hex[:8]}"
        from harness.session_store.impl import SessionStore
        store = SessionStore(base_path='./sessions/')
        store.create_session(sid, {'test': True})

        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
            session_id=sid,
        )
        pid = sandbox.spawn(config)
        time.sleep(0.3)

        # Check /workspace contents — should not contain session files
        stdout, _, code = sandbox.execute(pid, "ls /workspace/")
        sandbox.kill(pid)

        assert code == 0
        assert "meta.json" not in stdout
        assert "events.jsonl" not in stdout
        assert "checkpoints" not in stdout

    def test_workspace_writable_by_agent(self, sandbox):
        """Agent should be able to write files to /workspace/."""
        import uuid
        sid = f"test-write-{uuid.uuid4().hex[:8]}"
        from harness.session_store.impl import SessionStore
        store = SessionStore(base_path='./sessions/')
        store.create_session(sid, {'test': True})

        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            shared_folders=[],
            session_id=sid,
        )
        pid = sandbox.spawn(config)
        time.sleep(0.3)

        # Write through sandbox
        stdout, _, code = sandbox.execute(
            pid, "echo 'agent-data' > /workspace/agent-file.txt && ls /workspace/"
        )

        assert code == 0, f"Command failed: {stdout}"
        # Verify on host filesystem BEFORE kill (cleanup removes workspace)
        ws_path = f"./workspace/{sid}/agent-file.txt"
        assert os.path.exists(ws_path), f"File not found at {ws_path}"
        with open(ws_path) as f:
            assert "agent-data" in f.read()

        sandbox.kill(pid)


# ─────────────────────────────────────────────────────────────
# Firewall propagation tests
# ─────────────────────────────────────────────────────────────


class TestFirewallPropagation:
    """execute() must respect the parent sandbox's firewall setting."""

    def test_isolated_sandbox_blocks_network(self):
        """Spawn with network:"none" → execute() fails on network commands."""
        from harness.sandbox.impl import BubblewrapSandbox

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "none"},
            ttl_seconds=0,
        )
        pid = sandbox.spawn(config)
        try:
            stdout, stderr, code = sandbox.execute(
                pid, "curl -s --connect-timeout 1 https://httpbin.org/ip 2>&1 || echo BLOCKED"
            )
            # Network isolated: command should fail, BLOCKED should appear
            assert "BLOCKED" in stdout, (
                f"Expected network to be blocked, got stdout={stdout!r} stderr={stderr!r}"
            )
        finally:
            sandbox.kill(pid)

    def test_host_network_allows_connections(self):
        """Spawn with default network → execute() allows network access."""
        from harness.sandbox.impl import BubblewrapSandbox

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "host"},
            ttl_seconds=0,
        )
        pid = sandbox.spawn(config)
        try:
            stdout, stderr, code = sandbox.execute(
                pid, "curl -s --connect-timeout 10 https://httpbin.org/ip 2>&1 || echo NO_NET"
            )
            # Network available: NO_NET must NOT appear
            assert "NO_NET" not in stdout, (
                f"Expected network to work, got stdout={stdout!r}"
            )
        finally:
            sandbox.kill(pid)


class TestSharedFolderMode:
    """shared_folders mode: "ro" → --ro-bind, default → --bind."""

    def test_ro_mode_blocks_writes(self):
        """mode="ro" → kernel-level write protection."""
        from harness.sandbox.impl import BubblewrapSandbox
        import tempfile, shutil

        ro_dir = tempfile.mkdtemp()
        rw_dir = tempfile.mkdtemp()
        try:
            sandbox = BubblewrapSandbox()
            config = SandboxConfig(
                backend="bubblewrap",
                firewall={"network": "none"},
                shared_folders=[
                    {"host_path": ro_dir, "sandbox_path": "/mnt/ro",
                     "mode": "ro"},
                    {"host_path": rw_dir, "sandbox_path": "/mnt/rw"},
                ],
                ttl_seconds=0,
            )
            pid = sandbox.spawn(config)
            try:
                # Read-only mount: write must fail
                stdout, _, code = sandbox.execute(
                    pid,
                    "echo data > /mnt/ro/out.txt 2>&1; "
                    "echo EXIT:$?",
                )
                assert "EXIT:0" not in stdout, (
                    f"Write to ro mount should fail, got: {stdout!r}"
                )

                # Read-write mount: write must succeed
                stdout, _, code = sandbox.execute(
                    pid,
                    "echo data > /mnt/rw/ok.txt && echo OK",
                )
                assert "OK" in stdout, (
                    f"Write to rw mount should succeed, got: {stdout!r}"
                )
            finally:
                sandbox.kill(pid)
        finally:
            shutil.rmtree(ro_dir, ignore_errors=True)
            shutil.rmtree(rw_dir, ignore_errors=True)

    def test_ro_mode_case_insensitive(self):
        """mode="RO", "Ro" should also be treated as read-only."""
        from harness.sandbox.impl import BubblewrapSandbox
        import tempfile, shutil

        ro_dir = tempfile.mkdtemp()
        try:
            sandbox = BubblewrapSandbox()
            config = SandboxConfig(
                backend="bubblewrap",
                firewall={"network": "none"},
                shared_folders=[
                    {"host_path": ro_dir, "sandbox_path": "/mnt/ro",
                     "mode": "RO"},
                ],
                ttl_seconds=0,
            )
            pid = sandbox.spawn(config)
            try:
                stdout, _, code = sandbox.execute(
                    pid,
                    "echo data > /mnt/ro/out.txt 2>&1; "
                    "echo EXIT:$?",
                )
                assert "EXIT:0" not in stdout, (
                    f"Write to RO mount should fail, got: {stdout!r}"
                )
            finally:
                sandbox.kill(pid)
        finally:
            shutil.rmtree(ro_dir, ignore_errors=True)

    def test_file_access_rw_but_kernel_ro_blocks_writes(self):
        """Defence-in-depth: _check_access passes but kernel --ro-bind blocks.

        file_access grants read_write → Python gate passes.
        mode:"ro"               → kernel gate blocks.
        Both write_file and bash must fail; host file must stay unchanged.
        """
        from harness.sandbox.impl import BubblewrapSandbox
        from harness.tool_registry import ToolRegistry
        from harness.tools.file_io import register as register_fileio
        from harness.tools.sandbox_exec import register as register_sandbox_exec
        import tempfile, shutil

        ro_dir = tempfile.mkdtemp()
        try:
            sandbox = BubblewrapSandbox()
            config = SandboxConfig(
                backend="bubblewrap",
                firewall={"network": "none"},
                shared_folders=[
                    {"host_path": ro_dir, "sandbox_path": "/mnt/ro",
                     "mode": "ro"},
                ],
                ttl_seconds=0,
            )
            pid = sandbox.spawn(config)
            try:
                reg = ToolRegistry()
                register_fileio(reg)
                register_sandbox_exec(reg)

                audit_log = []
                reg.inject({
                    "execute": lambda cmd: sandbox.execute(pid, cmd),
                    "permissions": {"file_access": {ro_dir: "read_write"}},
                    "audit_log": audit_log,
                    "path_map": {"/mnt/ro": ro_dir},
                })

                # _check_access should pass (read_write allowed)
                result = reg.get_handler("write_file")("/mnt/ro/out.txt", "hello")
                assert "Error" in result, (
                    f"Expected kernel to block write_file despite read_write, "
                    f"got: {result!r}"
                )
                assert "Wrote" not in result, (
                    f"write_file should not have succeeded: {result!r}"
                )

                # bash should also be blocked
                stdout, _, _ = sandbox.execute(
                    pid,
                    "echo data > /mnt/ro/out.txt 2>&1; echo EXIT:$?",
                )
                assert "EXIT:0" not in stdout, (
                    f"bash write to ro mount should fail, got: {stdout!r}"
                )

                # Host file must be unchanged
                host_file = os.path.join(ro_dir, "out.txt")
                assert not os.path.exists(host_file), (
                    f"File was created on host despite ro mount: {host_file}"
                )
            finally:
                sandbox.kill(pid)
        finally:
            shutil.rmtree(ro_dir, ignore_errors=True)


class TestSandboxEnvVars:
    """SandboxConfig.env_vars injects custom environment variables."""

    def test_env_vars_injected(self):
        """User env_vars appear in sandbox environment."""
        from harness.sandbox.impl import BubblewrapSandbox

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "none"},
            env_vars={
                "MY_VAR": "hello-sandbox",
            },
            ttl_seconds=0,
        )
        pid = sandbox.spawn(config)
        try:
            stdout, _, code = sandbox.execute(
                pid, "echo MY_VAR=$MY_VAR"
            )
            assert "MY_VAR=hello-sandbox" in stdout, (
                f"env_var not injected, got: {stdout!r}"
            )
        finally:
            sandbox.kill(pid)

    def test_env_vars_override_path(self):
        """User can override PATH env var."""
        from harness.sandbox.impl import BubblewrapSandbox

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "none"},
            env_vars={
                "PATH": "/custom/bin:/usr/bin:/bin",
            },
            ttl_seconds=0,
        )
        pid = sandbox.spawn(config)
        try:
            stdout, _, code = sandbox.execute(
                pid, "echo $PATH"
            )
            assert stdout.startswith("/custom/bin"), (
                f"PATH not overridden, got: {stdout!r}"
            )
        finally:
            sandbox.kill(pid)

    def test_dead_security_vars_absent(self):
        """AGENT_CAP_TOKEN / AGENT_SESSION_ID no longer injected into sandbox."""
        from harness.sandbox.impl import BubblewrapSandbox

        sandbox = BubblewrapSandbox()
        config = SandboxConfig(
            backend="bubblewrap",
            firewall={"network": "none"},
            ttl_seconds=0,
        )
        pid = sandbox.spawn(config)
        try:
            stdout, _, _ = sandbox.execute(
                pid,
                "echo CAP=${AGENT_CAP_TOKEN:-ABSENT} "
                "SESSION=${AGENT_SESSION_ID:-ABSENT}"
            )
            assert "CAP=ABSENT" in stdout, (
                f"AGENT_CAP_TOKEN leaked into sandbox, got: {stdout!r}"
            )
            assert "SESSION=ABSENT" in stdout, (
                f"AGENT_SESSION_ID leaked into sandbox, got: {stdout!r}"
            )
        finally:
            sandbox.kill(pid)


# ═══════════════════════════════════════════════════════════
# SandboxManager.get_config() encapsulation
# ═══════════════════════════════════════════════════════════


class TestSandboxManagerGetConfig:
    """SandboxManager should expose public get_config() so callers
    don't access _configs directly."""

    def test_get_config_returns_config_for_known_name(self):
        from harness.sandbox.manager import SandboxManager
        from harness.interfaces import SandboxConfig

        cfg = SandboxConfig(backend="bubblewrap", shared_folders=[
            {"host_path": "/tmp/test", "sandbox_path": "/ws"}
        ])
        mgr = SandboxManager(configs={"hl_eval": cfg})
        result = mgr.get_config("hl_eval")
        assert result is cfg
        assert result.backend == "bubblewrap"

    def test_get_config_returns_none_for_unknown_name(self):
        from harness.sandbox.manager import SandboxManager
        mgr = SandboxManager(configs={})
        assert mgr.get_config("nonexistent") is None

    def test_get_config_for_pool_usage_pattern(self):
        from harness.sandbox.manager import SandboxManager
        from harness.interfaces import SandboxConfig

        cfg = SandboxConfig(backend="bubblewrap", shared_folders=[
            {"host_path": "/host/path", "sandbox_path": "/ws"}
        ])
        mgr = SandboxManager(configs={"main": cfg})
        config = mgr.get_config("main")
        assert config is not None
        assert config.shared_folders[0]["host_path"] == "/host/path"
