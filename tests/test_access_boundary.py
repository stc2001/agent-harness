"""Where the boundary is enforced — and where it deliberately is NOT.

Learned the hard way in this session:

  * The MOUNT is the enforcement point.  `shared_folders` defaults to "rw", so
    the source tree was writable from inside the sandbox through the writable
    mount (`echo PWN > /ws/out/../src/chunk_000.txt` overwrote the HOST file).
    `mode: "ro"` makes the kernel refuse it, `..` included.

  * The PRE-CALL permission check is an ADVISORY, not a boundary.  ADR-051 §9:
    it "本质是字段匹配，无法约束实际 fs/net 访问（bash 通配/变量/转义可绕）".
    A previous attempt to make it complete grew 100+ lines of command
    classification (read-only command lists, `2>/dev/null` handling, …) against
    an UNBOUNDED command space.  It was removed: the gate must stay small, and
    the only thing it must not do is raise false positives on legitimate reads.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.tool_registry import ToolRegistry  # noqa: E402
from harness.tools import sandbox_exec as _se  # noqa: E402
from harness.tools.file_io import _check_access, _resolve_host_path  # noqa: E402

SRC = "/tmp/acc/agent_ws/ja"
OUT = "/tmp/acc/agent_ws/out"
PERMS = {"subagent_spawn": False,
         "file_access": {SRC + "/": "read_only", OUT + "/": "read_write"}}
PATH_MAP = {"/ws/src": SRC, "/ws/out": OUT}


# ── the mount mode is the real fix ──────────────────────────────────────

# The translation-era source/out mount assertion was removed with the agent
# (ADR-002).  The pi equivalent is covered behaviourally:
# `tests/test_pi_workspace_boundary.py` (single /workspace rw mount) and
# `tests/test_pi_security.py` (Landlock e2e).


# ── path resolution correctness ─────────────────────────────────────────

def test_traversal_resolves_into_the_read_only_tree():
    """`/ws/out/../src/x` must NOT be classified as the writable output tree.

    (`/ws/out/../ja/x` is NOT a useful case: `..` from /ws/out leaves the mount
    namespace entirely, so nothing maps — see the test below.)
    """
    got = _resolve_host_path("/ws/out/../src/chunk_000.txt", PATH_MAP)
    assert got == SRC + "/chunk_000.txt", got


def test_sibling_prefix_resolves_then_is_denied():
    """A sibling mapping resolves, and the permission layer then denies it.

    `/ws/xyz` matches the mapping for `/ws/x` as a plain prefix (that is fine:
    the resolver's job is to produce a host path, not to judge it).  The host
    path it produces is `/tmp/xyz/f`, which is NOT inside the granted
    `/tmp/x/…`, so the write is refused.  Boundary judgement lives in `_within`
    at the permission layer, not in the resolver.
    """
    pm = {"/ws/x": "/tmp/x"}
    perms = {"file_access": {"/tmp/x/": "read_write"}}
    assert _resolve_host_path("/ws/xyz/f", pm) == "/tmp/xyz/f"
    with pytest.raises(PermissionError):
        _check_access("/ws/xyz/f", perms, "write", None, path_map=pm)


def test_normal_segments_inside_a_grant_still_resolve():
    assert _resolve_host_path("/ws/out/./out_000.txt", PATH_MAP) \
        == OUT + "/out_000.txt"
    assert _resolve_host_path("/ws/out/sub/../out_000.txt", PATH_MAP) \
        == OUT + "/out_000.txt"


def test_write_through_traversal_is_refused():
    """Normalised, this path lands in the read-only tree, so a write is refused."""
    with pytest.raises(PermissionError):
        _check_access("/ws/out/../src/chunk_000.txt", PERMS, "write", None,
                      path_map=PATH_MAP)


def test_reading_the_source_tree_is_allowed():
    _check_access("/ws/src/chunk_000.txt", PERMS, "read", None, path_map=PATH_MAP)


# ── the advisory gate: no false positives on legitimate reads ───────────

def _run(command):
    reg = ToolRegistry()
    _se.register(reg)
    ran = []
    try:
        out = reg.get_handler("bash")(
            command=command,
            execute=lambda c: (ran.append(c) or ("ok", "", 0)),
            permissions=PERMS, audit_log=None, path_map=PATH_MAP)
    except PermissionError as exc:
        out = f"Error: Access denied — {exc}"
    return out, ran


@pytest.mark.parametrize("command", [
    "cat /ws/src/chunk_000.txt",
    "ls /ws/src/",
    "cat /ws/src/chunk_000.txt 2>/dev/null",          # fd redirect, not a write
    'ls /ws/src/ && echo "---" && cat /ws/src/chunk_000.txt',
    "head -5 /ws/src/chunk_000.txt",
    "echo hi > /ws/out/out_001.txt",                  # legitimate write
])
def test_advisory_gate_does_not_block_legitimate_commands(command):
    out, ran = _run(command)
    assert "Access denied" not in out, f"{command!r} refused: {out}"
    assert ran, f"{command!r} never executed"


def test_unmapped_paths_are_left_to_the_kernel():
    """A path outside every mount is NOT judged by the pre-call gate.

    It cannot be: nothing on the host corresponds to it (there is no /etc/evil
    inside the sandbox), and `..` that leaves the mount namespace maps to
    nothing.  Deciding such paths here is exactly the unbounded guessing that was
    removed.  They are left for the kernel to refuse — which it does, because
    nothing outside the declared mounts exists in the sandbox.

    This test records that boundary honestly instead of asserting a guarantee the
    advisory gate cannot provide.
    """
    assert _resolve_host_path("/etc/evil.conf", PATH_MAP) is None
    assert _resolve_host_path("/ws/out/../ja/chunk_000.txt", PATH_MAP) is None


# ── the gate must stay small: no command classification ─────────────────

def test_no_command_classification_tables():
    """Guard against re-growing the unbounded-command-space heuristics."""
    src = (ROOT / "harness" / "tools" / "sandbox_exec.py").read_text(encoding="utf-8")
    for forbidden in ("_READ_ONLY_CMDS", "_WRITE_CMDS", "_INTERPRETERS",
                      "_segment_mode", "_split_segments", "_WRITE_REDIRECT_RE"):
        assert forbidden not in src, (
            f"{forbidden} is back — classifying commands is unbounded work and "
            "belongs in the kernel, not the pre-call gate (ADR-051 §9)")


def test_kernel_refuses_writes_that_the_advisory_gate_lets_through():
    """The whole design rests on this: what the gate misses, the kernel refuses.

    The gate deliberately allows a command whose paths are readable, even if the
    command actually WRITES (that is how `cat > x <<'EOF'` and `python3 - <<'PY'`
    work — the agent's ordinary way of producing files).  This test proves the
    gap is closed by the mount, including via `..` — the one traversal that used
    to reach the HOST file.

    Skipped when the kernel cannot provide it (no bwrap / no Landlock), because
    then there is nothing to prove.
    """
    import shutil
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap unavailable")

    from harness.interfaces import SandboxConfig
    from harness.sandbox.impl import BubblewrapSandbox

    root = ROOT / ".scratch" / "kernel_boundary"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    target = root / "src" / "chunk.txt"
    original = "ORIGINAL_CONTENT\n"
    target.write_text(original, encoding="utf-8")

    sb = BubblewrapSandbox()
    cid = sb.spawn(SandboxConfig(
        backend="bubblewrap", firewall={"network": "host"},
        shared_folders=[{"host_path": str(root / "src"),
                         "sandbox_path": "/ws/src", "mode": "ro"},
                        {"host_path": str(root / "out"),
                         "sandbox_path": "/ws/out", "mode": "rw"}]))
    try:
        # reading is fine
        out, _, _ = sb.execute(cid, "cat /ws/src/chunk.txt")
        assert "ORIGINAL_CONTENT" in out
        # writing the source directly is refused by the kernel
        _, err, _ = sb.execute(cid, "echo PWN > /ws/src/chunk.txt")
        assert "Read-only" in err or "read-only" in err.lower(), err
        # ...and so is writing it THROUGH the writable mount
        _, err2, _ = sb.execute(cid, "echo PWN2 > /ws/out/../src/chunk.txt")
        assert "Read-only" in err2 or "read-only" in err2.lower(), err2
        # writing the output tree works
        out3, _, _ = sb.execute(cid, "echo ok > /ws/out/x.txt && cat /ws/out/x.txt")
        assert "ok" in out3
    finally:
        sb.kill(cid)

    assert target.read_text(encoding="utf-8") == original, \
        "the HOST source file was modified despite mode='ro'"
