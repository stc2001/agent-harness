# ADR-048：策略中间件（gateway/hooks 定向规则 + preset 规则集，I-27）

## Status

Accepted (2026-09-05)

## Context

需要按 **agent 定向**施加命令/文件/网络/状态类约束（危险 bash、系统目录、read→edit、目标网关），但 hooks 是**进程级全局**（ADR-042 §8）。多 agent/多客户端共享同一 hooks 总线；约束必须能精确落到「特定 agent」而绝不影响其他 agent。同时策略内容（哪些规则、给谁）是**部署/编排决策**——框架不应默认开启任何规则（零配置 = 零行为变化）。

## Decision

### 1. 进程级 PolicyEngine + agent 定向生效集

- `harness/policy.py` 单例引擎：规则定义（define/add）+ **显式启用**（`enable(name, agent)`，agent 必填或 `"*"`）+ disable/list + `effective(agent)`。
- 生效集 = scope ∈ {agent, `"*"`} 的已启用条目；默认 ∅（**一切默认关**）。
- 注册全局 `authorize.veto/observe` + `tool.execute` hooks（resilient：注册表被清自动重挂）；hook 内 `ctx.spec.name` 不在生效集 → 放行。

### 2. 规则语义：并集 + deny-wins

多规则同作用于一次授权 = 各 veto 并集，任一 deny 即拒（安全侧）；无 approve 覆盖、无优先级层。规则条目 `{name, veto(ctx,cfg)->None|str, observe, observe_exec, cfg}`。

### 3. 声明式 opt-in 双通道 + 可扩展

- `AgentSpec.policy`（str|list|None）：register 自动 `enable(name, agent=spec.name)`（未知名 warn-ignore）。
- `client.policy.enable(name, agent)`：gateway 配置期全局（`"*"`）/定向开启。
- `client.policy.add(name, agent, veto, observe)`：插入 harness 无的自定义规则（add 即生效，agent 必填）。

### 4. 内置 preset（默认停用；启用时 app 供 cfg）

| preset | 语义 |
|--------|------|
| bash_whitelist | 危险命令通配 deny（rm -rf /、mkfs、dd of=/dev/、curl|sh…；cfg.allow） |
| fs_paths | 系统目录 deny（/etc /proc /sys /dev /boot /root /run /usr…） |
| read_before_edit | DSH 式版本化：observed-version per (agent,session,path)；hash 系纯参数门（hashread→hashedit old_hash）；literal 系 cfg.digest_provider（sha256 适配器），无则 seen fallback；角色/参数映射在 cfg，**与 read/edit 工具实现零耦合** |
| net_policy | 目标 deny（cfg.deny IP/CIDR/域名；命令级）；host 模式沙箱内 127.0.0.1 = 宿主机 |

### 5. 后端边界（roadmap，非本 ADR 决策）

动态检查（正则/路径解析/digest/时序状态）**必须 host-side**——Cedar 无正则/字符串算子（官方设计文）、单请求无状态。**2026-09-09 修订（I-35）**：Dogwood 设想取消（无 python 包）；角色收敛为 **Cedar = 静态决策核心**（allow/deny/NoDecision=放行+warn）+ **python veto = 动态/有状态 overlay**（寄生静态之后；read_before_edit 式时序/版本在此层）。exec-guard 允许域 = 规则**guard 数据段**直接编译（非 Cedar 决策输出——无翻译层）；python-only 规则=cedar 空+overlay（无独立兼容层）。Cedar 化经 I-34/I-35（engine+接线）/I-37/38（preset 静态/动态重写）落地。

### 6. 执行期链注记（2026-09-09，I-33）

本 ADR 的 veto 只覆盖 **pre-call 参数门**（约束模型提交的参数/版本），**不足以约束实际 fs/net 访问**（字段匹配可被 bash 通配等绕过，且真实访问只在执行时发生）。决策：策略作用链扩展为 `pre-call → tool_called → [执行期策略点] → tool-exec`——执行期强制/观察 = policy 基础设施，接口挂 sandbox.impl（`restrict_paths/restrict_net/access_events`），生效规则由 policy **编译**为这些调用，内核（Landlock/netns+nft/seccomp）执行、eBPF/audit 观察回填；详见 **ADR-051** 与 **I-33**。

### 7. 策略定义单元：三层结构（2026-09-09，I-35/I-37/I-38 定稿）

每条启用规则（per enabled rule）是一个**定义单元**，三层并列：

```
policy 定义单元（per enabled rule）
 ├── cedar 静态核心：业务级决策（该 Action×File/Cmd 放行？危险模式 forbid？）
 ├── guard 数据段：paths{rw,ro} + sys_read_deny + syscalls_deny/allow
 │        ← 直接编译进 SandboxRestriction（无 cedar→restriction 翻译层）
 └── veto overlay：动态/有状态（寄生静态之后；read_before_edit 式时序/版本）
```

- 执行链：`cedar 决策 → overlay veto → guard 编译 → execute(restrict)`；deny-wins（Cedar forbid 覆盖 permit；overlay deny 覆盖静态 allow）。
- `description`（选填）/`reason`（可 deny 的规则必填；Cedar forbid 无自定义消息时兜底）随定义单元声明。
- python-only 规则 = `cedar=None` + overlay（自然子集，无独立兼容层）。
- `kind`：`file`（File{path}）/`cmd`（Cmd{text}）/`auto`（按工具族自动选，fs_paths 用）。

### 8. Cedar 实践要点（2026-09-09，cedarpy 实测）

1. **实体必须用对象 uid**：`{"uid": {"type":"Cmd","id":"c"}, "attrs":{...}, "parents":[]}`——uid 字符串形式会被静默解析失败（`diagnostics.errors` 报错但决策仅表现为 Deny）。
2. **条件必须写在 `when { … }` 子句**：`resource is Cmd && …` 内联在括号里会得到 `unexpected token &&` 解析错。
3. **未匹配 = 隐式 Deny（不是 NoDecision）**：`is_authorized` 对「无 permit/forbid 命中」返回 Deny；用 `diagnostics.reasons`（仅带 `@id` 的命中策略才非空）区分**真 forbid** 与**隐式拒**，从而实现「未纳管=放行+warn」的 NoDecision 语义。
4. **`like` 是 glob，不是正则**：危险命令正则（`rm\s+-[a-z]*[fr]\s+/`、`curl|sh`）无法无损转写 → 保留 veto overlay（bash_whitelist）。
5. **评估错误 fail-closed**：cedarpy 异常 → 决策 deny（reason 指明 policy engine error），不静默放行。
6. **deny 子集不可达（Landlock allow-list）**：guard 只能加 allow；要禁读系统目录需 `sys_read_deny`（execguard 装配时从自动 RO 中排除 → 内核 EACCES）。

### 9. I-38 关闭理由（2026-09-09）

`read_before_edit` 与 `net_policy` 的现有实现本就是**定义单元的 veto overlay 子集**（有状态/端点文本解析，Cedar 无正则且无状态）。严格 Cedar 化不带来功能增量，且增加表达扭曲；故关闭 I-38——两者保持「cedar=None + veto + cfg」形态（与 bash_whitelist 同）。本轮唯一带来安全加固的适配是 **fs_paths**（cedar 静态 + guard.sys_read_deny 内核禁读）。

### 10. 作用域治理归 app（组作用域触发条件，2026-09-10）

引擎作用域现为 **per-agent + `"*"`**；「统一管理一组 agent」的分组维度（tenant / project / mission 节点 / 角色）是 **app 层治理问题**，harness 不先验猜测（ADR-038 边界）。触发引入**组作用域**（`enable(rule, group=…)` 或 spec 标签匹配）的条件：app 出现「≥2 个 agent 共享同一规则集且需要集中变更/按组审计」的实际需求。在此之前 app 侧逐个 enable 组合即可（显式、零引擎改动）。

## Consequences

- **正面**：多 agent 共享 hooks 总线而约束精确定向；零配置零行为变化（既有 582 测试全绿证明）；preset 内容可 opt-in；动态+声明分层清晰（Cedar/Dogwood 可后接）。
- **代价**：进程级引擎单例与 hooks 全局同构（ADR-042 §8）——跨 client 同进程共享，隔离靠 spec.name 定向（文档注明）；规则为 Python 函数而非声明文本（声明层留给 I-29）。
- **依赖**：ADR-042（hooks 全局语义）、I-26（沙箱实体寻址供 FS 适配器）。

## References

- ADR-042（hooks/全局·业务隔离）§8
- ADR-047（沙箱 uuid——read_before_edit 的 sha256 适配器经 sandbox 实体）
- Issues：I-27（实现）/ I-28（沙箱绑定策略面）/ I-29（Cedar/Dogwood hybrid backlog）
- Cedar 官方设计文（why-no-regex）、AWS Dogwood 开源公告（2026-08）