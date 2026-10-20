# Bridge Studio v0.4 Beta · GitHub development branch

Development branch: `feat/bridge-studio-v0.4`; version `0.4.0b1`. The existing stable `main` and production MCP installation are deliberately untouched.

## Implemented in this branch

- Chinese modern responsive Studio dashboard on **127.0.0.1:8768 only**, local owner key, short HttpOnly session, CSRF and Host/Origin validation.
- Actual project status, read-only project registration via guarded local helper, project-grant revocation (write elevation stays terminal-only).
- Bounded read-only task details: independent engine/test/writeback states, valid integer test exit codes and safe relative filenames; never raw prompts, diffs, credentials or logs.
- Private SQLite `studio.sqlite3` memories, project identity binding, search, revision-aware edits and archiving.
- **Explicit** verified task receipt → project memory: open a completed Studio task, choose 保存验收结果到项目记忆; this does not automatically collect conversations or store prompts.
- Existing 10 authenticated MCP tools plus 5 scoped memory/handoff tools: search_project_memories, save_project_memory, archive_project_memory, get_project_handoff, get_task_receipt.
- Handoffs carry a provenance warning, limited selection/byte budget, project scope. Task receipt checks persisted evidence instead of trusting model narration.
- CI for Python 3.10 / 3.12, security and integration regression tests.

## Local alpha/beta evaluation (no production replacement)

Clone the feature branch into a separate development directory:

```bash
git clone --branch feat/bridge-studio-v0.4 https://github.com/yiwancibeishang123/codex-local-bridge.git ~/codex-local-bridge-v04-beta
cd ~/codex-local-bridge-v04-beta
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
python3 scripts/bridge_studio.py
```

Use Ubuntu browser: **http://127.0.0.1:8768**. Login key is printed on first start (only in local terminal). Later retrieve locally with `python3 scripts/bridge_studio.py --show-key`. Never share key with models, screenshots, GitHub or Funnel.

Studio reads the private existing `~/.config/codex-local-bridge/config.json`; its own memories are stored in private `<state_dir>/studio.sqlite3`. Test with backup of production config/SQLite, especially before registering real new projects. Registration explicitly restarts the original Bridge service; mere Studio launch does not.

## Optional user systemd autostart (after validating a separate checkout)

Template `scripts/bridge-studio.service.in` is **not automatically installed**. Replace `@ROOT@` with the absolute checked-out directory, save as `~/.config/systemd/user/bridge-studio.service`, review, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now bridge-studio
systemctl --user status bridge-studio
```

If it fails, use `journalctl --user -u bridge-studio -n 60 --no-pager`. Bind remains loopback. The Studio process is separate from the existing OAuth MCP service.

## Integration notes

Source code on GitHub does **not** automatically change the locally deployed Codex Local Bridge service; existing ChatGPT connector will still expose the old 10 tools until an audited v0.4 MCP deployment + service restart + tool re-discovery.

For explicit memory handoff, save curated project notes and use `get_project_handoff(project_id, query)` from the upgraded MCP connector. The result is *untrusted context* to cite/verify against current source, not a license to execute stored commands. Save task results using the Studio button or manually through reviewed memory tools. Full background automatic memory ingestion and model-based semantic dedup are **not** implemented.

## Release gates still open

- Manual browser visual/UX review across desktop/mobile widths; real project registration & rollback on Ubuntu.
- Live OAuth ChatGPT MCP rediscovery of all 15 tools and real Codex task/receipt integration.
- Crash/power-loss, service restart, auth/session durability and backups restore drill.
- Security review before merging to main, including local admin process model.
