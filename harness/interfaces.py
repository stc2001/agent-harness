"""
Agent Harness — Module Interface Contracts
==========================================

This file defines the public interfaces (Protocols), shared data types (dataclasses),
and custom exceptions. Every module implements its corresponding Protocol;
cross-module communication happens through these interfaces only.

Post-Gateway-refactor module dependency graph (ADR-038 removed the
coordinator / orchestrator DAG layers — agents coordinate themselves):

    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │   gateway    │  │ session_store│  │  agent_spec  │  Layer 0
    └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
           │                 │                 │
           ▼                 ▼                 ▼
    ┌──────────────┐  ┌──────────────┐         │
    │   sandbox    │  │   session    │◄────────┘  Layer 1
    └──────┬───────┘  └──────┬───────┘
           │                 │
           └────────┬────────┘
                    ▼
           ┌────────────────┐
           │  agent_runtime │                              Layer 2
           └───────┬────────┘
                   ▼
           ┌────────────────┐
           │    cli / sdk   │                              Layer 3
           └────────────────┘
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

# ─────────────────────────────────────────────────────────────
# 1. Shared Enums
# ─────────────────────────────────────────────────────────────


class SessionStatus(enum.Enum):
    """Session lifecycle states."""

    CREATED = "created"
    RUNNING = "running"
    WAITING_HUMAN_GATE = "waiting_human_gate"
    COMPLETED = "completed"
    FAILED = "failed"
    REWOUND = "rewound"


# ─────────────────────────────────────────────────────────────
# 2. Shared Data Types
# ─────────────────────────────────────────────────────────────


@dataclass
class Event:
    """A single append-only event in the session log."""

    event_id: str
    session_id: str
    event_type: str
    # Known event_type values (extensible):
    #   agent_started, agent_completed, agent_failed,
    #   tool_called, tool_result,
    #   phase_started, phase_completed, phase_failed,
    #   human_gate_pending, human_gate_resolved,
    #   checkpoint_created, session_rewound, session_forked
    timestamp: float
    source: str  # Agent name that emitted this event
    parent_event_id: str | None = None  # For CI/CD traceability chains
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateInfo:
    """Immutable snapshot of a human gate for audit/query.

    Constructed from session event log entries (human_gate_pending +
    human_gate_resolved) so it survives process restarts.
    """

    event_id: str
    session_id: str
    status: str  # "pending" | "approved" | "rejected" | "modified"
    phase_name: str | None = None
    node_id: str | None = None


@dataclass
class AgentSpec:
    """Immutable agent definition loaded from registry YAML."""

    name: str
    description: str
    sandbox: str = "main"  # Named sandbox instance (refs sandbox.yaml)
    capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)  # Allowed tool names
    # ADR-054 §2: "" (not a vendor default) means "this spec expresses no
    # opinion", so the resolved LLM profile supplies it.  The old default made
    # `profile.model or spec.model` always pick the profile for the REQUEST while
    # spec.model was still used as the model-pool KEY — the two could disagree
    # with no warning.
    model: str = ""
    llm_profile: str = "default"  # References RuntimeConfig name
    llm_fallback: str = ""  # Optional fallback LLM profile
    # I-13: backend selection — "auto" takes the profile's resolved backend;
    # explicit "cloud"/"local" is validated against the profile (fail-fast).
    backend: str = "auto"
    adapter: str = ""          # LoRA adapter id, passed via extra_body
    reasoning_effort: str = ""  # Passed via extra_body (vendor-specific, unvalidated)
    system_prompt: str = ""
    max_iterations: int = 10
    timeout_seconds: int = 300  # Max runtime per task before forced termination

    # Model context window size in tokens.  Used by _build_messages to
    # truncate event history before it overflows the model's context limit.
    # Default: 128K (DeepSeek-Chat / GPT-4o class models).
    context_window: int = 128000
    # Fraction of context_window the ASSEMBLY may occupy.  Default 1.0 = "no
    # opinion": truncation stays the FALLBACK it is documented to be (ADR-053
    # revision), and how much context to use is the APP's decision (ADR-001).
    #
    # MEASURED why this matters: the real request path reads this field
    # (`prompt_preview.compose_llm_context`), so a 0.8 default pre-truncated at
    # 80% of the window while the app's high-water mark sat at 85% — the mark was
    # unreachable and compaction never fired.  A deployment may still tighten it
    # deliberately.
    context_budget_ratio: float = 1.0
    # ADR-053: complete ROUNDS kept in the irreducible set
    # (`SYS + mem(prefix) + anchor + mem(suffix) + last N rounds`).
    # A round = one agent_thought carrying tool_calls + all tool_results that
    # answer it; truncation never splits a round.  The task anchor is
    # independent of N and always kept.  Must be >= 1, else ValueError.
    context_min_keep_rounds: int = 1
    # Output budget per LLM call.  None = NO LIMIT: the field is omitted from the
    # request so the backend applies its own maximum.  A hardcoded default
    # silently capped models that support far more output (a full translation
    # needs far more than 16K).
    # Do NOT use -1 here: MEASURED api.deepseek.com rejects it ("expected u32")
    # while llama.cpp accepts it, so -1 is not portable; omitting is.
    max_tokens: int | None = None
    # ADR-053 R1/P3: budget-only output reservation, used when `max_tokens` is
    # None (request omits the field).  SEPARATE knob from the request output
    # cap: "let the provider decide the output limit" must not mean "the prompt
    # may fill the whole window".  App/profile may override.
    output_reserve_tokens: int = 16384
    permissions: dict[str, Any] = field(default_factory=lambda: {
        "subagent_spawn": False,
        "file_access": {},  # host-path prefixes, e.g. {"/home/user/results": "read_write"}
                           # Checked via path_map (sandbox→host mapping from shared_folders).
                           # Without path_map, checked as sandbox paths (backward-compat).
    })
    skills: list[str] = field(default_factory=list)  # Skill names to load

    # I-07 / ADR-041: explicit thinking controls — the framework does NOT
    # hardcode any vendor behaviour.
    reasoning_passback: bool = True   # Replay reasoning_content on tool-call rounds
    # Tri-state (ADR-054 §2/§4):
    #   None  -> no opinion: the profile's chat_template_kwargs decide (P path)
    #   True  -> thinking ON, explicit: overrides the profile (Q path)
    #   False -> thinking OFF, explicit: overrides the profile (Q path)
    # `False` could not be told apart from "unset", which is how the profile's
    # enable_thinking silently beat an explicit True on the llama.cpp backend.
    thinking: bool | None = None      # Enable thinking mode (extra_body passthrough)

    # I-27: policy presets this agent declares (enabled on register).
    # None/empty = no policy; names must be defined in the policy engine.
    policy: str | list[str] | None = None


@dataclass
class SandboxConfig:
    """Configuration for a named sandbox instance (from sandbox.yaml).

    Sandbox instances are independent of agents. Multiple agents can share
    one sandbox instance. File access control is per-agent, enforced by
    AgentRuntime (not at sandbox mount level).
    """

    backend: str  # "bubblewrap" | "docker"
    firewall: dict[str, Any] = field(default_factory=lambda: {"network": "host"})
    shared_folders: list[dict[str, str]] = field(default_factory=list)
    # [{"host_path": "/home/user/project", "sandbox_path": "/workspace",
    #   "mode": "ro"},  # "ro" → --ro-bind (kernel-level write protection);
    #                     # "rw" (default) → --bind
    # ]

    env_vars: dict[str, str] = field(default_factory=dict)
    # Custom environment variables injected via --setenv.
    # User entries override bwrap defaults (last-write-wins).

    # Backward-compat for spawn() path (not used by Coordinator/InstancePool)
    session_id: str = ""

    # Override the initial command run inside the sandbox.
    # When set, spawn() runs this instead of the AgentRuntime entrypoint.
    # Used by integration tests that need a long-running shell.
    command: list[str] = field(default_factory=list)
    # Named output directories in the sandbox.  declare_output_path tool
    # validates agent-declared paths against this map.
    # {"results": "/workspace/results", "logs": "/workspace/logs"}
    outputs: dict[str, str] = field(default_factory=dict)

    # TTL / idle timeout (seconds). 0 = disabled.
    ttl_seconds: int = 3600  # Max lifetime since spawn
    idle_timeout: int = 0    # Max idle time since last execute() (0 = no idle timeout)
    execute_timeout: int = 60  # Max seconds per execute() call

    # Docker-specific fields (left empty until Docker backend is wired)
    image: str = ""
    cpu_limit: str = ""
    memory_limit: str = ""
    network: str = ""  # "none" | "bridge" | "host" (for Docker backend)


# ─────────────────────────────────────────────────────────────
# 3. Custom Exceptions
# ─────────────────────────────────────────────────────────────


class HarnessError(Exception):
    """Base exception for all Agent Harness errors."""


class SessionNotFoundError(HarnessError):
    """Session directory or metadata not found in SessionStore."""


class SessionStateError(HarnessError):
    """Invalid state transition (e.g. rewinding a completed session)."""


SYS_NR_AMD64 = {
    "socket": 41, "connect": 42, "ptrace": 101, "mount": 165,
    "umount2": 166, "reboot": 169, "keyctl": 250, "bpf": 321,
    "unshare": 272, "setns": 308, "pivot_root": 155, "clone3": 435,
}


@dataclass
class SandboxRestriction:
    """I-34: exec-stage restriction passed to sandbox.execute().

    paths use sandbox-internal absolute paths: rw = writable allow set,
    ro = read-only allow set (base RO system mounts are added by the
    launcher automatically). syscalls_deny accepts x86_64 syscall numbers
    or names from SYS_NR_AMD64.
    """
    paths: dict[str, list[str]] = field(
        default_factory=lambda: {"rw": [], "ro": []})
    sys_read_deny: list[str] = field(default_factory=list)
    syscalls_deny: list[str | int] = field(default_factory=list)

    def __post_init__(self) -> None:
        unknown = set(self.paths) - {"rw", "ro"}
        if unknown:
            raise ValueError(
                f"SandboxRestriction path modes must be rw/ro, got {sorted(unknown)}")
        for mode in ("rw", "ro"):
            for p in self.paths.get(mode, []):
                if not (isinstance(p, str) and p.startswith("/")):
                    raise ValueError(
                        f"path must be sandbox-internal absolute: {p!r}")
        for p in self.sys_read_deny:
            if not (isinstance(p, str) and p.startswith("/")):
                raise ValueError(
                    f"sys_read_deny path must be absolute: {p!r}")
        for s in self.syscalls_deny:
            if isinstance(s, int):
                if s < 0:
                    raise ValueError(f"syscall number must be >=0: {s}")
            elif isinstance(s, str):
                if s not in SYS_NR_AMD64:
                    raise ValueError(f"unknown syscall name: {s}")
            else:
                raise ValueError(f"syscall must be int or str: {s!r}")


class SandboxRestrictionError(HarnessError):
    """I-34: exec-stage restriction could not be applied (fail-closed)."""


class SandboxSpawnError(HarnessError):
    """Failed to spawn Docker container."""


class AgentSpecNotFoundError(HarnessError):
    """Agent specification YAML not found in registry."""


class AgentSpecValidationError(HarnessError):
    """Agent specification failed validation checks."""


class HumanGateTimeoutError(HarnessError):
    """Human gate was not resolved within the timeout window."""


# ─────────────────────────────────────────────────────────────
# 4. Module Protocols (one per module, 7 total)
# ─────────────────────────────────────────────────────────────


class GatewayInterface(Protocol):
    """Gateway — unified authorization engine.

    Converges HumanGate, custom rules, and pre/post hooks into a single
    entry point.  Session-level token management for subagent spawn.
    """

    def authorize(self, ctx: Any) -> Any: ...
    def resolve(self, event_id: str, decision: str, amend: dict | None = None) -> None: ...
    def list_pending(self, session_id: str) -> list: ...
    def get_gate(self, event_id: str, session_id: str) -> Any: ...
    def register_rule(self, fn: Any, name: str = "") -> None: ...


class SessionStoreInterface(Protocol):
    """Module 2 — Session Store: pure persistence I/O for sessions.

    Manages ./sessions/{session_id}/ directory structure,
    append-only JSONL event log, and checkpoint files.
    Contains zero business logic.
    """

    def create_session(self, session_id: str, metadata: dict[str, Any]) -> None:
        """Create the session directory tree and initial metadata.

        Creates:
            ./sessions/{session_id}/
            ./sessions/{session_id}/events.jsonl  (empty)
            ./sessions/{session_id}/checkpoints/   (empty dir)
            ./sessions/{session_id}/meta.json

        Args:
            session_id: Unique session identifier (UUID recommended).
            metadata: Key-value metadata written to meta.json.

        Raises:
            FileExistsError: If the session directory already exists.
            OSError: If directory creation fails.
        """
        ...

    def append_event(self, event: Event) -> None:
        """Append one event as a JSON line to the session's events.jsonl.

        This is append-only — existing lines are never modified.

        Args:
            event: The event to append.

        Raises:
            SessionNotFoundError: If the session directory does not exist.
            OSError: If the write fails.
        """
        ...

    def read_events(
        self, session_id: str, after_event_id: str | None = None
    ) -> list[Event]:
        """Read events from a session log.

        Args:
            session_id: Session to read from.
            after_event_id: Return events after this event ID, or all if None.

        Returns:
            List of Event objects. Empty list if no events exist yet.

        Raises:
            SessionNotFoundError: If the session directory does not exist.
        """
        ...

    def write_checkpoint(
        self, session_id: str, checkpoint: dict[str, Any]
    ) -> None:
        """Write a named checkpoint file.

        Files are written as ./sessions/{session_id}/checkpoints/{timestamp}.json.

        Args:
            session_id: Session to checkpoint.
            checkpoint: Arbitrary JSON-serializable snapshot.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        ...

    def read_checkpoint(self, session_id: str) -> Optional[dict[str, Any]]:
        """Read the most recent checkpoint for a session.

        Args:
            session_id: Session to read from.

        Returns:
            The latest checkpoint dict, or None if no checkpoints exist.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        ...

    def list_sessions(self) -> list[str]:
        """List all session IDs in the store.

        Returns:
            List of session directory names.
        """
        ...


class AgentSpecInterface(Protocol):
    """Module 3 — Agent Spec: load and validate agent definitions from YAML registry."""

    def load(self, name: str) -> AgentSpec:
        """Load an agent specification from registry/{name}.yaml.

        Args:
            name: Agent name (matches the YAML filename without extension).

        Returns:
            Parsed and validated AgentSpec.

        Raises:
            AgentSpecNotFoundError: If the YAML file does not exist.
            AgentSpecValidationError: If the YAML is malformed or missing required fields.
        """
        ...

    def validate(self, spec: AgentSpec) -> list[str]:
        """Validate an AgentSpec and return a list of error messages.

        An empty list means the spec is valid.

        Args:
            spec: The AgentSpec to validate.

        Returns:
            List of human-readable error strings (empty = valid).
        """
        ...

    def list_all(self) -> list[str]:
        """List all available agent names in the registry.

        Returns:
            Sorted list of agent names (without .yaml extension).
        """
        ...


class SandboxInterface(Protocol):
    """Module 4 — Sandbox: Docker container lifecycle management.

    Spawns isolated execution environments, injects capability tokens,
    monitors container health, and collects logs.
    """

    def spawn(self, config: SandboxConfig, sandbox_id: str | None = None) -> str:
        """Start a sandboxed process with the given configuration.

        Phase 2: cap_token is passed via ToolRegistry injection, not env vars.
        The sandbox is a pure isolation boundary; AgentRuntime runs on the host.

        Args:
            config: Sandbox configuration (backend, firewall, shared folders).
            cap_token: Capability token string (reserved, no longer env-injected).

        Returns:
            Sandbox instance ID (PID string for bwrap, container ID for Docker).

        Raises:
            SandboxSpawnError: If container creation/start fails.
        """
        ...

    def stop(self, sandbox_id: str, timeout: int = 5) -> None:
        """Gracefully stop a running container.

        Sends SIGTERM and waits up to timeout seconds.
        Falls back to SIGKILL if process does not exit.

        Args:
            container_id: Docker container ID.
            timeout: Seconds to wait for graceful shutdown.

        Raises:
            SandboxSpawnError: If the stop operation fails.
        """
        ...

    def kill(self, sandbox_id: str , keep_workspace: bool = False) -> None:
        """Force-kill a running container.

        Args:
            container_id: Docker container ID.

        Raises:
            SandboxSpawnError: If the kill operation fails.
        """
        ...

    def is_alive(self, sandbox_id: str) -> bool:
        """Check whether a container is currently running.

        Args:
            container_id: Docker container ID.

        Returns:
            True if the container is running; False otherwise.
        """
        ...

    def get_logs(self, sandbox_id: str) -> str:
        """Retrieve stdout/stderr logs from a container.

        Args:
            container_id: Docker container ID.

        Returns:
            Combined stdout+stderr output since container start.
        """
        ...

    def execute(
        self, sandbox_id: str, command: str
    ) -> tuple[str, str, int]:
        """Run a command inside the sandbox.

        Spawns an isolated instance with the same mount configuration as
        the parent sandbox, then runs `command` inside it.

        Args:
            container_id: Sandbox instance ID.
            command: Shell command to execute.

        Returns:
            (stdout, stderr, exit_code).
        """
        ...


class SessionInterface(Protocol):
    """Module 5 — Session: state machine over SessionStore events.

    Manages session lifecycle (wake/rewind/fork/checkpoint), validates
    state transitions, and provides a high-level API on top of SessionStore.
    """

    @property
    def session_id(self) -> str:
        """The current session ID."""
        ...

    def wake(self, session_id: str) -> SessionStatus:
        """Restore a session from persistent storage and return its status.

        Reads events from SessionStore, replays state transitions,
        and determines the current SessionStatus.

        Args:
            session_id: The session to restore.

        Returns:
            The restored session's current status.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        ...

    def append(self, event: Event) -> None:
        """Append an event, persist it, and advance the state machine.

        Args:
            event: The event to append. session_id must match.

        Raises:
            SessionStateError: If the event causes an invalid state transition.
            SessionNotFoundError: If the session is not active.
        """
        ...

    def checkpoint(self) -> dict[str, Any]:
        """Create a snapshot of the current state and persist it.

        Returns:
            The checkpoint dict that was written.
        """
        ...

    def rewind(self, to_event_id: str) -> None:
        """Rewind the session to a previous event.

        All events after to_event_id are logically discarded.
        The session status is set to REWOUND.
        A new session_rewound event is appended.

        Args:
            to_event_id: The event ID to rewind to (inclusive — this event is kept).

        Raises:
            SessionStateError: If the target event does not exist.
        """
        ...

    def fork(self, new_session_id: str) -> str:
        """Fork the current session into a new session.

        Copies all events up to the current point into a new session.
        The original session is unaffected.

        Args:
            new_session_id: The ID for the forked session.

        Returns:
            The new session ID.

        Raises:
            SessionStateError: If new_session_id already exists.
        """
        ...

    def get_events(self) -> list[Event]:
        """Return all events in the current session.

        Returns:
            Chronological list of events.
        """
        ...

    def get_status(self) -> SessionStatus:
        """Return the current session status.

        Returns:
            Current SessionStatus enum value.
        """
        ...


class AgentRuntimeInterface(Protocol):
    """Module 6 — Agent Runtime: host-side execution loop.

    Runs on the host, not inside the sandbox. Capability token and
    session ID are injected via ToolRegistry, not environment variables.
    """

    def run_loop(self) -> None:
        """Execute the main agent loop until terminal.

        Steps per iteration:
        1. Read latest events from SessionStore to build context.
        2. Call the LLM with the current context and system prompt.
        3. Parse the LLM response for tool calls.
        4. For each tool call: verify authorization via Gateway.authorize().
        5. Execute allowed tools; emit tool_called / tool_result events.
        6. Check terminal condition (max_iterations reached, or LLM signals completion).
        7. Emit agent_completed or agent_failed event.

        Raises:
            HarnessError: If a tool call is blocked by authorization.
        """
        ...


class CLIInterface(Protocol):
    """Module 9 — CLI: command-line user interface.

    Parses subcommands, dispatches to HarnessClient / Session,
    and renders human-readable output.
    """

    def main(self, argv: list[str]) -> int:
        """Entry point for the CLI.

        Supported subcommands:
            harness run <job.yaml>      — Submit and start a job.
            harness status <job_id>     — Query job status.
            harness gate approve <id>   — Approve a pending human gate.
            harness gate reject <id>    — Reject a pending human gate.
            harness gate modify <id>    — Modify and approve a gate.
            harness session list        — List all sessions.
            harness session show <id>   — Show session event log.
            harness session rewind <id> <event_id> — Rewind a session.
            harness cancel <job_id>     — Cancel a running job.

        Args:
            argv: Command-line arguments (sys.argv[1:]).

        Returns:
            Exit code: 0 for success, non-zero for errors.
        """
        ...


@dataclass
class ToolHookContext:
    """Context passed to PostToolUse hook callback."""
    tool_name: str
    tool_args: dict[str, Any]
    tool_result: Any
    session_id: str
    sandbox: SandboxInterface
    store: Any  # SessionStore
    audit: Any  # AuditLog


def resolve_spec_model(spec_model: str, profile_model: str) -> str:
    """The ONE model resolution used by both the request and the pool key.

    ADR-054 §5: an explicit `spec.model` wins; otherwise the profile's model is
    used; if neither is set the caller must fail loudly rather than fall back to
    a hardcoded default (which would silently target the wrong model).
    """
    explicit = (spec_model or "").strip()
    if explicit:
        return explicit
    from_profile = (profile_model or "").strip()
    if from_profile:
        return from_profile
    raise ValueError(
        "no model resolved: AgentSpec.model is empty and the resolved LLM "
        "profile declares no `model`. Set one of them — e.g. `model:` in "
        "configs/llm.yaml (or ~/.harness/llm.d/<profile>.yaml), or an explicit "
        "`model` on the AgentSpec (ADR-054 §5).")
