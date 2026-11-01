"""Built-in tools: file I/O — read_file, write_file, diff.

All three share a permission-gating helper and accept these dependencies:
- ``execute(cmd) -> (stdout, stderr, code)`` — pre-bound sandbox executor
- ``permissions`` — per-agent file_access map (host paths)
- ``audit_log`` — append-only list for permission deny events
- ``path_map`` — {sandbox_prefix: host_prefix} for resolving agent paths
  to the real host filesystem before checking permissions.
"""

import base64


def _within(path: str, prefix: str) -> bool:
    """I-40: 边界感知的前缀判定（挂载根也属于其自身授权前缀）。

    朴素 startswith 会漏掉「挂载根」：授权键常写成 '<host_ws>/'，而挂载根
    解析结果为 '<host_ws>'（无尾斜杠）。这里对两侧做 rstrip("/") 后按路径
    边界比较，既放行根、也避免 '/ws' 误覆盖 '/ws-other'。
    """
    p = prefix.rstrip("/")
    if p == "":
        p = "/"
    if path == p:
        return True
    return path.startswith(p if p.endswith("/") else p + "/")


def _resolve_host_path(sandbox_path: str, path_map: dict[str, str]) -> str | None:
    """Map a sandbox path to its host counterpart, normalising `.`/`..` first.

    Uses plain longest-prefix matching against the path_map (sandbox_prefix →
    host_prefix): `/ws/xyz` DOES resolve under the prefix `/ws/x` (to
    `/tmp/xyz`), and the following permission check then denies it unless the
    grant actually covers it.  Resolving siblings is required so they are
    DENIED rather than silently treated as unmapped (unmapped sandbox paths are
    assumed to have no host-side impact).

    Normalisation matters: `/ws/out/../ja/x` used to be matched on the literal
    string prefix `/ws/out` and therefore classified as if it were inside the
    writable output tree, although it resolves into the READ-ONLY source tree.

    Returns None when nothing matches.  Callers treat that as "no host file",
    which is right for namespace-only paths (/dev/null, /proc/…) — those live in
    the sandbox tmpfs and have no host counterpart to protect.
    """
    import posixpath
    # Only ONE change from the original: normalise first, and normalise the
    # result.  MEASURED: without it `/ws/out/../src/x` matched the literal prefix
    # `/ws/out` and was judged against the writable output grant while actually
    # resolving into the read-only source tree.
    # Plain `startswith` matching is kept on purpose: a sibling like `/ws-other`
    # must still RESOLVE (so the permission prefix check can deny it) rather than
    # fall out of the map as "unmapped".
    norm = posixpath.normpath(sandbox_path)
    best_sb = ""
    best_host = ""
    for sb_prefix, host_prefix in path_map.items():
        if norm.startswith(sb_prefix) and len(sb_prefix) > len(best_sb):
            best_sb = sb_prefix
            best_host = host_prefix
    if best_sb:
        return posixpath.normpath(norm.replace(best_sb, best_host, 1))
    return None


def _check_access(path, permissions, mode, audit_log, path_map=None):
    """Gate file access by per-agent file_access permissions.

    ``path`` is the agent-requested **sandbox path**.
    When ``path_map`` is provided, the path is first resolved to a
    host path, then checked against ``file_access`` (which should
    contain host-path prefixes).  Without ``path_map`` the check is
    against the raw sandbox path (backward-compatible).

    Raises PermissionError if the path is not in the allowed set
    (or if mode is 'write' and the path is read_only).
    """
    file_access = (permissions or {}).get("file_access", {})

    if path_map:
        check_path = _resolve_host_path(path, path_map)
        if check_path is None:
            return  # sandbox-only path — no host filesystem impact
    else:
        check_path = path

    for prefix, access in file_access.items():
        if _within(check_path, prefix):
            if mode == "write" and access == "read_only":
                if audit_log is not None:
                    try:
                        audit_log.append({
                            "level": "warn",
                            "category": "permission",
                            "event": "file_access_deny",
                            "detail": {"path": path, "check_path": check_path, "reason": "read_only"},
                        })
                    except TypeError:
                        audit_log.append(
                            level="warn", category="permission",
                            event="file_access_deny",
                            detail={"path": path, "check_path": check_path, "reason": "read_only"},
                        )
                raise PermissionError(
                    f"Access denied — path '{path}' is read_only")
            return

    # Path not in allowed list — deny
    if permissions:
        if audit_log is not None:
            try:
                audit_log.append({
                    "level": "warn",
                    "category": "permission",
                    "event": "file_access_deny",
                    "detail": {"path": path, "check_path": check_path, "reason": "not_in_permissions"},
                })
            except TypeError:
                audit_log.append(
                    level="warn", category="permission",
                    event="file_access_deny",
                    detail={"path": path, "check_path": check_path, "reason": "not_in_permissions"},
                )
        raise PermissionError(
            f"Access denied — path '{path}' not in agent permissions")


def register(registry):
    """Register file I/O tools on the given ToolRegistry."""

    # ── read_file ──────────────────────────────────────────

    @registry.register(
        "read_file",
        "Read the contents of a file at the given sandbox path, "
        "e.g. /workspace/data.txt",
        {"path": str},
        deps=["execute", "permissions", "audit_log", "path_map"],
    )
    def read_file(path, execute=None, permissions=None, audit_log=None,
                  path_map=None):
        _check_access(path, permissions, "any", audit_log, path_map=path_map)
        stdout, stderr, code = execute(
            f"python -c \"print(open('{path}').read())\"")
        if code != 0:
            return f"Error reading '{path}': {stderr}"
        from harness.prompt_assembly import truncate_output
        return truncate_output(stdout.strip())

    # ── write_file ─────────────────────────────────────────

    @registry.register(
        "write_file",
        "Write content to a file at the given sandbox path, "
        "e.g. /workspace/output.txt",
        {"path": str, "content": str},
        deps=["execute", "permissions", "audit_log", "path_map"],
    )
    def write_file(path, content, execute=None, permissions=None,
                   audit_log=None, path_map=None):
        _check_access(path, permissions, "write", audit_log, path_map=path_map)
        encoded = base64.b64encode(content.encode()).decode()
        cmd = (
            f"python -c \""
            f"import base64; "
            f"open('{path}','w').write(base64.b64decode('{encoded}').decode())"
            f"\""
        )
        stdout, stderr, code = execute(cmd)
        if code != 0:
            return f"Error writing '{path}': {stderr}"
        return f"Wrote {len(content)} bytes to {path}"

    # ── diff ───────────────────────────────────────────────

    @registry.register(
        "diff",
        "Compare two files and return a unified diff.",
        {"path_a": str, "path_b": str},
        deps=["execute", "permissions", "audit_log", "path_map"],
    )
    def diff(path_a, path_b, execute=None, permissions=None, audit_log=None,
             path_map=None):
        _check_access(path_a, permissions, "any", audit_log, path_map=path_map)
        _check_access(path_b, permissions, "any", audit_log, path_map=path_map)
        cmd = (
            f"python -c \"import difflib,sys; "
            f"a=open('{path_a}').readlines(); "
            f"b=open('{path_b}').readlines(); "
            f"sys.stdout.writelines(difflib.unified_diff(a,b))\""
        )
        stdout, stderr, code = execute(cmd)
        if code != 0:
            return f"Error diffing: {stderr}"
        return stdout if stdout.strip() else "(no differences)"
