> **⚠️ Superseded by [ADR-035 (Gateway)](035-gateway-authorization.md)** — retained for historical context only.

# ADR-009：模块分组原则 —— 按消费集群而非抽象层级

**日期**：2026-05-13  
**状态**：已采纳  

## 上下文

重构后项目从 9 个模块扩展到 13 个。L2 层新增了 scheduler、executor、gate_manager 三个模块，加上移出的 instance_pool（更名为 pool），共 4 个模块。它们有两个共同特征：
1. **单一消费者**：全部只被 `coordinator` 使用
2. **派生存在**：它们的存在意义完全来自 Coordinator 的职责拆分——不是独立模块，是 Coordinator 被拆开后的零件

其他模块（session、sandbox、gateway、agent_runtime）被多个上层使用，有独立的职责边界。

## 决策

**只把满足"单一消费者 + 派生存在"条件的模块收进集群目录。其余模块保持平铺。**

平铺是默认。分组是例外，需要为每个分组辩护。

## 分组标准

| 条件 | 满足的模块 | 不满足的模块 |
|------|----------|------------|
| 只有单一消费者 | scheduler, executor, gate_manager, pool | session（被 coordinator + cli + ... 使用） |
| 存在意义来自消费者的拆分 | scheduler 是 Coordinator 的"编译"职责 | gateway 有独立的加密语义 |
| 消费者本身稳定（不会消失或重构） | Coordinator 是架构的固定层 | N/A |

只有同时满足以上条件的模块才被收入集群。

## 不按抽象层级分组的原因

按层级（L0/L1/L2/L3）分组会导致：

```
harness/
├── l0_infra/
│   ├── gateway/
│   ├── agent_spec/
│   ├── session_store/
│   └── sandbox/
├── l1_runtime/
│   ├── agent_runtime/
│   └── session/
├── l2_orchestration/
│   └── ...
```

问题：
1. **导入路径变长**：`from harness.l0_infra.gateway import ...` 比 `from harness.gateway import ...` 多了无意义的层级
2. **争论归属**：`session` 是 L1 还是 L2？`agent_runtime` 是 L1 还是 L0？每次新模块都要争论
3. **层级变动**：模块的层级可能随架构演进改变，目录结构不该跟着变
4. **无实际约束力**：Python 不能阻止跨层 import，层级目录只增加路径长度不增加安全性

## 后果

- `harness/orchestration/` 是项目中唯一的模块集群目录
- 未来如果 executor 发展出独立的生命周期管理，被多个消费者使用，它应该被移回 `harness/` 平铺层
- 平铺是默认——新模块放在 `harness/` 下，直到有证据表明它应该进入某个集群
- 集群目录的 `__init__.py` 提供统一的 re-export，外部不应直接 import 集群内部的 `.impl` 路径
