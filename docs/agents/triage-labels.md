# Triage Labels

All labels use the default canonical names. No custom overrides.

| Label | Role | Meaning |
|-------|------|---------|
| `needs-triage` | 待评估 | Maintainer needs to evaluate this issue |
| `needs-info` | 等待信息 | Waiting on the reporter for more details |
| `ready-for-agent` | Agent 可领取 | Fully specified — an AFK agent can pick it up with no human context |
| `ready-for-human` | 需人工 | Needs human implementation (judgment calls, external access, design decisions) |
| `wontfix` | 不修复 | Will not be actioned |

## State machine

```
[unlabeled] → needs-triage ──→ needs-info ──→ needs-triage (reporter replied)
                           ├──→ ready-for-agent
                           ├──→ ready-for-human
                           └──→ wontfix
```
