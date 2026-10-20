# Security boundary

The bridge deliberately uses Codex as a structured code-authoring engine. It receives
only authorized UTF-8 files matching the local project's include patterns and returns
full-file proposals via App Server outputSchema. Bridge validates every proposed
path and writes it into an independent Git snapshot. This is a usable code-change
workflow, but not unrestricted interactive access to the user's terminal.

- Native Codex shell/unified_exec, hooks, apps, remote plugins, web search and
  subagents are disabled for the bridge. Dedicated CODEX_HOME avoids changing the
  existing VS Code settings or loading its MCP configuration. Only a ChatGPT
  account returned by account/read is accepted. API-key inference is refused.
- Codex turns use read-only sandboxing, restricted readable roots, and never
  grant server requests to escape the sandbox. There is no tools/exec MCP method.
- Sources are transported as context through the authenticated Codex session.
  Authorizing a project means authorizing that source transmission to OpenAI.
  Include patterns must be narrow. .env, private-key files, symlinks and system/
  configuration directories are blocked. Secret detection is not exhaustive;
  never authorize source files containing embedded credentials.
- A write_test task is bound to its prompt, parent, project root, file scope,
  configured test argv, and a one-use local grant or a locally issued project grant.
  ChatGPT cannot issue or renew grants. Project grants bind configuration and root
  inode, expire optionally, and are checked again before testing and writeback.
- Test commands are local-config argv arrays, never shell strings. They run
  through bubblewrap with all namespaces isolated, no network, no home, no
  credentials, a read-only project snapshot and hidden .git. The runner fails
  closed on sandbox setup failure. Test output is capped at 1MB and execution at
  120 seconds. v0.1 supports system Python/unit tests; external dependencies need
  an explicitly designed read-only runtime mount in a future version.
- Code deletion is unsupported. Original file overwrite requires a separate
  exact-diff local approval unless a current project grant permits ordinary code
  and all configured tests pass. Before applying, the original must still match the
  baseline; symlinks and hardlinks are rejected. Backups are kept. Source Git
  commits remain a user operation. Each proposal gets its own snapshot commit.
- Applying several files is rollback-protected during normal errors, but not a
  crash-atomic filesystem transaction. Do not edit original files concurrently
  with apply. Review with VS Code first; use original Git for additional recovery.
- HTTPS is terminated by Tailscale Funnel. Funnel is public ingress, not private
  tailnet identity. Authentication is mandatory OAuth via MCP SDK (PKCE S256,
  exact redirect allowlist, one-use short-lived codes, resource-bound expiring
  tokens, rotation/revocation). Browser consent requires confirmation in the
  local Ubuntu CLI. This single-owner authorization provider is small custom
  glue, not an independently audited identity system. No paid IdP is required.
- OAuth bearer/refresh tokens and codes are stored as hashes. The owner-controlled
  SQLite and state directories are private. DCR client credentials, if used by
  the client, are persisted in the private database for SDK client authentication.
  Tokens are never placed in plugin files. HTTP access logs are disabled so
  authorization query strings are not logged. Generic task logs omit reasoning
  and final-source deltas and redact known credential patterns/environment secrets.
- One owner and one worker per state directory. Not a multiuser tenancy system.
  SQLite stores history, thread IDs, results and audit records. On interruption,
  no task is silently replayed; local retry or continue creates an explicit child
  with another approval when required. App Server resume uses the original ID.
- Local access to the user's Unix account remains trusted. A compromised host,
  kernel, Codex binary, or local config cannot be secured by this application.

Do not disable OAuth, use danger-full-access, bind the server to 0.0.0.0, or
replace bubblewrap with a direct subprocess just to make a failing check pass.


## v0.2 file extensions and service lifecycle

The original project list is authoritative. Five file tools share its OAuth MCP
endpoint; no second unauthenticated server or independent allowlist is created.
Direct file writes default off and require a boolean file_write_enabled. A write
request persists a proposal; a valid project grant auto-applies ordinary code.
Other changes require local CLI review and terminal confirmation. Each proposal binds the root/include/write policy digest
and original SHA; scope changes, expiry, replay, binary targets and conflicts are
rejected. Reads/writes use no-follow directory descriptors; applying reuses the
same open parent for its read and atomic replacement. File state is 0700 and DB
and backups are 0600. Up to 100 active proposals and 128 KiB per file are allowed.
This remains a single-owner service: every authorized client sees configured
projects; multi-user tenancy is outside this version. Redaction is defense in
depth and not a comprehensive secret scanner. Do not authorize sensitive trees.

The background queue worker belongs to the ASGI service lifespan, not an MCP
session. The App Server is configured at process startup with a named minimal
read profile for its empty working directory, network disabled for native tools,
and native execution tools disabled. Structured output is validated by Bridge;
bubblewrap runs the fixed local test commands in a separate isolated snapshot.
No paid API key fallback exists. CLI profile/protocol incompatibilities fail
closed and are reported rather than falling back to unrestricted execution.


## v0.3 project grants

See PROJECT_AUTHORIZATION.md for the complete policy and local CLI. Grants live
in owner-only SQLite, never in OAuth tokens or remote arguments. A project needs
all three local opt-ins (project_authorization, file_write_enabled, allowed_modes)
and an interactive local grant before automation. Configuration/root identity
changes invalidate grants; revocation needs no restart. Queued tasks bind the
grant generation and cannot acquire a later grant silently. A shared advisory
lock serializes grant/revoke and source writes; it is not held while inference
runs, so revocation can block later test starts and writes promptly. A sandboxed
test already executing may finish, but its output cannot authorize a later write.

Hidden paths, secrets, outside roots, symlinks and hardlinks remain blocked. Risk
classification is conservative and based on actual proposed file paths/content
and configured argv, not claims in the prompt. Arbitrary code may exist in normal
source files; it is tested only in the network-disabled snapshot sandbox, never
executed with host privileges. Automatic file tools do not execute the content.
Sensitive source patterns are omitted from model context; detection is best effort.

Automatic task batches back up all existing targets and write a recovery manifest
before replacing files using no-follow directory descriptors. Ordinary exceptions
roll back replacements. Power loss and process kill are not whole-project atomic;
restore from backups/manifest and inspect Git diff before resuming. The local Unix
owner remains trusted. Revocation prevents future automated writes; it does not
undo existing changes or retract source already sent to the authenticated model.
