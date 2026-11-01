# SOP：在 soft-serve 上发起 issue 与 merge 请求

> 面向在 `OH_workspace` 上协作的会话/agent。读完这一篇即可完成
> 「发起 issue → 开 MR → 等评审 → 合并」全流程，无需再猜服务端能力。
>
> 服务端：Soft Serve v0.12.2，仓库 `OH_workspace`，地址 `ssh://git@localhost:23231/OH_workspace.git`

---

## 0. 先明确：soft-serve 没有 issue 和 PR

这一点必须先说清，否则会浪费时间找不存在的功能。已实测核实：

- 二进制内不存在 issue / PR / merge request 相关实现（`strings` 检索无匹配）
- SSH 命令面只有 `repo`（`create`/`list`/`info`/`tree`/`blob`/`branch`/`collab`/`webhook`…）、
  `settings`、`pubkey`、`token`、`user`，**没有** issue/PR 子命令
- 没有 Web UI（HTTP 端口只提供 git-over-HTTP 与 LFS，`/` 返回 404）

所以本流程是 **在 git 协议之上自建的一层约定**：issue 与 MR 都是**仓库内的 markdown 文件**，
MR 的「载体」是**分支**，而「强制力」来自服务端的 **pre-receive 钩子**。
这不是权宜之计的伪流程——不合规的推送会被服务端真的拒绝。

---

## 1. 环境准备（新会话第一件事）

### 1.1 确认服务端在跑

```bash
cd /home/gexingdeyun/OH_workspace
ss -ltn | grep -E '23231|23232'   # 应有 127.0.0.1:23231 与 127.0.0.1:23232
```

端口没起来就启动它（它是后台常驻进程，不要重复启动多个实例）：

```bash
./.soft-serve/start.sh
```

### 1.2 确认 remote 与 SSH 命令已配好

```bash
git remote -v
# origin  ssh://git@localhost:23231/OH_workspace.git (fetch/push)

git config core.sshCommand
```

`core.sshCommand` 必须含 `-F /dev/null`。**原因**：本机 `/etc/ssh/ssh_config.d/` 与
`/etc/ssh/ssh_config` 属主是 `nobody:nobody`（系统既有问题，与 soft-serve 无关），
OpenSSH 会直接报 `Bad owner or permissions` 并拒绝启动。`-F /dev/null` 绕过它。

如果 `core.sshCommand` 为空（例如换了克隆目录），重新设上：

```bash
WS=/home/gexingdeyun/OH_workspace
git config core.sshCommand "ssh -F /dev/null \
  -i $WS/.soft-serve/keys/admin_ed25519 \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=$WS/.soft-serve/keys/known_hosts"
```

### 1.3 起一个简写（后文所有 `soft` 命令都用它）

```bash
WS=/home/gexingdeyun/OH_workspace
soft() { ssh -F /dev/null -i $WS/.soft-serve/keys/admin_ed25519 \
  -o IdentitiesOnly=yes -o UserKnownHostsFile=$WS/.soft-serve/keys/known_hosts \
  -p 23231 git@localhost "$@"; }

soft repo list          # 冒烟测试：应输出 OH_workspace
```

---

## 2. 发起 issue

### 2.1 约定

| 项 | 规定 |
|---|---|
| 路径 | `issues/<ID>-<slug>.md` |
| ID | 沿用项目既有 `I-NN` 编号，**取当前最大值 +1**（历史见 `issues/python-harness-issues.md` / `docs/archive/solved-issues-through-I-43.md`） |
| slug | kebab-case，与分支名一致 |
| 模板 | 复制 `issues/TEMPLATE.md` |
| 标签 | 只用 `docs/agents/triage-labels.md` 里的六个，不自造 |
| 分支 | issue 本身**不需要**分支，直接推 `main`（见 §5 策略第 4 条） |

查当前最大 ID：

```bash
ls issues/ | grep -oP 'I-\d+' | sort -t- -k2 -n | tail -1
```

### 2.2 写并提交

```bash
cd /home/gexingdeyun/OH_workspace
ID=I-45; SLUG=your-feature-slug
cp issues/TEMPLATE.md "issues/$ID-$SLUG.md"
$EDITOR "issues/$ID-$SLUG.md"        # 填 id/title/labels/created/status 与验收标准

git add "issues/$ID-$SLUG.md"
git commit -m "docs(issues): $ID 发起 — <一句话标题>"
git push origin main
```

**这一步允许直接推 main**：改动只落在 `issues/**`，属 tracker 记账。
若推送被拒，见 §7。

> 验收标准要能判 True/False。写「命令 + 期望输出」或「测试名」，不要写「优化」「完善」。

---

## 3. 提交 merge 请求

一个 MR = **一条分支** + **一个记录文件**。

### 3.1 开分支写代码

```bash
git switch main && git pull --ff-only origin main
git switch -c "mr/$ID-$SLUG"

# ...改代码...

git add -A
git commit -m "feat(<scope>): $ID <做了什么>"
```

分支名必须是 `mr/<ID>-<slug>`，`<ID>` 与 issue 相同——这是评审方定位 MR 的依据。

### 3.2 写 MR 记录

```bash
cp merge-requests/TEMPLATE.md "merge-requests/$ID.md"
$EDITOR "merge-requests/$ID.md"
```

必填 front matter（`head` 用当前提交短 SHA）：

```bash
echo "head=$(git rev-parse --short HEAD)"
```

### 3.3 推送分支

```bash
git commit -am "docs(mr): $ID 开启 merge 请求"
git push -u origin "mr/$ID-$SLUG"
```

推 `mr/*` 分支**总是允许**的。此时 MR 已「开启」，等评审。

### 3.4 同步 main：用 merge，**不要** rebase force-push

MR 开着的时候 main 往往会前进（别的 MR 合了），需要同步。**必须用 merge**：

```bash
git switch "mr/$ID-$SLUG"
git fetch origin
git merge main                 # ✅ 正确
git push origin "mr/$ID-$SLUG" # 普通推送，历史里保留评审提交
```

**禁止** `git rebase main` + `git push --force`：

```bash
git rebase main && git push --force origin "mr/$ID-$SLUG"   # ❌ 会抹掉评审记录
```

**为什么**：rebase 会**重写**分支上的提交，评审方推上来的评审提交（`docs(mr): <ID> 评审结论 …`）
不在重放范围内，于是**被静默丢弃** —— `git log` 看着干净、`merge-requests/<ID>.md` 也没冲突，
但 SOP §4.1 要求的评审留痕（评审表那几行）就没了。评审轮次越多，丢得越多。

这不是假设，是实测：I-49 的评审提交 `c9a9754` 在 rebase 后已**不是分支祖先**（对象仅为 dangling）：

```bash
git merge-base --is-ancestor c9a9754 HEAD   # 评审提交还在分支历史里吗？
echo $?                                     # 非 0 = 已被抹掉
```

**万一已被抹掉，尽快恢复**（对象一旦被 GC 就永久丢失）：

```bash
git cat-file -t <评审提交SHA>                   # 先确认对象还在（应输出 commit）
git show <评审提交SHA>:merge-requests/<ID>.md   # 取出含评审正文的完整版本
# 把评审小节贴回当前 merge-requests/<ID>.md，并在评审表补回缺失轮次
```

若确实必须 rebase（例如分支历史污染），**rebase 后必须**先确认评审提交是否还在历史里，
不在就按上述方法把评审正文贴回，**再** force-push。

---

## 4. 评审方：不克隆也能读 MR

评审方（人或另一个会话）无需 `git clone`，直接用 SSH 读服务端内容。
`repo tree` / `repo blob` 都接受 `REFERENCE` 参数，所以可以直接看分支：

```bash
BR=mr/I-44-soft-serve-issue-mr-workflow

soft repo branch list OH_workspace                  # 确认分支已存在
soft repo tree OH_workspace "$BR"                   # 分支根目录树
soft repo tree OH_workspace "$BR" docs/             # 子目录
soft repo blob OH_workspace "$BR" "merge-requests/I-44.md"   # 读 MR 记录
soft repo blob OH_workspace "$BR" README.md -l      # 带行号
```

需要看 diff 时用本地 fetch（只取对象，不动工作区）：

```bash
git fetch origin "$BR:refs/remotes/origin/$BR"
git diff --stat origin/main..."origin/$BR"
git log --oneline origin/main.."origin/$BR"
```

### 4.1 写评审结论

评审人在 `merge-requests/<ID>.md` 的评审表追加一行，并把 `status` 改为
`approved` / `changes-requested` / `rejected`，然后推回**该 MR 分支**：

```bash
git switch "$BR"
# 改 merge-requests/<ID>.md
git commit -am "docs(mr): $ID 评审结论 — approved"
git push origin "$BR"
```

---

## 5. 服务端强制策略（别试图绕过，会被拒）

钩子：`.soft-serve/data/hooks/pre-receive`（全局钩子，对每个仓库生效）。

> **源码在仓库里**：`scripts/soft-serve/pre-receive` 是策略的**单一真源**（`.soft-serve/` 整体被
> `.gitignore` 排除，所以部署位置不入库）。修改策略请改仓库里的那份，然后重新安装：
>
> ```bash
> ./scripts/soft-serve/install-hook.sh            # 部署/更新
> ./scripts/soft-serve/install-hook.sh --uninstall # 停用（等价于 break-glass）
> ```

对受保护分支 `main`，判定规则如下：

**放行条件**：沿 **first-parent 链** 从 old 到 new 的**每一个提交**，都必须是下列之一：

1. **合并提交**（≥2 个 parent）——评审通过的 MR 这样落地
2. **tracker-only 提交**——只改 `issues/**` 或 `merge-requests/**`

链上任一提交不满足 → 整次推送被拒。

| 推送内容 | 结果 |
|---|---|
| 删除 `main` | ❌ 拒绝 |
| 首次创建 `main` | ✅ 允许 |
| 非快进（force）推送 | ❌ 拒绝（已发布历史不可改写） |
| 纯合并提交 | ✅ 允许 |
| 合并提交 + tracker 收尾提交（一次推送） | ✅ 允许 |
| 只改 `issues/**` / `merge-requests/**` | ✅ 允许 |
| 合并后又在 main 上直接提交代码 | ❌ **拒绝**（被拒提交会被逐条列出） |
| 直接推代码/文档到 main | ❌ **拒绝** |

**为什么按 first-parent 链判定**：经由合并进来的 MR 分支提交**不在** main 的 first-parent 链上，
所以一个 MR 只由它的合并提交代表——这意味着「合并 + 关闭 issue」可以在**一次推送**里完成，
而合并之后偷偷追加的代码提交无法藏身（已实测：合并 + 代码提交组合推送被拒，且只有该提交被点名）。

其他分支（`mr/*` 等）照常可推。设 `SOFT_SERVE_POLICY_STRICT=1` 可额外拒绝非 `mr/*` 的分支名。

被拒时服务端会回显**被拒提交清单**与**违反策略的文件**，照提示做即可。

### 5.1 合并 MR（评审通过后）

```bash
git switch main && git pull --ff-only origin main
git merge --no-ff "mr/$ID-$SLUG" -m "merge: $ID <标题> (MR 评审通过)"

# 顺手收尾（tracker-only，与合并一起推，允许）
#   merge-requests/<ID>.md : status -> merged
#   issues/<ID>-*.md       : status -> closed
git commit -am "docs(issues): $ID 关闭（MR 已合并）"

git push origin main          # ✅ 合并提交 + tracker 提交，一次推送
```

**必须 `--no-ff`**：快进合并产生的是普通提交（1 个 parent），不含合并提交，
若该提交又改了 tracker 之外的文件就会被拒。`--no-ff` 生成的合并提交正是策略允许的形态，
同时保证「每个 MR 在 main 上留下可追溯的合并点」。

合并后删掉已合并的分支（本地 + 服务端）：

```bash
git branch -d "mr/$ID-$SLUG"
git push origin --delete "mr/$ID-$SLUG"
```

### 5.2 审计日志

钩子会为**受保护分支（`main`）的每一次推送决策**落盘一条记录：

```bash
tail -20 .soft-serve/data/log/push-audit.log
# 时间  user=admin  repo=...  ref=refs/heads/main  old=... new=...  ALLOW/DENY  原因
```

**范围仅限 `main`**：推 `mr/*` 等分支**不写审计日志**——钩子对非受保护分支直接放行返回，
唯一例外是 `SOFT_SERVE_POLICY_STRICT=1` 且分支名不合规时记一条 DENY。
所以别指望用日志证明「谁在什么时候推了哪条 MR 分支」。

自证（这条命令验证的正是「不记」这个事实本身）：

```bash
before=$(wc -l < .soft-serve/data/log/push-audit.log)
git push origin "mr/<ID>-<slug>"        # 推送成功
echo "新增记录数：$(( $(wc -l < .soft-serve/data/log/push-audit.log) - before ))"   # 期望 0
```

### 5.3 紧急放行（break-glass）

策略只能从**文件系统**解除，不能从 git 客户端绕过：

```bash
./scripts/soft-serve/install-hook.sh --uninstall   # 停用
# ...推送...
./scripts/soft-serve/install-hook.sh               # 恢复
```

等价的手工操作：把 `.soft-serve/data/hooks/pre-receive` 改名加 `.disabled` 后缀即可。

用完请恢复。这是刻意设计：git 客户端无法自我授权绕过评审。

> **已实测（在一次性仓库上验证，未触碰 OH_workspace）**：
> 策略启用 → 直推代码被拒；改名为 `.disabled` → 同一提交放行；恢复后 → 再次被拒。
> 另已确认：留一个多余的 `pre-receive.disabled` **不会**削弱策略（只有名字精确为
> `pre-receive` 的钩子会被执行）。`install-hook.sh` 会自动清掉这个残留。

---

## 6. 完整示例（I-44 实况）

```bash
cd /home/gexingdeyun/OH_workspace
ID=I-44; SLUG=soft-serve-issue-mr-workflow

# 1) 发起 issue —— 直接推 main
cp issues/TEMPLATE.md "issues/$ID-$SLUG.md" && $EDITOR "issues/$ID-$SLUG.md"
git add "issues/$ID-$SLUG.md"
git commit -m "docs(issues): $ID 发起 — soft-serve 上建立 issue → merge 请求工作流"
git push origin main                          # ✅ 允许（tracker-only）

# 2) 开 MR 分支干活
git switch -c "mr/$ID-$SLUG"
# ...改代码 + 写 merge-requests/$ID.md...
git push -u origin "mr/$ID-$SLUG"             # ✅ 允许（mr/*）

# 3) 评审方无克隆读取
soft repo tree OH_workspace "mr/$ID-$SLUG"
soft repo blob OH_workspace "mr/$ID-$SLUG" "merge-requests/$ID.md"

# 4) 合并
git switch main && git merge --no-ff "mr/$ID-$SLUG" -m "merge: $ID ..."
git push origin main                          # ✅ 允许（merge commit）
```

---

## 7. 故障排查

| 现象 | 原因与处理 |
|---|---|
| `Bad owner or permissions on /etc/ssh/ssh_config.d/...` | 系统 ssh 配置属主异常。确保命令含 `-F /dev/null`（§1.2） |
| `pre-receive hook declined`，提示「拒绝直接推送 main」 | 正常拦截。按回显清单改走 `mr/*` 分支（§3），合并时用 `--no-ff` |
| `Connection refused` on 23231 | 服务端没跑。`./.soft-serve/start.sh` |
| `repository not found` | 仓库名拼错；`soft repo list` 确认。仓库不存在时也可直接 push 自动创建 |
| 合并后推 main 仍被拒 | 三种可能：①用了快进合并 → 改 `git merge --no-ff`；②合并后在 main 上又提交了 tracker 之外的文件 → 把该改动挪进 MR 分支；③想改写历史（force）→ 不允许 |
| 回显「被拒的提交」里出现我没写的提交 | 说明该提交在 main 的 first-parent 链上（即直推的），不是经 MR 合并进来的 |
| **MR 记录里评审表是空的 / 评审提交不在分支历史里** | 分支被 rebase + force-push 过，评审提交被静默丢弃。按 §3.4 检测（`git merge-base --is-ancestor`）并从 dangling 对象恢复（`git show <sha>:merge-requests/<ID>.md`）——**要快**，对象 GC 后就永久丢失 |
| `repo create` 报 `accepts 1 arg(s), received N` | soft-serve CLI 要求每个 flag 整体引号包成一个参数：`repo create x '-d "desc"'` |
| 想确认某项改动会被允许 | 先在 `mr/*` 分支推；main 的策略只在推 main 时生效 |

---

## 8. 一页速查

```bash
# issue（直接进 main）
cp issues/TEMPLATE.md issues/I-NN-slug.md && $EDITOR $_ && git add $_ \
  && git commit -m "docs(issues): I-NN 发起 — ..." && git push origin main

# MR
git switch -c mr/I-NN-slug && git commit -am "feat: I-NN ..." \
  && cp merge-requests/TEMPLATE.md merge-requests/I-NN.md && $EDITOR $_ \
  && git commit -am "docs(mr): I-NN 开启 merge 请求" \
  && git push -u origin mr/I-NN-slug

# 评审（无克隆）
soft repo blob OH_workspace mr/I-NN-slug merge-requests/I-NN.md

# 同步 main（开了 MR 后 main 前进时）—— 用 merge，不要 rebase
git fetch origin && git merge main && git push origin mr/I-NN-slug

# 合并（必须 --no-ff）
git switch main && git merge --no-ff mr/I-NN-slug && git push origin main
```

| 想做的事 | 命令 |
|---|---|
| 列仓库 | `soft repo list` |
| 看分支 | `soft repo branch list OH_workspace` |
| 看目录树 | `soft repo tree OH_workspace <ref> <path>` |
| 看文件 | `soft repo blob OH_workspace <ref> <path>` |
| 仓库信息 | `soft repo info OH_workspace` |
| 审计日志 | `tail .soft-serve/data/log/push-audit.log` |
