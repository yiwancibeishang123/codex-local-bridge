# Codex Bridge Studio v0.4-beta · 本地试用说明

> GitHub 开发分支 `feat/bridge-studio-v0.4` 已包含可试用 Beta 代码；旧的 `bridge_maintenance` 副本用于隔离开发。**请在独立目录检出 Beta，不要直接覆盖正在运行的稳定 MCP 服务。** 详细步骤见 [Beta 发布说明](V04_BETA_RELEASE.md)。

## 启动（仅 Ubuntu 本机）

```bash
python3 scripts/bridge_studio.py
```

浏览器打开 `http://127.0.0.1:8768`，输入首次启动时终端显示的 Studio 管理密钥。密钥写在 owner-only `<state_dir>/studio-ui.key`，不要粘贴给聊天模型、Git 仓库或截图分享。忘记时可在本机命令行执行：

```bash
python3 ~/.local/share/codex-local-bridge/maintenance-workspace/scripts/bridge_studio.py --show-key
```

按 Ctrl+C 停止测试 UI。不要让 Tailscale Funnel、Nginx、Cloudflare 或任何公网入口转发端口 8768；它只是本机管理员操作台。原 MCP 服务保留 8767、OAuth 和 Tailscale 链路。

## Beta 可用操作

- **总览**：本地 8767 TCP 检查、已注册项目数、最近任务成功率（不等于测试通过率）、活跃记忆数量。
- **项目**：只读显示权限与项目路径；可以撤销已有自动写入授权；自动授权的发放仍要求本机 CLI 明确批准。新增只读项目支持双步骤：先预检，再本机 Studio 管理员确认注册和重启原 Bridge；已有配置备份/回滚脚本负责落盘。
- **共享记忆**：选择项目、新建项目/决策/任务/知识记忆，搜索标题/内容/标签，编辑时 revision 校验，归档不删除，来源/时间保留；数据库位于 `<state_dir>/studio.sqlite3`，与原 `bridge.sqlite3` 分离。
- **任务**：查看最近 50 条任务的执行状态、测试结果和写回状态。不会显示私有 prompt、Token、授权凭据或完整日志。
- **连接**：分别显示本地 Bridge TCP、Tailscale 进程状态、远程 MCP 未验证、模型实时在线未验证，避免误导。
- **设置**：只读查看本地端口、数据库名称、会话时长，不展示凭据。

**限制：**目前 Studio 所在机器的用户名拥有最高本地权限，首次进入需要独立管理员密钥。网页端 ChatGPT 通过新 MCP 工具访问共享记忆的能力已经在维护源码中实现（search/save/archive），但必须通过正式 SDK 契约/完整 pytest/部署/工具重新发现验收后，才可在 ChatGPT 网页端调用。Codex 自动挑选相关记忆注入任务上下文尚未完成；目前可显式读取记忆并把整理后的上下文附到 Codex 指令。

## 系统安全门禁

- 只能监听 `127.0.0.1`；Host/Origin 及 CSRF 校验。
- 60 分钟短会话，独立随机管理员密钥，HTTP-only SameSite=Strict Cookie。
- SQLite WAL、仅本机账户可读、参数化查询、项目身份绑定、乐观修订锁；对明显凭据拒绝写入。
- 本地管理员授权与公网 OAuth MCP 完全独立；不提供远程任意 Shell、sudo 或自动更改系统权限。
- 项目注册仅可添加只读项目。对任何已授权项目执行写入，都保留 SHA/基线安全策略；危险配置需本机批准。
- 新增项目的配置/Root 发生变化时，旧记忆不会自动归属新根目录；迁移需未来版本提供明确审核。
- 现有 OAuth、Token、Tailscale 与 `airgym_yingjie` 项目没有被本次 alpha 开发修改。

## 后续发布步骤（不能省略）

1. 复核维护 diff，特别是涉及 `bridge/server.py` 的 MCP 新工具注册以及项目注册操作。
2. 运行维护沙箱 `/usr/bin/python3 -m unittest discover -s maintenance_tests -v`。
3. 在完整依赖环境运行 `.venv/bin/python -m pytest -q`。历史断言固定为 10 个工具，v0.4 目标为 13 个，需逐个审查更新；受安全规则阻止的测试文件改动应经本机人工审核。
4. 在可回滚升级脚本中备份、测试、部署，不得由维护副本直接覆盖生产进程；保持 OAuth/SQLite 配置不变。
5. ChatGPT 网页端重新发现 MCP 工具，验证项目级共享记忆 CRUD、跨 Codex 会话续接、读写隔离、回滚和跨进程数据库可见性。
6. 真实重启和异常恢复测试通过后再考虑 `v0.4-stable`。

可回退：停止 8768 Studio UI 即可，不影响原 v0.3.1。SQLite 记忆数据保留在本地，不自动删除。
