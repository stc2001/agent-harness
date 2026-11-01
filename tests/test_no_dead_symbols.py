"""I-03: static guard — deleted DAG/coordinator symbols must stay gone.

After ADR-038 removed the DAG orchestration layer, the remaining shells
(interfaces.py stubs, GateManager, empty rules.py, node_result branch,
unused _SafeDict) are dead code.  This test scans harness/ and tests/
source and fails if any of them come back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# Symbols that must never reappear anywhere in harness/ or tests/ source.
_DEAD_SYMBOLS = [
    "DAGNode",
    "JobSpec",
    "JobStatusInfo",
    "JobStatus",
    "PhaseResult",
    "PhaseStatus",
    "CapabilityToken",
    "TokenExpiredError",
    "TokenInvalidError",
    "TokenPermissionError",
    "CoordinatorInterface",
    "OrchestratorInterface",
    "SubagentHookContext",
    "GateManager",
    "DAGCycleError",
    "DAGMissingDependencyError",
    "node_result",
]

# The test module itself mentions these names, so exclude it from scans.
_SELF = Path(__file__).name


def _source_files() -> list[Path]:
    root = Path(__file__).resolve().parents[1]  # OH_workspace
    files: list[Path] = []
    for base in (root / "harness", root / "tests"):
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.name == _SELF:
                continue
            files.append(path)
    return files


def test_dead_symbols_absent_from_source() -> None:
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for sym in _DEAD_SYMBOLS:
            if sym in text:
                offenders.append(f"{path.relative_to(Path.cwd())}: {sym}")
    assert not offenders, "Dead symbols found:\n" + "\n".join(offenders)


def test_rules_py_deleted() -> None:
    """The empty gateway/rules.py stub must be gone."""
    rules = Path(__file__).resolve().parents[1] / "harness" / "gateway" / "rules.py"
    assert not rules.exists(), "gateway/rules.py should have been deleted"


def test_safe_dict_not_in_agent_runtime() -> None:
    """agent_runtime keeps no private _SafeDict copy."""
    impl = Path(__file__).resolve().parents[1] / "harness" / "agent_runtime" / "impl.py"
    assert "_SafeDict" not in impl.read_text(encoding="utf-8")
