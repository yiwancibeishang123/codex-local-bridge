# Bridge Studio v0.4 Beta — Ubuntu 一键桌面版

**目标：第一次安装一次，以后在 Ubuntu 应用程序菜单点 Bridge Studio 图标就能打开。** 无需每次进终端、执行 Python 命令、重新登录 Codex 或启动 MCP。桌面入口仅管理 Studio UI，不会重新部署原有 OAuth MCP 服务。

## 一、首次安装（只需一次）

前提：你的 Ubuntu 上已经按原版要求配置过 Codex Local Bridge（存在私有 `~/.config/codex-local-bridge/config.json`），且用独立目录检出本仓库 v0.4 Beta。没有配置 Bridge 的新电脑，应先完成原版的一次性初始化。

```bash
cd ~/codex-local-bridge-v04-beta
git pull --ff-only
python3 scripts/studio_desktop.py install
```

安装器只在**当前用户**空间中写入应用图标、登录密钥辅助入口和可选 systemd **用户**服务，不使用 sudo，不更改现有 `codex-local-bridge.service`、OAuth、项目写入授权、Tailscale Funnel 或 SQLite 记忆。

安装时如果 Studio 尚未创建管理员密钥，会通过本机 Zenity 窗口显示一次。复制密钥，在随后自动打开的登录页粘贴即可。Ubuntu 未安装 Zenity 时，交互式终端会显示这一次密钥；以后也可通过应用菜单中的 **Bridge Studio · 查看登录密钥** 获取（Zenity 可用时）。

首次不想立即启动：`python3 scripts/studio_desktop.py install --no-launch`。先预览变化：`python3 scripts/studio_desktop.py install --dry-run`。如果不希望登录 Ubuntu 时启动后台：`python3 scripts/studio_desktop.py install --no-autostart`。

## 二、以后每天怎么用

打开 Ubuntu 应用程序（按 Super/Win 键，输入 **Bridge Studio**）→ 点击图标。

- 已在后台运行：直接打开浏览器工作台。
- 未运行：尝试启动用户 systemd 服务；不可用时按需启动独立本地进程，再打开浏览器。
- 网址始终是 `http://127.0.0.1:8768/`，后台网页 **只绑定 127.0.0.1**，不会发布到公网。
- 可在 Ubuntu Dock 中右键 **添加到收藏夹**，以后像普通软件一样点图标即可。
- 管理登录仍使用私有管理员密钥和会话限制；软件不会把密钥放进 URL、终端启动参数、GitHub 或 MCP 日志。
- 现有 ChatGPT 网页端 MCP、Codex 插件仍通过原服务运行；Studio 桌面按钮不会要求重连或重新 OAuth 认证。

如果你忘记 Studio 管理密钥，点击同一应用菜单中的 **Bridge Studio · 查看登录密钥**，在本机 Zenity 窗口查看。请勿把密钥发送到聊天、截图或远程仓库。

## 三、维护

| 需求 | 一次性操作 |
|---|---|
| 查看安装与连接状态 | `python3 scripts/studio_desktop.py doctor` |
| 暂停登录后自启动 | `systemctl --user disable --now bridge-studio.service` |
| 恢复自动启动 | `systemctl --user enable --now bridge-studio.service` |
| 卸载桌面程序入口 | `python3 scripts/studio_desktop.py uninstall` |
| 更新 Beta 代码 | 在相同检出目录 `git pull --ff-only`，重启 Studio 服务即可 |
| 没有图形密钥窗口 | 在 Ubuntu 本机安装/启用 Zenity，或临时用终端执行 `python3 scripts/studio_desktop.py key` |

卸载**不会删除** `studio.sqlite3`、原 `bridge.sqlite3`、配置、授权或 OAuth 凭据，也不会停止/卸载 `codex-local-bridge.service`。

## 四、安全与限制

- 桌面安装程序不会代替 Bridge 原始安装。原有 MCP 服务必须先部署配置完成。
- UI 会话认证仍保留，不会因桌面图标存在而自动获得管理员权限。
- systemd 自启动指的是**用户登录时自动启动**；未配置 Linux linger 的情况下，退出登录后用户服务可能停止，不代表机器关机仍能响应 ChatGPT。
- `127.0.0.1:8768` **不可以**通过 Tailscale Funnel、Clash 的反向代理或 Nginx 发布到公网。
- 仓库如果被移动到别的目录，需要从原检出位置先卸载桌面入口，再在新位置安装。
- 本版使用 Python 标准库和 Ubuntu 现有桌面程序（`xdg-open` / `gio`；Zenity 可选）；**尚未打包为 .deb 安装包**，没有承诺全新的 Ubuntu 主机可以无需一次性初始化直接使用。
- 不会声称 GitHub 自动化测试等同于真实 Ubuntu GNOME 菜单点击、登录、重启与全流程验收。正式稳定版发布前仍需用户机器上的界面和服务检查。
