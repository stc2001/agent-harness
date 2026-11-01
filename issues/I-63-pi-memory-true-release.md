---
id: I-63
title: pi/memory 迁移 I-57 true-release 与 I-53 observe 覆盖
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

I-62 将 app 记忆测试迁移到 `pi/memory` 时保留了旧行为断言；I-57 R2 的
true-release（prefix→before_task、suffix→after_task、mixed 两处）与 I-53 R2 的
`_observe`（preview `params.provider`、usage 计 reasoning/tool args）尚未迁入
`PiMemory`。本 issue 补齐实现与回归测试，harness 零改动。

## 验收标准 / Acceptance criteria

- [ ] `_observe` provider 来自 preview `params.provider`（缺失报错，不硬编码）；
      usage/rounds 计 content+reasoning+tool_call args；返回 prefix_count
- [ ] `_summarise_span` 按区域生成 placements；prefix→before_task、
      suffix→after_task、mixed 同一份摘要写两处；`_inject` 接受 slot，
      compactions/log 按 placement 记录
- [ ] 尺寸守卫 `framed × len(placements) >= shadowed` 拒绝；shadowed 含选中轮
      及配对 `tool_result`，全部经公开 `span_cost/count_tokens`
- [ ] I-57 R2 测试恢复：span prefix/suffix 选择、slot 释放、mixed 两处、真实
      `build_messages`（Task 保留/前后轮折叠/两 `<mem>`）、oversize/smaller、
      尺寸守卫 placements 乘数
- [ ] I-53 测试恢复：provider 来自 preview params（非 profile/硬编码）、usage 计
      reasoning/tool args
- [ ] ADR-001 §10 验证行注明 pi/memory 已按 R1 释放契约实现；契约条款不变
- [ ] 变异：删 after_task placement → mixed 测试红；去掉 placements 乘数 →
      超尺寸测试红
- [ ] OH 全量绿（6 项 401 环境失败除外）；`harness/` 零 diff；不复活 memory_bridge

## 关联

- 参考：`3d10bf3:scripts/memory_bridge.py`（I-57 R2 最终版）、
  `3d10bf3:tests/test_memory_state_machine.py`、`3d10bf3:tests/test_memory_observe.py`
- 前置：I-53 R2、I-57 R2、I-60、I-62
