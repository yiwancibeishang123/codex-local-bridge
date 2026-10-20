# 项目级授权（v0.3）

OAuth 负责认证连接；项目授权决定连接能否自动修改原项目。二者独立。
OAuth 同意不是自动写入授权。没有有效项目授权时原项目只读；原有逐次审批流程仍可生成隔离提案，最终写回必须另经本机审批。配置 `allowed_modes: ["read"]` 和 `file_write_enabled: false` 则直接拒绝全部写工具。

## 本机一次性设置

仅对你明确选择的项目设置三个开关；不要批量提升项目，也不要默认提升其他项目权限。

```json
{
  "id": "my_project",
  "root": "/home/YOUR_USER/projects/my_project",
  "include": ["*.py", "*.md"],
  "allowed_modes": ["read", "write_test"],
  "file_write_enabled": true,
  "project_authorization": true,
  "tests": [["/usr/bin/python3", "-m", "unittest", "discover", "-v"]]
}
```

保持 config.json 为 600。这三个开关只是允许本机发放授权，本身不会开启自动写入。

默认 systemd 将 home 设为只读，仅 state 可写。对于 state 以外的项目，首次配置时需添加精确的项目可写路径：

```bash
mkdir -p ~/.config/systemd/user/codex-local-bridge.service.d
# 本命令只输出经过检查的 [Service]/ReadWritePaths，不会直接修改 unit。
~/.local/bin/codex-local-bridge project-systemd my_project \
  > ~/.config/systemd/user/codex-local-bridge.service.d/my_project.conf
systemctl --user daemon-reload
systemctl --user restart codex-local-bridge
~/.local/bin/codex-local-bridge grant-project my_project
```

先核对屏幕上的项目路径、include 和测试命令，再在本机交互终端输入 `AUTHORIZE my_project`。无需在每个普通任务上输入 APPROVE。可选 `--hours 24` 指定有效期；省略则持续有效，直到撤销、配置变化或根目录身份变化。CLI 不接受 stdin 管道代替交互确认。项目不能是整个 home、根目录或 state 的父目录。

```bash
~/.local/bin/codex-local-bridge project-policy my_project
~/.local/bin/codex-local-bridge revoke-project my_project
```

撤销立即阻止随后开始的自动测试和写回，不需要重启。已经开始的隔离测试可能执行结束，但结果不能绕过撤销。授权、撤销和写入使用同一锁；撤销命令返回后不会再有持旧授权落盘的写入。撤销不回滚既有修改、不撤回已发送给 Codex 的上下文，也不撤销 OAuth 连接。之后如需重新授权，必须再次执行 grant-project；旧排队任务不能冒用新的授权 ID。

如项目移除，不再使用其服务可写路径，可删除相应 .conf 后 daemon-reload/restart；运行中服务不会根据远端输入调整可写路径。不要把整个 home 加入 ReadWritePaths。

## 权限策略

| 操作 | 有效项目授权 | 没有授权 / 显式只读 |
|---|---|---|
| 白名单文件读取、目录列举 | 自动，保留敏感路径拒绝和脱敏 | 自动，只在本机配置范围内 |
| 普通代码文件创建、修改 | 自动，先检查 SHA、备份并记录 diff | 等待单独本机审批；file_write_enabled=false 则拒绝 |
| 普通 Codex write_test | 自动排队，执行沙箱测试 | 等待逐任务审批；allowed_modes=[read] 则拒绝 |
| Codex 修改写回原项目 | 普通文件且至少一条配置测试、全部退出码 0 时自动写回 | 仅本机精确 diff 审批后写回 |
| 风险脚本、依赖/安全配置、含疑似密钥的内容 | 生成提案，等待本机单独审批；不自动测试风险提案 | 同样需单独审批或拒绝 |
| 越权路径、隐藏/凭证文件、符号/硬链接 | 拒绝，不提供远程提权接口 | 拒绝 |
| 任意 Shell、sudo、高权限命令、联网测试、删除、源仓库提交 | 无远程能力；配置中的非安全测试命令必须逐次审批且仍受沙箱限制，不会获得主机权限 | 拒绝或受限的本机审批流程 |

普通文件只包含源码内置后缀（policy.py 的 CODE_SUFFIXES），并受原 include 二次限制。AGENTS.md、setup.py、conftest.py、sitecustomize.py、依赖清单、名称含 auth/security/secret/token 等关键字、脚本/JSON/YAML/TOML 等非普通后缀均不自动落盘。常见内嵌密钥/密码赋值同样触发人工审批；源上下文遇到疑似凭证会跳过该文件。此分类是保守的规则，不能保证识别所有业务敏感信息，应只授权可以传给 Codex 的窄范围源代码。

自动测试仅支持固定 `/usr/bin/python3 -m unittest ...`，拒绝 shell、python -c、替代解释器、绝对/越级测试参数。其他测试框架暂时沿用逐任务审批；不要扩大沙箱以“兼容”。第三方依赖、GPU 和机器人运行环境没有自动挂载。测试在无网络、无主机 home、只读项目副本中执行。文件工具只改文本，不执行其内容，也不自动运行测试。

## 一致性、备份和审计

授权存于 owner-only SQLite，绑定完整项目配置、根目录设备/inode 和策略版本。配置变动后需重新核对和授权；服务读取配置变动仍需重启。OAuth token 不携带写权限，不支持通过 MCP 发放/延期/撤销项目授权。

每个任务在提交时绑定授权 ID，执行、每次测试开始、写回前重新验证。SHA/基线变化、原文件出现链接、授权撤销或 systemd 不可写都会阻止自动落盘，不能把 `state=succeeded` 误当成写回成功。检查 `tests_passed`、`changes_applied`、`apply_requires_approval` 和 `apply_error`。风险修改或测试失败不自动应用。

任务备份位于 state/backups/TASK_ID，包括原文件和 manifest.json。manifest 记录新建/既有路径，便于故障恢复。普通异常尝试撤回同一批次已替换的文件；断电/kill 不具备全项目事务原子性，应结合备份和原 Git diff 恢复。文件工具备份保存在 state/filetools/backups/PROPOSAL_ID.bak，提案 SQLite 保留新内容、原 SHA、策略摘要与审计。新文件没有旧内容备份，提案或 manifest 记录它原本不存在。

state/bridge.sqlite3 审计涵盖授权、撤销、任务提交/完成、阻止自动应用和自动写回；filetools/workspace.sqlite3 记录文件提案及应用。不会自动删除备份或自动提交原仓库。

## ChatGPT 工具

保留同一个 OAuth HTTPS /mcp，仍为十个工具：list_projects、delegate_task、get_task_status、get_task_result、continue_task、list_project_files、read_project_file、request_write_file、request_edit_file、get_write_status。

list_projects 的 authorization 显示有效权限。request_* 在自动授权下返回 applied，否则返回 pending_local_approval 或错误。ChatGPT 应以实际返回状态判断，不能因名称为 request 而认为尚未写入。

服务升级不会自动更新 ChatGPT 宿主缓存。如果只看到旧五个工具，请在该连接管理处刷新工具发现/重新连接。本地或公网 HTTP 客户端测试十工具成功不能替代 ChatGPT 宿主实际调用验收。
