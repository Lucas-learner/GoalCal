# 2026-11-17 新版归并记录

新版统一、数据保全、服务切换及旧实例退役已完成。主整合提交 `a352b03` 包含旧主仓库与新版独立仓库两个父提交。

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

## 退役归档

- 原迭代目录已移动至 `var/archive/retired-v2-iteration-20261117/workspace/`，保留独立历史、旧路径下的数据和环境，不再作为运行目录。
- V1 数据库、WAL/SHM、日志、备份、本机账号文件及旧生成配置位于 `var/archive/retired-v1-runtime-20261117/`。
- 两处均有 `ARCHIVED.txt`。不要按照归档中的绝对路径直接重启；恢复前先按当前手册评估数据与服务位置。
- 原 `local.goalcal.*` 用户自启配置已移除；三个 `local.goalcal-v2.*` 配置的工作目录均为主仓库。

## 验证与实际切换

- 切换时数据库 schema 8、integrity check、foreign key check 均通过；完整 SQL dump hash 完全一致，证据保存在快照目录 `database-transfer.json`。没有合并或覆盖为 V1 数据。
- 首次切换遇到 launchd 卸载延迟（bootstrap 错误 5），已恢复原 V2 服务；加入有界等待重试后成功切换，没有丢失新写入。
- 最终后端回归 718 项通过；前端单测 21 项、隔离库浏览器 E2E 8 项、离线规则评测 88/88 通过，前端类型检查和生产构建成功。
- 归档后再次确认生产健康检查正常；新生成备份 `var/production/backups/daily-20261117T123530942626Z.db` 的 integrity check 为 ok，foreign key check 无错误。
- 浏览器 E2E 覆盖：公开注册/邮箱登录/语言/反馈、目标草稿/排期/撤销、助手上下文、移动布局、ICS 导入、日历改期/取消/撤销、归档、自动续阶。
- 本机 `/_goalcal/health` 返回 200；真实浏览器打开正式域名登录页正常，直接打开公网健康页显示 `{"status":"ok","application":"goalcal-web"}`。
- 命令行经本机代理访问公网得到 403，绕过代理的直连超时；因此公网验收使用真实浏览器，不把命令行网络异常误记为应用故障，也不声称验证了所有外部网络。
- 8501、8502、8522、临时测试 8537 均关闭，8521 正常监听；app/tunnel 运行，backup 最近退出码为 0。

生产正式数据与密钥继续被 Git 忽略。README、生产手册、试用说明和 TODO 已切换为新版口径；Streamlit 兼容代码保留用于现有领域/安全/UI 回归，不再提供生产入口。
