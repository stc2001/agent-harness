"""Audit log — security event recording for Agent Harness.

Writes events to:
    - Global:  {base_dir}/audit_log.jsonl  (all events)
    - Per-session: {base_dir}/{session_id}/audit.jsonl  (events with session_id)

Thread-safe via per-instance threading.Lock.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    """Thread-safe audit log with global + per-session dual write."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._global_path = self._base_dir / "audit_log.jsonl"
        self._lock = threading.Lock()

    def append(
        self,
        level: str,
        category: str,
        event: str,
        job_id: str | None = None,
        session_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit event.

        Writes to global log always, and to per-session log if session_id given.
        """
        entry: dict[str, Any] = {
            "ts": time.time(),
            "level": level,
            "category": category,
            "event": event,
            "job_id": job_id,
            "session_id": session_id,
            "detail": detail or {},
        }
        line = json.dumps(entry, ensure_ascii=False)

        with self._lock:
            # Global log
            with self._global_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

            # Per-session log
            if session_id:
                session_path = self._base_dir / session_id / "audit.jsonl"
                session_path.parent.mkdir(parents=True, exist_ok=True)
                with session_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
