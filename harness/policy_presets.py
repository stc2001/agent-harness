"""I-27 builtin presets — all default OFF; enabling is the app choice.

Each maker returns {"veto": veto(ctx)->None|str, "observe": ..., "cfg": ...}.
"""

from __future__ import annotations

import ipaddress
import re


def _first_arg(ctx, keys):
    args = getattr(ctx, "tool_args", None) or {}
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _bash_whitelist():
    """Dangerous-command wildcard denial (default deny patterns)."""
    DANGER = [
        r"rm\s+-[a-z]*[fr]\s+/",
        r"mkfs", r"mkswap", r"dd\s+.*of=/dev/",
        r"chmod\s+-R\s*777\s*/", r"chown\s+-R\s*[^ ]+\s*/",
        r":\(\)\s*\{", r"shutdown", r"reboot", r"init\s+0",
        r">\s*/dev/sd", r"curl[^|]*\|\s*(ba)?sh",
        r"wget[^|]*\|\s*(ba)?sh",
    ]

    def veto(ctx, cfg):
        cmd = _first_arg(ctx, ("cmd", "command", "script"))
        if not cmd or ctx.tool_name not in ("bash", "execute", "run_script"):
            return None
        allows = (cfg or {}).get("allow", [])
        if any(a and a in cmd for a in allows):
            return None
        for pat in DANGER:
            if re.search(pat, cmd):
                return f"bash_whitelist denied dangerous command: {cmd[:80]}"
        return None

    return {"veto": veto, "cfg": {"allow": []}}


def _fs_paths():
    """I-37: 系统目录 deny — policy 定义单元（cedar 静态 + guard 内核排除）。

    cedar: File 工具路径 forbid 全部 SYS_PREFIXES；Cmd（bash/execute/run_script）
    文本 forbid 同一集合中「内核可安全排除」的子集（/usr /lib /bin 等执行必需
    目录不参与内核排除，否则 shell/库无法运行）。
    guard.sys_read_deny: 内核级禁读（Landlock 不授予 allow → 任意工具含 bash
    均 EACCES），覆盖历史遗留的「bash 绕过 fs_paths」问题。
    """
    SYS_PREFIXES = ["/etc", "/proc", "/sys", "/dev", "/boot", "/root",
                    "/run", "/var/lib", "/usr", "/bin", "/sbin", "/lib"]
    # 内核可排除（执行链不依赖）：/usr /lib /bin 等必须保留 allow
    KERNEL_DENY = ["/etc", "/proc", "/sys", "/dev", "/boot", "/root",
                   "/run", "/var/lib", "/sbin"]

    file_rules = "\n".join(
        'forbid(principal, action, resource is File) when {'
        ' resource.path like "%s*" };' % p for p in SYS_PREFIXES)
    cmd_rules = "\n".join(
        'forbid(principal, action, resource is Cmd) when {'
        ' resource.text like "* %s*" };' % p for p in KERNEL_DENY)
    cedar = "@id(\"fs-path\")\n" + file_rules + "\n@id(\"fs-cmd\")\n" + cmd_rules
    return {
        "cedar": cedar,
        "kind": "auto",
        "guard": {"sys_read_deny": list(KERNEL_DENY)},
        "description": "deny system paths (file tools + shell commands; kernel read-deny)",
        "reason": "fs_paths denied system path",
        "cfg": {"allow": []},
    }


def _read_before_edit():
    """DSH-style versioned read-before-edit (decoupled from tool internals).

    State per (agent, session, path) -> observed digest.  Hash tools gate on
    args alone (hashedit.old_hash == observed).  Literal tools compare the
    observed digest against a config-provided digest provider (sha256 adapter);
    without a provider they fall back to a seen-once marker.
    """
    from threading import Lock
    _state: dict[tuple[str, str, str], str] = {}
    _lock = Lock()

    def _key(agent, session, path):
        return (agent, session, path)

    def _path_from(args, keys):
        for k in keys:
            v = args.get(k) if isinstance(args, dict) else None
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    cfg_default = {
        "read_hash": {"hashread": "file_path"},
        "read_literal": {"read": "file_path", "read_file": "path"},
        "write_hash": {"hashedit": ("file_path", "old_hash")},
        "write_literal": {"edit": ("file_path", None),
                          "edit_file": ("file_path", None),
                          "write": ("file_path", None),
                          "write_file": ("path", None)},
        "digest_provider": None,  # callable(path)->digest (sha256 adapter)
    }

    def observe_exec(payload, agent, cfg):
        cfg = {**cfg_default, **(cfg or {})}
        args = payload.get("tool_args") or {}
        tname = payload.get("tool_name", "")
        session = payload.get("session_id", "")
        ok = payload.get("ok", True)
        if not ok:
            return
        if tname in cfg["read_hash"]:
            p = _path_from(args, (cfg["read_hash"][tname],))
            if not p:
                return
            result = payload.get("tool_result") or {}
            text = result.get("output") if isinstance(result, dict) else str(result)
            digest = (text or "").strip()
            with _lock:
                _state[_key(agent, session, p)] = digest or "seen"
        elif tname in cfg["read_literal"]:
            p = _path_from(args, (cfg["read_literal"][tname],))
            if not p:
                return
            provider = cfg["digest_provider"]
            digest = provider(p) if provider else "seen"
            with _lock:
                _state[_key(agent, session, p)] = digest

    def veto(ctx, cfg):
        cfg = {**cfg_default, **(cfg or {})}
        args = getattr(ctx, "tool_args", None) or {}
        tname = ctx.tool_name
        agent = getattr(getattr(ctx, "spec", None), "name", "")
        session = ctx.session_id
        if tname in cfg["write_hash"]:
            pkey, hkey = cfg["write_hash"][tname]
            p = _path_from(args, (pkey,))
            expected = args.get(hkey) if isinstance(args, dict) else None
            if not p or not expected:
                return None
            with _lock:
                observed = _state.get(_key(agent, session, p))
            if observed is None:
                return f"read_before_edit: {p} not read — read it first"
            if observed != expected.strip():
                return f"read_before_edit: {p} changed (stale) — re-read it"
            return None
        if tname in cfg["write_literal"]:
            pkey, _h = cfg["write_literal"][tname]
            p = _path_from(args, (pkey,))
            if not p:
                return None
            with _lock:
                observed = _state.get(_key(agent, session, p))
            provider = cfg["digest_provider"]
            if provider:
                current = provider(p)
                if observed is None or observed == "seen":
                    return f"read_before_edit: {p} not read — read it first"
                if current != observed:
                    return f"read_before_edit: {p} changed (stale) — re-read it"
            elif observed is None:
                return f"read_before_edit: {p} not read — read it first"
            return None
        return None

    return {"veto": veto, "observe_exec": observe_exec,
            "cfg": cfg_default}


def _net_policy():
    """Deny agent access to specific gateways/hosts (IP/CIDR/domain).

    No built-in target set (device/user specific): cfg.deny is supplied at
    enable time.  Enforcement is command-level (bash/execute arg URLs/IPs).
    Topology note: in host-network sandboxes 127.0.0.1 == host loopback.
    """
    def _hosts_from(cmd):
        hosts = []
        for m in re.finditer(r"(?:https?://)?([\w.-]+)(?::\d+)?(?:[/\s]|$)", cmd):
            tok = m.group(1)
            if tok and "." in tok and tok not in hosts:
                hosts.append(tok)
        return hosts

    def _is_ip(s):
        try:
            ipaddress.ip_address(s)
            return True
        except ValueError:
            return False

    def _match(target, deny):
        for d in deny:
            d = d.strip()
            if not d:
                continue
            if "/" in d and _is_ip(d.split("/")[0]):
                try:
                    net = ipaddress.ip_network(d, strict=False)
                    ip = ipaddress.ip_address(target)
                    if ip in net:
                        return True
                except ValueError:
                    pass
            elif _is_ip(d):
                if target == d:
                    return True
            elif target == d or target.endswith("." + d):
                return True
        return False

    def veto(ctx, cfg):
        tname = ctx.tool_name
        if tname not in ("bash", "execute", "run_script"):
            return None
        cmd = _first_arg(ctx, ("cmd", "command", "script"))
        if not cmd:
            return None
        deny = (cfg or {}).get("deny", [])
        if not deny:
            return None
        for host in _hosts_from(cmd):
            if _match(host, deny):
                return f"net_policy denied target {host}"
        return None

    return {"veto": veto, "cfg": {"deny": []}}


BUILTIN_PRESETS = {
    "bash_whitelist": _bash_whitelist,
    "fs_paths": _fs_paths,
    "read_before_edit": _read_before_edit,
    "net_policy": _net_policy,
}


