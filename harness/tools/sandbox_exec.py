"""Built-in tools: sandbox execution — bash, sandbox_execute.

Both execute shell commands inside the agent's sandbox.
- ``bash`` — simple shell executor, now with permission gating
- ``sandbox_execute`` — with permission + audit gating
"""

import re
import shlex

from harness.tools.file_io import _check_access


_SHELL_META = re.compile(r"[;&|()<>\n]")


def _extract_paths(command: str) -> list[str]:
    """Extract file paths from a shell command string.

    Uses shlex to tokenize, then filters tokens that look like
    file paths (containing '/' or starting with '.').  Intentional
    leniency: over-checking is safer than missing a path.

    I-40: shlex does not split shell metacharacters, so `ls -la /ws;`
    used to yield the glued token '/ws;' and trip the permission check.
    Metacharacters are normalised to spaces **for extraction only**
    (the command executed is untouched).
    """
    try:
        tokens = shlex.split(_SHELL_META.sub(" ", command))
    except ValueError:
        return None  # Unparseable — caller must refuse execution
    paths = []
    for raw in tokens:
        token = raw.strip("'\"")
        if not token:
            continue
        if "/" in token or "." in token:
            paths.append(token)
    return paths



def _check_command_paths(paths, permissions, audit_log, path_map) -> None:
    """Gate a shell command's paths, WITHOUT guessing what the command does.

    Try READ first: if every path is readable, the command may run.  This is what
    makes a read-only INPUT tree usable — `cat /ws/src/x` is a read of a
    read-only directory, which is exactly what read-only means.

    Only when a path is not readable (it is read-only for writing, or outside the
    grant) fall back to the WRITE check, so an obvious write intent to a
    read-only or ungranted tree is still refused up front.

    Deliberately NOT attempted: classifying commands ("is `sed` a writer?",
    "is `2>` a file redirect?").  That enumeration is unbounded — new tools,
    new flags, variables, quoting, `$(…)`, `cd`, inline `python3 -c` — and
    ADR-051 §9 already states the pre-call gate CANNOT constrain real fs access.
    The bounded guarantee lives in the kernel (mount mode / Landlock); this is an
    advisory that gives the model fast feedback instead of a doomed execution.
    """
    if not paths:
        return
    try:
        for path in paths:
            _check_access(path, permissions, "read", audit_log,
                          path_map=path_map)
    except PermissionError:
        # Read was refused.  Per ADR-051, re-check each path as WRITE so an
        # obvious write to a read-only/ungranted tree is rejected up front.
        # (The old code called `_check_command_paths` here, recursing into
        # itself; for a mapped-but-unauthorised path that recursed forever
        # and surfaced as RecursionError instead of PermissionError.)
        for path in paths:
            _check_access(path, permissions, "write", audit_log,
                          path_map=path_map)


def register(registry):
    """Register sandbox execution tools on the given ToolRegistry."""

    @registry.register(
        "bash",
        "Execute a shell command in the sandbox environment. "
        "Returns stdout, stderr, and exit_code. "
        "File paths in the command are checked against file_access permissions.",
        {"command": str},
        deps=["execute", "permissions", "audit_log", "path_map"],
    )
    def bash(command, execute=None, permissions=None, audit_log=None,
             path_map=None):
        paths = _extract_paths(command)
        if paths is None:
            return "[refused] command could not be parsed for path extraction"
        _check_command_paths(paths, permissions, audit_log, path_map)
        stdout, stderr, exit_code = execute(command)
        from harness.prompt_assembly import truncate_output
        result = truncate_output(stdout)
        if stderr:
            result += f"\n[stderr]\n{stderr}"
        result += f"\n[exit_code: {exit_code}]"
        return result

    @registry.register(
        "sandbox_execute",
        "Run a shell command in the sandbox environment. "
        "File paths in the command are checked against file_access permissions.",
        {"command": str},
        deps=["execute", "permissions", "audit_log", "path_map"],
    )
    def sandbox_execute(command, execute=None, permissions=None,
                        audit_log=None, path_map=None):
        paths = _extract_paths(command)
        if paths is None:
            return "[refused] command could not be parsed for path extraction"
        _check_command_paths(paths, permissions, audit_log, path_map)
        stdout, stderr, exit_code = execute(command)
        from harness.prompt_assembly import truncate_output
        result = truncate_output(stdout)
        if stderr:
            result += f"\n[stderr]\n{stderr}"
        result += f"\n[exit_code: {exit_code}]"
        return result
