# Changelog

## 0.3.2 — 2026-10-20

First public source release, based on the deployed v0.3.1 plus the reviewed empty-diff patch.

- Empty writeback batches no longer create backups or mark `changes_applied=true`.
- Added five regression cases covering automatic/manual no-op and real writeback behavior.
- Includes explicit structured MCP file responses, persistent revocable project grants, and isolated maintenance workspace registration.
- Added public installation/usage guides, an architecture illustration, sanitized validation notes, MIT license, contribution guidance and a credential-free CI template (not enabled in this release).
- Excludes private project names, live endpoint addresses, OAuth/Codex credentials, state databases and raw local acceptance logs.

This release does not change an existing machine's deployed service automatically.

## 0.3.1 — private deployment milestone

Explicit Pydantic output models for all five file tools; UTF-8, relative paths and original-byte SHA-256 are available in both text and structuredContent. Added isolated maintenance registration and response-contract regressions.

## 0.3.0 — private deployment milestone

Local project grants, revocation and scope binding. Ordinary code can auto-apply; task writeback requires passing configured tests. Dangerous edits retain independent review.

## 0.2.0 — private deployment milestone

Integrated file tools with the OAuth task service, fixed worker lifetime and App Server profile compatibility, and completed real two-turn task checks.
