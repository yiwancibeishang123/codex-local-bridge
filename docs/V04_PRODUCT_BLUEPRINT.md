# Codex Local Bridge · v0.4 Product Blueprint
Version: 0.4 development specification · 2026-10-20 · Status: architecture/prototype, not production deployed.

## Product
**Bridge Studio** — local-first control center for ChatGPT ↔ local Codex development, project access, persistent context and verifiable task execution.
Principles: no additional paid API, reuse ChatGPT-account Codex authentication, keep VS Code Codex, state remains Ubuntu-local in SQLite, least-privilege defaults, visible and reversible operations, and a UI that non-programmers can operate.

## Architecture and deployment
- Keep existing public OAuth-protected MCP endpoint at current port (8767) and Tailscale Funnel mapping, retaining backwards compatibility with all 10 v0.3.1 tools.
- Serve admin UI from a **separate localhost-only process** at 127.0.0.1:8768; never publish it through Funnel, /mcp, or 0.0.0.0. If remote UI becomes necessary, use authenticated tailnet access, never a public unauthenticated UI.
- Separate UI API/controller, project registry/config serializer, memory storage/index, task orchestration, health probes, and service supervision. Backend Python, server-rendered HTML or locally bundled static assets, no paid/cloud frontend.
- Read existing ~/.config/codex-local-bridge/config.json and ~/.local/share/codex-local-bridge/bridge.sqlite3; do not fork/overwrite existing grants or OAuth.
- Config changes require owner-local approval, strict directory scope, conflict check, backup and safe service reload/restart; revoke and dangerous grants are local only. The UI may request but cannot silently bestow root-level access.
- Runtime local UI routes: /, /projects, /projects/{id}, /tasks, /tasks/{id}, /memory, /activity, /connections, /settings. Local admin auth required even on loopback; include CSRF, Origin and Host validation, no external CDN.
- For browser frontend: prefer a compact dashboard with sidebar, breadcrumb, command/search bar, real-time status cards, readable tables, confirmation modals, toasts and keyboard navigation. Avoid large decorative charts, empty dashboards, or low-contrast text.

## Design language
Product identity: **Bridge Studio**. Default bright theme; sophisticated neutral pearl/off-white background; ink navy text; soft indigo/blue primary accent; restrained jade for success; amber for approval. Optional dark theme.
Layout: top 64px header, left 240px collapsible rail, cards 12-16px radius, 8px spacing system, 16px body, 12-14px auxiliary labels, responsive desktop/tablet. All labels in clear Chinese, with English technical names where helpful.
Home highlights real states: MCP online/offline, Funnel reachability (verified vs unknown), Codex login, worker status, authorized project count, pending approvals, last 5 tasks. Status must come from real probes, never invented demo states.
Project cards: alias, path abbreviated, permissions, connected state, indexed context files, last task. Actions: open, browse, edit scope (local approval), remove (with confirmation), revoke grant. Guided add wizard: pick/enter exact local folder, scan/select file extensions + starter context, review data to expose, choose read-only default, local confirmation, verify read.
Task detail: state timeline, safe summary, real test output/exit code, diff viewer, backups/recovery, follow-up button, link to thread id. Never conflate execution_succeeded, tests_passed and changes_applied.
Memory center: project selector, chronology, facts/decisions/bugs/constraints, source attribution, search, pin/unpin, correct, expire/delete, export/import, audit.

## Memory model (local SQLite)
Tables: memory_items(id,project_id,kind,title,body,source_type,source_id,confidence,created_at,updated_at,expires_at,pinned,version), memory_events(id,item_id,event,payload,at), task_summaries(id,project_id,task_id,thread_id,goal,results,tests,files_changed,open_questions,created_at), memory_links(...), ingestion_runs(...).
- Project isolation enforced at query/API level, no default global cross-project recall. Opt-in manually selected shareable memory can be linked to more than one project, with audit.
- Separate immutable raw task result pointer from editable curated memory; only save structured, validated, user-reviewable summaries/facts, not unredacted secrets.
- Capture pipeline after terminal task: summarize user goal, observed outcome, edited files, test code/exit, unresolved issues; write summary with provenance. Never claim tests passed from model narration.
- Retrieval pipeline before Codex delegation: project-local pinned facts + lexically relevant items + recent task summaries, with strict token/byte budget, citations to memory IDs, current checked file context outranking stale memories.
- Search using SQLite FTS5 when available, fallback LIKE; later optional on-device embeddings only after real eval (Chinese tokenization tradeoffs); no paid embedding API required.
- Dedup, update/correction, TTL, drift/conflict flag, project export/import JSON, snapshots, migration/versioning, per-project reset and explicit deletion. Do not assume ChatGPT itself has persistent global cross-chat memory; memory is provided through project-scoped MCP tools when explicitly requested.

## Suggested MCP extension
Keep original 10 stable tools, extend with:
1. request_add_project(project_path,project_id,...): **stages** a registration, returns proposal; MUST NOT register arbitrary host path or grant access remotely.
2. get_project_registration_status(proposal_id)
3. list_project_memories(project_id,...)
4. search_project_memory(project_id,query,...)
5. save_project_memory(project_id,...): memory content must be authenticated/project-scoped; sensitive content rejected; local UI can curate.
6. get_bridge_health(): sanitized status only; no credentials/full host paths by default.
Sensitive project removal, write grants, UI-local session management remain locally authorized only. Treat new MCP schema discovery/ChatGPT connector refresh as a release checkpoint.
Do not combine remote project registration and local privilege grant into one remote operation.

## Release plan
P0 (first local alpha): stable local UI + auth, status, project list/details/file browse, staged add project w/local approval, revoke, basic tasks/logs, SQLite migrations & backup/restore. Maintain existing 10 tools and public MCP.
P1: project memories, task summaries + provenance, FTS5/LIKE search, memory editorial controls, scoped injection into Codex, exports and recoverability. Add explicit acceptance tests for cross-project isolation & stale context.
P2: async event updates, multi-task queue/priority/cancel, approval inbox, visual diffs, audit trail, theme, keyboard shortcuts, connection diagnostics and guided onboarding.
P3: release packaging, service supervision/autostart, deployment migration/rollback, accessibility, resilience testing, user documentation. Optional LAN/mobile status view only after separate security design.

## Acceptance
- Local UI only accessible at 127.0.0.1; public Funnel host serves MCP but NOT dashboard/admin routes.
- New project cannot be added via remote MCP without separate local approval; scope is inspectable before consent; existing project grants not silently widened.
- Project A memory never appears in B retrieval unless explicitly shared; data remains on local disk and survives service restart.
- Search reproducible with known corpus; model-generated suggestions cannot silently overwrite curated facts.
- End-to-end test: UI register test project → ChatGPT list/read → delegate task → real exit codes/diff → optional safe write-back → summarize → new thread retrieves relevant project facts → backup/restart/recover.
- Visual: real working dashboard, no fake statuses, responsive, keyboard/ARIA, no remote fonts/CDNs.
- Release: unit/security/integration tests, actual browser screenshots and human QA, no production deployment before local owner review and rollback plan.

## Constraints found in v0.3.1
- bridge/server.py is the public OAuth MCP application, not a local UI; separate admin surface.
- bridge/store.py currently stores generic JSON records/audit in SQLite WAL; add versioned tables/migrations without breaking records.
- bridge/cli.py handles owner-only grant, revoke and approval. Preserve that trust boundary.
- Codex App Server thread ID is not durable knowledge; it complements but cannot replace indexed project memory.
- Existing registered projects include airgym_yingjie read-only, bridge_maintenance auto-write isolated, demo and policy_acceptance. Do not widen airgym permissions.
- bridge_maintenance test suite is intentionally limited to stdlib unittest in network-isolated sandbox. Full pytest and browser acceptance require local developer environment.
