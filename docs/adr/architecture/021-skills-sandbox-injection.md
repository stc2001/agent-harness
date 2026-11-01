# ADR-021: Skills 沙箱注入模型 — 文件系统化 + 渐进式披露

**日期**: 2026-05-19  
**状态**: 已接受  

## 上下文

Skills 系统最初实现为 Python 内存中的 `SkillRegistry`——Skill 文件在宿主机被解析为 Python 对象，正文通过 `_build_messages` 注入 system prompt。这种"预加载+注入"模型导致：(1) Skill 正文污染 KV Cache 前缀；(2) Agent 无法在运行时发现/安装 Skill；(3) 与 Anthropic 的渐进式披露模型不一致。

## 决策

**Skills 改为沙箱文件系统注入模型。** Skill 是沙箱中的文件系统 artifact，Agent 通过已有工具（`read_file`、`bash`）自主发现和读取。

具体变更：
1. `SkillRegistry` 类删除——不再维护 Python 内存中的 Skill 索引
2. `skills/` 目录扁平化（符合 AgentSkills.io 标准：`skills/{name}/SKILL.md`）
3. `_build_messages` 不再注入 Skill 正文——仅追加元数据指针（name + description）
4. `_SkillsClient.scan(sandbox, sandbox_id)` 通过沙箱 `ls` + `head` 命令扫描 Skill（I-26 起沙箱按 sandbox_id(uuid) 寻址，见 ADR-047）
5. `list_host_skill_metas()` 作为 CLI 宿主机辅助函数保留

## 理由

1. **符合行业标准**。Anthropic Claude Code 和 AgentSkills.io 标准均采用文件系统模型。Skill 是 Markdown 文件的文件夹，Agent 通过工具发现并按需读取。
2. **KV Cache 友好**。元数据指针（~50 tokens/skill）远小于正文（~500+ tokens/skill），且在 Skill 不常变动时指针顺序不变——前缀缓存稳定。
3. **Agent 自治**。Agent 可以通过 `bash git clone` + `install_skill` 工具在沙箱内安装 Skill，无需重启或重载。
4. **可扩展**。符合 AgentSkills.io 规范的 Skill（`scripts/`、`references/`、`assets/`）可直接被 Agent 执行和引用。

## 后果

- `list_skills` 和 `invoke_skill` 工具删除——Agent 用 `read_file` + `bash ls` 替代
- `skills_placement` 字段删除——注入位置不再是框架决定
- `_activated_skills` 追踪删除——Agent 自己决定何时读 Skill
- `install_skill` 工具新增——Agent 验证沙箱内 Skill 的 SKILL.md 有效性
- `_build_messages` 中的 Phase 3 (skills injection) 删除
- CLI `skills install --from` 和 `skills list --host/--sandbox-id` 新增

### 2026-05-19 更新：选择性挂载策略

**问题**：沙箱全量 bind-mount `skills/` 目录后，`AgentSpec.skills` 字段沦为死代码——所有 Skill 文件均对 Agent 可见，spec 列表失去意义。

**决策**：`AgentSpec.skills` 驱动选择性挂载。沙箱 `.skills/` 只包含 spec 列出的 skill 子目录。多 Agent 共享同一 sandbox name 时取并集。

**实现**：
1. `compute_skill_mounts(skill_names)` 纯函数——skill 名 → `shared_folders` 条目（去重、定制 skills_dir）
2. CLI `_client()` 读取所有已注册 AgentSpec → 收集 `{sandbox_name: set(skills)}` → 并集 → 注入 `SandboxConfig.shared_folders`
3. `prompts.assemble(skills=...)` 按 `AgentSpec.skills` 过滤元数据指针——Agent 只看到自己技能的提示

**取舍**：
- 全量挂载（简单，Agent 可发现任何 Skill）→ 拒绝：违背最小权限原则，AgentSpec.skills 无意义
- 完全隔离（每 Agent 独立沙箱）→ 拒绝：增加沙箱实例数，bwrap 开销虽小但管理复杂
- **选择性挂载 + 并集共享** → 采纳：最小权限 + 资源复用 + AgentSpec 字段恢复意义

**后果**：
- `AgentSpec.skills` 从死字段恢复为访问控制字段
- 沙箱 `.skills/` 不再全量可见——Agent 只能 `ls` 到授权的 Skill
- 新增 `compute_skill_mounts()` 存于 `harness/skills/__init__.py`（与 `SkillMeta`、`list_host_skill_metas` 同模块）
- 新增测试：8 单元 + 4 集成 + 3 E2E
- SDK `_AgentsClient.register()` 修复：补调 `spec_svc.register(spec)` 确保程序化注册可被扫描
