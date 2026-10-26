# 当前公网入口：ngrok 免费固定网址

GoalCal 先使用 ngrok，域名购买与 Cloudflare 正式隧道延后。

公网地址：**https://disciplelike-preoccupiedly-kay.ngrok-free.dev**

用户第一次打开可能看到 ngrok 的提示页，点击 **Visit Site** 后即可看到 GoalCal 登录页。使用自己的 GoalCal 账号；主机所有者账号为 `owner`。其他试用账号仍需通过本机管理命令生成邀请码注册。

## 已安装的本机配置

- 独立生产数据库：`var/goalcal-production.db`；账号、目标和日历继续保存在本机。
- 登录网关监听 `127.0.0.1:8501`，Streamlit 监听 `127.0.0.1:8502`；公网隧道只指向网关，保持 XSRF 防护。
- 用户级后台任务：`local.goalcal.app`、`local.goalcal.tunnel`、`local.goalcal.backup`。Mac 用户登录后启动，应用和隧道异常退出后自动恢复。
- 隧道使用本机已有 ngrok 认证文件 `~/Library/Application Support/ngrok/ngrok.yml`，权限为 600；认证令牌未写入仓库、命令参数或此文档。
- 固定入口通过 `--url` 写入隧道服务参数，避免重启时换地址；不要重置 ngrok 账号分配的开发域名。
- 关闭 ngrok 请求检查（`--inspect=false`），不把日历和聊天正文保存在本机流量调试器。隧道的运行日志按原有规则轮转。
- 每日 03:15 在线备份，保留最近 14 天；接电系统睡眠已关闭。

## 已验证与还需实测

已验证 HTTPS 健康检查、Streamlit 页面响应、真实浏览器的 GoalCal 登录表单，以及隧道子进程被杀后自动恢复并保持同一网址。已有应用自动化测试 342 项通过。另一次运行检查曾遇到 ngrok 心跳超时和 HTTP 503，隧道随后自动重连并恢复；本次验证不能据此保证持续可用性。后续 5 次健康探测中 4 次成功；禁用本机 HTTP 代理后另做的一次公网直连检查也成功（约 1.73 秒）。仍需观察不同网络及更长时间的可用性。

**用户已反馈无梯子可以打开公网入口，但首次加载较慢。仍需继续验证登录后的聊天、日历交互、ICS 上传和断线重连。** 同一台 Mac 经公网网址访问成功，不等同于外部移动网络验收完成。公网浏览器自动化的密码输入被浏览器扩展浮层阻挡，未据此声称已完成人工登录流程；临时验证账号已撤销并清理。

## 免费额度

按当前 [ngrok 官方说明](https://ngrok.com/docs/pricing-limits/free-plan-limits)，免费端点没有运行超时，包含账号分配的固定开发域名和 HTTPS；每月外发流量 1 GB、HTTP 请求 20,000 次，初次访问有提示页。额度耗尽会影响访问，不能将它当作不限流量的长期生产保证。

可在 [ngrok Usage](https://dashboard.ngrok.com/usage) 查看额度。不要为了去掉提示页而关闭应用登录或 XSRF。持续观察 5–20 人试用的实际流量后，再决定使用自有域名与正式 Cloudflare 隧道。

## 管理命令

在项目目录运行：

```sh
export GOALCAL_ENV_FILE="$PWD/.env.production"
.venv/bin/python -m goalcal.admin invite
.venv/bin/python -m goalcal.admin backup
launchctl print "gui/$(id -u)/local.goalcal.tunnel"
```

重新生成当前隧道配置（不自动安装）：

```sh
.venv/bin/python deploy/generate.py --provider ngrok --ngrok-url https://disciplelike-preoccupiedly-kay.ngrok-free.dev
```

停止隧道：

```sh
launchctl bootout "gui/$(id -u)/local.goalcal.tunnel"
```

启动隧道（先确认旧任务已经卸载，紧接 bootout 的 bootstrap 可能需要等待数秒）：

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.goalcal.tunnel.plist"
```

本机停电、退出 macOS 用户、断网时服务仍会中断。恢复与数据库维护按 [生产运行手册](production.md) 执行。未来切换 Cloudflare 只替换公网隧道和网址，继续使用当前生产数据库。

邀请用户与日志反馈迭代详见 [试用说明](trial.md)。
