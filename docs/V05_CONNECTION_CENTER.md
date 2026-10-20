# Bridge Studio v0.5 Alpha.4 — 首次使用引导、统一连接中心、项目自动记忆控制

本阶段目标：**安装一次，日常从 Ubuntu 桌面打开，不需要重复输入启动命令，也不需要靠猜测排查连接问题。** Qt 是原生桌面窗口，Studio 的本地数据库仍然只在 Ubuntu 保存。

## 首次打开

软件优先读取 `BRIDGE_CONFIG`（若由用户明确设置）或之前选择的 Studio 本地配置路径，然后尝试默认的 `~/.config/codex-local-bridge/config.json`。如果还没有找到，可以从图形窗口选择**已经存在**的私有 Bridge `config.json`；选择器检查用户归属、0600 文件权限、JSON 基本结构，之后仅把配置路径保存在 `~/.config/bridge-studio/profile.json`（0700 文件夹，0600 文件）。

**选择配置不会复制或上传 Token，不会登录 Codex，不会替换原 MCP 服务。** 全新电脑仍需要先部署一次后端；本版仅提供已有安装的图形化发现，不应宣称 .deb 单独完成从零配置 Tailscale/OAuth。

## 连接中心（原生 Qt）

进入 **连接与诊断**，系统展示八项独立状态：本机配置权限、用户 Bridge 服务、本地 MCP TCP 监听、Codex 可执行文件、Codex 本地共享记忆 MCP 注册、Bridge/Studio SQLite 数据库权限与完整性、Tailscale 后端、ChatGPT 远程 OAuth/Funnel 验证边界。

每行采用 **正常 / 需处理 / 未验证**，点击后有解释与下一步。存在明确的限制：

- MCP TCP 可连接 **≠** ChatGPT OAuth 成功。
- Codex 可执行文件存在 **≠** Codex 已登录或模型实际可用。
- Codex 记忆 MCP 已注册 **≠** 独立 VS Code Codex 每个会话已经自动读写记忆。
- Tailscale 状态 `Running` **≠** 公网 Funnel 已正确发布；必须从 ChatGPT 的真实远端工具调用验证。

**重启 Bridge** 按钮只对当前用户已知的 `codex-local-bridge.service` 执行明确确认后的受控 `systemctl --user restart`。如果任务仍在排队或执行，程序拒绝重启。不会运行任意 shell、重置账号、修改 Tailscale Funnel、静默授权项目或凭据。诊断通过 Qt 工作者线程运行，避免 UI 阻塞。每 60 秒检测一次，或用户手动点击。

## 项目共享记忆偏好

在 **长期共享记忆** 页面选择项目后，可以启用/关闭“自动保存本项目 Bridge 任务结果，并在后续任务中读取共享记忆”。

操作需本机确认：保存原 `config.json` 的私有备份，更新单个项目的 `auto_memory` 开关，重启 Bridge 用户服务后生效；若重启失败，恢复旧配置。切换前检查运行中的任务，防止中断研究。关闭自动记忆**不会删除以前的笔记**，你仍可手动查看、归档或修改记忆。

这个开关只控制 Bridge 任务生命周期和上下文加载。ChatGPT 网页端依旧通过项目级 MCP 记忆工具使用 SQLite；未经过 Bridge 的独立 Codex 会话仍需在桌面端注册本地记忆 MCP，并实际调用其工具。目前没有透明捕获全部聊天记录，也没有虚构跨端自动同步保证。

## .deb 安装版项目注册问题修复

早期原生窗口从 `scripts/add_readonly_project.py` 文件路径启动注册命令；在 PyInstaller 冻结安装包中该源码路径不可用。现在使用 PyInstaller 内置的受控注册模块，仍保留原有项目验证、私有备份、只读默认授权、systemd 重启及失败回滚流程；在后台线程运行，避免界面卡住。

新用户通过 **项目与权限 → 添加本地项目** 选择目录即可，不需要手动输入多行脚本。默认只读，写入需要单独在本机确认并满足测试/沙箱条件。

## 发布边界与验收

GitHub Actions 继续构建适用于 Ubuntu 22.04 amd64 的 `.deb`，执行 Python 3.10/3.12 全量单元测试、原生 Qt offscreen 启动、以及解包后冻结的 Qt 可执行文件启动。安装包发布在对应 CI Workflow Artifacts；生产环境默认不更新。稳定 `main` 与本地已部署服务保持不变。

**上线 Beta 前仍需在实际 Ubuntu 上验证**：安装包双击安装、未配置时的图形选择器、真实项目添加、权限与授权撤销、ChatGPT MCP OAuth 远程调用，以及 VS Code Codex 是否实际加载共享记忆工具。

本版未实现：无需准备任何后端的一键全新安装、签名自动更新、远程 OAuth 实时检查、独立 Codex 聊天自动抓取。这些会在独立的安全/兼容性验收通过后逐步开放。
