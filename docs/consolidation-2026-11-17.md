# 2026-11-17 新版归并记录

本文记录已授权的新版统一、数据保全和服务切换；最终验证结果在实施结束时补充。

## 源码与历史

- 旧主仓库含未提交修改，已保存为 `4c4d931`，另有源码压缩包、暂存/未暂存补丁和 Git bundle。
- 新版历史保留在 `archive/v2-before-consolidation` 分支（`83ac3a6`），通过双亲合并接入主仓库。
- 正式源码统一在 `/Users/macmini/projects/GoalCal`，不再依赖被忽略的迭代目录。
- 旧 Streamlit 页面及相关模块保留用于兼容回归，默认生产 service 只启动 Web 网关，旧路由关闭。

## 数据与回退

- 快照目录：`var/archive/pre-consolidation-20261117-163113/`，包含两边源码、Git bundle、环境和服务配置、SQLite 在线备份、SHA-256 清单；目录与凭据不提交 Git。
- 保留新版全部数据并复制到 `var/production/goalcal.db`，不合并旧用户数据库。
- 转移时先停止旧位置的 V2 应用/定时备份，使用 SQLite backup API；逐库校验 schema、integrity、foreign keys 和完整 SQL dump hash。
- 新实例继续使用 `local.goalcal-v2.*` 和 8521；仅改变代码与数据位置。Cloudflare 域名和 tunnel 标识保持原值。
- 归并前 V2 程序支持同一 schema 8；回滚前先停写并备份现有新库，优先以兼容程序读取最新数据。不得恢复归并前旧库而丢弃新写入。
- V1 schema 6 仅作历史存档，不作为新版程序回滚目标。

## 工程更新

修复 pytest 收集范围、开发代理、环境隔离、默认启动、健康检查、部署路径、预览工具以及浏览器测试；同步 README、生产/试用手册、架构、契约、设计和 TODO。旧资料及旧架构 PDF 均标为历史。
