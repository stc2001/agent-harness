# Go2Policy Quickstart

## 1. Interface Contract

Agent 必须生成一个 `policy.py` 文件，内含 `Go2Policy` 类：

```python
import numpy as np

class Go2Policy:
    def __init__(self):
        # 初始化内部状态
        self.last_actions = np.zeros(12)
        self.phase = 0.0  # 步态相位 [0, 2π]

    def step(self, obs: dict) -> dict:
        """
        每 0.02s (50Hz) 调用一次。

        Args:
            obs: 传感器融合字典

        Returns:
            控制命令字典，必须包含 "control_mode" 键
        """
        # 解析 obs
        joint_pos = obs["joint_pos"]        # (12,)
        joint_vel = obs["joint_vel"]        # (12,)
        imu_ang_vel = obs["imu_ang_vel"]    # (3,)
        imu_gravity = obs["imu_gravity"]    # (3,)
        ref_cmd = obs["ref_cmd"]            # {"lin_vel_x": float, ...}
        timestamp = obs["timestamp"]        # float

        # 计算控制量
        actions = ...

        return {
            "control_mode": "hybrid",
            "dof_pos_target": actions,
            "dof_vel_target": np.zeros(12),
        }
```

## 2. obs 字段详解

| 字段 | 类型 | 形状 | 单位 | 说明 |
|------|------|------|------|------|
| `joint_pos` | np.ndarray | (12,) | rad | 关节编码器读数 |
| `joint_vel` | np.ndarray | (12,) | rad/s | 关节速度 |
| `imu_ang_vel` | np.ndarray | (3,) | rad/s | 基座角速度（机体坐标系） |
| `imu_gravity` | np.ndarray | (3,) | m/s² | 重力方向在机体坐标系的投影 |
| `lidar_points` | np.ndarray \| None | (N, 3) | m | 点云数据（如有配置） |
| `ref_cmd` | dict | — | — | `{"lin_vel_x": m/s, "lin_vel_y": m/s, "ang_vel_yaw": rad/s}` |
| `timestamp` | float | — | s | 当前仿真时间 |

### 关节顺序

Go2 有 12 个关节，按以下顺序排列：

```
[FR_hip, FR_thigh, FR_calf,
 FL_hip, FL_thigh, FL_calf,
 RR_hip, RR_thigh, RR_calf,
 RL_hip, RL_thigh, RL_calf]
```

### IMU 坐标系

- x: 机器人前进方向
- y: 机器人左侧方向
- z: 机器人上方方向

`imu_gravity` 投影接近 `[0, 0, -1]` 表示机器人水平站立。

## 3. Control Modes

Agent 的 `step()` 返回值必须包含 `"control_mode"`，取值如下：

| 模式 | 必需字段 | Genesis API |
|------|----------|-------------|
| `"position"` | `dof_pos_target` | `robot.control_dofs_position(target, dofs_idx)` |
| `"velocity"` | `dof_vel_target` | `robot.control_dofs_velocity(target, dofs_idx)` |
| `"force"` | `dof_force` | `robot.control_dofs_force(force, dofs_idx)` |
| `"hybrid"` | `dof_pos_target`, `dof_vel_target` | `robot.control_dofs_position_velocity(pos, vel, dofs_idx)` |

### PD 参数说明

Genesis 默认 PD 增益：
- `kp = 20.0` (刚度)
- `kd = 0.5` (阻尼)

Agent 可以在 `__init__` 中调用以下 API 修改增益（如需自适应）：

```python
robot.set_dofs_kp([kp] * 12, motors_dof_idx)
robot.set_dofs_kv([kd] * 12, motors_dof_idx)
```

**注意**：修改 PD 增益需要在仿真开始时一次性完成，不要在 `step()` 中每帧调用（开销大）。

## 4. 产物要求

### 文件路径
```
/workspace/results/generator/{uuid}/policy.py
```

### 命名检查
验证器会确认：
1. 文件名为 `policy.py`
2. 类名为 `Go2Policy`
3. 包含 `step(self, obs: dict) -> dict` 方法
4. 没有使用禁用库（见下方白名单）

## 5. 允许与禁止的库

### ✅ 允许
- `numpy`, `np` — 数值计算
- `scipy` — 信号处理、优化
- `cv2` — 图像/点云处理
- `math` — 基础数学
- `genesis`, `gs` — 物理引擎（如需直接访问场景）
- `gymnasium`, `gym` — 环境接口
- 标准库：`os`, `sys`, `typing`, `dataclasses`, `collections`, `functools`, `json`

### ❌ 禁止（任何深度学习 / 权重训练）
- `torch`, `torch.nn`, `torch.optim`, `torch.utils.data`
- `stable_baselines3`, `sb3`
- `tensorflow`, `tf`, `keras`
- `jax`, `flax`
- `sklearn`, `scikit-learn`
- `transformers`
- `eval`, `exec`, `compile`, `__import__`
- `urllib`, `http.client`, `socket`, `requests`
- `os.system`, `os.popen`, `subprocess`

**重要**：本框架属于 **Heuristic Learning** —— Agent 生成的是纯控制学策略代码（状态机、PD 控制器、规则引擎、MPC），不涉及任何神经网络权重训练或参数更新。

## 6. 验证流程

1. 验证器加载 `policy.py` 中的 `Go2Policy`
2. 初始化 Genesis 场景（Go2 + 平面 + IMU + LiDAR）
3. 每 0.02s 调用 `policy.step(obs)`
4. 根据 `control_mode` 调用对应 Genesis API
5. 运行 20s 后输出评估指标到 `/workspace/results/validator/genesis_output.json`

### Pass 标准
```json
{
  "forward_distance_m": >= 5.0,
  "max_pitch_deg": <= 10.0,
  "max_roll_deg": <= 10.0,
  "avg_lin_vel_x": >= 0.25,
  "termination_reason": "time_limit"
}
```
