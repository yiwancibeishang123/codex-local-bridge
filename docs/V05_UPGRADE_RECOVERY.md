# Bridge Studio v0.5 Alpha.3 · Ubuntu 图形化升级与恢复

这版在原生 Qt 桌面程序新增 **「升级与恢复」**，目标是日常完全不用终端启动，版本切换也尽量通过 GUI 完成，但**不会偷偷运行 pip、下载第三方代码、绕过审批或重置你的 Codex 账号授权**。

## 你在界面中能做什么

- **检查当前后端**：读取本机用户 systemd 中受到管理的 MCP 服务，并展示当前工作目录。只读取元数据，不显示 OAuth Token、API Key 或完整聊天日志。
- **立即备份**：把 `config.json`、Bridge 与 Studio 的 SQLite（使用 SQLite 自带在线 Backup API，包括未 checkpoint 的 WAL）、用户级 systemd 服务定义保存到私有目录 `<state_dir>/studio-snapshots/<ID>/`。目录 0700，文件 0600，SHA256 清单可核验，备份内容不会上传 GitHub。
- **验证备份**：重新校验每个备份文件的摘要、大小、SQLite 完整性；校验失败则拒绝恢复。
- **选择新版后端**：你先把想要的版本放在 Ubuntu 主目录下的**独立本地 Git 检出**，并预装该版本的 `.venv`，在界面选择这个文件夹。升级器要求其来自 `yiwancibeishang123/codex-local-bridge` 的受信任 Git origin、无未提交的跟踪文件改动、虚拟环境可以加载本地 Bridge、版本号吻合。
- **本机确认后升级**：拒绝在有待处理任务时升级；自动创建可核验快照，再只修改**受管理的用户级 systemd 服务**的 ExecStart/WorkingDirectory，daemon-reload、restart、检查服务活动状态和本地端口。如果失败，会尝试将旧服务和数据库恢复至升级前。
- **恢复所选备份**：需再次在 Qt 本机确认。恢复之前还会保存一个紧急快照；随后停止服务、恢复私有配置和两个 SQLite 库（使用 SQLite API）、旧服务定义，再重启验证。
- **打开桌面 .deb**：选择本地安装包，仅检查包名、版本和 amd64 架构；显式确认后交由 Ubuntu 软件安装器安装。**包名检查不等于密码学签名校验**，必须自行信任下载来源。

## 极重要的升级约束

这不是自动升级所有组件。桌面 .deb 与 Bridge MCP 后端是两个不同的部署对象。此版本不会自行执行 Git pull、pip 安装或运行未知远端代码；**候选后端的安装和测试仍需要提前完成**。这是为了避免损坏你已经成功运行的 Tailscale Funnel、ChatGPT OAuth、Codex 登录和科研项目。完整后端一键下载/更新只有在签名、回滚和兼容性机制经过实机审核后才适合默认启用。

候选版本必须是当前 Ubuntu 用户主目录下的具体文件夹，包含 `pyproject.toml`、`bridge/server.py` 和 `.venv/bin/codex-local-bridge`。如果你的 systemd 服务不是本仓库脚本生成的样式，升级器**会拒绝修改它**，而不是冒险改写未知服务。

升级后以 `systemctl --user is-active` 和 loopback 监听作为后端运行检查；这**不等同于 ChatGPT OAuth/15 个 MCP 工具的真实端到端验收**。生产更新还需要在 ChatGPT 对话里检查项目列表与记忆工具，必要时重新发现连接器；也要验证 Tailscale 的 HTTPS 隧道是否仍可连。

## 恢复能做到什么、不能做到什么

备份及恢复范围：Bridge 本地私有 config、bridge.sqlite3、studio.sqlite3、当前用户生成的 systemd MCP 服务文件。**不复制或覆盖用户项目仓库、不备份或修改其它应用账户文件、不保证恢复 Codex 外部目录中的所有会话**。请仍按你原来的方法独立备份项目源码、Git 分支和 `CODEX_HOME`。

回滚前要结束运行中的任务，否则恢复旧数据库后可能丢失任务状态。界面有任务非终态检测。日志中仅展示操作的非敏感 ID、状态与版本，备份里的 config / SQLite 包含本机私密内容，**不要上传到 GitHub**。

为了避免用户误以为升级完成，测试中会检查断电后未完成状态、备份篡改、路径穿越、服务不受管理、失败重启及回退。Alpha 阶段无自动定时备份、无签名的应用自动更新、无受支持的跨主版本 SQLite schema 迁移。

## 首次与后续使用

升级包及后端的下载仍来自仓库 [GitHub Actions 的 .deb 构建工作流](https://github.com/yiwancibeishang123/codex-local-bridge/actions/workflows/deb.yml)。安装后在 Ubuntu 应用菜单打开 Bridge Studio，点击左侧「升级与恢复」。无需打开网页管理后台，也不需要每天执行终端指令。

目前仍是开发版。真实 Ubuntu GNOME 实机安装、服务升级、备份恢复和自动回退必须单独验收；未经过这些检查前请不要直接对生产环境点“升级后端”。
