"""I-18: Program 执行器（SDK 执行面包装）。

Deterministic zero-token script execution — a configuration subset of the
agent class, NOT a new engine: no AgentRuntime / no LLM / no inference loop.

grill 2026-09-04: entry = ``client.program.run``; output written to session
events (primary); sandbox optional (scripts write their own FS artifacts).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar


T = TypeVar("T")


def with_retry(
        fn: Callable[[], T],
        retries: int = 2,
        retryable: Callable[[Exception], bool] | None = None,
        delay: float = 0.0) -> T:
    """Run fn, retrying transient failures up to ``retries`` times.

    Retry semantics (mission §2 TimeoutGuard/RetryPolicy): transient
    failures (default: OSError/ConnectionError) retried; timeouts and other
    permanent failures propagate immediately.  Raises the LAST exception
    when retries are exhausted.
    """
    if retryable is None:
        retryable = lambda exc: isinstance(
            exc, (OSError, ConnectionError))
    last: Exception | None = None
    attempts = 0
    while attempts <= retries:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - policy decides
            attempts += 1
            if not retryable(exc) or attempts > retries:
                raise
            last = exc
            if delay > 0:
                time.sleep(delay)
    assert last is not None
    raise last



def run_script_host(script: str, timeout: float = 60.0,
                    retries: int = 0) -> ProgramResult:
    """Execute a deterministic script on the host (no sandbox)."""
    import subprocess

    def attempt() -> tuple:
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            timeout=timeout,
        )

    try:
        proc = with_retry(attempt, retries=retries)
    except subprocess.TimeoutExpired:
        return ProgramResult(status="timed_out",
                             output="", attempts=1)
    except Exception as exc:  # noqa: BLE001
        return ProgramResult(status="failed", output=str(exc), attempts=1)
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return ProgramResult(
        status="completed" if proc.returncode == 0 else "failed",
        output=out.strip(),
        exit_code=proc.returncode,
    )


def run_script_sandbox(script: str, sandbox_name: str, manager,
                       sandbox_id: str | None = None,
                       retries: int = 0) -> ProgramResult:
    """Execute a deterministic script inside a sandbox entity.

    I-26: sandbox addressed by sandbox_id(uuid) — resolve canonical entity
    (or pinned id) through the manager; scripts may persist artifacts to the
    sandbox FS (ADR-018).  No LLM / no agent loop by construction.
    """
    from harness.sandbox.manager import SandboxEntity

    if sandbox_id is not None:
        entity = manager.ensure(sandbox_name, sandbox_id)
    else:
        entity = manager.get_or_create(sandbox_name)

    def attempt():
        return entity.backend.execute(entity.sandbox_id, script)

    try:
        stdout, stderr, rc = with_retry(attempt, retries=retries)
    except Exception as exc:  # noqa: BLE001
        return ProgramResult(status="failed", output=str(exc), attempts=1)
    output = (stdout or "") + (("\n" + stderr) if stderr else "")
    return ProgramResult(
        status="completed" if rc == 0 else "failed",
        output=output.strip(),
        exit_code=rc,
    )

@dataclass
class ProgramResult:
    """Outcome of one program (script) run."""

    status: str  # completed | failed | timed_out
    output: str = ""
    exit_code: int | None = None
    attempts: int = 1
    session_id: str = ""