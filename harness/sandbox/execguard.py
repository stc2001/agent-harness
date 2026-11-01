"""I-34: exec-stage guard productization — landlock C launcher + seccomp BPF.

Zero runtime python deps inside the sandbox: the C launcher is compiled
host-side once and RO-bind mounted; seccomp BPF is generated host-side
(mephi42-seccomp) and handed to bwrap via --seccomp FD.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from harness.interfaces import SYS_NR_AMD64, SandboxRestriction

_GUARD_SRC = Path(__file__).with_name("execguard.c")
_lock = threading.Lock()


class ExecguardError(RuntimeError):
    """Assembly of the exec-stage guard failed (fail-closed upstream)."""


def ensure_binary(cache_dir: str | Path) -> str:
    """Compile the C launcher once into cache_dir (thread-safe)."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    exe = cache / "execguard"
    if exe.exists():
        return str(exe)
    with _lock:
        if exe.exists():
            return str(exe)
        gcc = shutil.which("gcc")
        if gcc is None:
            raise ExecguardError("gcc missing: cannot build execguard launcher")
        tmp = cache / "execguard.tmp"
        rc = subprocess.run([gcc, "-O2", "-o", str(tmp), str(_GUARD_SRC)],
                            capture_output=True).returncode
        if rc != 0:
            raise ExecguardError(f"gcc failed compiling execguard (rc={rc})")
        os.replace(tmp, exe)
    return str(exe)


def syscall_numbers(restriction: SandboxRestriction) -> list[int]:
    out = []
    for s in restriction.syscalls_deny:
        out.append(s if isinstance(s, int) else SYS_NR_AMD64[s])
    return out


def launcher_argv(restriction: SandboxRestriction, binary: str) -> list[str]:
    """execguard argv for one execute: [bin, rw/ro pairs..., --]."""
    argv = [binary]
    for p in restriction.sys_read_deny:
        argv += ["roex", p]
    for p in restriction.paths.get("rw", []):
        argv += ["rw", p]
    for p in restriction.paths.get("ro", []):
        argv += ["ro", p]
    argv.append("--")
    return argv


def build_seccomp_bpf(restriction: SandboxRestriction,
                      dest: str | Path) -> str:
    """Export a deny-list BPF (default ALLOW) via mephi42-seccomp."""
    try:
        import seccomp
    except Exception as exc:  # pragma: no cover - dependency gate
        raise ExecguardError(f"mephi42-seccomp unavailable: {exc}") from exc
    numbers = syscall_numbers(restriction)
    if not numbers:
        return ""
    f = seccomp.SyscallFilter(seccomp.ALLOW)
    for nr in numbers:
        f.add_rule(seccomp.ERRNO(seccomp.errno.EPERM), nr)
    with open(dest, "wb") as fh:
        f.export_bpf(fh)
    return str(dest)


def landlock_available() -> bool:
    try:
        lsm = Path("/sys/kernel/security/lsm").read_text()
    except OSError:
        return False
    return "landlock" in lsm
