"""I-33 feasibility (no integration): Landlock C-launcher + seccomp BPF via bwrap.

Each test builds its own sandbox from scratch and asserts the chain works.
Skips when bwrap/gcc/seccomp/landlock kernel support is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_GUARD_C = r'''#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/prctl.h>
#include <sys/syscall.h>

#ifndef __NR_landlock_create_ruleset
#define __NR_landlock_create_ruleset 444
#define __NR_landlock_add_rule 445
#define __NR_landlock_restrict_self 446
#endif
struct ll_ruleset { unsigned long long handled_access_fs; };
struct ll_path { unsigned long long allowed_access; int parent_fd; } __attribute__((packed));
#define FS_ALL ((1ULL << 15) - 1)
#define FS_RO (1ULL << 0 | 1ULL << 2 | 1ULL << 3)

int main(int argc, char** argv) {
  int i, cmd_i = argc;
  prctl(38, 1);
  struct ll_ruleset rs = { FS_ALL };
  int fd = (int)syscall(__NR_landlock_create_ruleset, &rs, sizeof(rs), 0);
  if (fd < 0) { perror("landlock_create_ruleset"); return 2; }
  for (i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--")) { cmd_i = i; break; }
    const char* mode = argv[i]; i++;
    if (i >= argc) return 2;
    int pfd = open(argv[i], O_PATH | O_CLOEXEC);
    if (pfd < 0) { perror("open path"); return 2; }
    unsigned long long acc = !strcmp(mode, "rw") ? FS_ALL : FS_RO;
    struct ll_path pr = { acc, pfd };
    if (syscall(__NR_landlock_add_rule, fd, 1, &pr, 0)) { perror("add_rule"); return 2; }
    close(pfd);
  }
  if (syscall(__NR_landlock_restrict_self, fd, 0)) { perror("restrict_self"); return 2; }
  if (cmd_i >= argc) return 0;
  execvp(argv[cmd_i + 1], &argv[cmd_i + 1]);
  perror("execvp"); return 2;
}
'''

_SYS_BINDS = ["/usr", "/lib64", "/bin", "/etc"]


def _have():
    if shutil.which("bwrap") is None or shutil.which("gcc") is None:
        return False
    try:
        import seccomp  # noqa: F401
    except Exception:
        return False
    return True


def _landlock_ok() -> bool:
    try:
        return os.path.exists("/sys/kernel/security/lsm") and "landlock" in             Path("/sys/kernel/security/lsm").read_text()
    except Exception:
        return False


def _run_bwrap(args: list[str], timeout: float = 20.0):
    return subprocess.run(["bwrap", "--unshare-all", "--clearenv",
                           "--unshare-net", "--tmpfs", "/tmp", "--proc",
                           "/proc", "--dev", "/dev", "--dir", "/workspace",
                           *[x for s in _SYS_BINDS for x in ("--ro-bind", s, s)],
                           *args],
                          capture_output=True, text=True, timeout=timeout)


@pytest.mark.skipif(not _have(), reason="needs bwrap+gcc+seccomp")
@pytest.mark.skipif(not _landlock_ok(), reason="no landlock LSM")
def test_landlock_launcher_narrower_than_mount(tmp_path):
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "sub").mkdir()
        gate = ws / "guard"
        subprocess.run(["gcc", "-O2", "-o", str(gate), "-x", "c", "-"],
                       input=_GUARD_C, text=True, check=True)
        res = _run_bwrap([
            "--bind", str(ws), "/workspace", "--",
            "/workspace/guard", "rw", "/workspace/sub",
            "ro", "/workspace", "ro", "/usr", "ro", "/lib64", "ro", "/bin",
            "ro", "/etc", "ro", "/tmp", "rw", "/proc", "ro", "/dev", "--",
            "/bin/sh", "-c",
            "touch /workspace/sub/x && echo SUB_OK; "
            "touch /workspace/a 2>&1"])
        out = (res.stdout or "") + (res.stderr or "")
        assert "SUB_OK" in out, out
        assert "Permission denied" in out, out


@pytest.mark.skipif(not _have(), reason="needs bwrap+gcc+seccomp")
def test_seccomp_bpf_denies_socket_via_bwrap_fd(tmp_path):
    import seccomp
    bpf = tmp_path / "f.bpf"
    f = seccomp.SyscallFilter(seccomp.ALLOW)
    f.add_rule(seccomp.ERRNO(seccomp.errno.EPERM), 41)  # socket(AF_INET)
    with open(str(bpf), "wb") as fh:
        f.export_bpf(fh)
    fd = os.open(str(bpf), os.O_RDONLY)
    try:
        p = subprocess.run(
            ["bwrap", "--unshare-all", "--clearenv", "--unshare-net",
             "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
             "--seccomp", str(fd), *[x for s in _SYS_BINDS
                                     for x in ("--ro-bind", s, s)],
             "--", "/usr/bin/python3", "-c",
             "import socket; socket.socket()"],
            pass_fds=(fd,), capture_output=True, text=True, timeout=20.0)
    finally:
        os.close(fd)
    text = (p.stdout or "") + (p.stderr or "")
    assert "Operation not permitted" in text, text
    assert p.returncode != 0, text
