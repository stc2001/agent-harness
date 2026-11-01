"""I-12 (ADR-044): Stop 双轨 — reason 谱系与内置裁决 hook。

- **framework 硬闸门**：`agent.stop.framework` veto 链，内置 hook 保留高
  优先级（1000）返回 condition → vetoed → 强制停；app 无法覆盖（运行时另有
  fail-closed 条件复查——硬条件是事实，链未 veto 也停）。
- **app 软裁决**：`agent.stop.app` veto 链，内置默认裁决表（priority=0）：
  合法 reason → 返回 None（批准优雅停）；需确认 reason → 返回否决文本
  （转降级/升级路径）。app 可覆盖：veto hook（priority>0）强制否决；
  需确认 reason 的强制批准用 around hook（返回无 veto 的 TriggerResult）。
- reason 合法性（missing / no_reason / 未知）由运行时 fail-closed 前置
  校验（validate_stop_reason），不进入 hook。
"""

from __future__ import annotations

from harness.hooks import HookMode, HookRegistry, points

# ADR-044 §5 reason 谱系
LEGAL_REASONS = frozenset({
    "objective_met", "verify_passed", "user_requested",
    "parent_cancelled", "client_disconnected", "redirect",
})
NEEDS_CONFIRM_REASONS = frozenset({"budget_policy", "quality_unsatisfied"})
STOP_REASONS = LEGAL_REASONS | NEEDS_CONFIRM_REASONS

# framework 内置 hook 优先级：**最低**（-100）——先让 observe/transform 观察链
# 跑完；内置 veto 作为最后一道必然触发（返回 condition → vetoed），且运行时
# fail-closed 条件复查兜底——app 无法覆盖（veto 或条件成立都会停）。
FRAMEWORK_PRIORITY = -100


def validate_stop_reason(reason: str | None) -> bool:
    """reason 是否在合法/需确认谱系内（fail-closed 前置校验）。"""
    return reason in STOP_REASONS


def _framework_veto(ctx) -> str:
    """内置 framework 硬闸门 veto：condition 存在 → 返回它（veto = 强制停）。"""
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}
    condition = payload.get("condition")
    if condition:
        return f"framework-stop:{','.join(condition)}"
    return "framework-stop"


def _app_default_table(ctx) -> str | None:
    """内置 app 默认裁决表（ADR-044 §5，priority=0，app 可覆盖）。

    合法 reason → None（批准优雅停）；需确认 reason → 否决（转降级/升级）。
    """
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}
    reason = payload.get("reason")
    if reason in LEGAL_REASONS:
        return None
    if reason in NEEDS_CONFIRM_REASONS:
        return f"needs-confirm:{reason} (deny → route to app policy)"
    return "illegal-reason"


def install_stop_hooks(registry: HookRegistry | None = None) -> HookRegistry:
    """安装内置 stop hooks（幂等，按函数身份判定——registry.clear() 后可重装）。

    AgentRuntime.__init__ 调用；首实例安装，其余 no-op。
    """
    registry = registry or HookRegistry.get()
    if not any(h.fn is _framework_veto for h in registry._hooks):
        registry.on(points.AGENT_STOP_FRAMEWORK, HookMode.VETO,
                    _framework_veto, priority=FRAMEWORK_PRIORITY)
    if not any(h.fn is _app_default_table for h in registry._hooks):
        registry.on(points.AGENT_STOP_APP, HookMode.VETO,
                    _app_default_table, priority=0)
    return registry


__all__ = [
    "LEGAL_REASONS",
    "NEEDS_CONFIRM_REASONS",
    "STOP_REASONS",
    "FRAMEWORK_PRIORITY",
    "validate_stop_reason",
    "install_stop_hooks",
]
