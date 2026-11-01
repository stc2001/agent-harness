# ADR-002：Bubblewrap vs Docker（MVP 默认沙箱后端）

**日期**：2026-05-12  
**状态**：已采纳  

## 上下文

需要为 Agent 提供隔离执行环境。两个候选方案：
- **Docker**：容器运行时，需要 daemon，需要镜像
- **Bubblewrap (bwrap)**：Linux namespace 隔离，零 daemon，零镜像

## 决策

**选择 Bubblewrap 为 MVP 默认后端，Docker 为次选后端。**

## 理由

1. **零 daemon 依赖**。Bubblewrap 不需要 dockerd 运行，适合单机本地开发和 CI 环境。
2. **启动速度**。Bubblewrap 直接 fork + namespace 切换，无镜像拉取开销。
3. **简单性**。`bwrap` 命令行的参数直接对应 Linux namespace 原语（`--unshare-all`、`--bind`、`--share-net`），概念模型清晰。
4. **Docker 作为后备**。通过 `SandboxManager` + `sandbox.yaml` 的多后端注册表，切换到 Docker 只需改一行配置（`backend: docker`），不涉及业务代码修改。

## 后果

- Docker 后端已实现但未经集成测试（TODO.md P0-4）
- Bubblewrap 依赖 Linux 内核 namespace，不跨平台（macOS/Windows 不支持）
- 两个后端通过 `SandboxInterface` Protocol 统一，切换对上层透明
