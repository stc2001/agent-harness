"""dsh ↔ py-harness 提示词装配差分（diagnose 2026-09-11）。

Ground truth: tests/fixtures/dsh_prompt_capture.json —— 由**真实 dsh 运行时**
（桌面端已编译包 + 自写 CaptureAdapter）捕获的 3 个 GenerateOptions：
  request0: user "hi"
  request1: + assistant(text+tool-call) + tool-result(user role)
  request2: + assistant(text) + user "second turn"
dsh 装配语义（packages/core/session/src/surface.ts deriveEventMessage + 
packages/llm/llm-deepseek/src/serialize.ts serializeMessages）：
  - surface 节点逐字透传；无装配期截断（压缩由 compaction 插件 replace）
  - assistant: content=扁平文本（无则 ""，**永不 null**）+ reasoning_content（有则带）+ tool_calls
  - user 消息里的 tool-result 展开为 role:"tool" 单独消息
本文件锁定：① 已对齐的 parity 项 ② 仍存在的结构差异（防静默漂移）。
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.prompt_assembly import build_messages

FIXTURE = Path(__file__).with_name("fixtures") / "dsh_prompt_capture.json"


def _mk(t, payload, eid=None, parent=None):
    return type("_E", (), {"event_type": t, "payload": payload,
                           "event_id": eid or t, "parent_event_id": parent,
                           "source": "t"})()


def _scenario():
    return [
        _mk("agent_started", {"input": "hi"}, "e1"),
        _mk("agent_thought", {"content": "let me echo",
                              "tool_calls": [{"id": "c1", "name": "echo",
                                              "arguments": '{"text":"one"}'}]}, "e2"),
        _mk("tool_result", {"tool": "echo", "result": "echo: one"}, "e3",
            parent="c1"),
        _mk("agent_thought", {"content": "done one", "tool_calls": []}, "e4"),
        _mk("agent_completed", {"output": "done one", "reason": "stop"}, "e5"),
        _mk("agent_started", {"input": "second turn"}, "e6"),
    ]


def test_fixture_is_real_dsh_capture():
    caps = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert [c["index"] for c in caps] == [0, 1, 2]
    assert caps[0]["messages"][0]["content"][0]["text"] == "hi"


def test_parity_assistant_tool_and_final_shapes():
    """与 dsh wire 对齐：单条最终 assistant、role tool、tool_call_id、空 content=""。"""
    dsh_system = json.loads(FIXTURE.read_text(encoding="utf-8"))[2]["system"]
    msgs = build_messages(dsh_system, _scenario())
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    tool_msg = [m for m in msgs if m["role"] == "tool"][0]
    assert tool_msg["tool_call_id"] == "c1" and tool_msg["content"] == "echo: one"
    finals = [m for m in msgs if m["role"] == "assistant"
              and m.get("content") == "done one"]
    assert len(finals) == 1  # dsh 也只有一条（diagnose 修复前我们是两条）


def test_parity_system_prompt_when_same_string():
    dsh_system = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]["system"]
    msgs = build_messages(dsh_system, _scenario())
    assert msgs[0] == {"role": "system", "content": dsh_system}


def test_divergence_task_prefix():
    """差异①：我们给任务消息加 "Task: " 前缀；dsh user/message 逐字透传。"""
    msgs = build_messages("sys", _scenario())
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert users == ["Task: hi", "Task: second turn"]


def test_divergence_assembly_truncation_exists():
    """差异②：我们按 budget 截断（丢最旧历史）；dsh 装配期从不截断（靠 compaction）。"""
    events = [_mk("agent_started", {"input": "hi"}, "e0")]
    for i in range(12):
        events.append(_mk("agent_thought", {"content": "x" * 400,
                                            "tool_calls": []}, "t%d" % i))
        events.append(_mk("agent_completed", {"output": "o" * 400}, "c%d" % i))
    events.append(_mk("agent_started", {"input": "last"}, "eN"))
    msgs = build_messages("sys", events, context_window=2000, budget_ratio=0.8)
    assert len(msgs) < len(events)  # 有截断发生（dsh 侧不会）


def test_parity_empty_thought_before_completed_is_deduped():
    """空内容的最终 thought 被 completed 去重 → 与 dsh「跳过空 assistant」等价。"""
    msgs = build_messages("sys", [
        _mk("agent_started", {"input": "hi"}, "e1"),
        _mk("agent_thought", {"content": "", "tool_calls": []}, "e2"),
        _mk("agent_completed", {"output": "ok"}, "e3"),
    ])
    ass = [m for m in msgs if m["role"] == "assistant"]
    assert [m.get("content") for m in ass] == ["ok"]


def test_divergence_standalone_empty_assistant_kept():
    """差异③：**未被 completed 收编**的空 assistant 我们仍会发出（dsh 一律跳过空内容轮）。"""
    msgs = build_messages("sys", [
        _mk("agent_started", {"input": "hi"}, "e1"),
        _mk("agent_thought", {"content": "", "tool_calls": []}, "e2"),
        _mk("agent_started", {"input": "next"}, "e3"),
    ])
    assert any(m["role"] == "assistant" and m.get("content") == "" for m in msgs)


def test_divergence_memory_slot_message():
    """差异④：我们用 memory_slot 折叠成一条 user 消息（harness 占位符 `<mem>`）+ cutoff；
    dsh 无装配期折叠（压缩由 compaction 插件 replace）。

    槽位位置由 app 显式指定（ADR-001）；dsh 的 `[memory]` 约定已不再追随。
    """
    msgs = build_messages("sys", [
        _mk("agent_started", {"input": "hi"}, "e1"),
        _mk("agent_thought", {"content": "old", "tool_calls": []}, "e2"),
        _mk("memory_summary", {"content": "FOLDED", "slot": "before_task",
                               "after_event_id": "e2"}, "m1"),
        _mk("agent_thought", {"content": "new", "tool_calls": []}, "e3"),
    ])
    folds = [m for m in msgs if m["role"] == "user"
             and str(m.get("content", "")).startswith("<mem>")]
    assert folds and folds[0]["content"] == "<mem>FOLDED</mem>", folds
