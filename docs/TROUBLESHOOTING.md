# Troubleshooting

| Symptom | Check and next action |
|---|---|
| Missing codex binary | The installer first checks `PATH`, then discovers the official binary bundled by the VS Code extension. If neither exists, install the official CLI alongside the extension using npm (README). Bridge does not automate extension UI or share its live turn. |
| Codex ChatGPT login required | Run `.venv/bin/codex-local-bridge login` using your existing Plus account. Bridge uses a separate private CODEX_HOME; never paste auth.json into chat. |
| Codex flags/schema unsupported | Tested with CLI 0.162.1. Use its App Server schema generator to verify thread/start and turn/start. Update the official CLI only after choosing to do so. No legacy MCP-server fallback. |
| 401 from /mcp | Expected without OAuth. Select OAuth when creating the ChatGPT custom MCP connection. Check the well-known metadata and authorize in the local CLI. |
| invalid_redirect_uri | Copy the EXACT callback shown by ChatGPT's connection management page into config.redirect_uris, restart service, retry registration. Never allow arbitrary redirect hosts. |
| Consent page remains waiting | Run `auth-requests` locally, match the request ID and callback, then `approve-auth ID`. Reload browser consent page. Requests expire after ten minutes. |
| OAuth issuer/resource mismatch | public_url must be the origin Tailscale actually returns. issuer has a trailing slash, resource is origin + /mcp. Changing origin invalidates existing tokens; reconnect. |
| ChatGPT cannot add custom MCP | Your account/workspace UI permissions control availability. Package upload does not unlock it. No paid upgrade is assumed; local MCP testing remains possible. |
| Funnel fails | Run `tailscale status`, `tailscale funnel status`, check MagicDNS, HTTPS and funnel nodeAttrs in official docs. The personal/noncommercial free plan is the intended plan. Localhost installation and tests remain available, but ChatGPT web access requires a reachable HTTPS origin. |
| URL unavailable | Keep PC awake, connected, tailscaled and Bridge service running. DNS propagation can take time; Funnel has bandwidth limits and is not a hosting SLA. |
| awaiting_approval | `inspect-task ID`, then `approve-task ID` if you authorize exactly that source/task/test scope. |
| Submitted but never running | `systemctl --user status codex-local-bridge`; inspect `journalctl --user -u codex-local-bridge --no-pager`. Run only one HTTP/stdio worker per state directory. |
| bwrap Operation not permitted | The current container used for development blocks namespace setup. On Ubuntu check unprivileged user namespaces and security policy. Do not disable sandboxing; inspect `doctor` and use an allowed host. |
| Tests failed despite succeeded state | succeeded means Codex returned a valid proposal; tests_passed independently records exit codes. Read result.tests output and use continue_task to repair. |
| Third-party Python imports missing | Tests use /usr/bin/python3 in isolation. v0.1 demo uses stdlib unittest. Do not expose your entire home/venv to bypass isolation; add controlled runtime support later. |
| Service restart during turn | Task becomes interrupted. History and thread ID persist. `retry ID` or continue_task; write_test needs a fresh local approval. |
| Original changed since snapshot | Do not force apply. Review local edits and submit a fresh task against the updated original or manually merge in VS Code. |
| systemd exits with permission error | Install path must remain readable; config mode 600. State must be writable under ReadWritePaths. Template keeps home read-only and only state writable. |
| systemd reports `218/CAPABILITIES` before ExecStart | The user manager cannot apply a capability hardening option. The provided user unit keeps filesystem protection, private home, private tmp and task limits, while omitting kernel namespace restrictions that can fail for unprivileged user managers. Check `journalctl --user -u codex-local-bridge` and retry after reloading the unit. |
| User service stops after logout | It normally follows your login session. If desired, deliberately enable user lingering with `sudo loginctl enable-linger "$USER"` after reviewing that persistent background access. |

Stop safely: `systemctl --user stop codex-local-bridge`. Stop public ingress using
`tailscale funnel reset` only if this device's Funnel is dedicated to Bridge;
otherwise consult `tailscale funnel --help` to disable just the mapping you created.
Do not overwrite existing Funnel mappings without reviewing them.


## v0.2 真实执行故障

- `readOnly.access is no longer supported`：旧版 App Server 参数与当前 CLI 不兼容。
  使用 v0.2 的命名权限 profile 实现，不要删去隔离策略直接退到完整文件系统访问。
- `failed to load workspace requirements`：检查 profile 是否只在 thread config 中
  临时注入。v0.2 在进程启动时同时设置 permissions.bridge 和 default_permissions，
  使 requirements reload 与初始配置一致。实际修复须以一次真实模型回合成功为准。
- 任务立刻 interrupted，但 systemd 没重启：旧版把 worker 绑定在短生命周期 MCP
  会话；升级为服务级生命周期实现，重新显式提交/续接，不自动重放旧任务。
- ChatGPT 只能看到五个工具：服务升级不会刷新宿主缓存。在连接管理中刷新工具
  发现；先用 scripts/check-connection.py 检查服务是否已经返回十个工具。
- `file_write_enabled must be a JSON boolean`：配置应写 true/false，不要写字符串
  "true"/"false"；默认关闭。只能在本机编辑配置并重启服务。
- `Project policy changed`：提案绑定 root/include/write 范围；配置已改变，需要重新
  读取文件与 SHA 并提交提案，不可复用旧审批。
- 提案 expired：重新提交文件请求，或从 ChatGPT 重建过期 OAuth 连接请求；刷新
  已过期的 consent 页面不会生成新的 OAuth ID。

## v0.3 项目自动授权

- 有 OAuth 但仍 awaiting_approval：OAuth 不代表项目写权限；运行 project-policy，核对三个本机开关及是否已 grant-project。非安全固定测试命令仍要求逐次审批。
- 已授权但 changes_applied=false：读取 apply_error 与真实测试退出码；风险文件、缺少测试、测试失败、原文件冲突、授权撤销和 systemd 只读均会保留提案。不要将执行成功等同于原项目写入成功。
- Read-only file system：按 PROJECT_AUTHORIZATION.md 添加该项目的精确 ReadWritePaths，daemon-reload/restart 后再提交；不要让整个 home 可写。
- 配置变动后变回 read_only：授权绑定完整项目配置和根目录 inode；重启后重新本机核对、授权。旧任务不自动使用新授权，应重新委派。
- 授权一直有效：默认无到期时间，可用 --hours 设置时限，或 revoke-project 立即撤销。OAuth 的 token 生命周期保持独立，不承诺永久免登录。
- 新建文件在后续任务不可见：Git 模式会读取未忽略、匹配 include 的未跟踪文件；若配置 context_files，仍严格只读取该明确列表，需要在本机显式调整。
