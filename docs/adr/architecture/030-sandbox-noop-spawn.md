> **⚠️ 修正标注（2026-08-04）**：本 ADR 曾错误标注为 "Superseded by ADR-035"。
> ADR-035 是 Gateway 授权引擎，与 sandbox spawn 语义无关，标注系误标，已移除。
> spawn 的锚点进程树结构与生命周期决策见 **ADR-040**（含 PDEATHSIG 并发误杀根因与孤儿清理约定）。

# ADR-030: Sandbox spawn() 改为 no-op 容器 — 消除框架代码入沙箱

**日期**: 2026-05-22  
**状态**: 已接受  

## 上下文

`sandbox/impl.py` 的 `spawn()` 启动了一个 `agent_main.py` 进程在 bwrap 沙箱内运行。这导致：

1. **框架代码运行在隔离环境内**：AgentRuntime（框架层）被塞进沙箱，而沙箱的本意是隔离"不可信的用户代码"
2. **部署路径硬编码**：`agent_main.py` 使用宿主绝对路径（`/home/.../.venv/bin/python`），生产环境部署到 site-packages 时失效
3. **shared_folders 互斥 bug**：`if shared_folders:` 和 `else:` 互斥，非空时跳过系统目录（`/usr`, `/bin`, `/lib`）的只读挂载
4. **execute() 与 spawn() 语义混乱**：`spawn()` 启动的进程和 `execute()` 创建的实例无关——后者每次创建新的 bwrap 实例

## 决策

**`spawn()` 启动 no-op 进程（`sleep infinity`），AgentRuntime 始终在宿主运行，通过 `execute()` 在沙箱内执行工具命令。**

```
修复前：                              修复后：
spawn():                              spawn():
  bwrap -- python agent_main.py         bwrap -- /bin/sh -c "sleep infinity"
    └─ 沙箱内运行 AgentRuntime            └─ 空转容器，保持 namespace 存活
                                           
execute("cat file"):                  execute("cat file"):
  新建 bwrap 实例（与 spawn 无关）       每次自建 bwrap 实例执行命令
    └─ 独立 namespace                      └─ 独立 namespace（不复用锚点，见 ADR-040）
```

与 Docker 后端一致：`docker run` 启动容器，`docker exec` 在容器内执行命令。

## 后果

- `harness/agent_main.py` 删除（无调用方）
- `_build_bwrap_cmd()` 删除 `entrypoint` / `python_bin` 参数
- 系统挂载（`/usr`, `/lib`, `/bin`, `/etc`）始终绑定，不受 `shared_folders` 影响
- `execute()` 复用 `spawn()` 的 `gateway` 和 `session_id`（修复了之前传空的 bug）
- Bubblewrap 后端与 Docker 后端语义对齐
