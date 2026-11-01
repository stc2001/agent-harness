"""I-43: 组装产物（发送给 LLM 的上下文）单一真源。

runtime loop 与 SDK/CLI 预览共用 `compose_llm_context()`——任何一方绕过它都会
造成「预览与实际请求不符」（I-41 的教训：契约声明 ≠ 消费者遵守）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def compose_llm_context(store, spec, session_id: str, *,
                        system_prompt: str | None = None,
                        task: str | None = None,
                        event_id: str | None = None,
                        agent_name: str | None = None,
                        overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """推导“本次调用将发送给 LLM 的上下文”（只读，无副作用）。

    管道：物理日志 → logical_view（rewind 重放）→ [历史前缀 | 合成 task] →
    assemblable（剥离未配对工具轮）→ build_messages（预算/折叠/passback）→ hooks。
    """
    from harness.interfaces import Event
    from harness.prompt_assembly import assemblable, build_messages
    from harness.session.impl import logical_view_of

    ov = dict(overrides or {})
    events = logical_view_of(store, session_id)

    as_of = None
    if event_id is not None:
        for i, e in enumerate(events):
            if e.event_id == event_id:
                events = events[:i + 1]
                as_of = event_id
                break
        else:
            raise ValueError(
                f"event '{event_id}' not found in session '{session_id}'")

    if task is not None:
        events = events + [Event(event_id='ev_preview_task',
                                 session_id=session_id,
                                 event_type='agent_started',
                                 timestamp=time.time(),
                                 source='preview',
                                 payload={'input': task})]

    pre_shape = len(events)
    shaped = assemblable(events)
    stripped_dangling = len(shaped) != pre_shape

    if ov.get('system_prompt') is not None:
        system_prompt, src = ov['system_prompt'], 'override'
    elif system_prompt is not None:
        src = 'override'
    else:
        system_prompt = getattr(spec, 'system_prompt', '') or ''
        src = 'current-spec'

    ctx = ov.get('context_window', getattr(spec, 'context_window', 128000))
    ratio = ov.get('budget_ratio', getattr(spec, 'context_budget_ratio', 1.0))
    passback = ov.get('reasoning_passback', getattr(spec, 'reasoning_passback', True))
    provider = ov.get('provider')
    if provider is None:
        # I-49: `provider` selects `tokenizer_dir/<provider>` as the FALLBACK
        # tokenizer (used only when the server's own /tokenize is unreachable).
        # It used to be hardcoded to "deepseek" regardless of the served model,
        # so a self-hosted model fell back to a vocab with NO CJK at all
        # (measured: 0 CJK entries vs 33,986 in the served model's vocab) and
        # counted Chinese as 0 tokens.  Now it comes from the resolved profile;
        # the default stays "deepseek" so nothing changes unless configured.
        try:
            from harness.sdk.config import resolve_llm_profile
            _prof = getattr(spec, 'llm_profile', '') or 'default'
            provider = getattr(
                resolve_llm_profile(_prof, project_dir=Path.cwd()),
                'provider', '') or 'deepseek'
        except Exception:
            provider = 'deepseek'      # no profile resolvable -> legacy default
    # ADR-053: rounds protected by the irreducible set, and the output
    # reservation used for the physical (raise) threshold.
    min_rounds = ov.get('min_keep_rounds',
                        getattr(spec, 'context_min_keep_rounds', 1))
    mtok = ov.get('max_tokens', getattr(spec, 'max_tokens', 0))
    # ADR-053 R1/P3: the output budget reserve is a separate knob from the
    # request's max_tokens (which may be omitted / None).
    reserve = ov.get('output_reserve_tokens',
                     getattr(spec, 'output_reserve_tokens', 0))

    meta: dict[str, Any] = {}
    messages = build_messages(system_prompt, shaped, ctx, ratio,
                              provider=provider, reasoning_passback=passback,
                              meta_out=meta, min_keep_rounds=min_rounds,
                              max_tokens=mtok,
                              output_reserve_tokens=reserve)

    from harness.hooks import HookRegistry, points
    reg = HookRegistry.get()
    if reg.has_hooks(points.TOOL_RESULT):
        for m in messages:
            if m.get('role') == 'tool':
                m['content'] = reg.trigger(points.TOOL_RESULT, m['content'],
                                           agent=agent_name).payload

    meta.update({
        'as_of_event_id': as_of,
        'synthetic_task': task is not None,
        'system_prompt_source': src,
        'stripped_dangling': stripped_dangling,
        'context_window': ctx,
        'budget_ratio': ratio,
        'reasoning_passback': passback,
        'min_keep_rounds': min_rounds,
        'max_tokens': mtok,
        'output_reserve_tokens': reserve,
    })
    return {
        'messages': messages,
        'model': getattr(spec, 'model', ''),
        'params': {'context_window': ctx, 'budget_ratio': ratio,
                   'provider': provider, 'reasoning_passback': passback,
                   'min_keep_rounds': min_rounds, 'max_tokens': mtok,
                   'output_reserve_tokens': reserve},
        'meta': meta,
    }