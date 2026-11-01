# ADR-054：SDK 调用中 AgentSpec 是一等公民，profile 提供默认值与部署绑定

**日期**: 2026-10-02
**状态**: 已采纳
**关系**: 修订 ADR-014（spec/runtime-config 分离）、ADR-041 §4/§5（thinking 与 extra_body 透传）；延续 ADR-038（原语 vs app 边界）

---

## 上下文

### 触发事实（实测，非推断）

用本地 llama.cpp 后端（`spark-x2.5-4b`）跑长程任务时，`AgentSpec.thinking=True`
**完全没有效果**。逐层核查后得到完整因果链：

| 层 | 事实 | 证据 |
|---|---|---|
| 框架发出 | `extra_body = {"thinking": True}` | `agent_runtime/impl.py`（改前） |
| 后端反应 | **静默忽略** | 实测 `thinking:true` 与 `thinking:false` 输出**字节级相同**（300 completion / 761 reasoning 字符，temperature=0，各 3 次） |
| 真正开关 | `chat_template_kwargs.enable_thinking` | 实测 `false`→121/0，`true`→300/761（[上游 PR #22336](https://github.com/ggml-org/llama.cpp/pull/22336) 才加入按请求开关） |
| 覆盖关系 | profile 的 `enable_thinking: false` **压过** spec 的 `thinking=True` | 改前 reasoning=0，改后=212 |
| 是否已知 | **不知情** —— 无告警、无审计、无字段可查 | `grep` 全仓无 `enable_thinking` 与 `thinking` 的桥接代码 |

### 更一般的问题：spec 与 profile 的优先级**无约定**

审计全部「spec 与 profile 双方都声明」的字段后确认：

| 字段 | 冲突时现状 | 是否静默 |
|---|---|---|
| `model` | **profile 永远赢**（`LLMConfig.model` 默认非空 ⇒ `profile.model or spec.model` 的 `or` 分支不可达） | **静默** |
| `thinking` / `chat_template_kwargs` | profile 赢 | **静默** |
| `backend` | 不一致 ⇒ `RuntimeError` | 不静默（fail-fast） |
| `max_tokens` | spec 独有 | 无冲突 |
| `api_key` / `base_url` | profile 独有 | 无冲突 |

**`model` 的问题尤其隐蔽**:请求里发的 model 名来自 profile，而**模型服务池的键**来自
`spec.model`（`pool.py`）——两者解耦。本机之所以能跑，只是因为两处恰好都写了
`spark-x2.5-4b`；只改其一就会「池找到了但模型名发错」或反之，**无任何告警**。

### 为什么现在必须定这个规则

框架的既定定位是**本地智能体开发**：绝大多数使用场景是**开发者写代码、调 SDK**。
在这个定位下，**代码里的 `AgentSpec` 是意图的表达处**，profile 是「这台机器/这个环境
怎么连」的绑定。让 profile 静默压过代码意图，等于让**环境配置改写程序语义**，
且失败模式是「跑起来了但行为不对」——最难排查的一类。

现有 ADR 只覆盖了局部：ADR-014 的三层查找讲的是**同名 profile 找哪个文件**；
ADR-041 §5 只对 `backend` 与 `model` 各写了一句，方向还相反
（`backend` fail-fast、`model` profile 覆盖 spec），且**没有一般规则**。

## 决策

### 1. 一般规则：`AgentSpec` 优先，profile 兜底

> **SDK 调用路径上，`AgentSpec` 中显式设定的字段覆盖 profile；profile 提供默认值与
> 部署绑定（凭证、端点、机器相关的模型名）。**

### 2. 「显式设定」的判定（本 ADR 引入，已进 `CONTEXT.md`）

规则要可实现，必须先区分「**调用方写了这个字段**」与「**字段用了默认值**」。判定标准：

| 字段类型 | 「未表达意见」的取值 | 行为 |
|---|---|---|
| 可空数值/布尔/字符串 | `None` | 让位 profile |
| 字符串 | `""` | 让位 profile |
| 其他（含 bool 的 `False`） | **任何值都是显式** | 覆盖 profile |

**因此凡是需要「三态」（开 / 关 / 未表态）语义的字段，必须用 `None` 而非 `False` 作默认**
——`False` 无法与「未表态」区分，这正是本次缺陷的机制。

### 3. `backend` 例外：保留 fail-fast，不纳入覆盖规则

`spec.backend` 显式值必须与 profile 的 `backend` 一致，否则 `RuntimeError`（现状保留）。

**理由：安全边界，不是表现偏好。** `backend` 决定**凭证与端点的来源**（`cloud` 用 API Key，
`local` 用本机/内网服务）。若允许 spec 单方面把 `cloud` 覆盖成 `local`，
请求会**带着云端凭证打到本机服务**（或反向），属凭证外泄面。
`model` / `thinking` 只影响**请求内容**，不涉及信任边界，故适用一般规则。

### 4. 冲突处理：默认（P）+ 显式覆盖且不静默（Q）

- **P（默认路径）**：spec 未表态 ⇒ 一切按 profile。**这也是推荐用法** ——
  不同 agent 用不同 profile（如主 agent 用 `thinking` profile、总结器用
  `memory_summariser` profile），此时**根本不会产生冲突**。
- **Q（显式路径）**：spec 显式设定且与 profile 矛盾 ⇒ **spec 赢，但不静默**：
  记一条 audit 事件，说明哪个字段被覆盖、原值、新值。

**P 为默认表达了「职责分离」：agent 的身份与用途由 profile 表达，不该靠 spec 逐字段对抗。**

### 5. `model`：池键与请求名**统一**

`model` 的解析必须在**两处使用同一结果**，消除解耦：

```
resolved_model = spec.model (非空)  or  profile.model (非空)
```

- 请求体 `model` 用 `resolved_model`
- 模型服务池键用 `model_service (非空) or resolved_model`
- 两者**皆空** ⇒ 构造/执行期 fail-fast（带修复指引），不再回落到硬编码默认

### 6. 三层查找优先级**不变**

ADR-014 的「项目级 > 用户级 > 系统级」讲的是**同名 profile 的取用顺序**，
与本 ADR 的 spec-vs-profile 规则**正交**，不修改。

## 逐字段对照（改后）

| 字段 | spec 默认值 | 未表态时 | 显式时 | 备注 |
|---|---|---|---|---|
| `model` | `""` | 用 `profile.model` | 用 `spec.model` | 池键与请求名统一 |
| `thinking` | `None` | 用 profile 的 `chat_template_kwargs` | 覆盖 `enable_thinking`（P/Q） | 三态 |
| `backend` | `"auto"` | 用 `profile.backend` | **必须一致，否则 fail-fast** | 安全边界 |
| `max_tokens` | `16384` | — (spec 独有) | `spec.max_tokens` | 无冲突 |
| `context_window` / `context_budget_ratio` | `128000` / `0.8` | — (spec 独有) | spec 值 | 无冲突 |
| `api_key` / `base_url` / `timeout` | 无字段 | 总是 profile | — | 部署绑定，spec 不表达 |

## 后果

1. **行为变更（有，且是本次目的）**：`spec.model` 缺省不再静默等于「用 profile 的模型名
   但仍以硬编码默认值作池键」；`spec.thinking=True` 从此真正生效并记 audit。
2. **默认值变更**：`AgentSpec.model` 由 `"deepseek-v4-flash"` 改为 `""`。
   现有显式写死 model 的 spec **行为不变**；依赖旧默认值的 spec 会改为取 profile ——
   这正是期望（本地开发本就该由 profile 提供模型名）。
3. **校验放宽**：`agent_spec` 的 `model` 非空校验改为「`spec.model` 或 `profile.model`
   至少一个非空」。注册是 compile-only，profile 解析在执行期，故校验分两段。
4. **新增审计**：Q 路径产生 `agent_spec_override` 审计事件。
5. **文档**：ADR-041 §4/§5 需同步（其"不硬编码供应商特定开关"的表述要与
   「后端适配层」区分）；`CONTEXT.md` 增补术语。

## 被否决的方案

| 方案 | 否决理由 |
|---|---|
| **profile 永远优先**（现状） | 让环境配置静默改写程序语义；失败模式是「跑起来了但行为不对」 |
| **spec 永远优先（含默认值也算）** | 需每个 agent 都声明模型名等，profile 的默认值能力变死字段；与「profile 提供部署绑定」冲突 |
| **全部字段冲突即 fail-fast** | `thinking`/`model` 是正常的表现偏好，逐字段报错会迫使调用方写冗余显式值；fail-fast 只保留给安全边界（`backend`） |
| **spec 覆盖 profile 且静默** | 与本次缺陷同类：不可观测的覆盖 |

## Rev 注记

- 2026-10-02：首版。触发自查见 §上下文；`backend` 例外与 P/Q 语义经维护者裁决。

## 参考资料

- 上游 llama.cpp 按请求开关 thinking：[PR #22336](https://github.com/ggml-org/llama.cpp/pull/22336)
- ADR-014（spec/runtime-config 分离与三层查找）
- ADR-041（reasoning_content 供应商契约；§4 明确不做、§5 I-13 注记）
- ADR-038（原语 vs app 边界）
