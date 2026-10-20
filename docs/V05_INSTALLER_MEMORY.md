# Bridge Studio v0.5 · 原生 Ubuntu 软件安装与共享记忆

本轮提供 Qt 原生桌面程序的 Debian 安装包、更加严格的项目权限与独立 Codex 的本地记忆接口。仍属于 Beta/Alpha 开发分支，不代替原有 MCP 服务。

## 1. 下载并安装 .deb

打开 [Build Ubuntu Desktop Package](https://github.com/yiwancibeishang123/codex-local-bridge/actions/workflows/deb.yml)，选择最近一次成功构建，下载名为 BridgeStudio-Ubuntu22.04-amd64-deb 的附件，解压获得 bridge-studio-native_0.5.0~alpha2_amd64.deb。

用 Ubuntu 软件安装器打开文件，点击安装；以后按 Super 键搜索 Bridge Studio，点击图标。安装包是纯桌面前端，文件位于 /opt/bridge-studio/，不会自动替换正在运行的 Codex Bridge，也不会更改 OAuth、用户权限和 SQLite 记忆。

本机仍需先部署和登录原 MCP 服务。该包无需启动浏览器，不要求额外付费 API。第一次图形验收建议在测试账号或备份后完成。

## 2. 三档项目权限

关闭：远程 MCP 不再列出或授权访问该项目。只读：只能查看和分析文件。读写：仅对具备沙箱测试命令且经过本机确认的项目，允许普通文件自动写回，敏感文件和危险命令仍需额外审批。

系统将项目根目录写入私有、受管理的 systemd ReadWritePaths drop-in（只限经过授权的具体项目，不扩大到整个 home）；降级为只读或关闭时撤销写权限并移除对应目录白名单；变更前备份配置，重启失败则回滚配置和受管理 drop-in。详细见 bridge/project_modes.py 和 bridge/project_sandbox.py。

注意：如果机器有其他管理员自行配置的 systemd 授权，本模块不会编辑这些授权；务必在 Ubuntu 临时项目上做实机读写和撤销测试。

## 3. 独立 VS Code Codex 记忆连接

Bridge 管理的 Codex 任务继续自动记录执行结果。独立 Codex 会话可选接入一个本地 stdio MCP 服务器：

    cd ~/codex-local-bridge-v04-beta
    .venv/bin/python -m bridge.codex_memory_stdio --print-config

检查输出的 [mcp_servers.bridge_memory] 配置段后，手动合并到 Codex 的 MCP 配置，并重新载入 Codex。它提供搜索项目记忆、显式保存项目记忆、项目交接和实际任务回执四种工具，与 ChatGPT MCP 共用原本的本机 studio.sqlite3 数据库。

新增原生软件中的「连接 VS Code Codex 记忆」按钮：程序先自动识别现有 Bridge 的 Python 虚拟环境；需要时可手动选择一次后端解释器。用户在本机确认后，调用官方 Codex CLI 的 mcp add 命令进行注册，检测已有同名 MCP 时拒绝覆盖，注册前私有备份已有 Codex 设置。无需自己编辑 TOML；完成后需重新载入 VS Code Codex。\n\n安全边界：只有 Codex 客户端确实支持并加载该 stdio MCP，这些工具才可用；独立 VS Code Codex 的每个聊天仍需要明确调用它们。不能宣称能够自动捕获所有独立会话，也不读取未授权项目。可以在项目内的 AGENTS.md 约定开工读取记忆、收工保存经过验证的决策。记忆仅为历史数据，不应提升为命令或授权。

## 4. 构建与验收

Build Ubuntu Desktop Package GitHub Actions 工作流会用 PyInstaller 打包 PySide6，封装成 .deb，在 CI 检查元数据和安装脚本，并实际从解包目录运行冻结 Qt 二进制的 offscreen smoke test；成功后上传构建附件。

这个打包文件不含后安装脚本，不自动删除用户数据或变更服务。源码编译与 GitHub CI 通过不等于在你的 Ubuntu GNOME 上完成双击安装、窗口交互和权限切换的实机验收。

开发文件：scripts/build_deb.py、bridge/project_sandbox.py、bridge/codex_memory_stdio.py；测试：tests/test_deb_packaging.py、tests/test_project_sandbox.py、tests/test_codex_memory_stdio.py。
