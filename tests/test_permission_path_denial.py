"""Regression: mapped-but-unauthorised paths must raise PermissionError.

R1 measured a real bug in `_check_command_paths`: when the READ check raised
PermissionError it called itself instead of re-checking as WRITE, so a path that
maps to the host but is outside `file_access` (e.g. `/ws/xyz/f` under the
`/ws/x` mapping) recursed forever and surfaced as `RecursionError`.

These tests go through the PUBLIC tool handlers (no private helper), for both
`bash` and `sandbox_execute`.
"""
from __future__ import annotations

import pytest

from harness.tool_registry import ToolRegistry


@pytest.fixture
def reg():
    reg = ToolRegistry()
    from harness.tools.sandbox_exec import register as _register
    _register(reg)
    return reg


@pytest.fixture
def deps():
    """`/tmp/x/` is granted read_only; `/ws/x` maps to `/tmp/x`."""
    return {
        "execute": lambda cmd: ("ok", "", 0),
        "permissions": {"file_access": {"/tmp/x/": "read_only"}},
        "audit_log": [],
        "path_map": {"/ws/x": "/tmp/x"},
    }


@pytest.mark.parametrize("handler", ["bash", "sandbox_execute"])
def test_mapped_but_unauthorised_path_is_permission_denied(reg, deps, handler):
    """`cat /ws/xyz/f` maps to /tmp/xyz/f, outside /tmp/x/ → clean denial."""
    reg.inject(deps)
    with pytest.raises(PermissionError, match="not in agent permissions"):
        reg.get_handler(handler)("cat /ws/xyz/f")


@pytest.mark.parametrize("handler", ["bash", "sandbox_execute"])
def test_authorised_read_only_path_still_allowed(reg, deps, handler):
    """The same mapping, but inside the grant: a read is allowed (no false deny)."""
    reg.inject(deps)
    result = reg.get_handler(handler)("cat /ws/x/f")
    assert "ok" in result
