# Security Policy

## 报告

请不要在公开渠道披露漏洞细节。本仓库当前仅部署在本地 soft-serve（无公开 issue
通道），请通过仓库管理员私下报告（soft-serve admin，当前维护者 @gexingdeyun），
并附上复现步骤与影响面。

## 支持范围

- 最新版本：0.1.0（main 上的 latest 快照）
- 第三方依赖问题：请注明包名与版本

## 凭据与密钥

- 绝不提交 credentials、token、key、.env 或会话密钥。
- 发现泄漏先轮换/吊销，再协调清理（历史改写需维护者决策）。
- 不要把密钥贴进 issue、MR、chat 或日志；用占位符。
- 提交前自检：bash .dsh/skills/secret-audit/scripts/secret_scan.sh
- 历史说明：本仓库公开快照不携带 git 历史，敏感历史仅保留在本地私有仓库。

## 权限与沙箱

- 沙箱相关变更必须保持 fail-closed；不要放宽 read_only/write 路径而不加测试。
- 修改 .soft-serve/、pre-receive 策略或 .githooks 属安全敏感变更，需单独评审。
