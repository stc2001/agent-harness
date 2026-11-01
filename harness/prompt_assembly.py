"""Prompt assembly utilities — pure functions shared by SDK and orchestration.

Used by _PromptsClient (SDK) and NodeExecutor (orchestration) as equal
consumers — no layer violation.

Token counting is the one exception to "no IO": when a local llama.cpp server
is configured it is asked to tokenize (authoritative, offline), see
_count_tokens below.
"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable


# ── Tokenizer (lazy-loaded per provider) ────────────────

_TOKENIZERS: dict[str, object] = {}
_TOKENIZER_PATHS: dict[str, str] = {}

# ── Exact local tokenizer via the inference server ──────
#
# WHY: _count_tokens used to always go through tokenizer_dir/<provider>, i.e.
# the DeepSeek BPE.  Against the locally served Spark-X2.5 (gpt2-family BPE,
# vocab 131072) that tokenizer is badly wrong, and its vocab contains NO CJK
# characters at all — a pure-Chinese string encodes to ZERO tokens, so the
# context budget was computed as if Chinese text were free.
#
# Measured divergence (deepseek count vs real prompt_tokens):
#     English prose   25 vs 16   (+56% over)
#     Python          28 vs 35   (-20% under)
#     JSON schema     44 vs 56   (-21% under)
#     Chinese          0 vs 49   (-100% under)
#     mixed zh/en      7 vs 15   (-53% under)
#
# So when an OpenAI-compatible **local** server is configured we ask IT to
# tokenize.  llama.cpp exposes POST /tokenize {content} -> {tokens:[...]}.
# This is exact, offline, and needs no tokenizer files.
#
# SCOPE (R2): strictly localhost.  A remote origin is refused even when a
# profile's base_url points at one, so cloud deployments never ship prompt text
# to an undeclared endpoint and simply keep the HF fallback.
#
# Cost is acceptable: one realistic agentic turn issued 43 _count_tokens calls
# over ~199k chars; the local HF path took ~1.7 s for that, and 43 localhost
# HTTP round-trips are faster than that.
# Results are memoised, bounded, and keyed by a HASH of the text — caching the
# text itself would retain every prompt ever counted for the process lifetime
# (a turn issues ~43 counts over ~199k chars), growing memory with history.
_TOKEN_COUNT_CACHE: "OrderedDict[tuple, int]" = OrderedDict()
_TOKEN_COUNT_CACHE_MAX = 4096
_TOKENIZE_URL_CACHE: str | None | bool = False   # False = not resolved yet
# When the server is down we must not retry on every single call: one turn makes
# ~43 calls and each failed round-trip would add latency for nothing. After a
# failure, skip HTTP for this long (seconds) and use the HF fallback.
_TOKENIZE_DOWN_UNTIL: float = 0.0
_TOKENIZE_DOWN_TTL: float = 30.0
# I-48: why the server endpoint is unavailable, when it was refused rather than
# simply not configured.  Empty string means "no refusal".
_TOKENIZE_DISABLED_REASON: str = ""
_TOKENIZE_WARNED: bool = False


def _warn_tokenizer_degraded(reason: str) -> None:
    """Warn ONCE per process that counting falls back to a CJK-blind tokenizer.

    I-47's fail-closed loopback gate is correct, but on a deployment whose server
    is NOT on loopback (e.g. llama.cpp on another LAN host) it silently falls back
    to the HF tokenizer — where a pure-Chinese string still counts as **0
    tokens**, so the context budget is meaningless.  Silence was the real defect:
    nothing in a log or a returned value revealed it.

    Warnings are emitted via `warnings.warn` (deduped by the warnings machinery
    as well) and guarded by a module flag so a long turn issuing ~43 counts
    prints this once, not 43 times.
    """
    global _TOKENIZE_WARNED
    if _TOKENIZE_WARNED:
        return
    _TOKENIZE_WARNED = True
    import warnings
    warnings.warn(
        "token counting is using the local HF fallback tokenizer, NOT the model "
        "server (%s). On the cloud-profile tokenizer a pure-CJK string counts as "
        "0 tokens, so context budgeting for CJK text is unreliable. Run the "
        "server on loopback (127.0.0.1) or set HARNESS_TOKENIZE_URL to a "
        "loopback endpoint." % reason,
        UserWarning, stacklevel=3)


def _is_loopback(url: str) -> bool:
    """True only for localhost URLs.

    Guards R2: the endpoint must never be a remote origin.  Deriving it from an
    arbitrary profile's base_url meant a CLOUD profile produced
    `https://api.deepseek.com/tokenize`, i.e. the prompt text was POSTed to an
    undeclared remote endpoint (every 30s, since it 404s).  The issue scoped
    this feature to the LOCAL server and promised not to change cloud behaviour.
    """
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")


def _tokenize_url() -> str | None:
    """Resolve the /tokenize endpoint of the **local** server, or None.

    Order: HARNESS_TOKENIZE_URL, then the active LLM profile's base_url
    (configs/llm.yaml -> "default"), then the conventional local port.
    Set HARNESS_TOKENIZE_URL=off to force the HF tokenizer.

    Two semantics, deliberately different:

    * ``HARNESS_TOKENIZE_URL`` is the **complete endpoint** and is used verbatim
      (an operator naming a full URL means it — but it must still be loopback);
    * the value derived from a profile is a **base_url**, so a trailing ``/v1``
      is stripped and ``/tokenize`` appended — llama.cpp serves it at the ROOT
      (POSTing ``/v1/tokenize`` returns 404).

    A non-loopback result is refused and returns None — **including an explicit
    override that names a remote host** (fail-closed: prompt text must never
    leave the box for a count; see ``test_remote_env_override_is_refused``).

    Refusal is never silent: it emits ONE warning per process (I-48) and records
    the reason, because on such a deployment CJK text still counts as 0 tokens.
    """
    global _TOKENIZE_URL_CACHE, _TOKENIZE_DISABLED_REASON
    if _TOKENIZE_URL_CACHE is not False:
        return _TOKENIZE_URL_CACHE  # type: ignore[return-value]

    url: str | None = None
    reason = ""
    env = os.environ.get("HARNESS_TOKENIZE_URL")
    if env:
        env = env.strip()
        if env.lower() in ("off", "0", "none"):
            url = None                      # deliberate opt-out: no warning
        elif _is_loopback(env):
            url = env                      # complete endpoint, used verbatim
        else:
            # explicit but remote: still refuse (I-47 R2) — counting must not egress
            url = None
            reason = "explicit override is not loopback: %s" % env
    else:
        origin = ""
        try:
            from harness.sdk.config import resolve_llm_profile
            cfg = resolve_llm_profile("default", project_dir=Path.cwd())
            origin = (cfg.base_url or "").rstrip("/")
        except Exception:
            origin = ""
        if not origin:
            origin = "http://127.0.0.1:8080/v1"
        if origin.endswith("/v1"):
            origin = origin[: -len("/v1")]
        candidate = origin + "/tokenize"
        if _is_loopback(candidate):
            url = candidate
        else:
            url = None
            reason = "profile base_url is not loopback: %s" % candidate

    _TOKENIZE_DISABLED_REASON = reason
    if reason:
        _warn_tokenizer_degraded(reason)
    _TOKENIZE_URL_CACHE = url
    return url


def _server_count_tokens(text: str) -> int | None:
    """Exact token count from the local server, or None if unavailable."""
    global _TOKENIZE_DOWN_UNTIL
    url = _tokenize_url()
    if not url:
        return None
    # key by HASH: keeping the raw text would retain every prompt the process
    # ever counted. sha1 collisions are irrelevant for a memo key.
    import hashlib
    key = (url, hashlib.sha1(text.encode("utf-8", "replace")).hexdigest())
    hit = _TOKEN_COUNT_CACHE.get(key)
    if hit is not None:
        _TOKEN_COUNT_CACHE.move_to_end(key)
        return hit
    import time as _time
    if _time.monotonic() < _TOKENIZE_DOWN_UNTIL:
        return None                      # recently failed — don't hammer it
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            data=json.dumps({"content": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        # Short timeout: a count must never stall a turn; the negative cache
        # only helps AFTER a failure, so this bounds the very first stall. Kept
        # small (a local tokenizer answers in single-digit ms).
        with urllib.request.urlopen(req, timeout=2) as resp:
            n = len(json.load(resp)["tokens"])
        _TOKEN_COUNT_CACHE[key] = n
        if len(_TOKEN_COUNT_CACHE) > _TOKEN_COUNT_CACHE_MAX:
            _TOKEN_COUNT_CACHE.popitem(last=False)      # LRU eviction
        _TOKENIZE_DOWN_UNTIL = 0.0
        return n
    except Exception:
        _TOKENIZE_DOWN_UNTIL = _time.monotonic() + _TOKENIZE_DOWN_TTL
        return None


def _get_tokenizer(provider: str, tokenizer_dir: str = "tokenizer_dir") -> object:
    """Return the fallback tokenizer for `provider` from `tokenizer_dir/<provider>`.

    `provider` is configured per profile (``LLMConfig.provider``, I-49) and names
    the DIRECTORY holding the fallback tokenizer.  It must match the model being
    served: a mismatch is not a precision issue but a correctness one.

    LOAD THE FAST CLASS, not `AutoTokenizer`.  MEASURED: on these artifacts
    (only `tokenizer.json`, no `tokenizer.model`) `AutoTokenizer.from_pretrained`
    returns the SLOW `LlamaTokenizer`, which does not implement the byte-level BPE
    this vocab depends on, so every CJK string counted as **0 tokens**:

        日文 2973 字符 → 0   (the server reports 2293)
        中文 2160 字符 → 0   (the server reports 1564)

    The vocab itself is fine — it holds ~35,679 byte-level entries (`ä`, `ä¸`,
    `äº`, i.e. UTF-8 bytes mapped to latin-1) — so this was purely a loader
    choice.  Loaded fast, the same file is accurate to within 1%:

        日文 2289 vs 2293 (-0.2%)   中文 1560 vs 1564 (-0.3%)   英文 501 vs 505 (-0.8%)

    A tokenizer that cannot encode non-empty text is a CORRECTNESS bug: budgets,
    water marks and the truncation alarm all silently misreport.  Hence the
    zero-check below — it turns that class of failure loud.

    A missing directory raises a clear error instead of letting transformers
    treat the path as a HuggingFace repo id, which would attempt network access
    and stall on retries (see issues/local-backend-issues.md L2-02).
    """
    if provider not in _TOKENIZERS:
        path = os.path.join(tokenizer_dir, provider)
        if not os.path.isdir(path) and not os.path.isabs(tokenizer_dir or ""):
            # ADR-015 (2026-10-19): a RELATIVE tokenizer_dir is a deployment
            # artifact, not a process-cwd artifact.  An app launched in a user
            # workspace (the pi CLI) must still fall back to the checkout's
            # tokenizer_dir; $HARNESS_TOKENIZER_DIR wins when a deployment moved
            # it.  Without this, budgets fail only because cwd changed.
            for base in (os.environ.get("HARNESS_TOKENIZER_DIR"),
                         os.path.dirname(os.path.dirname(
                             os.path.abspath(__file__)))):
                if not base:
                    continue
                candidate = os.path.join(base, tokenizer_dir, provider)
                if os.path.isdir(candidate):
                    path = candidate
                    break
        if not os.path.isdir(path):
            raise FileNotFoundError(
                f"fallback tokenizer for provider {provider!r} not found at "
                f"{os.path.abspath(path)!r}. Either point the LLM profile's "
                f"`provider` at an existing tokenizer_dir/<provider> directory, "
                f"set HARNESS_TOKENIZER_DIR, or make the model server's "
                f"/tokenize endpoint reachable so exact counting is used and no "
                f"fallback is needed.")
        tok = _load_fast_tokenizer(path)
        _TOKENIZERS[provider] = tok
        _TOKENIZER_PATHS[provider] = path
    return _TOKENIZERS[provider]


def _load_fast_tokenizer(path: str):
    """Load `path` as a FAST tokenizer, verifying it can encode CJK.

    Preference order:
      1. `tokenizer.json` via `PreTrainedTokenizerFast` — honours the file's own
         byte-level BPE, which is what these artifacts need.
      2. `AutoTokenizer.from_pretrained` — for directories that ship a
         SentencePiece `tokenizer.model` instead.
    Whatever loads must pass the CJK check; otherwise we raise rather than hand
    back a counter that reports 0 for real content.
    """
    import json

    tok = None
    json_path = os.path.join(path, "tokenizer.json")
    if os.path.isfile(json_path):
        try:
            from transformers import PreTrainedTokenizerFast
            tok = PreTrainedTokenizerFast(tokenizer_file=json_path)
        except Exception:                                   # noqa: BLE001
            tok = None
    if tok is None:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(path)

    # loud correctness check (see the docstring): non-empty CJK must count
    probe = "厦门朝日俱乐部"
    if not len(tok.encode(probe)):
        raise ValueError(
            f"fallback tokenizer at {path!r} counts CJK as 0 tokens "
            f"(encoded {probe!r} to nothing). It is unusable for budgets; "
            f"either supply a byte-level/fast artifact or keep the model "
            f"server's /tokenize endpoint reachable.")
    return tok


def _count_tokens(text: str, provider: str, tokenizer_dir: str = "tokenizer_dir") -> int:
    """Tokens in ``text``.

    Prefers the local inference server's own tokenizer (exact for the served
    model, and CJK-correct); falls back to tokenizer_dir/<provider> when no
    server is reachable or HARNESS_TOKENIZE_URL=off.

    There are TWO guards around the fallback:
    1. LOAD-time (`_load_fast_tokenizer`): a tokenizer that encodes a CJK probe
       to zero raises immediately.
    2. COUNT-time (here): a cached/already-loaded tokenizer that reports 0 for
       any non-empty text raises instead of silently under-reporting budgets.
    Empty/whitespace-only text legitimately counts as 0.  The server path is
    authoritative and is returned as-is (no count-time guard).
    """
    text = "" if text is None else str(text)
    n = _server_count_tokens(text)
    if n is not None:
        return n
    tok = _get_tokenizer(provider, tokenizer_dir)
    n = len(tok.encode(text))
    if n == 0 and text.strip():
        path = _TOKENIZER_PATHS.get(provider,
                                    os.path.abspath(os.path.join(
                                        tokenizer_dir, provider)))
        raise ValueError(
            f"fallback tokenizer for provider {provider!r} at {path!r}: "
            "non-empty text counted as 0 tokens; budgets/water marks would be "
            "silently wrong. Supply a fast/byte-level artifact or keep the "
            "model server's /tokenize endpoint reachable.")
    return n


# ── SafeDict: silent missing keys for .format_map() ─────

class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


# ── Prompt-shape projection (I-41) ───────────────────────

def assemblable(events: list) -> list:
    """I-41: 提示词形状层投影 — 剥离「缺 tool_result 的工具轮」。

    这是**模型侧**约束（assistant 的 tool_calls 必须跟 tool 结果，否则消息链
    非法），不属于日志事实，因此归组装层而非 session 层（ADR-034/039/050）。

    范围：**全量扫描**（不按 agent_started 划界）——任何位置的未配对工具轮都会
    造成非法消息链，无论它属于当前任务还是上一轮遗留；干净历史原样返回。
    """
    result_ids = {(e.parent_event_id or e.event_id)
                  for e in events if e.event_type == "tool_result"}

    thought_ids: set[str] = set()
    dangling: set[str] = set()
    for e in events:
        if e.event_type == "agent_thought":
            for tc in e.payload.get("tool_calls", []) or []:
                tid = tc.get("id")
                if tid:
                    thought_ids.add(tid)
                    if tid not in result_ids:
                        dangling.add(tid)
    # Legacy (pre-I-07) tool_called entries not covered by an agent_thought
    # must also pair with a tool_result.
    for e in events:
        if (e.event_type == "tool_called" and e.payload.get("allowed")
                and e.event_id not in thought_ids
                and e.event_id not in result_ids):
            dangling.add(e.event_id)

    if not dangling:
        return events

    out: list = []
    for e in events:
        if e.event_type == "agent_thought":
            ids = {tc.get("id") for tc in (e.payload.get("tool_calls") or [])}
            if ids & dangling:
                continue
        if e.event_type == "tool_called" and e.event_id in dangling:
            continue
        out.append(e)
    return out


# ── Prompt assembly ──────────────────────────────────────

def assemble_prompt(
    base_prompt: str,
    vars: dict[str, str] | None = None,
    skills: list | None = None,
    append: str | None = None,
    amend: dict[str, Any] | None = None,
) -> str:
    """Assemble a system prompt from components.

    Pipeline: base_prompt.format_map(vars) → amend → append → skills metadata.
    Skills are sorted alphabetically by name for KV cache stability.
    """
    all_vars = dict(vars or {})
    if amend:
        all_vars.update(amend.get("vars", {}))
    prompt = base_prompt.format_map(_SafeDict(all_vars))

    if amend and amend.get("input"):
        prompt += f"\n\n[修改指令]\n{amend['input']}"

    if append:
        prompt += f"\n\n[追加指令]\n{append}"

    if skills:
        sorted_skills = sorted(skills, key=lambda s: s.name)
        lines = ["\n\nAvailable skills (use read_file on"
                 " .skills/{name}/SKILL.md"
                 " to load full instructions):"]
        for s in sorted_skills:
            lines.append(f"- {s.name}: {s.description}"
                         f" (.skills/{s.name}/SKILL.md)")
        prompt += "\n".join(lines)

    return prompt


# ── Output size guard (ADR-034 Rev2 R4) ────────────────

TOOL_RESULT_LIMIT = 8192  # max chars per tool_result message


def truncate_output(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    """Truncate oversized tool output with an explicit marker.

    Head is kept, marker reports total chars and line count so the LLM
    knows data was dropped.  Applied at the tool layer first; build_messages
    re-applies it as a fallback for third-party tools.
    """
    if len(text) <= limit:
        return text
    lines = text.count("\n") + 1
    marker = f"[... truncated {len(text)} chars, {lines} lines]"
    return f"{text[:limit]}\n{marker}"


# ── Message construction ─────────────────────────────────

# ADR-053: the irreducible set is `SYS + mem(prefix) + anchor + mem(suffix) +
# last N rounds`.  N lives on AgentSpec.context_min_keep_rounds; this module
# default is used when a caller does not forward the kwarg (older call sites).
DEFAULT_MIN_KEEP_ROUNDS = 1


# ── Round grouping (ADR-053) ──────────────────────────────
#
# A "round" is one `agent_thought` carrying tool_calls plus every
# `tool_result` whose parent_event_id points at one of those call ids.
# Truncation MUST treat a round as atomic: dropping the assistant message
# while keeping its tool result produces an orphan `role:"tool"` message,
# which violates the ADR-052 §5 invariant and is rejected by providers.

def _tool_call_ids(ev) -> set:
    """Tool-call ids declared by an agent_thought event."""
    if ev.event_type != "agent_thought":
        return set()
    return {tc.get("id") for tc in (ev.payload.get("tool_calls") or [])
            if tc.get("id")}


def _split_rounds(events: list) -> tuple[list, list]:
    """Split `events` into (protected singletons, droppable units).

    A **droppable unit** is either:
      * a full round — one `agent_thought` carrying tool_calls plus every
        `tool_result` that answers one of those ids; or
      * a single non-terminal event with no tool_calls (its own unit, so it
        can be dropped without splitting anything).

    **Protected singletons** are **terminal** events (`agent_completed`,
    `agent_failed`): they carry the task's outcome and must survive even when
    every round is dropped.

    Both sides preserve ORIGINAL relative order, so the caller can concatenate
    its kept pieces and still emit a causal message list.
    """
    # 1. which event index issues each call id (used to bind results to their
    #    issuing thought, independent of arrival order)
    owner: dict = {}
    for i, ev in enumerate(events):
        for cid in _tool_call_ids(ev):
            owner[cid] = i

    # 2. walk once, grouping results onto their thought
    protected: list = []
    units: list = []
    cur: dict | None = None
    for i, ev in enumerate(events):
        ids = _tool_call_ids(ev)
        if ids:
            cur = {"events": [ev], "owner_idx": i}
            units.append(cur)
            continue
        if ev.event_type == "tool_result":
            cid = ev.parent_event_id or ev.event_id
            if cur is not None and owner.get(cid) == cur["owner_idx"]:
                cur["events"].append(ev)
                continue
        if ev.event_type in _TERMINAL_EVENT_TYPES:
            protected.append(ev)
            continue
        cur = None
        units.append({"events": [ev], "owner_idx": None})
    return protected, [u["events"] for u in units]


# Terminal events carry the task outcome; dropping them would lose the result
# of a finished task even when the round that produced it is gone.
_TERMINAL_EVENT_TYPES = ("agent_completed", "agent_failed")

# ADR-053 R1/P3: when a request omits `max_tokens` (AgentSpec default None =
# "let the provider decide"), the budget still has to reserve room for the
# response.  Otherwise the prompt may fill the whole window and the provider's
# (unbounded) output overflows it.  This is only the BUDGET reservation; the
# request may still omit the field.
DEFAULT_OUTPUT_RESERVE_TOKENS = 16384

# COMPATIBILITY FALLBACK: costs computed by the most recent `build_messages`
# call in this process, keyed by (id(event), reasoning_passback).  `_cost` uses
# it when no explicit `cost_map` is supplied.  Contract: only the MOST RECENT
# build is exact; interleaved/concurrent builds must use `cost_map_out`.
# Value keeps a strong reference to the event: otherwise Python may reuse the
# id() of a freed event and a later `_cost` call would read a stale entry.
_LAST_COST_MAP: dict[tuple[int, bool], tuple[object, int]] = {}
_LAST_COST_LOCK = threading.Lock()


def _cost_map_snapshot(events: list, cost_map: dict[int, int],
                       reasoning_passback: bool) -> dict:
    """Per-build cost context: (id(event), passback) -> (event, cost).

    Keeping a strong event reference avoids `id()` reuse after a build.  This is
    the deterministic form: callers keep the snapshot and pass it back to
    `_cost`, so interleaved/concurrent builds cannot overwrite each other.
    """
    return {(id(ev), bool(reasoning_passback)): (ev, cost_map[id(ev)])
            for ev in events}


def _message_cost(messages: list[dict], provider: str,
                  tokenizer_dir: str) -> int:
    """Token cost of messages exactly as they will be sent."""
    total = 0
    for m in messages:
        total += _count_tokens(str(m.get("content") or ""), provider,
                               tokenizer_dir)
        if m.get("reasoning_content"):
            total += _count_tokens(str(m["reasoning_content"]), provider,
                                   tokenizer_dir)
        for tc in (m.get("tool_calls") or []):
            args = (tc.get("function") or {}).get("arguments") or ""
            total += _count_tokens(str(args), provider, tokenizer_dir)
    return total


def _agent_failed_text(payload: dict) -> str:
    error_text = payload.get("error")
    if error_text is not None:
        return str(error_text)
    reason = payload.get("reason", "")
    detail = {k: v for k, v in payload.items() if k != "reason"}
    if reason and detail:
        return f"{reason}: {detail}"
    if reason:
        return str(reason)
    return json.dumps(payload)


def _emit_context(events: list, reasoning_passback: bool):
    """Derive the pairing decisions shared by costing and message emission.

    Returns (covered_tool_ids, skipped_thought_ids, completed_reasoning):
      * `tool_called` is a legacy duplicate when an agent_thought carries its id;
      * a plain-text agent_thought is merged into the following agent_completed
        (one assistant message; its reasoning moves onto the completed one).
    """
    covered: set[str] = set()
    # Identity, not event_id: tests/legacy sessions may repeat ids.
    skipped: set[int] = set()
    completed_reasoning: dict[int, str] = {}
    pending_reason: str | None = None
    pending_thought: int | None = None
    for ev in events:
        if ev.event_type == "agent_completed":
            if pending_thought is not None:
                skipped.add(pending_thought)
                if pending_reason:
                    completed_reasoning[id(ev)] = pending_reason
            pending_reason = None
            pending_thought = None
        elif ev.event_type == "agent_thought":
            tool_calls = ev.payload.get("tool_calls", []) or []
            if not tool_calls:
                pending_thought = id(ev)
                pending_reason = ev.payload.get("reasoning_content") or None
            else:
                for tc in tool_calls:
                    if tc.get("id"):
                        covered.add(tc["id"])
                pending_reason = None
                pending_thought = None
        elif ev.event_type == "agent_started":
            pending_reason = None
            pending_thought = None
    return covered, skipped, completed_reasoning


def _event_messages(ev, reasoning_passback: bool, content_filter,
                    covered_tool_ids: set[str], skipped_thought_ids: set[int],
                    completed_reasoning: dict[int, str]) -> list[dict]:
    """The ONE mapping from an event to the message(s) actually emitted."""
    et = ev.event_type
    p = ev.payload or {}
    if et == "agent_started":
        task = p.get("input", "Execute the assigned task")
        retry_error = p.get("_retry_error")
        if retry_error:
            task = f"{task}\n\n[Previous attempt failed: {retry_error}]\n"
        return [{"role": "user", "content": f"Task: {task}"}]
    if et == "agent_thought":
        if id(ev) in skipped_thought_ids:
            return []
        tool_calls = p.get("tool_calls", []) or []
        asst: dict[str, Any] = {
            "role": "assistant",
            "content": "" if p.get("content") is None else p.get("content"),
        }
        if tool_calls:
            asst["tool_calls"] = [{
                "id": tc.get("id"),
                "type": "function",
                "function": {
                    "name": tc.get("name"),
                    "arguments": tc.get("arguments", "{}"),
                },
            } for tc in tool_calls]
        reasoning = p.get("reasoning_content")
        if reasoning_passback and reasoning:
            asst["reasoning_content"] = reasoning
        return [asst]
    if et == "agent_completed":
        asst = {"role": "assistant", "content": p.get("output", "")}
        if reasoning_passback and id(ev) in completed_reasoning:
            asst["reasoning_content"] = completed_reasoning[id(ev)]
        return [asst]
    if et == "tool_called" and p.get("allowed") \
            and ev.event_id not in covered_tool_ids:
        return [{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": ev.event_id,
                "type": "function",
                "function": {
                    "name": p.get("tool"),
                    "arguments": json.dumps(p.get("args", {}), ensure_ascii=False),
                },
            }],
        }]
    if et == "tool_result":
        content = str(p.get("result", ""))
        if content_filter is not None:
            content = content_filter(content)
        content = truncate_output(content)
        return [{"role": "tool",
                 "tool_call_id": ev.parent_event_id or ev.event_id,
                 "content": content}]
    if et == "agent_failed":
        # A denial/unknown-tool failure is already represented by the paired
        # tool_result; re-emitting it would duplicate the notice and its cost.
        if p.get("represented_by_tool_result"):
            return []
        return [{"role": "user",
                 "content": f"[tool error] {_agent_failed_text(p)}"}]
    if et == "memory_summary":
        if p.get("superseded") or p.get("slot") not in ("before_task", "after_task"):
            # Legacy (pre-slot) or superseded summaries are not assembled; the
            # harness cannot place text it cannot attribute to a slot.
            return []
        # Harness-owned placeholder: slots are self-identifying so a replacement
        # read can never confuse a slot with the Task anchor.
        return [{"role": "user",
                 "content": f"<mem>{p.get('content', '')}</mem>"}]
    return []


def _event_cost(ev, provider: str, tokenizer_dir: str,
                reasoning_passback: bool = True, *, content_filter=None,
                covered_tool_ids=frozenset(),
                skipped_thought_ids=frozenset(),
                completed_reasoning=None) -> int:
    return _message_cost(
        _event_messages(ev, reasoning_passback, content_filter,
                        set(covered_tool_ids), set(skipped_thought_ids),
                        completed_reasoning or {}),
        provider, tokenizer_dir)


def _cost(ev, provider: str, tokenizer_dir: str,
          reasoning_passback: bool = True, *,
          cost_map: dict | None = None) -> int:
    """Token cost of the text this event contributes to the prompt.

    ADR-053 decision 3: measure what is actually SENT, not `str(payload)`.
    R1/P1: this delegates to the same "event -> messages" mapping as
    `build_messages`; when `build_messages` has already run, its exact
    per-event costs are reused (including skip/coverage pairing).  A standalone
    call (no context) applies the same mapping without pairing context.

    Pass `cost_map` (from `build_messages(..., cost_map_out=...)`) for a
    deterministic per-build result.  Without it, the module-global map is used:
    only the MOST RECENT build in this thread is exact; interleaved/concurrent
    builds must pass their own snapshot.
    """
    key = (id(ev), bool(reasoning_passback))
    if cost_map is not None:
        cached = cost_map.get(key)
    else:
        with _LAST_COST_LOCK:
            cached = _LAST_COST_MAP.get(key)
    if cached is not None and cached[0] is ev:
        return cached[1]
    return _event_cost(ev, provider, tokenizer_dir, reasoning_passback)


def _split_orphans(events: list, universe: list | None = None
                   ) -> tuple[list, list]:
    """Separate `tool_result` events whose issuing assistant is absent.

    Mirror of `assemblable()`'s dangling-call removal: that side drops a round
    when its results are missing; this side drops results when their call is
    gone.  Without it a `role:"tool"` message can be emitted with no preceding
    `assistant.tool_calls` for that id — an illegal chain (ADR-052 §5
    invariant 2; verified defect in docs/evidence/l3-03-grill-evidence.md §2).

    This is deliberately strict: a result whose call id appears NOWHERE is also
    dropped, because the provider contract requires the pairing regardless of
    why it is missing.  Callers that want to emit a bare result must supply the
    issuing `agent_thought`.
    """
    call_ids: set = set()
    for ev in events:
        if ev.event_type == "agent_thought":
            call_ids |= _tool_call_ids(ev)
        elif ev.event_type == "tool_called" and ev.payload.get("allowed"):
            call_ids.add(ev.event_id)

    kept: list = []
    orphans: list = []
    for ev in events:
        if ev.event_type == "tool_result":
            cid = ev.parent_event_id or ev.event_id
            if cid not in call_ids:
                orphans.append(ev)
                continue
        kept.append(ev)
    return kept, orphans


def build_messages(
    system_prompt: str,
    events: list,
    context_window: int = 128000,
    budget_ratio: float = 1.0,
    provider: str = "deepseek",
    tokenizer_dir: str = "tokenizer_dir",
    content_filter: Callable[[str], str] | None = None,
    reasoning_passback: bool = True,
    meta_out: dict | None = None,
    min_keep_rounds: int | None = None,
    max_tokens: int = 0,
    output_reserve_tokens: int = 0,
    cost_map_out: dict | None = None,
) -> list[dict]:
    """Convert events to OpenAI messages with token-aware truncation.

    Only does event conversion + token budgeting — no prompt assembly.

    Args:
        content_filter: Optional APP-layer callback to filter sensitive
            content from tool_result messages before they reach the LLM.
            Framework provides the slot, APP defines the rules.
        reasoning_passback: I-07/ADR-041 — replay reasoning_content when
            an assistant turn carried it (DS thinking-mode multi-turn rule:
            any such turn — tool rounds AND plain/final rounds — must be
            passed back on later requests when enabled).
        max_tokens: Output cap sent WITH the request (0/None = omit the field).
            Only a non-zero value is also used as the budget output reserve.
        output_reserve_tokens: ADR-053 R1/P3 — budget-only output reservation
            used when `max_tokens` is omitted.  This is deliberately a
            SEPARATE knob from the request's output cap: "request may be
            unbounded" must not mean "prompt may fill the whole window".
            I-64: the *effective* reserve is capped at
            `max(1, ceil(context_window/4))`; `meta` reports both original and
            effective values.
        cost_map_out: Optional mutable dict filled by this call with this
            build's exact `(id(event), passback) -> (event, cost)` snapshot.
            Pass it back as `_cost(..., cost_map=...)` for deterministic
            interleaved/concurrent use.  Without it `_cost` falls back to the
            module-global map, which is only exact for the MOST RECENT build.
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt}
    ]

    # ── FALLBACK-ONLY TRUNCATION (ADR-053 revision) ─────────────────────
    # The mechanism's job is to never overflow the model's context — NOT to
    # decide how much context to use.  That decision belongs to the app layer
    # (ADR-001's high-water state machine).
    #
    # Previously `budget_tokens = context_window * 0.8`, so assembly began
    # dropping rounds at ~64% of the window.  Two consequences, both measured:
    #   * the harness took a policy decision that is the app's to take;
    #   * truncation CAPS the assembled cost, so the app's high-water signal was
    #     unreachable — the context could never approach the mark meant to
    #     trigger compaction.
    #
    # So the limit IS the hard limit (which reserves the output budget), and
    # dropping happens only when the request would actually exceed it.
    # `budget_ratio` remains as a DEPLOYMENT knob to tighten deliberately; the
    # default no longer pre-truncates.
    #
    # R1/P3: `max_tokens` (request field, may be omitted) and
    # `output_reserve_tokens` (budget reservation, always in effect) are two
    # independent knobs.  An explicit max_tokens wins; otherwise the reserve
    # applies.  The reserve is clamped to leave at least 1 token of prompt
    # budget, so a tiny context_window cannot produce hard_limit <= 0.
    reserve = (max_tokens if (max_tokens is not None and max_tokens > 0)
               else output_reserve_tokens)
    if reserve < 0:
        raise ValueError(
            f"output reservation must be >= 0, got {reserve}")
    # I-64: a reserve larger than the window must not starve the prompt.  Clamp
    # to a quarter of the window so tiny windows keep a usable high-water mark;
    # a deployment that truly wants more can lower context/window settings.
    reserve_cap = max(1, (context_window + 3) // 4)
    effective_reserve = min(reserve, reserve_cap)
    hard_limit = max(1, context_window - effective_reserve)
    if meta_out is not None:
        meta_out["output_reserve_tokens"] = reserve
        meta_out["output_reserve_effective"] = effective_reserve
    # 策略上限：默认 = 物理上限（即不提前截断，只在将溢出时兜底）。
    # 部署仍可用 budget_ratio<1 主动收紧，但永不超过物理上限。
    budget_tokens = min(int(context_window * budget_ratio), hard_limit)

    # Memory slots are the fold of dropped history.  ADR-053 rev2: the harness
    # owns the two POSITIONS and the placeholder, while the app names the slot;
    # `after_event_id` is a per-slot FOLD BOUNDARY, not a global cutoff.  A
    # boundary only removes events BEFORE it in its own region (prefix for
    # before_task, suffix for after_task); the Task anchor is never a region
    # event, so it can never be removed by a summary.
    memory_summaries = [e for e in events if e.event_type == "memory_summary"]
    other_events = [e for e in events if e.event_type != "memory_summary"]

    # R1/P1: compute exact per-event costs with the SAME emission context the
    # assembler uses.  The budget decision and the emitted messages must never
    # drift, including the skip/coverage pairing rules.
    emit_covered, emit_skipped, emit_completed = _emit_context(
        other_events, reasoning_passback)
    # Orphan tool results are dropped by `_split_orphans` below and are NOT
    # emitted; their cost must be 0 for the invariant to hold.
    _, _all_orphans = _split_orphans(other_events, other_events)
    orphan_ids = {id(e) for e in _all_orphans}
    cost_map: dict[int, int] = {}
    for ev in events:
        if id(ev) in orphan_ids:
            cost_map[id(ev)] = 0
            continue
        cost_map[id(ev)] = _event_cost(
            ev, provider, tokenizer_dir, reasoning_passback,
            content_filter=content_filter,
            covered_tool_ids=emit_covered,
            skipped_thought_ids=emit_skipped,
            completed_reasoning=emit_completed)
    snapshot = _cost_map_snapshot(events, cost_map, reasoning_passback)
    if cost_map_out is not None:
        cost_map_out.clear()
        cost_map_out.update(snapshot)
    with _LAST_COST_LOCK:
        _LAST_COST_MAP.clear()
        if content_filter is None:
            _LAST_COST_MAP.update(snapshot)

    def _ev_cost(ev) -> int:
        return cost_map[id(ev)]

    # ADR-034 Rev2 R1 (Rev3 fix 2026-09-07): the LATEST agent_started is
    # the CURRENT in-flight task anchor.  Conversation order must stay
    # causal: [older finished-task history (truncatable)] then [anchor +
    # all later events of the current task] — the current task always ends
    # the message list right where the model continues writing.  Never
    # front-load the anchor (that inverted causality across tasks).
    #
    # ADR-053 (2026-10-01): the current task is NO LONGER "keep ALL".
    #   * it is truncated on ROUND boundaries (a round = one agent_thought
    #     carrying tool_calls + all tool_results answering it), so a
    #     tool_call/result pair is never split;
    #   * the anchor event itself is always kept, but the anchor's ROUND is
    #     not specially protected — losing it is compensated by mem(suffix);
    #   * at least `min_keep_rounds` rounds survive; if even the
    #     irreducible set (SYS + both mems + anchor + last N rounds) cannot
    #     fit, the request is UNSATISFIABLE and reported via meta instead of
    #     being sent anyway (previously: silent over-long request -> server 400).
    anchor_idx = next((i for i in range(len(other_events) - 1, -1, -1)
                       if other_events[i].event_type == "agent_started"), None)
    if anchor_idx is not None:
        prefix_events = other_events[:anchor_idx]
        anchor_event = other_events[anchor_idx]
        suffix_events = other_events[anchor_idx + 1:]
    else:
        prefix_events = list(other_events)
        anchor_event = None
        suffix_events = []

    # Select the newest summary per slot.  A summary without a valid slot is a
    # LEGACY event (pre-ADR-053-rev2): the harness has no basis to place it, so
    # it is ignored and counted in meta for migration/observability.
    slot_before = None
    slot_after = None
    ignored_legacy = 0
    for m in memory_summaries:
        if m.payload.get("superseded"):
            continue
        slot = m.payload.get("slot")
        if slot == "before_task":
            slot_before = m
        elif slot == "after_task":
            slot_after = m
        else:
            ignored_legacy += 1

    def _apply_fold_boundary(region: list, summary) -> list:
        """Drop history BEFORE the summary's boundary, inside its OWN region.

        The Task anchor is not in `region` (prefix/suffix are split around it),
        so a slot boundary can never remove the task.  A boundary outside the
        region (e.g. an after_task boundary placed in prefix) is ignored: the
        summary still occupies its fixed position, but it does not fold anything.
        """
        if summary is None:
            return region
        boundary = summary.payload.get("after_event_id")
        if not boundary:
            return region
        idx = next((i for i, e in enumerate(region)
                    if e.event_id == boundary), None)
        if idx is None:
            return region
        return region[idx:]       # boundary event stays; only older history folds

    prefix_events = _apply_fold_boundary(prefix_events, slot_before)
    suffix_events = _apply_fold_boundary(suffix_events, slot_after)

    # ctx / min-rounds resolution: module default when the caller is an older
    # call site that does not forward the kwarg yet.
    if min_keep_rounds is None:
        min_keep_rounds = DEFAULT_MIN_KEEP_ROUNDS
    if min_keep_rounds < 1:
        raise ValueError(
            f"context_min_keep_rounds must be >= 1 (the anchor and at least one "
            f"round are irreducible), got {min_keep_rounds}")

    # ── ADR-053: round-aware truncation, both sides ──────────────────────
    #
    # Drop unit is a ROUND, never a single event: splitting a round yields an
    # orphan `role:"tool"` message (verified defect, see logs/l3-03-*).
    # Two passes because the FIRST pass has to know the terminal size of what
    # it is about to keep.

    # 1. results whose issuing assistant is gone are un-sendable: drop them
    #    (mirror of assemblable()'s dangling-call removal).
    prefix_events, orphan_tools = _split_orphans(prefix_events, other_events)
    suffix_events, suffix_orphans = _split_orphans(suffix_events, other_events)
    orphan_tools = orphan_tools + suffix_orphans

    # 2. memory slots: selected above; their fixed positions are applied when
    #    `recent` is assembled (slot_before / Task / slot_after).  Their
    #    boundaries were already applied to their own regions.

    # 3. cost the real (emitted) text, honouring content_filter.  `cost_map`
    #    already contains the exact emitted cost, including `Task: `, retry
    #    text and the `<mem>` placeholder (R1/P1).
    sp_cost = _count_tokens(system_prompt, provider, tokenizer_dir)
    fixed = sp_cost
    fixed += sum(_ev_cost(m) for m in (slot_before, slot_after)
                 if m is not None)
    anchor_cost = _ev_cost(anchor_event) if anchor_event is not None else 0
    fixed += anchor_cost

    # 4. prefix: keep the NEWEST droppable units that fit the remaining budget.
    #    Terminal events in the prefix are ALWAYS kept (task outcome).
    p_protected, p_rounds = _split_rounds(prefix_events)
    kept_prefix_units: list = []
    spent = fixed
    spent += sum(_ev_cost(ev) for ev in p_protected)
    for rnd in reversed(p_rounds):
        c = sum(_ev_cost(ev) for ev in rnd)
        if spent + c > budget_tokens:
            break
        kept_prefix_units.append(rnd)
        spent += c
    kept_prefix_units.reverse()
    kept_prefix_all = [e for r in kept_prefix_units for e in r] + list(p_protected)
    # keep original order so the causal sequence is preserved
    _porder = {id(e): k for k, e in enumerate(prefix_events)}
    kept_prefix_events = sorted(kept_prefix_all,
                                key=lambda e: _porder.get(id(e), 1 << 30))

    # 5. suffix: anchor + at least min_keep_rounds newest units, then more
    #    while the budget allows.  The anchor's own round is NOT protected.
    s_protected, s_rounds = _split_rounds(suffix_events)
    rcost = [sum(_ev_cost(ev) for ev in rnd) for rnd in s_rounds]
    spent += sum(_ev_cost(ev) for ev in s_protected)

    keep_n = min(min_keep_rounds, len(s_rounds))
    kept_suffix_units = list(s_rounds[len(s_rounds) - keep_n:])
    spent += sum(rcost[len(s_rounds) - keep_n:])
    for i in range(len(s_rounds) - keep_n - 1, -1, -1):
        if spent + rcost[i] > budget_tokens:
            break
        kept_suffix_units.insert(0, s_rounds[i])
        spent += rcost[i]
    kept_suffix_all = [e for r in kept_suffix_units for e in r] + list(s_protected)
    _sorder = {id(e): k for k, e in enumerate(suffix_events)}
    kept_suffix_events = sorted(kept_suffix_all,
                                key=lambda e: _sorder.get(id(e), 1 << 30))

    # 6. irreducible set overflow => the request cannot be satisfied.  Report
    #    it (fact) instead of sending an over-long prompt and paying for a
    #    prefill the server will reject (measured: 204s wasted).
    dropped_counts = (len(prefix_events) - len(kept_prefix_events)
                      + len(suffix_events) - len(kept_suffix_events))

    unsatisfiable = spent > hard_limit
    if unsatisfiable:
        # diagnostics: does a SINGLE unit already blow the budget?
        worst = 0
        if s_rounds:
            worst = max(rcost)
        if anchor_cost > worst:
            worst = anchor_cost
        if fixed > worst:
            worst = fixed
        kind = ("single_tool_result" if worst > hard_limit - (fixed - sp_cost)
                else "multi_round")
        if meta_out is not None:
            meta_out["over_budget_unresolvable"] = True
            meta_out["unresolvable_kind"] = kind
            meta_out["budget_tokens"] = budget_tokens
            meta_out["hard_limit"] = hard_limit
            meta_out["needed_tokens"] = spent
            meta_out["irreducible_cost"] = fixed
            meta_out["largest_droppable_unit"] = worst

    # 7. assemble in ORIGINAL event order: prefix(kept) -> mem(prefix) ->
    #    anchor -> mem(suffix) -> suffix(kept).  Insertion is positional, not
    #    index-arithmetic on `kept_prefix`, so mem lands where the dropped
    #    history actually was even when the whole prefix is gone.
    # Order: earlier tasks (prefix, oldest first) → slot_before → Task anchor →
    # slot_after → current-task rounds (suffix).  The slots hug the Task, which
    # is what makes them "insertion points around the anchor" rather than
    # positions somewhere in the history.
    recent: list = list(kept_prefix_events)
    if slot_before is not None:
        recent.append(slot_before)
    if anchor_event is not None:
        recent.append(anchor_event)
    if slot_after is not None:
        recent.append(slot_after)
    recent += kept_suffix_events

    # I-43: 预览/诊断元数据（回答「内容去哪了」）
    if meta_out is not None:
        meta_out["dropped_event_count"] = max(0, dropped_counts)
        meta_out["truncated"] = dropped_counts > 0
        meta_out["folded_summary"] = bool(slot_before or slot_after)
        if ignored_legacy:
            meta_out["ignored_legacy_summary_count"] = ignored_legacy
        if orphan_tools:
            meta_out["dropped_orphan_tool_results"] = len(orphan_tools)
        meta_out["kept_rounds"] = len(kept_prefix_units) + len(kept_suffix_units)
        meta_out["dropped_rounds"] = (
            len(p_rounds) - len(kept_prefix_units)
            + len(s_rounds) - len(kept_suffix_units))
        # ADR-053: the physical threshold is always reported so an APP can see
        # how much room is left for its own compaction trigger.
        meta_out["hard_limit"] = hard_limit
        meta_out["budget_tokens"] = budget_tokens

    # R1/P1: emit through the SAME event->message mapping used for costing.
    # Pairing is derived from the FINAL kept list: a thought dropped by
    # truncation must not merge into (or suppress) anything.
    recent_covered, recent_skipped, recent_completed = _emit_context(
        recent, reasoning_passback)
    for ev in recent:
        messages.extend(_event_messages(
            ev, reasoning_passback, content_filter,
            recent_covered, recent_skipped, recent_completed))
    return messages


# ── Tool description helpers ─────────────────────────────

def describe_tools(registry, filter_names: list[str] | None = None):
    """Return OpenAI-format tool definitions, optionally filtered."""
    all_defs = registry.get_definitions()
    if filter_names is None:
        return all_defs
    return [t for t in all_defs if t["function"]["name"] in filter_names]
