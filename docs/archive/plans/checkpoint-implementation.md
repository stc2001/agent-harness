# Session + Sandbox Checkpoint 实现计划

## 背景

长程自主运行 Agent 需要崩溃恢复能力。本计划覆盖两个层级的 checkpoint：
- **Session checkpoint**（框架层）：打包 `sessions/{sid}/` 事件流 + `workspace/{sid}/` 文件，支持完整回滚
- **Sandbox checkpoint**（沙箱层）：workspace 文件系统快照，支持 overlayfs（快）或 tar（通用）

## 目标

- SessionManager 提供 `snapshot(sid, label)` / `restore(sid, label)` API
- SandboxManager 提供 `snapshot(name)` / `restore(name)` API，自动检测 overlayfs 支持
- 两者解耦：Session checkpoint 不依赖 Sandbox checkpoint 的具体实现

---

## Phase 1: SessionManager Checkpoint

### 新增 API

```python
# harness/session/impl.py
class SessionManager:
    def snapshot(self, label: str) -> str:
        """Pack session dir + workspace dir into a tar.gz snapshot.

        Returns:
            snapshot_id (e.g. "{sid}@{label}-{ts}")
        """

    def restore(self, snapshot_id: str) -> None:
        """Replace current session dir + workspace dir from snapshot.

        Requires:
            - Session is not active (no running job)
            - snapshot_id exists in snapshots/
        """

    def list_snapshots(self) -> list[str]:
        """List all snapshot IDs for this session."""
```

### 存储布局

```
snapshots/
  {sid}@{label}-{ts}.tar.gz   # tar 包含 sessions/{sid}/ + workspace/{sid}/
```

### 实现要点

1. `snapshot()` 使用 `tarfile` 打包，排除 `.tmp.*` 文件
2. `restore()` 原子替换：解压到临时目录 → rename 覆盖
3. `restore()` 后自动调用 `wake()` 重建 SessionManager 内存状态
4. 需要 `_ensure_inactive()` 检查（状态不能是 RUNNING）

### 测试

- `test_snapshot_creates_tar`: snapshot 后 tar 文件存在
- `test_restore_replaces_events`: restore 后事件流与 snapshot 一致
- `test_restore_replaces_workspace`: restore 后 workspace 文件与 snapshot 一致
- `test_restore_fails_when_running`: RUNNING 状态 restore 抛出 SessionStateError
- `test_list_snapshots`: 返回有序列表

---

## Phase 2: SandboxManager Checkpoint

### 抽象层

```python
# harness/sandbox/backends.py 或新文件
from abc import ABC, abstractmethod

class WorkspaceBackend(ABC):
    @abstractmethod
    def snapshot(self, session_id: str, label: str) -> str: ...

    @abstractmethod
    def restore(self, session_id: str, snapshot_id: str) -> None: ...

    @abstractmethod
    def is_available(self) -> bool: ...
```

### TarBackend（通用 Fallback）

```python
class TarBackend(WorkspaceBackend):
    def snapshot(self, session_id, label):
        ws = Path("workspace") / session_id
        snap_dir = ws / ".snapshots"
        snap_dir.mkdir(exist_ok=True)
        snap_id = f"{label}-{int(time.time())}"
        tar_path = snap_dir / f"{snap_id}.tar.gz"
        subprocess.run(["tar", "-czf", str(tar_path), "-C", str(ws), "."])
        return snap_id

    def restore(self, session_id, snapshot_id):
        ws = Path("workspace") / session_id
        tar_path = ws / ".snapshots" / f"{snapshot_id}.tar.gz"
        # 清空 workspace（保留 .snapshots）
        for item in ws.iterdir():
            if item.name == ".snapshots":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        subprocess.run(["tar", "-xzf", str(tar_path), "-C", str(ws)])
```

### OverlayFSBackend（快速，需支持度检测）

```python
class OverlayFSBackend(WorkspaceBackend):
    """OverlayFS snapshot: copy upperdir instead of tar.

    Requires:
        - Linux 5.11+ with unprivileged overlayfs
        - workspace mounted as overlayfs (lowerdir=base, upperdir=changes)
    """

    def snapshot(self, session_id, label):
        # 直接 cp -a upperdir/ → snapshot dir
        pass

    def restore(self, session_id, snapshot_id):
        # umount + remount 换 upperdir
        pass

    def is_available(self):
        # 尝试一次临时 overlayfs 挂载
        pass
```

### 支持度检测

```python
def _overlayfs_supported() -> bool:
    """Check if unprivileged overlayfs mount is supported."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for d in ["lower", "upper", "work", "merged"]:
            os.makedirs(f"{tmp}/{d}")
        try:
            subprocess.run([
                "mount", "-t", "overlay", "overlay",
                "-o", f"lowerdir={tmp}/lower,upperdir={tmp}/upper,workdir={tmp}/work",
                f"{tmp}/merged"
            ], check=True, capture_output=True)
            subprocess.run(["umount", f"{tmp}/merged"], check=True, capture_output=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
```

### SandboxManager 集成

```python
class SandboxManager:
    def __init__(self, ...):
        self._workspace_backend = (
            OverlayFSBackend() if _overlayfs_supported() else TarBackend()
        )

    def snapshot_workspace(self, name: str, label: str) -> str:
        """Snapshot the workspace of a sandbox instance."""
        backend, instance_id = self._instances[name]
        # 需要 session_id → 从 instance 关联获取
        ...

    def restore_workspace(self, name: str, snapshot_id: str) -> None:
        """Restore workspace from snapshot."""
        ...
```

### 测试

- `test_tar_backend_snapshot_and_restore`: tar 打包/恢复完整流程
- `test_overlayfs_available`: 检测函数正确返回
- `test_sandbox_manager_uses_correct_backend`: 根据环境选择 backend

---

## Phase 3: 集成与协同

### 使用模式

```python
# App 层调用示例
client = HarnessClient()

# 1. 关键节点 snapshot（人工或自动）
snap_id = client.sessions.snapshot(sid, label="before-mutation")

# 2. Agent 运行中...

# 3. 出错了，回滚
client.sessions.stop(sid)
client.sessions.restore(snap_id)
client.jobs.submit_spec(job_spec)  # 重新提交
```

### 自动触发（可选，App 层配置）

```yaml
# sandbox.yaml 或 job spec
checkpoint:
  auto: true
  interval: 10  # 每 10 次 LLM 调用
  on_phase_end: true
```

---

## 文件变更预估

| 文件 | 变更 | 行数 |
|------|------|:--:|
| `harness/session/impl.py` | +snapshot/restore/list_snapshots | ~80 |
| `harness/sandbox/backends.py` | +WorkspaceBackend, TarBackend, OverlayFSBackend | ~120 |
| `harness/sandbox/manager.py` | +snapshot_workspace/restore_workspace | ~30 |
| `tests/test_session.py` | +snapshot/restore 测试 | ~60 |
| `tests/test_sandbox.py` | +backend 测试 | ~80 |

总计：~370 行实现 + 测试

---

## 依赖

- `tar` 命令（已存在于所有 Linux 发行版）
- `overlayfs` 内核模块（可选，自动 fallback）
- Python `tarfile` 标准库（fallback 用）

---

## 风险

| 风险 | 缓解 |
|------|------|
| OverlayFS 不支持 | 自动 fallback 到 TarBackend，无功能损失 |
| Snapshot 文件过大 | 排除 `.snapshots/` 递归，只打包当前状态 |
| Restore 时 session 仍在运行 | `_ensure_inactive()` 检查，抛出明确错误 |
| 并发 snapshot | SessionManager 加锁（已有 per-session lock） |

---

## 状态

- [ ] Phase 1: SessionManager snapshot/restore
- [ ] Phase 2: SandboxManager WorkspaceBackend
- [ ] Phase 3: 集成测试 + App 层示例
