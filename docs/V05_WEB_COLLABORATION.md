# Bridge Studio v0.5 Alpha.5：项目安全测试配置与 ChatGPT 网页端协作

本版修复用户实机反馈：`airgym_yingjie` 项目原本没有测试命令，UI 点击“读写”时报错“写入权限需要先在配置中设定沙箱测试命令”，但软件没有对应配置入口。

## 无须编辑 JSON：直接在 Ubuntu Studio 设置项目测试

打开 **项目与权限** → 选中项目（如 `airgym_yingjie`） → 点击 **配置安全测试**。应用显示经过限制的选择项：

- **项目现有 unittest**：若项目根下包含 `tests/`、`test/` 或 `maintenance_tests/` 且其中有 `test_*.py`，可选择该测试套件。测试仍运行于原 Bridge 隔离环境，不允许任意 Shell 命令。
- **创建基础 Python 语法检查**：用于没有 unittest 测试的项目。明确确认后仅创建 `bridge_studio_tests/test_bridge_syntax_smoke.py`，在**项目根目录**静态解析（不执行）最多 64 个顶层 Python 源文件；不会更改算法或外部依赖。测试命令为固定的 `/usr/bin/python3 -m unittest discover -s bridge_studio_tests -v`，满足既有 Bridge 项目测试策略；更新私有项目配置、留下备份并重启后端。失败时回滚配置和新建的检测文件。**它只验证最低级别的语法和检查器存在，不证明 AirGym PPO、FAST-LIVO2 或 ROS 功能正常。**
  
测试设置完成后再次选择 **读写 → 应用所选项目权限**。本机 Bridge 会执行原有批准与 systemd 项目写入白名单设置。敏感代码、危险操作仍需要独立确认。对真实研究代码的写回仍应该额外运行项目正式测试。

## ChatGPT 网页端到底有没有真实读写 Ubuntu 文件？

新增左侧 **网页端协作** 页面。可选项目，查看当前只读/写入授权情况、项目沙箱测试，以及最近的文件写入提案和 Codex 执行/测试/写回记录。

点击 **复制网页端验收指令**，粘贴到**同一个有 Bridge 插件权限的 ChatGPT 网页对话**。指令要求：

1. ChatGPT 调用 `list_projects` 和 `list_project_files`。
2. 使用 `read_project_file` 读取授权文件，显示真实 SHA-256。
3. **仅当项目已授权读写**，在项目根目录使用 `request_write_file` 创建随机命名的 `bridge_web_probe_<随机>.py` 无害测试文件，期望旧 SHA 为 `ABSENT`；再用 `read_project_file` 和 `get_write_status` 验证新 SHA 与应用记录。原生产文件不修改。
4. 返回桌面软件点击 **刷新执行记录**，核对提案状态与路径。显示的本地记录不自动等于“ChatGPT OAuth 测试通过”；必须与此次 ChatGPT 实际 MCP 返回结果配对。

**直接验证的先例（2026-10-20）**：本对话已通过现有 `ChatGPT_Codex_MCP` 在 `bridge_maintenance` 本地隔离工作区直接创建/读取/编辑/重新读取 `maintenance_tests/bridge_web_access_alpha5.py`，返回 `applied` 和变化的 SHA。另成功只读获取 `airgym_yingjie/mid360s_mapping_dataset_20261014_205253/mid360s_lidar_only.yaml`。**没有修改 AirGym 文件。**

## 看懂协作实际效果

- **网页端协作**：显示写入提案是否已 `applied`、授权状况和真实历史记录。
- **Codex 任务详情**：以普通中文状态展示任务是否已完成、测试通过、文件是否实际写回、涉及文件、沙箱隔离情况。不会以“任务已完成”混淆“测试通过”和“写回成功”。
- **升级与恢复**：以状态小节显示当前后端、可恢复备份和数据库存储位置，不再将 `{"unit":...}` 之类的原始 JSON 当用户界面。

## 一条清晰的日常使用链路

**从 Ubuntu 应用菜单打开 → 连接与诊断检查 Bridge/Tailscale/Codex → 添加项目 → 配置安全测试 → 设置只读/读写 → 网页端协作页面复制验收指令到 ChatGPT → 确认实际读写记录 → 通过 Codex 任务执行工作 → 长期共享记忆 → 升级与恢复维护。**

记忆仍按原来的隐私界限自动保存 Bridge 管理任务的*真实执行状态*；独立 VS Code Codex 需要加载并调用自己的本地记忆 MCP，不会被暗中记录全部会话。

## 安装与版本

v0.5 Alpha.5 是**桌面客户端源码和后端候选版本**，发布分支 `feat/bridge-studio-v0.4`，不自动替换生产后端。GitHub CI 构建 Ubuntu 22.04 amd64 的 `.deb`，但旧服务若未升级到与本机 Qt 匹配的版本，新功能可能只写入候选配置而不被旧服务理解。**先通过 GUI 检查当前后端并做完整数据快照，再在临时项目上测试读写授权。**

当前 Bridge MCP 已批准的项目列表不包含 `/home/mtrya/A-mcp` 的直接写入授权，只有受控的 `bridge_maintenance` 隔离副本。GitHub 的修改不能被误称为已经同步到该实际本地文件夹；若希望写入，请从 GUI 把该目录作为明确的项目添加，并选择合适权限，然后在新会话通过 MCP 操作，或在本机 Codex 中主动 `git pull` 选定的开发分支（生产环境需先备份）。
