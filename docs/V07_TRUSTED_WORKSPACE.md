# v0.7 — Trusted workspace for ChatGPT MCP

本阶段以 ChatGPT 网页端连接的 MCP 为主要入口，桌面软件可选。目标是：已授权的 AirGym、A-mcp 项目能够直接读写文件夹内的各种普通文件，不再逐个申请扩展名和目录访问。

## 日常用法

- “浏览 AirGym 整个目录，列出所有数据集和大型文件。”
- “读取 AirGym 的训练日志和 ROSbag 文件信息。”
- “修改 A-mcp 的这个配置文件，保存并核对修改。”
- “这部分适合 Codex，委派任务并报告测试/写回结果。”
- “总结刚才的决策，保存到 AirGym 的共享记忆。”

## 新增 MCP 工具

| Tool | Function |
|---|---|
| workspace_info | 显示已注册项目整个文件夹的有效权限 |
| browse_workspace | 分页查看任何子目录，包括非代码类型文件 |
| read_workspace_file | UTF-8 文本或 base64 二进制，按 48KiB 分块读取 |
| hash_workspace_file | 本地计算完整 SHA-256，无须上传全部内容 |
| write_workspace_file | 单次调用写入不超过 48KiB 的文件 |
| begin_workspace_write | 开始大型文件的分块写入 |
| append_workspace_chunk | 追加下一块数据 |
| finish_workspace_write | 校验内容、备份旧文件并原子发布 |
| recent_workspace_changes | 查看真实已完成的本地文件操作 |

当前项目已经在本机开启的 read/write 授权直接适用，不需要额外授予数据格式权限或逐文件批准。只读项目依旧只读。文件路径始终相对于已注册项目根目录，禁止越界与向外跟随符号链接；已存在的文件在修改前自动创建私有备份。

新工具不替代原有的 OAuth MCP、任务委派、旧文件工具及共享记忆。ChatGPT 直接操作简单文件以节省 Codex 使用量，长任务可以继续交给 Codex。

## 长期共享记忆

写回成功后自动记录变更信息，并尝试写入原有 studio.sqlite3 项目记忆。ChatGPT 使用 search_project_memories、get_project_handoff，独立 Codex 使用本地 bridge_memory MCP 的 search_bridge_project_memories、get_bridge_project_handoff。两端读取同一份 SQLite；独立 Codex 客户端仍需实际调用记忆工具，不能把“可访问”误认为“每次会话都自动回忆”。

## 大文件说明

任何类型的普通文件都可按字节分块读取。单次读取上限为 48KiB，不代表文件总大小上限是 48KiB。大文件分析优先在 Ubuntu 本地完成，不建议把多 GB 的原始字节逐块传进对话，以免消耗上下文。工具不会主动执行二进制文件中的代码。

## 发布与验收

代码与测试进入 GitHub 开发分支，不会自动替换运行中的本地 MCP。升级一次后需让 ChatGPT 重新发现新增工具。验证时先针对 AirGym、A-mcp 的一次性测试文件执行读取、创建、编辑、回读，再用 Codex 本地 memory MCP 搜索对应共享记忆。

已授权的项目无需重新授权，也无需额外安装桌面软件。真实部署还需要 Ubuntu 主机上的端到端验收。
