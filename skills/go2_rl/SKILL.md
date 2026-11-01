---
name: go2_locomotion_policy
description: Pure control-theoretic knowledge for Go2 quadruped locomotion
---

# Go2 Locomotion Control Knowledge Base

## 0. Self-Test Script
Before submitting, the Generator MUST run:
  `bash "python /workspace/skills/go2_rl/test.py /workspace/output/policy.py"`
This checks: Go2Policy class exists, step() returns dict with control_mode,
no banned imports (torch/eval/exec), policy loads without crash.
PASS = ready for validator.  FAIL = fix and re-test before submitting.

## 1. Quadruped Gait Patterns

### Trot Gait (推荐用于平面行走)
- 对角足同步摆动：FL+RR 为一组，FR+RL 为一组
- 占空比（duty factor）：0.5
- 相位偏移：对角对之间 π rad
- 步态周期：0.4–0.6s（对应 1.67–2.5Hz）
- 抬腿高度：0.05–0.08m
- **默认站立关节角（Go2 标称）:**
  - hip: 0.0 rad
  - thigh: 0.9 rad
  - calf: -1.5 rad（注意：-1.8 rad 过于弯曲，会导致机身下沉 squat）
- **步态相位初始化：** phase 从 0 开始，Pair A (FR+RL) 在 phase < π 时摆动，
  Pair B (FL+RR) 在 phase >= π 时摆动。确保初始时刻至少有一对足在支撑相。

### Walk Gait (低速稳定)
- 四足依次着地，任意时刻三足支撑
- 占空比：0.75
- 相位偏移：相邻足之间 0.5π rad
- 适用于斜坡或不平地形

### Crawl / Static Gait
- 每次仅移动一足，重心投影始终落在支撑三角形内
- 占空比：> 0.75
- 极低速（< 0.1 m/s），极高稳定性

## 2. Body Stabilization Strategies

### Pitch Compensation
- 检测量：base_pitch（通过 IMU gravity 投影计算）
- 策略：当 pitch > 2° 时，前腿髋关节后拉，后腿髋关节前推
- 增益建议：kp_pitch = 0.8, kd_pitch = 0.05

### Roll Compensation
- 检测量：base_roll
- 策略：当 roll > 2° 时，高侧腿外展角减小，低侧腿外展角增大
- 注意：外展角（hip abduction）直接影响重心横向位置
- 增益建议：kp_roll = 0.6, kd_roll = 0.04

### CoM Trajectory Planning
- 足端位置 = 默认位置 + 摆动轨迹 + CoM 补偿项
- CoM 补偿项 = kp_body * (desired_CoM - actual_CoM)
- desired_CoM 根据足端支撑多边形实时更新

## 3. Swing Leg Trajectory

### Continuous Foot Trajectory (推荐，避免冲击)
足端轨迹必须保证 swing→stance 和 stance→swing 过渡处连续（位置连续，速度尽量连续）。

坐标系约定：x 正方向 = 机器人前进方向。

**Swing Phase** (t ∈ [0, 1], 摆动相):
  x(t) = -step_len/2 + step_len * (t - sin(2πt)/(2π))   # 从 -step_len/2 到 +step_len/2
  z(t) = swing_height * sin(πt)                           # 抬腿再放下

**Stance Phase** (t ∈ [0, 1], 支撑相):
  x(t) = step_len/2 - step_len * (t - sin(2πt)/(2π))     # 从 +step_len/2 到 -step_len/2
  z(t) = 0.0

关键验证：在 t=0 (swing start) 时 x=-step_len/2，t=1 (swing end) 时 x=+step_len/2；
在 t=0 (stance start) 时 x=+step_len/2，t=1 (stance end) 时 x=-step_len/2。
swing→stance 过渡处 x 连续（均为 +step_len/2），stance→swing 过渡处 x 连续（均为 -step_len/2）。

**Foot Workspace → Joint Space Mapping（线性近似）:**
  thigh_delta = x_offset * 1.8    # rad per meter of foot x-displacement
  calf_delta  = -z_offset * 2.2   # rad per meter of foot z-displacement
  注意：thigh_delta 的正负号必须与物理方向一致——thigh 角增大使足端向后移动。
  验证方法：在平地上，正 step_len 应产生正的前进距离。

## 4. Terrain Adaptation

### Flat Plane
- 标准 PD 参数即可
- kp = 20.0, kd = 0.5（Genesis 默认值）

### Rough Terrain
- kp 降低 20–30%，增加阻尼 kd 提高 50%
- 加入足端力反馈：接触力 > 阈值时提前切换支撑相
- 降低期望速度 30%

### Slope
- 上坡：前腿大腿关节增加预紧力矩补偿重力
- 下坡：后腿大腿关节增加预紧力矩
- 预紧力矩 = m_leg * g * sin(slope_angle) * l_cog

## 5. Reference Tracking

### Velocity Command to Gait Parameter Mapping
```
step_length = v_desired * T_stance
where T_stance = duty_factor * T_gait
```
- v_desired = 0.5 m/s（参考值）
- T_gait = 0.5s → step_length ≈ 0.125m
- **重要：step_length 的符号必须与 v_desired 一致。v_desired > 0 时 step_length > 0，
  表示足端在摆动相向前移动（相对于身体），在支撑相向后移动（相对于身体）。**

### Heading Control
- 偏航误差 → 左右腿步长差分
- delta_step = k_yaw * yaw_error
- 右腿步长 += delta_step，左腿步长 -= delta_step

## 6. Failure Recovery Heuristics

### Foot Slip Detection
- 标志：joint position 到达目标但足端线速度 > 阈值
- 修复：降低该足 kp 20%，增加着地检测延迟

### Backward Drift
- 原因：参考坐标系定义错误或相位初始化偏移
- 修复：检查 gravity 投影方向与期望前进方向是否一致
- 检查 stance 轨迹的 x(t) 符号：支撑相足端应从 +step_len/2 移动到 -step_len/2
  （相对于身体向后移动），这样才能推动身体向前。

### Body Oscillation
- 原因：PD 增益过高或缺少微分滤波
- 修复：kd 增加 30%，或对 action 做一阶低通滤波（tau = 0.05s）

### Early Fall (Pitch/Roll > 10°)
- 紧急策略：四足同时撑地，CoM 降低至最低
- 后续迭代：增加站立相占空比，降低步高

### CoM Drop (机身下沉)
- 原因：默认 calf 角过弯（如 -1.8 rad），或 swing/stance 轨迹不连续导致冲击
- 修复：将 default calf 角设为 -1.5 rad；确保 swing→stance 过渡处足端位置连续

## 7. Parameter Ranges (Tuning Guidelines)

| Parameter | Nominal | Min | Max | Notes |
|-----------|---------|-----|-----|-------|
| kp_position | 20.0 | 10.0 | 40.0 | 越高越刚性，越容易振荡 |
| kd_position | 0.5 | 0.1 | 2.0 | 主要抑制振荡 |
| action_scale | 0.25 | 0.1 | 0.5 | 越小越保守 |
| gait_period | 0.5s | 0.3s | 0.8s | 越小步频越高 |
| swing_height | 0.06m | 0.03m | 0.10m | 越高越耗能，越容易绊倒 |
| duty_factor | 0.5 | 0.3 | 0.75 | < 0.5 为动态步态 |
| default_calf_angle | -1.5 rad | -1.7 rad | -1.3 rad | 标称站立角，-1.8 会导致 squat |
| step_length_base | 0.125m | 0.08m | 0.20m | v=0.5m/s, T_gait=0.5s, duty=0.5 时理论值 |
| kp_pitch | 0.8 | 0.4 | 1.2 | 俯仰稳定增益 |
| kd_pitch | 0.05 | 0.02 | 0.10 | 俯仰阻尼 |
| kp_roll | 0.6 | 0.3 | 1.0 | 横滚稳定增益 |
| kd_roll | 0.04 | 0.02 | 0.08 | 横滚阻尼 |
| filter_alpha | 0.3 | 0.1 | 0.5 | 低通滤波系数，越小越平滑 |
