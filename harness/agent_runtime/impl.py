"""Module 6 — Agent Runtime: standalone agent execution loop.

AgentRuntime is decoupled from the sandbox as a process, but every agent MUST
have a sandbox assigned (the sandbox is the agent's filesystem workspace).
The sandbox is an independent entity — multiple agents can share the same
sandbox instance. File access control is per-agent, enforced by
AgentRuntime before delegating to sandbox.execute().
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from typing import Any

import openai

from harness.gateway.gateway import Gateway, GateContext
from harness.interfaces import AgentSpec, Event, SandboxInterface
from harness.session_store.impl import SessionStore
from harness.tool_registry import ToolRegistry
from harness.tools import load_builtins
from harness.agent_runtime.stop import install_stop_hooks, validate_stop_reason


# Built-in tools loaded from harness/tools/ at runtime via ToolRegistry.
# Test-only tools (echo, sum, validate) moved to tests/.


class AgentRuntime:
    """Standalone agent that executes tasks via LLM → tool-call → events.

    Every agent MUST have a sandbox assigned. The sandbox provides the
    filesystem workspace. Multiple agents may share one sandbox instance.
    File access control is enforced per-agent based on AgentSpec.permissions.
    """

    def __init__(
        self,
        spec: AgentSpec,
        gateway: Gateway,
        store: SessionStore,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        sandbox: SandboxInterface = None,  # type: ignore[assignment]
        sandbox_instance_id: str = "",
        registry_path: str | None = None,
        audit_log=None,
        # I-13: full resolved LLM configs (primary + optional fallback).
        llm_config=None,
        llm_fallback_config=None,
        # ADR-015 (2026-10-19): the client-level tool registry (copied here so
        # dependency injection stays per-runtime).  None = builtins only.
        tool_registry=None,
    ) -> None:
        if sandbox is None:
            raise ValueError("AgentRuntime requires a sandbox (sandbox cannot be None)")
        self.spec = spec
        self._gateway = gateway
        self.store = store
        self.api_key = api_key
        self._pending_restriction = None  # I-35 exec-stage guard
        self.base_url = base_url
        self._sandbox = sandbox
        self._sandbox_id = sandbox_instance_id  # I-26: uuid sandbox identity
        self._registry_path = registry_path
        self._audit_log = audit_log
        # I-13: llm_config (when provided) fully specifies credentials/model.
        self._llm_config = llm_config
        self._fallback_config = llm_fallback_config
        if llm_config is not None:
            self._client = openai.OpenAI(
                api_key=llm_config.api_key or api_key,
                base_url=llm_config.base_url or base_url)
        else:
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self._fallback_client: openai.OpenAI | None = None
        if self._fallback_config is not None:
            self._fallback_client = openai.OpenAI(
                api_key=self._fallback_config.api_key or api_key,
                base_url=self._fallback_config.base_url or base_url)
        # I-13: backend of the last successful call (for agent_thought marker).
        self._last_llm_backend: str = "primary"
        # ADR-015 (2026-10-19): the pool hands in the client-level registry; we
        # COPY it because `inject()` binds deps per runtime (a shared registry
        # would cross-talk between instances).  Direct AgentRuntime callers keep
        # the builtins default.
        self._tool_registry = (tool_registry.copy()
                               if tool_registry is not None else load_builtins())
        self._spawner = None
        self._config: dict[str, Any] = {}
        self._current_session_id: str | None = None
        # ADR-055: which driver rendered the last request (audit/OTel attribution)
        self._last_llm_driver: str = ""
        # I-10/ADR-043 lifecycle: compiled -> idle <-> running -> stopped
        self._status: str = "compiled"
        self._last_idle_at: float | None = None
        self._last_truncation_sig: tuple | None = None  # audit dedup
        self.instance_id: str = ""  # assigned by the pool
        self._deps_injected = False
        self._last_injected_config = ""  # Sandbox config fingerprint for re-entrant inject
        self._sandbox_config = None
        # I-11/ADR-043: wake machinery — worker thread + FIFO inbox.
        self._inbox: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._loop_lock = threading.Lock()
        self._stop_requested = False
        self._disposed = False
        # I-12/ADR-044: pending app stop request (verdict at next step window).
        self._stop_request: dict[str, Any] | None = None
        self._stop_request_lock = threading.Lock()
        install_stop_hooks()  # idempotent — first runtime installs built-ins
        self._current_task: dict[str, Any] | None = None

    def _inject_tool_deps(self) -> None:
        """Inject runtime dependencies into the tool registry.

        Re-entrant: rebuilds path_map when sandbox config changes
        (multi-epoch pipelines, shared-folder host path updates).
        """
        config_fingerprint = str(self._sandbox_config or {})
        if self._deps_injected and config_fingerprint == self._last_injected_config:
            return

        # Build sandbox_path → host_path mapping for host-aware permission checks
        path_map: dict[str, str] = {}
        if self._sandbox_config:
            for folder in getattr(self._sandbox_config, "shared_folders", []) or []:
                sb = folder.get("sandbox_path", "")
                host = folder.get("host_path", "")
                if sb and host:
                    path_map[sb] = host
            # Sort by sandbox_path length descending so longer (more specific)
            # prefixes are matched first by _resolve_host_path.
            path_map = dict(sorted(path_map.items(), key=lambda x: len(x[0]), reverse=True))

        self._tool_registry.inject({
            "execute": lambda cmd: self._sandbox.execute(
                self._sandbox_id, cmd,
                restrict=self._pending_restriction),
            "permissions": self.spec.permissions,
            "audit_log": self._audit_log,
            "sandbox_config": self._sandbox_config,
            "path_map": path_map,
        })
        self._deps_injected = True
        self._last_injected_config = config_fingerprint

    def _call_llm(self, messages: list[dict[str, Any]],
                  tools: list[dict[str, Any]] | None = None) -> tuple[Any, str]:
        """Call the LLM with provided tools. Returns (message, finish_reason).

        I-07/ADR-041: thinking mode is opt-in per spec and passed through as
        extra_body — the framework never hardcodes vendor behaviour.

        I-09/ADR-042: the call runs inside the llm.call hook point so
        around hooks can wrap it (routing/metering/span) and veto hooks
        can block it.

        I-13: single-level fallback — on whitelisted transient failures
        (connection/timeout/rate-limit/5xx) the primary attempt is retried
        once against ``llm_fallback_config``.  Each attempt triggers
        ``llm.call`` independently with backend/attempt in the context.
        Sets ``self._last_llm_backend`` (primary|fallback) for the
        agent_thought marker.
        """
        from openai import (
            APIConnectionError, APITimeoutError, RateLimitError,
            InternalServerError,
        )
        _TRANSIENT = (APIConnectionError, APITimeoutError, RateLimitError,
                      InternalServerError)

        try:
            msg, finish = self._call_llm_once(
                messages, tools, backend="primary", attempt=1)
            self._last_llm_backend = "primary"
            return msg, finish
        except _TRANSIENT as exc1:
            if self._fallback_client is None:
                raise
            if self._audit_log is not None:
                self._audit_log.append(
                    level="warn", category="llm", event="agent_llm_fallback",
                    session_id=self._current_session_id,
                    detail={
                        "primary": getattr(self.spec, "llm_profile", ""),
                        "fallback": getattr(self.spec, "llm_fallback", ""),
                        "reason": str(exc1)[:300],
                    })
            try:
                msg, finish = self._call_llm_once(
                    messages, tools, backend="fallback", attempt=2)
                self._last_llm_backend = "fallback"
                return msg, finish
            except Exception as exc2:
                raise RuntimeError(
                    f"LLM call failed — primary: {exc1}; fallback: {exc2}"
                ) from exc2

    def _call_llm_once(self, messages: list[dict[str, Any]],
                       tools: list[dict[str, Any]] | None,
                       backend: str, attempt: int) -> tuple[Any, str]:
        """One LLM attempt against the primary/fallback client.

        ADR-054: an explicit spec.model wins, else the profile's model; extra_body
        carries thinking / adapter / reasoning_effort.  The ``llm.call`` hook payload gains backend/attempt
        so metering (I-15) and tracing (I-21) can attribute per attempt.
        """
        cfg = self._llm_config if backend == "primary" else self._fallback_config
        # ADR-054 §5: ONE resolution for both the request model and the pool
        # key.  Explicit spec.model wins; otherwise the profile's; empty on both
        # sides fails loudly instead of silently using a hardcoded default.
        from harness.interfaces import resolve_spec_model
        model = resolve_spec_model(
            getattr(self.spec, "model", ""),
            getattr(cfg, "model", "") if cfg is not None else "")
        client = self._client if backend == "primary" else self._fallback_client

        # Temperature: upstream hardcodes 0.0 (greedy). A local profile may
        # override it; None keeps greedy.
        temp = 0.0
        if cfg is not None and getattr(cfg, "temperature_override", None) is not None:
            temp = float(cfg.temperature_override)

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            tools=tools or [],
            temperature=temp,
        )
        # Unlimited output is expressed by OMITTING the field, not by -1/None:
        # DeepSeek rejects -1 ("expected u32") while llama.cpp accepts it, so
        # omitting is the only portable "no limit".
        if self.spec.max_tokens is not None:
            kwargs["max_tokens"] = self.spec.max_tokens
        extra: dict[str, Any] = {}
        # ── ADR-055: vendor rendering belongs to the DRIVER, not the runtime ──
        # The framework owns the SEMANTICS (ADR-054: an explicit AgentSpec value
        # wins over the profile, and a contradicting override is audited); the
        # driver owns the FIELD NAMES (llama.cpp wants
        # `chat_template_kwargs.enable_thinking`, others read a neutral
        # `thinking`).  Keeping `if backend == "local"` here would violate
        # ADR-041 §4 and turn the runtime into a pile of vendor branches.
        from harness.llm_driver import driver_for
        thinking_flag = getattr(self.spec, "thinking", None)
        profile_ctk = (getattr(cfg, "chat_template_kwargs", None)
                       if cfg is not None else None) or {}
        try:
            _driver = driver_for(self.spec, cfg)
            extra.update(_driver.build_extra_body(spec=self.spec, cfg=cfg,
                                                  thinking=thinking_flag))
            self._last_llm_driver = _driver.name
        except Exception as exc:
            # A named-but-unregistered driver must fail loudly rather than let a
            # different renderer run silently (ADR-055 §3).
            raise RuntimeError(
                "LLM driver resolution failed: %s" % exc) from exc

        # Q path (ADR-054 §4): an explicit spec value that CONTRADICTS the
        # profile must never win silently — that is exactly how the original
        # defect hid.  Compare what the driver actually produced.
        if thinking_flag is not None and self._audit_log is not None:
            sent = (extra.get("chat_template_kwargs") or {}).get("enable_thinking")
            prior = profile_ctk.get("enable_thinking")
            if prior is not None and sent is not None and bool(prior) != bool(sent):
                self._audit_log.append(
                    level="info", category="llm", event="agent_spec_override",
                    session_id=self._current_session_id,
                    detail={
                        "field": "thinking",
                        "driver": self._last_llm_driver,
                        "rendered_as": "chat_template_kwargs.enable_thinking",
                        "profile_value": bool(prior),
                        "spec_value": bool(thinking_flag),
                        "profile": getattr(self.spec, "llm_profile", ""),
                        "rule": "ADR-054 §4 Q path",
                    })

        if getattr(self.spec, "adapter", ""):
            extra["adapter"] = self.spec.adapter
        if getattr(self.spec, "reasoning_effort", ""):
            extra["reasoning_effort"] = self.spec.reasoning_effort
        if extra:
            kwargs["extra_body"] = extra

        def do_call(_payload: Any = None):
            response = client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            # I-15 (ADR-042 §8): surface the server's authoritative token
            # usage to any llm.call around-hook consumer (app layer meters /
            # audits / routes). No estimation, no aggregation — a backend
            # that omits usage (usage is None) leaves the fields unset.
            usage = getattr(response, "usage", None)
            if usage is not None:
                set_context_fields(
                    tokens_in=getattr(usage, "prompt_tokens", None),
                    tokens_out=getattr(usage, "completion_tokens", None),
                )
            # Normalise a MISSING finish_reason carefully.  Treating it as
            # "stop" unconditionally let an empty response be reported as a
            # natural completion (`agent_completed` with output=None).  A missing
            # reason is only evidence of completion when the round actually
            # produced something; otherwise keep it missing — None != "stop", so
            # the no-progress guard below catches it.
            _fr = choice.finish_reason
            if not _fr:
                _produced = bool((choice.message.content or "").strip()
                                 or choice.message.tool_calls)
                _fr = "stop" if _produced else None
            return choice.message, _fr

        from harness.hooks import HookRegistry, points
        from harness.hooks.context import set_context_fields
        result = HookRegistry.get().trigger(
            points.LLM_CALL,
            {"messages": messages, "tools": tools or []},
            final=do_call,
            agent=getattr(self.spec, "name", None),
            session_id=self._current_session_id,
            backend=backend,
            attempt=attempt,
        )
        if result.vetoed:
            raise RuntimeError("llm.call vetoed: " + str(result.veto_reason))
        return result.payload

    def llm_messages(self, session_id: str, system_prompt: str = "",
                     task: str | None = None, event_id: str | None = None,
                     overrides: dict[str, Any] | None = None,
                     ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """I-43: 组装产物**单一真源**（runtime loop 与 SDK/CLI 预览共用）。

        管道：logical_view（rewind 重放）→ assemblable（消息链形状）→
        build_messages（预算截断/折叠/reasoning passback）→ tool.result hooks。
        任何一方绕过本方法都会造成「预览与实际请求漂移」（I-41 的教训）。
        """
        from harness.prompt_preview import compose_llm_context
        out = compose_llm_context(
            self.store, self.spec, session_id,
            system_prompt=(system_prompt or None),
            task=task, event_id=event_id, overrides=overrides,
            agent_name=getattr(self.spec, "name", None))
        self._audit_truncation(session_id, out.get("meta") or {})
        return out["messages"], out["meta"]

    def _audit_truncation(self, session_id: str, meta: dict) -> None:
        """Record a WARNING whenever the assembly fallback discards context.

        Truncation is not routine: the app layer owns memory policy (ADR-001's
        high-water state machine) and the harness only guarantees the request
        fits the model's window (ADR-053 revision).  So a truncation means rounds
        were dropped for budget reasons WITHOUT the app having compacted them
        first — the condition an operator must be able to see.  It also makes the
        app's high-water trigger unreachable (dropping caps the assembled cost),
        so silence here would hide a real misconfiguration.

        Deduplicated by the shape of the loss: this method is called from every
        assembly (including previews), and the same loss would otherwise be
        reported many times.
        """
        unresolvable = bool(meta.get("over_budget_unresolvable"))
        truncated = bool(meta.get("truncated"))
        if not truncated and not unresolvable:
            self._last_truncation_sig = None
            return
        sig = (unresolvable, meta.get("dropped_rounds"),
               meta.get("dropped_event_count"), meta.get("kept_rounds"),
               meta.get("hard_limit"))
        if sig == getattr(self, "_last_truncation_sig", None):
            return
        self._last_truncation_sig = sig
        if self._audit_log is None:      # audit is optional in bare runtimes
            return

        # Two DIFFERENT outcomes, deliberately not one umbrella event:
        #   * history was dropped and the request now fits -> the fallback doing
        #     its job (warn).  The name says WHAT was truncated: the history.
        #     SYS and the Task anchor are never truncated, so "prompt_truncated"
        #     would misdescribe it.
        #   * nothing left to drop and it still does not fit -> the app's fixed
        #     overhead (SYS + slots) exceeds the window.  That is a CONFIG error,
        #     not a fallback, and must be loud (error).
        detail = {
            "dropped_rounds": meta.get("dropped_rounds"),
            "dropped_event_count": meta.get("dropped_event_count"),
            "kept_rounds": meta.get("kept_rounds"),
            "budget_tokens": meta.get("budget_tokens"),
            "hard_limit": meta.get("hard_limit"),
            "preserved": ["system", "task", "memory_slots"],
        }
        if unresolvable:
            detail["note"] = ("SYS + the Task anchor (the irreducible set) exceed "
                              "the context limit on their own; no history was "
                              "available to drop. Fix the app's fixed overhead "
                              "or raise the configured context window.")
            self._audit_log.append(
                level="error", category="context",
                event="over_budget_unresolvable", session_id=session_id,
                detail=detail)
        else:
            detail["note"] = ("dropped the OLDEST history rounds; SYS and the "
                              "Task anchor are never truncated")
            self._audit_log.append(
                level="warn", category="context",
                event="history_context_truncated", session_id=session_id,
                detail=detail)

    def _emit_event(self, session_id: str, event_type: str,
                    payload: dict[str, Any],
                    parent_event_id: str | None = None,
                    event_id: str | None = None) -> str:
        """Persist an event to the session store. Returns the event_id."""
        event_id = event_id or str(uuid.uuid4())
        event = Event(
            event_id=event_id,
            session_id=session_id,
            event_type=event_type,
            timestamp=time.time(),
            source=self.spec.name,
            parent_event_id=parent_event_id,
            payload=payload,
        )
        self.store.append_event(event)
        return event_id

    # ── spawn subagent ────────────────────────────────────────

    def set_spawner(self, spawner) -> None:
        """Inject spawner for subagent creation.

        Called by AgentPool after acquire().  spawn_subagent tool
        calls receive the spawner as an extra keyword argument at
        invocation time (via _execute_single_tool).
        """
        self._spawner = spawner

    # ── agent loop ────────────────────────────────────────────

    def _agent_loop(self, session_id: str,
                    system_prompt: str = "",
                    tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Core agent loop with timeout and max_iterations guard.

        Never raises.  All error paths return a status dict.
        """
        start_time = time.time()
        self._inject_tool_deps()

        llm_call_count = 0
        # Output-limit continuations (see the "length" branch below).  Bounded so
        # a model that always truncates cannot loop forever.
        continuations = 0
        MAX_CONTINUATIONS = 4
        # Text produced before the output limit hit.  Without accumulating it,
        # a continued answer ends up reporting ONLY its final fragment (measured:
        # '7 8 9' instead of '1 2 3 4 5 6 7 8 9'), i.e. the earlier pieces are
        # still lost even though the model was correctly resumed.
        carried = ""

        for _ in range(self.spec.max_iterations):
            # ── I-12/ADR-044: framework 硬闸门 ──
            # timeout / max_llm_calls / sandbox_death 是事实 → agent.stop.framework
            # veto 链 → 强制停（app 不可覆盖，fail-closed）。
            fw_stop = self._framework_stop_check(session_id, start_time,
                                                 llm_call_count)
            if fw_stop:
                return fw_stop

            # ── I-12/ADR-044: app 软裁决 ──
            # 待决 stop 请求 → agent.stop.app veto 链 → 批准优雅停 / 否决续跑。
            app_stop = self._app_stop_check(session_id)
            if app_stop:
                return app_stop

            from harness.hooks import HookRegistry, points
            HookRegistry.get().trigger(
                points.AGENT_PRE_STEP,
                {"iteration": llm_call_count},
                agent=getattr(self.spec, "name", None),
                session_id=session_id,
            )

            # I-41/I-43: runtime 只消费**单一真源** llm_messages——
            # 日志投影（logical_view）与形状投影（assemblable）都在其中。
            messages, _meta = self.llm_messages(session_id, system_prompt)

            # ── ADR-053: 装配期不可满足 ⇒ fail loudly ──
            # 不可再精简集（SYS + mem(prefix) + anchor + mem(suffix) +
            # 最后 N 轮）已超物理上限：发出去只会让服务端在**全额 prefill
            # 之后**用 400 拒绝（实测白付 204s）。框架不自行压缩
            # （ADR-038：压缩触发归 app），只报告"做不到"。
            if _meta.get("over_budget_unresolvable"):
                self._emit_event(session_id, "agent_failed", {
                    "reason": "context_irreducible_set_exceeds_limit",
                    "kind": _meta.get("unresolvable_kind"),
                    "needed_tokens": _meta.get("needed_tokens"),
                    "hard_limit": _meta.get("hard_limit"),
                    "budget_tokens": _meta.get("budget_tokens"),
                    "irreducible_cost": _meta.get("irreducible_cost"),
                    "largest_droppable_unit": _meta.get("largest_droppable_unit"),
                }, event_id="af_%s" % uuid.uuid4().hex[:8])
                return {
                    "status": "context_overflow",
                    "reason": "irreducible_set_exceeds_limit",
                    "error": (
                        "context window cannot fit the irreducible set "
                        "(SYS + memories + task anchor + last "
                        "%s round(s)): needs %s tokens, hard limit %s "
                        "(kind=%s). APP may compress the session "
                        "(client.memory.compress) or raise context_window / "
                        "lower max_tokens." % (
                            getattr(self.spec, "context_min_keep_rounds", 1),
                            _meta.get("needed_tokens"),
                            _meta.get("hard_limit"),
                            _meta.get("unresolvable_kind"))),
                    "output": "",
                }

            msg, finish_reason = self._call_llm(messages, tools)
            llm_call_count += 1

            # ── Traceability: raw LLM response for audit/debug ──
            # I-07/ADR-041: reasoning_content is captured for passback on
            # tool-call rounds (never replayed into plain content).
            reasoning = getattr(msg, "reasoning_content", None)
            self._emit_event(session_id, "agent_thought", {
                "content": msg.content,
                "finish_reason": finish_reason,
                "reasoning_content": reasoning,
                # I-13: which backend served this round (primary|fallback).
                "llm_backend": self._last_llm_backend,
                "tool_calls": [
                    {"id": tc.id, "name": tc.function.name,
                     "arguments": tc.function.arguments}
                    for tc in (msg.tool_calls or [])
                ],
            })

            # ── Tool calls ──
            if msg.tool_calls:
                result = self._execute_tool_calls(
                    msg.tool_calls, session_id)
                if result:              # 工具执行失败
                    return result
                continue                # 成功 → 下一轮 LLM

            # ── Completion ──
            # finish_reason="stop" means the model signalled natural completion.
            # No tool calls + model decided to stop → task is done.
            if finish_reason == "stop":
                full = (carried + (msg.content or "")) if carried else msg.content
                self._emit_event(session_id, "agent_completed",
                                 {"reason": finish_reason,
                                  "output": full,
                                  "continuations": continuations})
                return {"status": "completed", "reason": finish_reason,
                        "output": full}

            # Non-stop finish (e.g. "length" due to token limit) is normally
            # recoverable — continue so the model can resume (existing contract,
            # covered by test_length_with_content_still_continues).
            #
            # BUT a round that produced NEITHER content NOR a tool call is not
            # recoverable: the next iteration sends identical input, so the loop
            # spins with no progress, no error and no signal.  MEASURED across
            # 779 recorded agent_thought events: 63 rounds were
            # `finish_reason=length` with empty content and no tool calls, and
            # they were invisible — the run only ended when the repeated empty
            # assistant messages eventually formed an invalid list and the server
            # answered 400 "Cannot have 2 or more assistant messages at the end
            # of the list", i.e. minutes of wasted prefill later.
            #
            # Failing loudly here turns an unobservable stall into a diagnosable
            # one; the app can then raise the output budget or disable reasoning
            # (a long reasoning block is charged to max_tokens, so it can consume
            # the whole allowance before any content is emitted).
            partial = (msg.content or "")

            # (a) Nothing usable at all: a truncated reasoning block normally ate
            # the whole budget before content began.  Re-sending is identical, so
            # fail loudly instead of spinning (63 such rounds were invisible).
            if not partial.strip() and not msg.tool_calls:
                self._emit_event(session_id, "agent_failed", {
                    "reason": "no_progress_truncated",
                    "finish_reason": finish_reason,
                    "detail": "the model returned neither content nor a tool "
                              "call; a truncated reasoning block is the usual "
                              "cause — raise max_tokens or disable thinking",
                })
                return {"status": "failed",
                        "reason": "no_progress_truncated",
                        "error": "LLM round produced no content and no tool "
                                 "calls (finish_reason=%r); refusing to spin"
                                 % (finish_reason,)}

            # (b) Partial text: KEEP it and ask the model to resume.  Discarding
            # it and re-sending identical input produced byte-identical output
            # forever (measured: three rounds of '1 2 3 4 5 6 7 8 9 10 11 ' —
            # every prefill paid, zero progress).  MEASURED against the real
            # server: appending the partial assistant message WITHOUT an explicit
            # instruction makes the model RESTART from the beginning, so the
            # instruction is required, not decorative.
            if partial.strip() and not msg.tool_calls:
                if continuations >= MAX_CONTINUATIONS:
                    self._emit_event(session_id, "agent_failed", {
                        "reason": "continuation_exhausted",
                        "finish_reason": finish_reason,
                        "detail": "output limit reached %d times without the "
                                  "model finishing; raise max_tokens"
                                  % continuations,
                    })
                    return {"status": "failed",
                            "reason": "continuation_exhausted",
                            "error": "the model kept hitting the output limit "
                                     "after %d continuations; raise max_tokens"
                                     % continuations}
                continuations += 1
                carried += partial
                self._emit_event(session_id, "agent_output_continued", {
                    "continuation": continuations,
                    "finish_reason": finish_reason,
                    "partial_chars": len(partial),
                })
                # The instruction must go through the SESSION, not the local
                # `messages` list: the loop rebuilds messages from
                # `llm_messages()` every iteration (single source of truth,
                # I-41/I-43), so a local append is silently discarded — measured:
                # the model never saw the instruction and produced consecutive
                # assistant messages, which the server later rejects with
                # 400 "Cannot have 2 or more assistant messages at the end".
                # `agent_started` is the existing carrier of user input and is
                # rendered as a `user` message by the assembler (verified), so
                # the continuity survives budget truncation like any other task
                # text — and the instruction stays in the audit trail.
                self._emit_event(session_id, "agent_started", {
                    "input": "Continue exactly where you stopped. "
                             "Do not repeat what you already wrote.",
                    "continuation": continuations,
                })
                continue

            # (c) A truncated TOOL CALL cannot be resumed by appending text:
            # the arguments are incomplete JSON.  Fail loudly so the app can
            # raise the budget rather than act on a half-parsed call.
            self._emit_event(session_id, "agent_failed", {
                "reason": "tool_call_truncated",
                "finish_reason": finish_reason,
                "detail": "the tool call was cut off mid-arguments; raise "
                          "max_tokens so the call completes",
            })
            return {"status": "failed", "reason": "tool_call_truncated",
                    "error": "tool call truncated by the output limit "
                             "(finish_reason=%r)" % (finish_reason,)}

        return {"status": "completed", "reason": "max_iterations",
                "error": None}

    def _execute_tool_calls(
        self,
        tool_calls: list[Any],
        session_id: str,
    ) -> dict[str, Any] | None:
        """Execute all tool calls in a single LLM response.

        Returns None if all succeed (continue to next LLM round).
        Returns a failed status dict if any tool call fails.
        """
        for tc in tool_calls:
            result = self._execute_single_tool(tc, session_id)
            if result:
                return result
        return None

    def _fire_post_tool_hook(self, tool_name: str, tool_args: dict,
                             tool_result: Any, session_id: str,
                             ok: bool) -> None:
        """I-09/ADR-042: fire tool.execute.around hooks after tool use.

        Replaces the legacy _tool_hook PostToolUse callback.  An around
        hook returning a non-None value triggers a hook_result event.
        """
        from harness.hooks import HookRegistry, points
        payload = {"tool_name": tool_name, "tool_args": tool_args,
                   "tool_result": tool_result, "session_id": session_id,
                   "ok": ok}
        result = HookRegistry.get().trigger(
            points.TOOL_EXECUTE, payload,
            agent=getattr(self.spec, "name", None),
            session_id=payload.get("session_id"),
        )
        # Only emit hook_result when a hook actually REWROTE the payload
        # (new object).  Echoing the untouched payload would duplicate the
        # tool_result event on every tool use.
        if result.payload is not payload:
            self._emit_event(session_id, "hook_result",
                             {"tool": tool_name, "result": result.payload})

    def _execute_single_tool(
        self,
        tc: Any,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Execute a single tool call with permission check.

        Returns None on success.
        Returns a failed status dict on error (permission denied, unknown
        tool, tool execution error).

        Emits tool_called (allowed=True/False) and tool_result events.
        """
        tool_name = tc.function.name
        try:
            tool_args = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            tool_args = {}

        # ── Permission check + Atomic Gate via Gateway ──
        # node.config > spec.permissions priority (ADR-026)
        atomic_gate = self._config.get("atomic_gate") or self.spec.permissions.get("atomic_gate")
        gate_type = ""
        if atomic_gate is not None:
            validated = _validate_atomic_gate(atomic_gate) if isinstance(atomic_gate, tuple) else None
            if validated is not None:
                _, tool_list = validated
                if tool_list is None or tool_name in tool_list:
                    gate_type = "atomic"
            elif atomic_gate and not isinstance(atomic_gate, tuple):
                gate_type = "atomic"
        ctx = GateContext(
            gate_type=gate_type,
            session_id=session_id,
            spec=self.spec,
            tool_name=tool_name,
            tool_args=tool_args,
        )
        auth_result = self._gateway.authorize(
            ctx, timeout=self.spec.timeout_seconds or None)
        if auth_result.decision == "denied":
            reason = auth_result.reason or "gateway denied"
            self._emit_event(session_id, "tool_called",
                             {"tool": tool_name, "args": tool_args,
                              "allowed": False,
                              "reason": reason},
                             event_id=tc.id)
            # ADR-033 (2026-10-19 note): a denial must be a CLOSED soft-continue.
            # The old path emitted only tool_called(allowed=False)+agent_failed;
            # the assembler strips an orphan tool call, so the model never saw
            # why and repeated it until max_iterations.
            denial_text = f"[tool error] {reason}"
            self._emit_event(session_id, "tool_result",
                             {"tool": tool_name, "result": denial_text,
                              "denied": True},
                             parent_event_id=tc.id)
            self._emit_event(session_id, "agent_failed",
                             {"error": f"Gateway denied '{tool_name}': {reason}",
                              "exit_code": 1,
                              "represented_by_tool_result": True},
                             parent_event_id=tc.id)
            self._fire_post_tool_hook(tool_name, tool_args, denial_text,
                                      session_id, ok=False)
            if self._audit_log is not None:
                self._audit_log.append(
                    level="warn", category="gate",
                    event="gateway_authorize_denied",
                    session_id=session_id,
                    detail={"tool": tool_name, "agent": self.spec.name,
                            "reason": reason},
                )
            return None
        if auth_result.decision == "modified" and auth_result.amend_args:
            tool_args = {**tool_args, **auth_result.amend_args}

        # ── Tool lookup ──
        try:
            handler = self._tool_registry.get_handler(tool_name)
        except KeyError:
            # ADR-033 (2026-10-19 note): make the unknown tool a closed soft
            # continue too — the model needs the error text to self-correct.
            unknown_text = f"[tool error] Unknown tool: {tool_name}"
            self._emit_event(session_id, "tool_called",
                             {"tool": tool_name, "args": tool_args,
                              "allowed": False, "reason": "unknown tool"},
                             event_id=tc.id)
            self._emit_event(session_id, "tool_result",
                             {"tool": tool_name, "result": unknown_text},
                             parent_event_id=tc.id)
            self._emit_event(session_id, "agent_failed",
                             {"error": f"Unknown tool: {tool_name}",
                              "exit_code": 1,
                              "represented_by_tool_result": True},
                             parent_event_id=tc.id)
            self._fire_post_tool_hook(tool_name, tool_args, unknown_text,
                                      session_id, ok=False)
            if self._audit_log is not None:
                self._audit_log.append(
                    level="error", category="tool",
                    event="tool_error",
                    session_id=session_id,
                    detail={"tool": tool_name, "agent": self.spec.name,
                            "error": f"Unknown tool: {tool_name}"},
                )
            return None

        # ── Tool execution ──
        # I-35: compile enabled guard into the restriction handed to
        # sandbox.execute via the injected execute dependency.
        self._pending_restriction = None
        try:
            from harness.policy import engine as _policy_engine
            self._pending_restriction = _policy_engine.compile_guard(
                getattr(self.spec, "name", ""))
        except Exception as exc:  # fail-closed: don't execute unguarded intent
            self._pending_restriction = None
            guard_text = f"[tool error] guard compile failed: {exc}"
            self._emit_event(session_id, "tool_called",
                             {"tool": tool_name, "args": tool_args,
                              "allowed": False,
                              "reason": guard_text},
                             event_id=tc.id)
            self._emit_event(session_id, "tool_result",
                             {"tool": tool_name, "result": guard_text},
                             parent_event_id=tc.id)
            return None
        try:
            # spawn_subagent gets spawner as extra kwarg (not a DI dep)
            if tool_name == "spawn_subagent":
                tool_result = handler(**tool_args, spawner=self._spawner)
            else:
                tool_result = handler(**tool_args)
        except Exception as exc:
            error_msg = str(exc)[:500]
            tc_event_id = self._emit_event(
                session_id, "tool_called",
                {"tool": tool_name, "args": tool_args, "allowed": True},
                event_id=tc.id)
            self._emit_event(
                session_id, "tool_result",
                {"tool": tool_name, "result": f"Error: {error_msg}"},
                parent_event_id=tc.id)
            self._emit_event(
                session_id, "agent_failed",
                {"error": f"Tool error: {error_msg}", "exit_code": 1},
                parent_event_id=tc.id)
            if self._audit_log is not None:
                self._audit_log.append(
                    level="error", category="tool",
                    event="tool_error",
                    session_id=session_id,
                    detail={"tool": tool_name, "agent": self.spec.name,
                            "error": error_msg},
                )
            return None

        # ── Structured return: dict with "ok" field ──
        if isinstance(tool_result, dict) and "ok" in tool_result:
            ok = tool_result["ok"]
            content = tool_result.get("content", str(tool_result))
            tc_event_id = self._emit_event(
                session_id, "tool_called",
                {"tool": tool_name, "args": tool_args, "allowed": True},
                event_id=tc.id)
            self._emit_event(
                session_id, "tool_result",
                {"tool": tool_name, "result": str(content)},
                parent_event_id=tc.id)
            if not ok:
                error_text = tool_result.get("error", "Tool returned failure")
                exit_code = tool_result.get("exit_code", 1)
                self._emit_event(
                    session_id, "agent_failed",
                    {"error": error_text, "exit_code": exit_code},
                    parent_event_id=tc.id)
            if self._audit_log is not None:
                self._audit_log.append(
                    level="info" if ok else "warn", category="tool",
                    event="tool_result",
                    session_id=session_id,
                    detail={"tool": tool_name, "agent": self.spec.name,
                            "ok": ok,
                            "result_preview": str(content)[:200]},
                )
            # PostToolUse: tool.execute.around hook (I-09/ADR-042)
            self._fire_post_tool_hook(tool_name, tool_args, tool_result,
                                      session_id, ok=ok)
            return None

        # ── Success: emit events ──
        tc_event_id = self._emit_event(session_id, "tool_called",
                         {"tool": tool_name, "args": tool_args,
                          "allowed": True},
                         event_id=tc.id)
        self._emit_event(session_id, "tool_result",
                         {"tool": tool_name, "result": str(tool_result)},
                         parent_event_id=tc.id)

        if self._audit_log is not None:
            self._audit_log.append(
                level="info", category="tool",
                event="tool_result",
                session_id=session_id,
                detail={"tool": tool_name, "agent": self.spec.name,
                        "result_preview": str(tool_result)[:200]},
            )

        # ── Traceability: spawn_subagent → subagent_spawned event ──
        if tool_name == "spawn_subagent" and "Subagent spawned:" in str(tool_result):
            child_id = str(tool_result).split(": ", 1)[1].split(".")[0]
            self._emit_event(session_id, "subagent_spawned", {
                "parent_id": session_id,
                "child_id": child_id,
                "agent_name": tool_args.get("agent_name", ""),
                "task": tool_args.get("task", ""),
            }, parent_event_id=tc_event_id)

        # PostToolUse: tool.execute.around hook (I-09/ADR-042)
        self._fire_post_tool_hook(tool_name, tool_args, tool_result,
                                  session_id, ok=True)

        return None

    @property
    def status(self) -> str:
        """I-10/ADR-043: compiled | idle | running | stopped."""
        return self._status

    def stop(self) -> None:
        """I-10/ADR-043: explicit stop — terminal state, resource released
        by the pool.  Emits an agent.lifecycle stop event.

        I-11/ADR-043: also tears down the wake worker thread and clears
        the inbox (see dispose()).  Full dual-track stop semantics land
        with I-12/ADR-044.
        """
        self._status = "stopped"
        if self._current_session_id is not None:
            from harness.hooks import HookRegistry, points
            HookRegistry.get().trigger(
                points.AGENT_LIFECYCLE,
                {"event": "stop", "status": "stopped",
                 "session_id": self._current_session_id, "explicit": True},
                agent=getattr(self.spec, "name", None),
                session_id=self._current_session_id,
            )
        self.dispose()

    def is_idle_expired(self, idle_timeout: float) -> bool:
        """I-10/ADR-043: idle for longer than the timeout."""
        if self._status != "idle" or self._last_idle_at is None:
            return False
        return (time.time() - self._last_idle_at) > idle_timeout

    # ── I-12/ADR-044: stop 双轨 ─────────────────────────────

    def request_stop(self, reason: str | None, requester: str = "app",
                     session_id: str | None = None) -> bool:
        """I-12/ADR-044: app stop 请求（软裁决，stop 是请求不是结论）。

        仅线程安全地置待决请求；裁决发生在下一个 loop 步窗口
        （``agent.stop.app`` veto 链，见 _app_stop_check）。
        fail-closed：非 RUNNING（§5 状态不符）或 reason 非法
        （missing / no_reason / 未知）→ 立即拒绝（返回 False，不触发 hook）。

        Returns:
            True — 请求已被接受，等待裁决；False — 被拒绝。
        """
        if self._status != "running":
            return False
        if not validate_stop_reason(reason):
            return False
        with self._stop_request_lock:
            self._stop_request = {
                "reason": reason,
                "requester": requester,
                "session_id": session_id or self._current_session_id,
                "ts": time.time(),
            }
        return True

    def _framework_stop_check(self, session_id: str, start_time: float,
                              llm_call_count: int) -> dict[str, Any] | None:
        """I-12/ADR-044: framework 硬闸门 — timeout / 预算 / sandbox 死亡.

        硬条件是**事实**：触发 ``agent.stop.framework`` veto 链（内置 hook
        保留高优先级返回 condition → vetoed），随后 fail-closed——条件仍成立
        即强制停（写 agent_failed，返回 failed dict）。app 无法覆盖。

        Returns:
            failed 结果 dict，或 None（无硬条件）。
        """
        _config = self._config or {}
        timeout = _config["timeout_seconds"] \
                  if "timeout_seconds" in _config \
                  else getattr(self.spec, "timeout_seconds", 300)
        max_calls = _config.get("max_llm_calls") if _config else None

        conditions: list[str] = []
        if time.time() - start_time > timeout:
            conditions.append("timeout")
        if max_calls is not None and llm_call_count >= max_calls:
            conditions.append("max_llm_calls")
        sandbox = getattr(self, "_sandbox", None)
        if sandbox is not None and hasattr(sandbox, "is_alive"):
            try:
                if not sandbox.is_alive(self._sandbox_id):
                    conditions.append("sandbox_death")
            except Exception:
                pass

        if not conditions:
            return None

        from harness.hooks import HookRegistry, points
        HookRegistry.get().trigger(
            points.AGENT_STOP_FRAMEWORK,
            {"condition": conditions, "session_id": session_id, "hard": True},
            agent=getattr(self.spec, "name", None),
            session_id=session_id,
        )

        reason = conditions[0]
        if reason == "timeout":
            self._emit_event(session_id, "agent_failed",
                             {"reason": "timeout", "timeout_seconds": timeout})
            if self._audit_log is not None:
                self._audit_log.append(
                    level="warn", category="runtime",
                    event="agent_timeout",
                    session_id=session_id,
                    detail={"timeout_seconds": timeout,
                            "elapsed": round(time.time() - start_time, 2),
                            "reason": "max_runtime_exceeded"},
                )
            return {"status": "failed", "reason": "timeout",
                    "error": f"Agent exceeded timeout of {timeout}s"}
        if reason == "max_llm_calls":
            self._emit_event(session_id, "agent_failed",
                             {"reason": "max_llm_calls_exceeded",
                              "max_llm_calls": max_calls})
            return {"status": "failed", "reason": "max_llm_calls",
                    "error": f"LLM call budget exhausted ({max_calls} calls)"}
        self._emit_event(session_id, "agent_failed",
                         {"reason": "sandbox_death"})
        return {"status": "failed", "reason": "sandbox_death",
                "error": "Sandbox is not alive"}

    def _app_stop_check(self, session_id: str) -> dict[str, Any] | None:
        """I-12/ADR-044: app 软裁决 — 消费待决 stop 请求。

        批准（链未 veto）→ 优雅停：写 ``agent_completed``{reason: stopped,
        stop_reason}（复用 completed 事件保证 wake 续跑可见前轮，SSE 语义）
        → 返回 stopped 结果；否决 → 写 ``agent_stop_denied`` 事件 + 清请求
        + 返回 None（续跑 / follow-up）。

        Returns:
            stopped 结果 dict，或 None（无请求 / 被否决）。
        """
        with self._stop_request_lock:
            req = self._stop_request
            self._stop_request = None
        if not req:
            return None

        from harness.hooks import HookRegistry, points
        result = HookRegistry.get().trigger(
            points.AGENT_STOP_APP,
            {"reason": req["reason"], "session_id": session_id,
             "requester": req.get("requester", "app")},
            agent=getattr(self.spec, "name", None),
        )
        if result.vetoed:
            self._emit_event(session_id, "agent_stop_denied",
                             {"reason": req["reason"],
                              "verdict": str(result.veto_reason)})
            return None  # 否决 → 续跑 / follow-up
        self._emit_event(session_id, "agent_completed",
                         {"reason": "stopped",
                          "stop_reason": req["reason"], "output": ""})
        return {"status": "stopped", "reason": "stopped",
                "stop_reason": req["reason"]}

    # ── I-11/ADR-043: wake machinery ────────────────────────

    def wake(self, message: Any, session_id: str) -> bool:
        """I-11/ADR-043: enqueue a message and wake the worker thread.

        The worker runs a continuation round on the session (reusing the
        ADR-039 resume-safe context assembly), then returns to IDLE.
        Messages arriving while RUNNING are queued (FIFO) — one loop at a
        time, no duplicate execution.  No-op on STOPPED/disposed agents.

        Returns True if the message was enqueued, False otherwise.
        """
        if self._disposed or self._status == "stopped":
            return False
        self._inbox.put((message, session_id))
        self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        """Lazily start the daemon worker thread (first wake)."""
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_main,
                name=f"agent-wake-{self.instance_id or id(self)}",
                daemon=True,
            )
            self._worker.start()

    def _worker_main(self) -> None:
        """Worker loop: block on the inbox, run one continuation round per
        message, return to blocking.  Exits on stop/dispose."""
        while not self._stop_requested:
            try:
                message, session_id = self._inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                with self._loop_lock:
                    if self._stop_requested or self._disposed:
                        break
                    self._run_agent_loop(message, session_id)
            except Exception:
                # The worker must never die: surface as agent_failed.
                try:
                    self._emit_event(session_id, "agent_failed",
                                     {"error": "worker crash"})
                except Exception:
                    pass
            finally:
                self._inbox.task_done()

    def dispose(self) -> None:
        """I-11/ADR-043: tear down the worker thread and inbox.

        Idempotent.  Joins a running worker (bounded) so no thread leaks.
        After dispose, wake() is a no-op and the agent is STOPPED.
        """
        if self._disposed:
            return
        self._disposed = True
        self._stop_requested = True
        self._status = "stopped"
        worker = self._worker
        if (worker is not None and worker.is_alive()
                and worker is not threading.current_thread()):
            worker.join(timeout=2.0)
        with self._worker_lock:
            self._worker = None
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                break


    def _spec_tools(self) -> list[dict[str, Any]]:
        """I-23 (Q7): builtin tool definitions filtered by spec.tools.

        Mirrors the former run_loop-only logic so every entry point
        (execute_task / run_loop / wake worker) gets spec-consistent tools:
        whitelist filter + spawn_subagent appended when permitted.
        Empty spec.tools -> [] (no tools, pure-LLM tasks).
        """
        all_defs = self._tool_registry.get_definitions()
        tools = [t for t in all_defs
                 if t["function"]["name"] in self.spec.tools]
        if self.spec.permissions.get("subagent_spawn"):
            for t in all_defs:
                if t["function"]["name"] == "spawn_subagent":
                    tools.append(t)
        return tools

    def _run_agent_loop(
        self,
        task_input: str | dict[str, Any] | None,
        session_id: str,
        config: dict[str, Any] | None = None,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Shared wrapper: emit start event → run loop → emit terminal event."""
        # I-23 (Q7): normalize tools=None → spec whitelist (all entry points).
        if tools is None:
            tools = self._spec_tools()
        self._config = config or {}
        self._current_session_id = session_id

        # Emit agent_started with task context (replaces old token-based event)
        tool_names = [t["function"]["name"] for t in (tools or [])[:10]] if tools else []
        payload: dict[str, Any] = {"tools": tool_names}
        if task_input is not None:
            # I-39: 事件是模型 Task 的唯一来源（loop 由事件重建消息），
            # 必须存全文——截断会让模型看不到 200 字符之后的指令。
            payload["input"] = str(task_input)
        retry_error = self._config.get("_retry_error")
        if retry_error:
            payload["_retry_error"] = retry_error
        start_event_id = self._emit_event(session_id, "agent_started", payload)

        # I-11/ADR-039: record the current task (Task 五元组) for
        # checkpoint/crash-recovery (P0-4) to consume.
        self._current_task = {
            "start_event_id": start_event_id,
            "agent_spec": getattr(self.spec, "name", ""),
            # I-39: checkpoint/崩溃恢复需要全文（P0-4 恢复将回放该 prompt）
            "prompt": str(task_input) if task_input is not None else "",
            "max_iterations": self.spec.max_iterations,
        }

        self._status = "running"
        self._last_idle_at = None

        from harness.hooks import HookRegistry, points
        HookRegistry.get().trigger(
            points.AGENT_LIFECYCLE,
            {"event": "start", "session_id": session_id},
            agent=getattr(self.spec, "name", None),
            session_id=session_id,
        )

        try:
            loop_result = self._agent_loop(session_id, system_prompt, tools)
        except Exception as exc:
            self._emit_event(session_id, "agent_failed", {"error": str(exc)})
            # I-39: 异常同样必须把实例放回 IDLE（否则永久占用 running）
            self._finish_run(session_id, "failed")
            return {"status": "failed", "output": None, "error": str(exc),
                    "instance_status": self._status}

        self._finish_run(session_id, loop_result["status"])

        if loop_result["status"] == "completed":
            return {"status": "completed",
                    "output": loop_result.get("reason"),
                    "instance_status": self._status}
        if loop_result["status"] == "stopped":
            # I-12/ADR-044: app 批准停 → 优雅停，实例回 IDLE（可恢复，SSE 语义）
            return {"status": "stopped", "reason": "stopped",
                    "stop_reason": loop_result.get("stop_reason"),
                    "instance_status": self._status}
        return {"status": "failed", "output": None,
                "error": loop_result.get("error"),
                "instance_status": self._status}

    def _finish_run(self, session_id: str, loop_status: str) -> None:
        """I-10/ADR-043 + I-39: loop 退出（含异常）→ IDLE + lifecycle 事件。"""
        from harness.hooks import HookRegistry, points
        HookRegistry.get().trigger(
            points.AGENT_LIFECYCLE,
            {"event": "stop", "status": loop_status,
             "session_id": session_id},
            agent=getattr(self.spec, "name", None),
            session_id=session_id,
        )
        self._status = "idle"
        self._last_idle_at = time.time()

    def execute_task(
        self,
        task_input: str | dict[str, Any],
        session_id: str,
        config: dict[str, Any] | None = None,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute a single task as an independent agent instance.

        I-11/ADR-043: serialized against the wake worker via _loop_lock —
        one loop at a time (a concurrent wake queues, never duplicates).
        """
        with self._loop_lock:
            return self._run_agent_loop(task_input, session_id,
                                        config, system_prompt, tools)

    def run_loop(
        self,
        session_id: str | None = None,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        """Execute the main agent loop until a terminal condition is met."""
        session_id = session_id or os.environ.get("AGENT_SESSION_ID")
        if not session_id:
            raise RuntimeError("AGENT_SESSION_ID environment variable not set")

        if not system_prompt:
            system_prompt = self.spec.system_prompt or (
                "You are an agent runtime assistant."
            )

        # I-11/ADR-043: serialized against the wake worker via _loop_lock.
        # tools=None → _run_agent_loop applies spec whitelist (I-23/Q7).
        with self._loop_lock:
            result = self._run_agent_loop(None, session_id,
                                           None, system_prompt, tools)
        if result["status"] == "failed":
            raise RuntimeError(result.get("error", "Agent failed"))

# ── Helpers ───────────────────────────────────────────────


def _validate_atomic_gate(conf):
    """Validate atomic gate configuration tuple.

    Returns:
        None — gate disabled
        (True, None) — gate all tools
        (True, [tool_names]) — gate only listed tools

    Raises ValueError for:
        - (False, [non-empty list])
        - (True, str) — tool_list must be a list
    """
    if conf is None:
        return None
    enabled, tool_list = conf
    if not enabled:
        if tool_list:
            raise ValueError(
                f"Cannot specify tool list {tool_list} with enabled=False"
            )
        return None
    if tool_list is not None and not isinstance(tool_list, list):
        raise ValueError(
            f"tool_list must be a list, got {type(tool_list).__name__}"
        )
    return conf
