"""I-40: 工具路径权限前置检查 — 挂载根误拒 + shell 元字符粘连 — TDD."""

from __future__ import annotations

import pytest

from harness.tools.file_io import _check_access
from harness.tools.sandbox_exec import _extract_paths

HOST_WS = "/host/ws"
PATH_MAP = {"/ws": HOST_WS}
PERMS_SLASH = {"file_access": {HOST_WS + "/": "read_write"}}
PERMS_NO_SLASH = {"file_access": {HOST_WS: "read_write"}}


def _denied(path, perms=PERMS_SLASH, path_map=PATH_MAP):
    try:
        _check_access(path, perms, "write", None, path_map=path_map)
        return False
    except PermissionError:
        return True


def test_mount_root_is_allowed():
    """挂载根 /ws 属于已授权前缀（此前因尾斜杠不匹配被拒）。"""
    assert not _denied("/ws")


def test_subpath_is_allowed():
    assert not _denied("/ws/summary_zh.md")


def test_sibling_prefix_still_denied():
    """边界安全：/ws-other 不得被 /ws 的授权前缀覆盖。"""
    assert _denied("/ws-other")


def test_permission_key_without_trailing_slash_equivalent():
    assert not _denied("/ws", perms=PERMS_NO_SLASH)
    assert not _denied("/ws/x", perms=PERMS_NO_SLASH)
    assert _denied("/ws2", perms=PERMS_NO_SLASH)


def test_extract_paths_splits_shell_metacharacters():
    paths = _extract_paths("ls -la /ws; echo ---; ls -la /ws")
    assert "/ws" in paths
    assert "/ws;" not in paths


def test_extract_paths_handles_and_or_and_redirect():
    assert "/ws" in _extract_paths("cd /ws && ls")
    assert "/ws/out.txt" in _extract_paths("echo hi > /ws/out.txt")
    assert "/ws/in.txt" in _extract_paths("cat < /ws/in.txt")


def test_bash_tool_executes_command_touching_mount_root():
    """工具层回归：引用了挂载根的命令必须被执行（不被前置检查拦下）。"""
    from harness.tool_registry import ToolRegistry
    from harness.tools.sandbox_exec import register as register_exec

    reg = ToolRegistry()
    register_exec(reg)
    calls = []
    reg.inject({
        "execute": lambda cmd: (calls.append(cmd), ("ok", "", 0))[1],
        "permissions": PERMS_SLASH,
        "audit_log": [],
        "path_map": PATH_MAP,
    })
    handler = reg.get_handler("bash")
    out = handler(command="cd /ws && echo ok > /ws/t.txt")
    assert calls, "command was refused instead of executed: " + str(out)
    assert "denied" not in str(out).lower()
