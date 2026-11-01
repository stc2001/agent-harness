---
id: I-64
title: harness 度量/预算健壮性 — per-build cost map + 小窗口 reserve
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

两个 harness 侧健壮性缺口：

1. `_LAST_COST_MAP` 是模块全局，`build_messages` 每次 clear+repopulate；交错 build
   会让 `_cost` 读到另一次 build 的 pairing/skip 上下文，并发线程还会竞争。
2. 小窗口预算：`reserve = max_tokens or 16384`，`effective_reserve` 仅钳到
   `context_window-1` ⇒ 3200 窗口 `hard_limit=1`，prompt 不可用、app 85% 高水位
   不可达。

## 验收标准 / Acceptance criteria

- [ ] `build_messages(..., cost_map_out=dict)`：由调用方传入可变 dict、build 填充
      本 build 的 `(id(event), passback) -> (event, cost)` 快照
- [ ] `_cost(ev, ..., cost_map=...)` 使用显式上下文；全局 map 仅兼容回退，docstring/
      注释写明「仅最近一次 build 精确」，交错/并发必须显式 map
- [ ] 交错 build 测试：两次 build 后分别用各自 map 求 `_cost`，总和等于各自
      `build_messages` 输出成本；并发测试：每线程不同事件集 + 各自 map，不依赖全局
- [ ] `effective_reserve = min(reserve, max(1, ceil(context_window/4)))`；`hard_limit`
      使用 effective；`max_tokens` 请求原值不变；meta 报告 original/effective 两值
- [ ] tiny window（3200 + 默认 16384）hard_limit > 1，high-water 可达；显式
      max_tokens=5000 同样被钳制；大窗口（100K + 16384）不受影响
- [ ] I-51 旧 `effective=window-1` 断言按新语义更新，保持精确判据
- [ ] ADR-053 追加规则与理由
- [ ] 变异：`_cost` 忽略显式 map → 交错测试红；恢复旧 reserve 钳制 → tiny 测试红
- [ ] OH 全量绿（6 项 401 环境失败除外）；`harness/` 仅预期文件；translate/
      merge_TODO/部署物零 diff

## 关联

- 参考：`harness/prompt_assembly.py` `_LAST_COST_MAP` / `_cost` / `build_messages`
- 前置：I-51 R1（output reserve）、I-63
