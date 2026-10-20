# Bridge Studio v0.5 Native Alpha — 原生桌面应用

版本定位：**真实 Qt Widgets 原生 Linux 程序，不是网页、Electron 或嵌套 WebView。** 源码：`scripts/bridge_studio_native.py`。与现有 OAuth MCP、Codex VS Code 插件、SQLite 本地数据兼容；不声称已经得到 Ubuntu 实机发布认证。

## 第一次安装（Ubuntu 22.04 / GNOME）

首次只需运行一条安装脚本命令。脚本会安装 Qt 依赖并创建应用菜单图标；以后无需终端。

本机已经有原版 Bridge 的 `~/.config/codex-local-bridge/config.json`、本地 `.venv` 和仓库代码。推荐在独立的 v0.5 开发检出目录试用，不覆盖正在运行的旧版服务。

```bash
cd ~/codex-local-bridge-v04-beta
git pull --ff-only
bash scripts/install_native.sh
```

安装后按 Ubuntu Super 键搜索 **Bridge Studio**，点击图标进入原生窗口。可以固定在 Dock。之后使用不需要浏览器、无需每天输入终端命令；本机 UI 身份由当前 Ubuntu 登录用户确认，**没有重复输入 Studio Web 管理密钥**。

`[desktop]` 使用官方 Qt for Python PySide6，安装依赖只需首次运行。以后打开：Ubuntu 应用菜单 → Bridge Studio。开发者需要手动调试时可以运行 `.venv/bin/python scripts/bridge_studio_native.py`。

## 原生 UI 已有的交互

1. **总览**：项目、最近任务、记忆数量、本机 MCP 连接状态，快速进入工作页面。
2. **项目管理**：图形文件夹选择器新增只读项目，逐个选择权限档位 **关闭 / 只读 / 读写**，本机弹窗明确确认后应用。
3. **Codex 任务**：选择项目、输入任务、选择只读分析或编程测试并提交到 Bridge；刷新与查看经筛选的任务执行证据。
4. **长期记忆**：项目筛选、关键词搜索、创建、修改、归档，以及显示带来源提示的项目记忆交接。
5. **文件浏览**：授权根目录下的文件列表、UTF-8 内容预览、疑似凭据拒绝。
6. **连接诊断**：独立显示 MCP 和旧版 Web Studio 的本地监听状态，未知状态不伪装已连接。

原生 UI 由 Qt 控件绘制，不在 Chromium / Firefox 选项卡运行。为降低风险，UI 不提供任意 Shell 命令入口，也不在服务状态检查中打印 OAuth 凭证。

## 三档权限的实际含义

- **关闭**：`enabled=false`，重新加载后所有 Bridge `project()` 调用拒绝项目，MCP 项目列表隐藏。Studio 的授权配置仍保留，可以再开启。
- **只读**：`enabled=true`、`allowed_modes=['read']`，禁用所有写权限开关。
- **读写**：`enabled=true`、`allowed_modes=['read','write_test']`、启用本机项目授权。要求预先配置沙箱测试命令；自动应用仍受普通代码白名单、测试成功、路径隔离以及服务沙箱的 `ReadWritePaths` 限制。**即使在 UI 选择读写，也不意味着任意文件、任意命令可以无审核执行**。

项目权限管理器：`bridge/project_modes.py`。更改在本机执行、备份私有配置、进行本机授权、重启用户 Bridge 服务并验收运行情况。失败时尝试恢复原配置；不会从公网 MCP 远程无条件提升权限。对于 systemd 限制写入的项目，仍需本机管理员审核系统沙箱路径配置。

## 自动长期记忆（两个不同层级）

1. **Bridge 执行任务结束自动写入**：`bridge/auto_memory.py` 从本地任务数据库提取终态、引擎执行/测试/写回标记，写入 `<state_dir>/studio.sqlite3`。以 `task:<ID>` 去重；不复制原 prompt、源码、日志、密钥。
2. **新的 Bridge→Codex 委派任务读取记忆**：在 Codex App Server 运行前，检索本项目有限条数的已保存记忆，将其作为带有“非可信历史资料”标志的数据加入任务上下文，当前源码证据优先。
3. **ChatGPT 网页端读取同一数据库**：使用原有的 `search_project_memories` 和 `get_project_handoff` MCP 工具检索；也可以显式写入经过核对的项目决策。需要将包含新 MCP 工具的服务部署并重新发现工具才能调用。
4. **独立 VS Code Codex 会话**：如果 VS Code Codex 不通过 Bridge App Server 运行，当前版本**不能透明截获该独立会话**；其记忆不会自动写入 Bridge SQLite。可选择使用 Bridge Studio 的“Codex 任务”页面进行自动保存/恢复。不要把“Bridge 管理的 Codex 自动共享”误认为所有独立 Codex 会话自动共享。
5. 每个项目可通过配置 `auto_memory=false` 停用自动记录/注入。历史记录支持 Studio 归档；项目根目录身份绑定防止跨项目泄漏。

## 部署边界及下一步

- `main` 的已验证稳定版不受此 GitHub 开发分支代码修改影响；不会自动替换正式运行的 MCP。
- 仍需真实 Ubuntu 上检查桌面窗口布局、GNOME 单实例激活、权限切换后服务重启/项目访问和 Linux 权限系统沙箱边界。
- 本版本**不是**已打包和签名的 `.deb` 或 AppImage；首次安装仍需一次依赖安装和桌面入口注册。
- GitHub CI 的无 GUI / offscreen Qt smoke 可以验证窗口能启动，但不能替代人机交互与视觉 QA。
- 项目级权限选择与自动记忆会改变运行行为；应在隔离项目和配置备份上进行验收，确认后再正式部署。

见开发分支的 CI 与测试用例：`tests/test_project_modes.py`、`tests/test_auto_memory.py`、`tests/test_native_qt.py`。
\n## 安全升级与恢复\n\n新增桌面「升级与恢复」标签；参见 [备份、升级、回退](V05_UPGRADE_RECOVERY.md)。\n