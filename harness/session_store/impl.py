"""Module 2 — Session Store implementation.

Pure filesystem-based persistence for sessions with atomic append semantics.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from harness.interfaces import Event, SessionNotFoundError


class SessionStore:
    """Filesystem-backed session store implementing SessionStoreInterface.

    Directory layout per session:
        {base_path}/{session_id}/
            meta.json
            events.jsonl
            checkpoints/
                {timestamp}.json
    """

    def __init__(self, base_path: str = "./sessions/") -> None:
        self.base_path = Path(base_path).resolve()
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _session_dir(self, session_id: str) -> Path:
        return self.base_path / session_id

    def _events_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "events.jsonl"

    def _meta_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "meta.json"

    def _checkpoints_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "checkpoints"

    def _ensure_session_exists(self, session_id: str) -> None:
        if not self._session_dir(session_id).exists():
            raise SessionNotFoundError(
                f"Session '{session_id}' not found in store."
            )

    def _get_lock(self, session_id: str) -> threading.Lock:
        """Return a per-session append lock, creating it if necessary."""
        with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def create_session(self, session_id: str, metadata: dict[str, Any]) -> None:
        """Create the session directory tree and initial metadata."""
        session_dir = self._session_dir(session_id)
        if session_dir.exists():
            raise FileExistsError(
                f"Session directory '{session_id}' already exists."
            )

        session_dir.mkdir(parents=True, exist_ok=False)
        (session_dir / "events.jsonl").write_text("")
        (session_dir / "checkpoints").mkdir(exist_ok=False)

        meta_path = session_dir / "meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

    def get_session_metadata(self, session_id: str) -> dict[str, Any]:
        """Read a session's metadata dict ({} if the session is missing).

        I-11: used to find the agent instance a session is bound to.
        """
        meta_path = self._meta_path(session_id)
        if not meta_path.exists():
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def update_session_metadata(self, session_id: str,
                                patch: dict[str, Any]) -> None:
        """Merge ``patch`` into a session's meta.json (I-32 binding edges).

        Raises SessionNotFoundError when the session does not exist.
        """
        meta_path = self._meta_path(session_id)
        with self._get_lock(session_id):
            if not meta_path.exists():
                raise SessionNotFoundError(
                    f"Session not found: {session_id}")
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                meta = {}
            meta.update(patch or {})
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())

    def append_event(self, event: Event) -> None:
        """Append one event as a JSON line to the session's events.jsonl.

        Thread-safe via per-session lock.  Uses kernel-level atomic
        write (< PIPE_BUF) with fsync for durability."""
        session_id = event.session_id
        self._ensure_session_exists(session_id)

        # Serialize to a single line with trailing newline.
        line = json.dumps(
            {
                "event_id": event.event_id,
                "session_id": event.session_id,
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                "source": event.source,
                "parent_event_id": event.parent_event_id,
                "payload": event.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
        line_bytes = line.encode("utf-8")

        events_path = self._events_path(session_id)

        with self._get_lock(session_id):
            with open(events_path, "ab") as f:
                f.write(line_bytes)
                f.flush()
                os.fsync(f.fileno())

    def events_path(self, session_id: str) -> str:
        """I-42: 会话事件文件路径（审计读面入口；物理日志的唯一落盘位置）。"""
        return str(self._events_path(session_id))

    def read_events(self, session_id: str,
                    after_event_id: str | None = None) -> list[Event]:
        """Read events from a session log.

        When ``after_event_id`` is ``None``, returns all events.
        When specified, returns events whose ``event_id`` sorts strictly
        after the given event in the append-only log.
        """
        self._ensure_session_exists(session_id)

        events_path = self._events_path(session_id)
        if not events_path.exists():
            return []

        events: list[Event] = []
        found = after_event_id is None  # skip until marker found

        with open(events_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                raw = json.loads(stripped)

                if not found:
                    if raw["event_id"] == after_event_id:
                        found = True
                    continue

                events.append(
                    Event(
                        event_id=raw["event_id"],
                        session_id=raw["session_id"],
                        event_type=raw["event_type"],
                        timestamp=raw["timestamp"],
                        source=raw["source"],
                        parent_event_id=raw.get("parent_event_id"),
                        payload=raw.get("payload", {}),
                    )
                )

        return events

    def write_checkpoint(
        self, session_id: str, checkpoint: dict[str, Any]
    ) -> None:
        """Write a named checkpoint file."""
        self._ensure_session_exists(session_id)

        timestamp = time.time()
        checkpoint_path = self._checkpoints_dir(session_id) / f"{timestamp}.json"

        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

    def read_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        """Read the most recent checkpoint for a session."""
        self._ensure_session_exists(session_id)

        checkpoints_dir = self._checkpoints_dir(session_id)
        if not checkpoints_dir.exists():
            return None

        checkpoint_files = sorted(
            [
                f
                for f in checkpoints_dir.iterdir()
                if f.is_file() and f.suffix == ".json"
            ]
        )

        if not checkpoint_files:
            return None

        latest = checkpoint_files[-1]
        with open(latest, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_sessions(self) -> list[str]:
        """List all session IDs in the store."""
        if not self.base_path.exists():
            return []

        return sorted(
            [
                d.name
                for d in self.base_path.iterdir()
                if d.is_dir()
                and (d / "meta.json").exists()
                and (d / "events.jsonl").exists()
                and (d / "checkpoints").exists()
            ]
        )
