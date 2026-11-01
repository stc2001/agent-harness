# ADR-047：沙箱 uuid 统一身份与 agent→sandbox 关联（I-26，命名空间层解耦）

## Status

Accepted (2026-09-04)

## Context

沙箱此前以**配置名即身份**：`SandboxManager` 每配置名单例（`_instances[name] = (backend, instance_id)`），`SandboxInterface` 全部方法以 OS 句柄（bwrap PID / docker 容器 id）寻址——「沙箱身份」在不同层出现三种含义（配置名 / PID / 容器 id），agent 与沙箱的命名空间并未真正解耦：想表达「同一配置的两个 agent 各用独立沙箱」或「一组 agent 共享一个真实容器」都只能通过改配置名实现。

## Decision

### 1. 统一身份：sandbox_id = uuid（全层唯一）

- **SandboxInterface 按 sandbox_id(uuid) 寻址**：`spawn(config, sandbox_id)`、`execute/kill/is_alive/stop/get_logs(sandbox_id, …)`。OS 句柄（PID/容器 id）**绝不跨出后端**——bwrap/docker 内部维护 `sandbox_id → OS id` 私有映射（`_by_uuid`），reap_orphans（宿主级 pgid+env 清扫）与后端 TTL reaper（内部按 OS id）不变。
- **实体**：`SandboxEntity(sandbox_id, config_name, backend)`——无公开 backend_instance_id；uuid 由管理器铸造。

### 2. 管理器 API 双层

| 层 | API | 语义 |
|----|-----|------|
| name 级（粗粒度，保留） | `get_or_create(name, create_new=False)` | 默认复用规范实体（配置名→唯一默认实体，行为与 I-26 前一致）；`create_new=True` 同名铸造独立新 uuid（规范指针不动） |
| 细粒度（uuid） | `create(name)`（总是新建，失败 raise）；`ensure(name, sandbox_id)`（绑定解析/重物化）；`get_entity(sandbox_id)`；`destroy(sandbox_id)`；`list()` | 同一配置可多实体共存（uuid 互异）；实体以 uuid 查询/销毁/枚举 |

### 3. 生命周期：uuid 稳定

- **respawn 保 uuid**：进程死/配置变更 → 管理器以**同一 sandbox_id 覆盖式**重 spawn（实体逻辑身份稳定；锚点可换、身份不变——ADR-040「锚点死亡不影响可用性」在实体层的延续）。
- **透明重物化（ensure）**：绑定 sandbox_id 缺失（进程重启 / 曾被 destroy）→ 用 `spec.sandbox` 配置以该 sandbox_id 懒重新 spawn。destroy = 现在拆除；默认 `get_or_create` 后铸新 uuid；pinned `ensure` 同 uuid 重新供给。
- **agent 生命周期无关**：agent stop/dispose 不销毁沙箱实体；显式 `destroy(sandbox_id)` 是唯一销毁途径。

### 4. 关联：agent 实例持 sandbox_id

- `AgentRuntime._sandbox_id`（原 `_sandbox_instance_id` 收敛改名）持有绑定实体 uuid；pool/register/run/wake 传 `AgentInstance.sandbox_id`（可选；None = 配置名规范实体）。
- 跨配置复用 sandbox_id → ValueError（防授权错位：gateway sandbox_spawn 授权仍按配置名进行）。

### 5. 明确不做

- 共享/隔离策略、组策略、动态重绑（绑定目标由编排决定）→ **I-27**（策略层，基于本 ADR 的实体关联原语）。
- 实体注册持久化（对齐 ADR-043「不做持久化 agent 状态」先例；uuid 作为跨进程逻辑句柄经 ensure 重物化）。

## Consequences

- **正面**：agent 与沙箱命名空间层解耦；同配置多实体表达自由（隔离/共享都可经绑定表达）；单一 uuid 词汇贯穿 manager→pool→runtime→接口，杜绝「沙箱身份是 PID 还是 uuid」歧义；配置变更不再漂移 agent 绑定。
- **代价**：`SandboxInterface` 签名变更（container_id→sandbox_id）+ 双后端翻译层；既有调用点/测试迁移（64 处 get_or_create + 后端级）；OS 句柄在后端内部再映射一层。
- **依赖**：I-25（实例分派先决）；I-26 是本 ADR 的实现载体。

## References

- ADR-040（锚点生命周期——§5 I-26 对齐注记；reap_orphans/锚点语义不变）
- ADR-046（agent 实例身份与运行时分派——实体关联叠加其「注册实例 ⇄ 运行时」不变量）
- ADR-021（skills scan 改按 sandbox_id 寻址）
- Issues：I-26（实现）/ I-27（策略层 draft）