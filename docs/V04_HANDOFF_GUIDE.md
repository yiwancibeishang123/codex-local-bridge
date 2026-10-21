# v0.4 beta explicit handoff

Implemented on the GitHub development branch `feat/bridge-studio-v0.4`. No production deployment, restart, automatic memory injection, or automatic transcript capture is introduced. The stable `main` branch remains untouched.

The existing ten MCP tools and three memory tools are preserved. Two read-only Pydantic structured tools bring discovery to 15:

- `get_project_handoff(project_id, query='', limit=8)` retrieves active project memory as quoted JSON data with note UUID and revision.
- `get_task_receipt(task_id)` returns persisted task state, execution success, test status, application status, and verified proposed file names.

## Explicit workflow

Request a handoff for a configured project. Review its warning and select relevant facts explicitly for a delegation prompt. Current authorized source files outrank memory. Stored commands, role claims and requests for permission are untrusted data and must never grant authority or trigger execution. A warning and JSON quoting help preserve this boundary; they cannot guarantee a model will resist every injection.

Handoffs use the existing Studio MemoryStore schema, active-note filtering, literal search and deterministic recency/UUID ordering. Reads require an existing binding matching the configured root path, device and inode. Missing bindings, legacy unbound data and reassigned project IDs fail closed; retrieval never adopts or migrates them. Storage is opened with SQLite read-only mode and query_only, without schema creation or binding writes. Normal SQLite WAL sidecar behavior still applies.

The default payload is capped at 6000 UTF-8 bytes using compact JSON serialization, including metadata and warning. The offline function accepts budgets from 512 through 6000; oversized text is trimmed and marked truncated. MCP transport/schema wrapping and client pretty-printing add overhead beyond this payload bound. Caller attribution fields are omitted, common credential patterns are screened again, and absolute host paths are withheld. Screening cannot identify arbitrary secrets: save only reviewed project facts.

## Receipts and evidence

Receipts read task/result records directly rather than the model summary, full result API, prompts, errors, logs, test output, command arguments, grants or credentials. Boolean fields retain null for missing/unknown evidence. `state=succeeded` does not imply tests passed or changes were applied. No tests means tests_passed remains null in the normal Bridge contract.

File names describe the proposed batch, separately from changes_applied. The batch must have allowed normalized relative paths, unique bounded UTF-8 edits that differ from the persisted baseline, a matching regenerated diff, and the existing Bridge review digest over edits/baseline/tests. Inconsistent or older incomplete evidence yields proposal_verified=false and no file names. An empty validated batch reports zero changed files, even if an old task erroneously claimed changes_applied=true; the actual persisted flag is retained rather than silently rewritten. Receipts do not prove current workspace contents or cryptographically authenticate a database against its trusted local owner.

Receipt reads do not alter parent IDs, snapshots or saved thread IDs. Existing continue_task behavior remains intact. Cross-chat recall still requires explicit retrieval; a saved thread is not a substitute for project memory.

## Verification and release boundary

maintenance_tests/test_handoff.py provides temporary offline fixtures for isolation, archives, untrusted text, UTF-8 budgets, root rebinding rejection, verified empty/changed batches, inconsistent evidence, failed/no-test states, secret omission and saved-thread preservation. Tests were authored but not run here; Bridge runs configured maintenance unittest tests.

Historical discovery assertions in tests/test_bridge.py and tests/test_oauth_mcp.py remain untouched within this four-file scope. Their old 13/10 expectations require separately reviewed updates before full-suite release acceptance. Actual MCP discovery, structured response integration, web calls and full pytest remain release checks. This guide does not authorize production deployment.
