# Security policy

This is a single-owner local bridge, not a multi-tenant execution platform. The active public release is maintained on main; a formal security SLA is not offered.

Read the [security boundary](docs/SECURITY.md) before exposing the server. Tailscale Funnel is public ingress, so OAuth must remain enabled. Project authorization sends selected source context to Codex through the signed-in account; only authorize code you may share that way.

Do not submit credentials, live tokens, auth.json, private endpoint addresses or proprietary code in public issues. Use GitHub's private vulnerability reporting when available. If it is unavailable, open a minimal issue requesting a private reporting channel without exploit details or sensitive attachments.

Backups and local deployment snapshots contain private data. They belong in the owner-only state directory, never in this repository or release assets.
