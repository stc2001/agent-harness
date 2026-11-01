---
id: I-50
title: SOP 增补 — 同步 main 不得用 rebase force-push（会抹掉评审记录）
labels: [ready-for-agent]
author: admin
created: 2026-10-02
status: closed
---

## 背景 / What & Why

I-49 复评时发现：MR 记录里的**评审表是空的**——上一轮的 `changes-requested` 结论不见了。
原因不是作者删的，而是 **rebase + force-push 把评审提交从分支历史里丢弃了**。

实证（I-49）：

```
$ git cat-file -t c9a9754                    # 评审提交
commit                                       # 对象还在（dangling）
$ git merge-base --is-ancestor c9a9754 HEAD
→ NO                                         # 已不是分支祖先 = 评审痕迹被抹
```

这是**静默**的：rebase 后 `git log` 看着干净、MR 记录文件本身也没冲突，
评审历史就这么没了。而 SOP 把 `merge-requests/<ID>.md` 当作评审轮次的**持久留痕**
（§4.1 要求「评审人在评审表追加一行」）—— 每次 rebase 都会抹掉它，评审轮次越多丢得越多。
本次已从 dangling 对象逐字恢复，但**不能指望每次都恰好还能捞回来**（对象一旦被 GC 即永久丢失）。

## 验收标准 / Acceptance criteria

- [ ] **SOP §3 新增小节**（如 3.4「同步 main」）：明确要求在 MR 分支上同步 main 一律用
      `git merge main`，**禁止** `git rebase main` + `git push --force`；写明理由（评审提交会被丢弃）
- [ ] 给出**检测方法**：`git merge-base --is-ancestor <评审提交SHA> HEAD` 判定评审提交是否还在历史里
- [ ] 给出**恢复方法**：`git show <dangling-sha>:merge-requests/<ID>.md` 取回评审正文，并说明
      **必须在对象被 GC 前**恢复
- [ ] **§7 故障排查**新增一行：「MR 记录里评审表是空的 / 评审提交不在分支历史里」→ 指向上述小节
- [ ] §8 一页速查补一行「同步 main：`git merge main`（不要 rebase）」
- [ ] 引用实证：I-49 的 `c9a9754`（含事后恢复的做法）

## 不在范围内 / Out of scope

- **不改服务端钩子**（例如禁止 `mr/*` 的 non-fast-forward 推送）。那是**强制**方案，
  与本次「文档约定」是两条不同路线，应单独开 issue 评估（会同时禁掉合法 rebase，
  需要权衡），不在本次夹带。
- 不重写已合并的 I-49 历史（其评审记录已恢复，无需改写）

## 关联

- 指南：`docs/sop/soft-serve-issue-mr-workflow.md`（§3 / §7 / §8）
- 实证来源：I-49 复评（`merge-requests/I-49.md` 的「第二轮复评意见」节）
- 相关：I-49（已合并 `badc045`）
