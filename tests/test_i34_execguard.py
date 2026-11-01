"""I-34 S1: SandboxRestriction contract + validation — TDD."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harness.interfaces import SandboxRestriction


def test_valid_restriction():
    r = SandboxRestriction(paths={"rw": ["/workspace/sub"], "ro": ["/workspace"]},
                           syscalls_deny=["socket", 101])  # socket + ptrace
    assert r.paths["rw"] == ["/workspace/sub"]
    assert 101 in r.syscalls_deny


def test_defaults_empty():
    r = SandboxRestriction()
    assert r.paths == {"rw": [], "ro": []}
    assert r.syscalls_deny == []


def test_relative_path_rejected():
    with pytest.raises(ValueError):
        SandboxRestriction(paths={"rw": ["workspace/sub"]})


def test_unknown_path_mode_rejected():
    with pytest.raises(ValueError):
        SandboxRestriction(paths={"exec": ["/workspace"]})


def test_unknown_syscall_name_rejected():
    with pytest.raises(ValueError):
        SandboxRestriction(syscalls_deny=["not_a_syscall"])


def test_non_positive_syscall_rejected():
    with pytest.raises(ValueError):
        SandboxRestriction(syscalls_deny=[-1])



# ─────────────────────────────────────────────────────────────
# I-34 S2: execguard launcher / BPF assembly
# ─────────────────────────────────────────────────────────────


def test_syscall_number_mapping():
    from harness.sandbox.execguard import syscall_numbers
    r = SandboxRestriction(syscalls_deny=["socket", 101])
    assert syscall_numbers(r) == [41, 101]


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no gcc")
def test_ensure_binary_compiles_once(tmp_path):
    from harness.sandbox.execguard import ensure_binary
    b1 = ensure_binary(tmp_path)
    assert Path(b1).exists()
    b2 = ensure_binary(tmp_path)
    assert b1 == b2  # cached


def test_launcher_argv_order():
    from harness.sandbox.execguard import launcher_argv
    r = SandboxRestriction(paths={"rw": ["/workspace/sub"],
                                  "ro": ["/workspace"]})
    argv = launcher_argv(r, "/opt/harness/execguard")
    assert argv[:5] == ["/opt/harness/execguard", "rw", "/workspace/sub",
                        "ro", "/workspace"]
    assert argv[-1] == "--"


def test_build_seccomp_bpf_writes(tmp_path):
    import seccomp  # noqa: F401  (gate)
    from harness.sandbox.execguard import build_seccomp_bpf
    dest = tmp_path / "f.bpf"
    r = SandboxRestriction(syscalls_deny=["socket"])
    out = build_seccomp_bpf(r, dest)
    assert out == str(dest)
    assert dest.stat().st_size > 0




# ─────────────────────────────────────────────────────────────
# I-34 S3: execute(restrict=...) end-to-end via BubblewrapSandbox
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def bwrap_sandbox(tmp_path):
    from harness.sandbox.impl import BubblewrapSandbox
    from harness.interfaces import SandboxConfig
    if shutil.which("bwrap") is None:
        pytest.skip("no bwrap")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sub").mkdir()
    sb = BubblewrapSandbox()
    pid = sb.spawn(SandboxConfig(
        backend="bubblewrap",
        firewall={"network": "none"},
        shared_folders=[{"host_path": str(ws),
                         "sandbox_path": "/workspace", "mode": "rw"}],
        ttl_seconds=0,
    ))
    yield sb, pid, ws
    sb.kill(pid)


def test_execute_no_restrict_unchanged(bwrap_sandbox):
    sb, pid, ws = bwrap_sandbox
    out, err, rc = sb.execute(pid, "echo hi")
    assert rc == 0 and "hi" in out


@pytest.mark.skipif(not Path("/sys/kernel/security/lsm").exists()
                    or "landlock" not in Path("/sys/kernel/security/lsm").read_text(),
                    reason="no landlock")
def test_execute_restrict_paths_narrower_than_mount(bwrap_sandbox):
    from harness.interfaces import SandboxRestriction
    sb, pid, ws = bwrap_sandbox
    r = SandboxRestriction(paths={"rw": ["/workspace/sub"],
                                  "ro": ["/workspace"]})
    out, err, rc = sb.execute(
        pid,
        "touch /workspace/sub/x && echo SUB_OK; "
        "touch /workspace/a 2>&1", restrict=r)
    text = out + err
    assert "SUB_OK" in text, text
    assert "Permission denied" in text, text


@pytest.mark.skipif(not Path("/sys/kernel/security/lsm").exists()
                    or "landlock" not in Path("/sys/kernel/security/lsm").read_text(),
                    reason="no landlock")
def test_execute_fail_closed_when_landlock_missing(bwrap_sandbox, monkeypatch):
    from harness.interfaces import SandboxRestriction, SandboxRestrictionError
    import harness.sandbox.execguard as eg
    monkeypatch.setattr(eg, "landlock_available", lambda: False)
    sb, pid, ws = bwrap_sandbox
    r = SandboxRestriction(paths={"rw": ["/workspace/sub"]})
    with pytest.raises(SandboxRestrictionError):
        sb.execute(pid, "echo SHOULD_NOT_RUN", restrict=r)




def test_execute_syscalls_deny_socket(bwrap_sandbox):
    from harness.interfaces import SandboxRestriction
    sb, pid, ws = bwrap_sandbox
    r = SandboxRestriction(syscalls_deny=["socket"])
    out, err, rc = sb.execute(
        pid, "python3 -c 'import socket; socket.socket()'", restrict=r)
    text = out + err
    assert "Operation not permitted" in text or "EPERM" in text, text
    assert rc != 0, text




# ─────────────────────────────────────────────────────────────
# I-34 EXT: sys_read_deny — kernel-level deny-read of system dirs
# ─────────────────────────────────────────────────────────────


def test_sys_read_deny_validation():
    r = SandboxRestriction(paths={"rw": ["/workspace"]},
                           sys_read_deny=["/etc", "/proc"])
    assert r.sys_read_deny == ["/etc", "/proc"]
    with pytest.raises(ValueError):
        SandboxRestriction(sys_read_deny=["etc"])  # 非绝对路径拒


def test_launcher_argv_includes_roex():
    from harness.sandbox.execguard import launcher_argv
    r = SandboxRestriction(paths={"rw": ["/workspace/sub"]},
                           sys_read_deny=["/etc", "/proc"])
    argv = launcher_argv(r, "/opt/harness/execguard")
    assert argv[:5] == ["/opt/harness/execguard", "roex", "/etc",
                        "roex", "/proc"]
    assert "/workspace/sub" in argv


@pytest.mark.skipif(not Path("/sys/kernel/security/lsm").exists()
                    or "landlock" not in
                    Path("/sys/kernel/security/lsm").read_text(),
                    reason="no landlock")
def test_sys_read_deny_blocks_cat_etc(bwrap_sandbox):
    sb, pid, _ws = bwrap_sandbox
    r = SandboxRestriction(paths={"ro": ["/workspace"]},
                           sys_read_deny=["/etc"])
    out, err, rc = sb.execute(pid, "cat /etc/hostname 2>&1; echo rc=$?",
                              restrict=r)
    text = out + err
    assert "Permission denied" in text or "EACCES" in text, text

