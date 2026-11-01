"""Module 4 — Sandbox: Bubblewrap-based process isolation.

Replaces DockerSandbox with native Linux namespace isolation via bubblewrap (bwrap).
No daemon, no image registry, no network dependency. Just namespaces + bind mounts.

Phase 2 refactor: spawn() starts a no-op process (sleep infinity) instead of
running agent_main.py. The sandbox is a pure isolation boundary — AgentRuntime
runs on the host and uses execute() to run tool commands inside the sandbox.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from harness.interfaces import SandboxConfig, SandboxSpawnError


# Cached discovery at import time
_BWRAP_PATH = shutil.which("bwrap")
# Project root: env var HARNESS_PROJECT_ROOT overrides cwd.
# No longer derived from __file__ to avoid breakage when deployed to site-packages.
_PROJECT_ROOT = Path(os.environ.get("HARNESS_PROJECT_ROOT", os.getcwd()))
_WORKSPACE_ROOT = _PROJECT_ROOT / "workspace"


class BubblewrapSandbox:
    """Bubblewrap-based sandbox implementing SandboxInterface.

    Uses Linux namespaces (via bwrap) for process isolation instead of Docker.
    No daemon, no images, no registry — just the kernel's namespace primitives.

    Tracks sandboxed processes by PID. Each process is a no-op container
    that stays alive so execute() can run commands inside it.

    Supports TTL and idle timeout via a background reaper thread.
    """

    # Reaper scan interval in seconds (override in tests)
    _REAPER_INTERVAL: float = 30.0

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._log_files: dict[str, tuple[str, str]] = {}  # pid_str → (stdout_path, stderr_path)
        self._workspace_dirs: dict[str, str] = {}  # pid_str → workspace_dir_path
        self._shared_folders_meta: dict[str, list[dict[str, str]]] = {}  # pid_str → shared_folders
        self._session_ids: dict[str, str] = {}  # pid_str → session_id
        self._firewall_meta: dict[str, dict[str, str]] = {}  # pid_str → firewall
        self._env_vars_meta: dict[str, dict[str, str]] = {}  # pid_str → env_vars
        self._created_at: dict[str, float] = {}  # pid_str → timestamp
        self._last_activity: dict[str, float] = {}  # pid_str → timestamp
        self._ttl_config: dict[str, tuple[int, int]] = {}  # pid_str → (ttl_seconds, idle_timeout)
        self._timeouts: dict[str, int] = {}  # pid_str → execute_timeout
        self._by_uuid: dict[str, str] = {}  # I-26: sandbox_id(uuid) → pid_str
        self._reaper_started = False
        self._reaper_lock = threading.Lock()

    # ─────────────────────────────────────────────────────────────
    # SandboxInterface
    # ─────────────────────────────────────────────────────────────

    def spawn(self, config: SandboxConfig,
              sandbox_id: str | None = None) -> str:
        """Start a sandboxed no-op process via bubblewrap.

        The container stays alive (sleep infinity) so that execute()
        can run commands inside it.  Environment variables are injected
        for the execute() commands to inherit.

        Returns:
            PID string of the sandboxed process.
        """
        if _BWRAP_PATH is None:
            raise SandboxSpawnError("bubblewrap (bwrap) not found in PATH")

        session_id = getattr(config, "session_id", None)
        workspace_dir = _resolve_workspace_dir(session_id) if session_id else None

        # Ensure host directories for shared_folders exist before bwrap binds them
        for folder in getattr(config, "shared_folders", []) or []:
            host_path = folder.get("host_path")
            if host_path and not os.path.isdir(host_path):
                os.makedirs(host_path, exist_ok=True)

        # Capture stdout / stderr to temp files
        stdout_fd, stdout_path = tempfile.mkstemp(prefix="bwrap-stdout-", suffix=".log")
        stderr_fd, stderr_path = tempfile.mkstemp(prefix="bwrap-stderr-", suffix=".log")

        # Build bwrap command: no-op process (sleep infinity)
        cmd = _build_bwrap_cmd(
            config, workspace_dir,
            extra_command=["/bin/sh", "-c", "sleep infinity"],
            anchor_container=True,
        )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_fd,
                stderr=stderr_fd,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            os.close(stdout_fd)
            os.close(stderr_fd)
            raise SandboxSpawnError(f"Failed to spawn bwrap process: {exc}") from exc

        pid_str = str(proc.pid)
        # I-26: record sandbox_id(uuid) → pid so upper layers address
        # sandboxes by uuid only; respawn re-registers the same id.
        if sandbox_id is not None:
            self._by_uuid[sandbox_id] = pid_str
        now = time.time()
        self._processes[pid_str] = proc
        self._log_files[pid_str] = (stdout_path, stderr_path)
        self._session_ids[pid_str] = getattr(config, "session_id", "")
        firewall = getattr(config, "firewall", None) or {}
        self._firewall_meta[pid_str] = dict(firewall) if isinstance(firewall, dict) else {}
        env_vars = getattr(config, "env_vars", {}) or {}
        self._env_vars_meta[pid_str] = dict(env_vars)
        self._created_at[pid_str] = now
        self._last_activity[pid_str] = now
        ttl = getattr(config, "ttl_seconds", 0) or 0
        idle = getattr(config, "idle_timeout", 0) or 0
        if ttl > 0 or idle > 0:
            self._ttl_config[pid_str] = (ttl, idle)
            self._start_reaper()
        self._timeouts[pid_str] = getattr(config, "execute_timeout", 60) or 60
        if workspace_dir:
            self._workspace_dirs[pid_str] = workspace_dir
        shared = getattr(config, "shared_folders", []) or []
        if shared:
            self._shared_folders_meta[pid_str] = shared

        return pid_str

    def kill(self, sandbox_id: str, keep_workspace: bool = False) -> None:
        """Force-kill a sandboxed process.

        Sends SIGKILL to the entire process group.
        If keep_workspace is True, workspace files are preserved for recovery.
        """
        container_id = self._by_uuid.get(sandbox_id, sandbox_id)
        proc = self._processes.get(container_id)
        if proc is None:
            return

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        self._cleanup(container_id, keep_workspace)

    def stop(self, sandbox_id: str, timeout: int = 5) -> None:
        """Gracefully stop a sandboxed process.

        Sends SIGTERM and waits up to timeout seconds.
        Falls back to SIGKILL if process does not exit.
        """
        container_id = self._by_uuid.get(sandbox_id, sandbox_id)
        proc = self._processes.get(container_id)
        if proc is None:
            return

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        self._cleanup(container_id)

    def is_alive(self, sandbox_id: str) -> bool:
        """Check whether a sandboxed process is still running."""
        proc = self._processes.get(self._by_uuid.get(sandbox_id, sandbox_id))
        if proc is None:
            return False
        return proc.poll() is None

    def get_logs(self, sandbox_id: str) -> str:
        """Retrieve captured stdout/stderr from the sandboxed process."""
        paths = self._log_files.get(self._by_uuid.get(sandbox_id, sandbox_id))
        if paths is None:
            return ""

        stdout_path, stderr_path = paths
        parts = []

        for label, path in [("stdout", stdout_path), ("stderr", stderr_path)]:
            try:
                with open(path, "r", errors="replace") as f:
                    content = f.read()
                if content.strip():
                    parts.append(f"[{label}]\n{content}")
            except FileNotFoundError:
                pass

        return "\n".join(parts)

    def execute(
        self, sandbox_id: str, command: str,
        restrict: Any = None,
    ) -> tuple[str, str, int]:
        """Run a command inside the sandbox namespace.

        Spawns a new bwrap instance with the same mount configuration
        as the parent sandbox, then runs `command` inside it.

        Args:
            container_id: The PID string of the parent sandbox instance.
            command: Shell command to execute.

        Returns:
            (stdout, stderr, exit_code)
        """
        container_id = self._by_uuid.get(sandbox_id, sandbox_id)
        workspace_dir = self._workspace_dirs.get(container_id)
        shared_folders = self._shared_folders_meta.get(container_id, [])
        
        session_id = self._session_ids.get(container_id, "")
        firewall = self._firewall_meta.get(container_id, {"network": "host"})
        env_vars = self._env_vars_meta.get(container_id, {})

        # Refresh idle timer on every execute()
        self._last_activity[container_id] = time.time()

        from harness.interfaces import (SandboxRestriction,
                                        SandboxRestrictionError)
        from harness.sandbox import execguard

        seccomp_fd = None
        tmp_bpf = None
        if restrict is not None:
            if not isinstance(restrict, SandboxRestriction):
                raise TypeError("restrict must be a SandboxRestriction")
            if not execguard.landlock_available():
                raise SandboxRestrictionError(
                    "Landlock unavailable in kernel LSM (fail-closed)")
            cache_dir = os.path.join(
                tempfile.gettempdir(), "harness-execguard")
            try:
                binary = execguard.ensure_binary(cache_dir)
                if restrict.syscalls_deny:
                    import tempfile as _tf
                    tmp_bpf = _tf.NamedTemporaryFile(
                        prefix="execguard-", suffix=".bpf", delete=False)
                    tmp_bpf.close()
                    execguard.build_seccomp_bpf(restrict, tmp_bpf.name)
                    seccomp_fd = os.open(tmp_bpf.name, os.O_RDONLY)
            except execguard.ExecguardError as exc:
                raise SandboxRestrictionError(str(exc)) from exc
            launch = execguard.launcher_argv(
                restrict, "/opt/harness/execguard")
            extra = launch + ["/bin/sh", "-c", command]
        else:
            binary = None
            extra = ["/bin/sh", "-c", command]

        cmd = _build_bwrap_cmd(
            SandboxConfig(
                backend="bubblewrap",
                firewall=firewall,
                shared_folders=shared_folders,
                session_id=session_id,
                env_vars=env_vars,
            ),
            workspace_dir=workspace_dir,
            extra_command=extra,
            execguard_bin=binary,
            seccomp_fd=seccomp_fd,
        )

        try:
            timeout = self._timeouts.get(container_id, 60)
            # bytes + explicit tolerant decode, NOT `text=True`: the strict
            # default raised UnicodeDecodeError on any command emitting a
            # non-UTF-8 byte (measured: a truncated multi-byte sequence made the
            # whole tool call fail, which then broke the tool_call pairing in the
            # next request).  A shell tool must be byte-tolerant — the model needs
            # to SEE the output, with undecodable bytes replaced.
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                pass_fds=(seccomp_fd,) if seccomp_fd is not None else (),
            )
            result.stdout = (result.stdout or b"").decode("utf-8", errors="replace")
            result.stderr = (result.stderr or b"").decode("utf-8", errors="replace")
            if (restrict is not None and result.returncode == 2
                    and "execguard:" in (result.stderr or "")):
                raise SandboxRestrictionError(
                    "exec-stage restriction failed inside sandbox "
                    "(fail-closed): " + (result.stderr or "").strip())
            return (
                result.stdout,
                result.stderr,
                result.returncode,
            )
        except subprocess.TimeoutExpired:
            return ("", "Command timed out (60s)", -1)
        except OSError as exc:
            raise SandboxRestrictionError(str(exc)) from exc
        finally:
            if seccomp_fd is not None:
                os.close(seccomp_fd)
            if tmp_bpf is not None:
                try:
                    os.unlink(tmp_bpf.name)
                except OSError:
                    pass

    # ─────────────────────────────────────────────────────────────
    # TTL / idle timeout reaper
    # ─────────────────────────────────────────────────────────────

    def _start_reaper(self) -> None:
        """Start the background reaper thread if not already running."""
        with self._reaper_lock:
            if self._reaper_started:
                return
            self._reaper_started = True
            t = threading.Thread(target=self._reaper_loop, daemon=True)
            t.start()

    def _reaper_loop(self) -> None:
        """Periodically scan and kill expired sandboxes."""
        while True:
            time.sleep(self._REAPER_INTERVAL)
            now = time.time()
            expired = []
            for pid_str, (ttl, idle) in list(self._ttl_config.items()):
                if not self.is_alive(pid_str):
                    expired.append(pid_str)
                    continue
                created = self._created_at.get(pid_str, 0)
                last = self._last_activity.get(pid_str, created)
                if ttl > 0 and now - created > ttl:
                    expired.append(pid_str)
                elif idle > 0 and now - last > idle:
                    expired.append(pid_str)
            for pid_str in expired:
                # TTL expires → delete.  Unexpected death → keep workspace.
                if not self.is_alive(pid_str):
                    self.kill(pid_str, keep_workspace=True)
                else:
                    self.stop(pid_str, timeout=3)

    # ─────────────────────────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────────────────────────

    def _cleanup(self, pid_str: str, keep_workspace: bool = False) -> None:
        """Remove tracking state for a finished process."""
        self._processes.pop(pid_str, None)
        self._created_at.pop(pid_str, None)
        self._last_activity.pop(pid_str, None)
        self._ttl_config.pop(pid_str, None)
        paths = self._log_files.pop(pid_str, None)
        if paths:
            for p in paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        if not keep_workspace:
            ws_dir = self._workspace_dirs.pop(pid_str, None)
            if ws_dir and os.path.isdir(ws_dir):
                shutil.rmtree(ws_dir, ignore_errors=True)
        else:
            self._workspace_dirs.pop(pid_str, None)
        self._shared_folders_meta.pop(pid_str, None)
        self._session_ids.pop(pid_str, None)
        self._firewall_meta.pop(pid_str, None)
        self._env_vars_meta.pop(pid_str, None)
        self._timeouts.pop(pid_str, None)
        # I-26: drop uuid→pid inverse mapping on cleanup
        for sid, mapped_pid in list(self._by_uuid.items()):
            if mapped_pid == pid_str:
                self._by_uuid.pop(sid, None)


# ─────────────────────────────────────────────────────────────
# Bwrap command builder
# ─────────────────────────────────────────────────────────────


def _build_bwrap_cmd(
    config: SandboxConfig,
    
    workspace_dir: Optional[str] = None,
    extra_command: Optional[list[str]] = None,
    anchor_container: bool = False,
    execguard_bin: Optional[str] = None,
    seccomp_fd: Optional[int] = None,
) -> list[str]:
    """Build the full bwrap command line.

    Args:
        anchor_container: True for the long-lived no-op anchor (spawn).
            The anchor must NOT use --die-with-parent (PDEATHSIG chain is
            misfired under concurrent spawns and kills the anchor) nor
            --new-session (fragments the tree into separate process groups
            so killpg() can't reap it).  It stays a single process group
            via Popen's start_new_session, and killpg() cleans the whole
            tree.  execute() commands keep both flags (per-command session
            isolation + cleanup on parent exit).
    """

    cmd = [
        _BWRAP_PATH,
        "--unshare-all",
        "--clearenv",
    ]
    if not anchor_container:
        cmd.append("--new-session")
        cmd.append("--die-with-parent")

    # Network
    firewall = getattr(config, "firewall", None)
    if isinstance(firewall, dict):
        network_mode = firewall.get("network", "host")
    else:
        network_mode = getattr(config, "network", "host")
    if network_mode == "host":
        cmd.append("--share-net")
    else:
        cmd.append("--unshare-net")

    # Environment: defaults then user overrides
    user_vars = dict(getattr(config, "env_vars", {}) or {})
    env = {
        "HOME": "/tmp",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
    }
    if anchor_container:
        # Tag the anchor so orphan reaping (see reap_orphan_anchors)
        # can find leftover trees after the host process dies.
        env["HARNESS_SANDBOX_ANCHOR"] = "1"
    env.update(user_vars)
    for key, val in env.items():
        cmd.extend(["--setenv", key, val])

    # Mounts
    cmd.extend(["--tmpfs", "/tmp"])
    cmd.extend(["--proc", "/proc"])
    cmd.extend(["--dev", "/dev"])
    cmd.extend(["--dir", "/workspace"])

    # System mounts (read-only) — always present
    for sys_dir in ["/usr", "/lib64", "/lib", "/bin", "/etc"]:
        if os.path.isdir(sys_dir):
            cmd.extend(["--ro-bind", sys_dir, sys_dir])

    # Shared folders — additional mounts on top
    shared_folders = getattr(config, "shared_folders", []) or []
    for folder in shared_folders:
        host_path = folder.get("host_path")
        sandbox_path = folder.get("sandbox_path")
        if host_path and sandbox_path and os.path.isdir(host_path):
            mode = folder.get("mode", "rw")
            bind_flag = "--ro-bind" if mode.casefold() == "ro" else "--bind"
            cmd.extend([bind_flag, host_path, sandbox_path])
            if sandbox_path == "/workspace":
                cmd.extend(["--chdir", "/workspace"])

    # Workspace directory → /workspace (Agent work area, isolated from sessions/)
    if workspace_dir and not any(
        f.get("sandbox_path") == "/workspace" for f in shared_folders
    ):
        cmd.extend(["--bind", workspace_dir, "/workspace"])
        cmd.extend(["--chdir", "/workspace"])

    # I-34 exec-stage: bind launcher + seccomp filter before the command.
    if execguard_bin:
        cmd.extend(["--ro-bind", execguard_bin, "/opt/harness/execguard"])
    if seccomp_fd is not None:
        cmd.extend(["--seccomp", str(seccomp_fd)])

    # Command
    cmd.extend(["--"])
    if extra_command:
        cmd.extend(extra_command)
    else:
        cmd.extend(["/bin/sh", "-c", "sleep infinity"])

    return cmd


def _resolve_session_dir(session_id: str) -> Optional[str]:
    """Resolve the filesystem path to a session directory."""
    if not session_id:
        return None
    return str(_PROJECT_ROOT / "sessions" / session_id)


def _resolve_workspace_dir(session_id: str) -> Optional[str]:
    """Resolve the filesystem path to a workspace directory.

    Creates the directory if it does not exist.  This directory is
    bind-mounted into the sandbox as /workspace, keeping session
    data (events, checkpoints) isolated from agent work files.
    """
    if not session_id:
        return None
    ws = _WORKSPACE_ROOT / session_id
    ws.mkdir(parents=True, exist_ok=True)
    return str(ws)


# ─────────────────────────────────────────────────────────────
# Orphan anchor reaping
# ─────────────────────────────────────────────────────────────

_ANCHOR_ENV_TAG = "HARNESS_SANDBOX_ANCHOR"


def _scan_tagged_processes() -> dict[int, dict[str, object]]:
    """Scan /proc for anchor-tagged processes.

    Returns: {pid: {"ppid": int, "env": str, "pgid": int}} for every
    process whose environ contains the anchor tag.  Non-tagged and
    inaccessible processes are skipped.
    """
    found: dict[int, dict[str, object]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        environ_path = f"/proc/{entry}/environ"
        try:
            with open(environ_path, "rb") as f:
                env_bytes = f.read()
        except (OSError, PermissionError):
            continue
        env = env_bytes.decode("utf-8", errors="replace")
        if _ANCHOR_ENV_TAG + "=1" not in env:
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                parts = f.read().split()
            ppid = int(parts[3])
            pgid = int(parts[4])
        except (OSError, ValueError, IndexError):
            continue
        found[pid] = {"ppid": ppid, "env": env, "pgid": pgid}
    return found


# The env tag is injected with --setenv, which bwrap applies to the command
# layer *inside* the sandbox — not to the Popen-returned anchor root.  So a
# tagged process is a child of the tree root; the tree root is its pgid
# leader (bwrap keeps the whole tree in one process group).  Reaping targets
# that pgid.
def _pgid_leader_ppid(pgid: int) -> int | None:
    """Return the ppid of the process group leader, or None if absent."""
    try:
        with open(f"/proc/{pgid}/stat") as f:
            parts = f.read().split()
        return int(parts[3])
    except (OSError, ValueError, IndexError):
        return None


def _is_orphan_pgid(pgid: int) -> bool:
    """True if the pgid leader's parent is not the current harness process.

    A live anchor's tree root is spawned by this process, so its ppid is
    os.getpid().  An orphan's parent died and it was reparented to init or
    the systemd user manager (any other ppid).
    """
    root_ppid = _pgid_leader_ppid(pgid)
    return root_ppid is not None and root_ppid != os.getpid()


def _killpg(pgid: int, sig: int) -> None:
    """Kill a process group by pgid (wrapped for testability)."""
    os.killpg(pgid, sig)


def reap_orphan_anchors() -> int:
    """Reap leftover anchor trees whose host process has died.

    The anchor env tag lives on the sandbox-layer process inside the tree
    (bwrap --setenv applies to the command layer).  Its process group is the
    whole tree (anchor root is the pgid leader).  For each tagged process,
    resolve the tree root by pgid; if that root was reparented (parent is no
    longer the current harness process), kill the whole group.

    Returns the number of trees reaped.
    """
    reaped_pgids: set[int] = set()
    for info in _scan_tagged_processes().values():
        pgid = int(info["pgid"])
        if pgid in reaped_pgids:
            continue
        if _is_orphan_pgid(pgid):
            try:
                _killpg(pgid, signal.SIGKILL)
                reaped_pgids.add(pgid)
            except (ProcessLookupError, PermissionError):
                continue
    return len(reaped_pgids)
