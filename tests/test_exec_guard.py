"""Landlock exec-guard: the enforcement that does not care about the command.

WHY THIS EXISTS
  The pre-call field gate cannot constrain real filesystem access — ADR-051 §9:
  shell wildcards, variables, quoting, `$(…)` and inline `python3 -c` all bypass
  it.  This session proved the consequence: a mount declared read-only was
  written through the writable mount (`echo PWN > /ws/out/../src/chunk_000.txt`
  changed the HOST file), and `python3 -c "open(path,'w')"` is invisible to any
  path extraction.

  The mount fixes that for the mounted trees.  Landlock adds what mounts CANNOT
  express: a foreign `rw` mount is still unwritable unless the guard allowlists
  it, and the restriction is enforced per path regardless of the program.

WHAT IT MUST NOT DO: break ordinary work.  MEASURED friction: `/dev` is only in
  execguard's read-only base set, so `2>/dev/null` failed with "Permission
  denied" until `/dev` was added to the rw allowlist (it is a namespace tmpfs —
  no host filesystem is exposed by writing to /dev/null).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bwrap") is None,
                                reason="bubblewrap unavailable")


def _sandbox_with_guard():
    from harness.interfaces import SandboxConfig, SandboxRestriction
    from harness.sandbox.impl import BubblewrapSandbox
    from harness.sandbox import execguard

    if not execguard.landlock_available():
        pytest.skip("Landlock unavailable in this kernel/LSM")

    root = ROOT / ".scratch" / "exec_guard"
    for sub in ("src", "out", "other"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "src" / "f.txt").write_text("JA\n", encoding="utf-8")
    (root / "other" / "secret.txt").write_text("SECRET\n", encoding="utf-8")

    sb = BubblewrapSandbox()
    cid = sb.spawn(SandboxConfig(
        backend="bubblewrap", firewall={"network": "host"},
        shared_folders=[{"host_path": str(root / "src"),
                         "sandbox_path": "/ws/src", "mode": "ro"},
                        {"host_path": str(root / "out"),
                         "sandbox_path": "/ws/out", "mode": "rw"},
                        # a FOREIGN rw mount: mounts alone would allow writing here
                        {"host_path": str(root / "other"),
                         "sandbox_path": "/ws/other", "mode": "rw"}]))
    restrict = SandboxRestriction(
        paths={"rw": ["/ws/out", "/dev"],   # /dev: /dev/null redirects
               "ro": ["/ws/src"]})
    return sb, cid, restrict, root


def _run(sb, cid, restrict, cmd):
    out, err, code = sb.execute(cid, cmd, restrict=restrict)
    return out or "", err or "", code


# ── ordinary work must keep working (no false positives) ────────────────

@pytest.mark.parametrize("command", [
    "echo hello",
    "ls 2>/dev/null; echo done",
    "cat /ws/src/f.txt 2>/dev/null",
    "python3 -c 'print(6*7)'",
    "python3 - <<'PY'\nprint(2*3)\nPY",
    "cat > /ws/out/h.txt <<'EOF'\nhi\nEOF\ncat /ws/out/h.txt",
    "printf '%s\\n' a b > /ws/out/l.txt && cat /ws/out/l.txt",
])
def test_guard_does_not_break_ordinary_commands(command):
    sb, cid, restrict, _ = _sandbox_with_guard()
    try:
        _, err, code = _run(sb, cid, restrict, command)
        assert code == 0, f"{command!r} failed under the guard: {err[:200]}"
    finally:
        sb.kill(cid)


# ── enforcement independent of the command form ─────────────────────────

@pytest.mark.parametrize("command", [
    "echo x > /ws/other/evil.txt",
    "python3 -c \"open('/ws/other/evil.txt','w').write('x')\"",
    "cd /ws/other && echo x > evil2.txt",
    "VAR=/ws/other; echo x > $VAR/evil3.txt",
    "dd if=/dev/zero of=/ws/other/evil4.txt bs=1 count=1 2>/dev/null",
])
def test_guard_blocks_writes_to_an_unauthorised_rw_mount(command):
    """A mount being rw is NOT authorization — the guard decides.

    This is the capability a mount cannot express: /ws/other IS mounted rw, and
    the kernel still refuses writes because the guard allowlists only /ws/out.
    """
    sb, cid, restrict, root = _sandbox_with_guard()
    try:
        _, err, code = _run(sb, cid, restrict, command)
        assert code != 0, f"{command!r} wrote to an unauthorised mount"
    finally:
        sb.kill(cid)
    leaked = list((root / "other").glob("evil*"))
    assert leaked == [], f"host files created: {leaked}"


def test_guard_blocks_writes_to_the_read_only_source():
    sb, cid, restrict, root = _sandbox_with_guard()
    try:
        _, err, code = _run(sb, cid, restrict, "echo PWN > /ws/src/f.txt")
        assert code != 0
        # and through the writable mount, which is how it leaked before
        _, err2, code2 = _run(sb, cid, restrict, "echo PWN2 > /ws/out/../src/f.txt")
        assert code2 != 0
    finally:
        sb.kill(cid)
    assert (root / "src" / "f.txt").read_text(encoding="utf-8") == "JA\n", \
        "the HOST source file was modified"


def test_guard_still_allows_reads_of_the_source():
    sb, cid, restrict, _ = _sandbox_with_guard()
    try:
        out, err, code = _run(sb, cid, restrict, "cat /ws/src/f.txt")
        assert code == 0 and "JA" in out
    finally:
        sb.kill(cid)
