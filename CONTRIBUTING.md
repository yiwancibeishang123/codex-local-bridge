# Contributing

Issues and pull requests are welcome. Describe the concrete trigger, expected behavior, actual result and relevant versions. Redact project content, account identifiers, domains and credentials before sharing logs.

## Local checks

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
bash -n scripts/install.sh scripts/start.sh
```

The default suite requires working Linux user/mount namespaces and bubblewrap. It includes deterministic protocol fixtures and real SDK/HTTP/filesystem tests; these are not browser or model inference evidence. Real Codex tests are explicit opt-in and use your existing subscription allowance. Never upload a config, auth.json, database or test run containing private project sources.

## Change boundaries

Keep changes small. Preserve OAuth resource checks, explicit local grants, SHA conflicts, no-follow path handling, close-on-exec descriptors, backups and audit. Do not silently relax sandboxing, auto-approve risky operations or add paid-service fallbacks. A failing test must stop deployment.

For a bug fix, add a regression that observes meaningful behavior. State which checks used real Codex and which used fixtures. Do not describe a protocol mock as end-to-end success.

## Updating a deployed installation

1. Identify the actual systemd ExecStart, WorkingDirectory and Python import path.
2. Prepare a separate candidate checkout and review its diff.
3. Back up changed source, private config and state. Use SQLite's backup API for live DB snapshots.
4. Run the full suite in the candidate. Run opt-in model checks only when local credentials/allowance permit.
5. Publish only the tested changes, then restart the user service.
6. Verify OAuth, persistent project grants, SQLite integrity, HTTPS/Funnel and Codex login. Confirm no-op tasks do not report source changes and real edits still apply.

Do not treat `register-maintenance` as deployment: it creates a separate source workspace. Its default tests are syntax/manifest smoke checks only.

## Optional hosted CI

The configuration is in docs/ci/tests.yml. See docs/ci/README.md before enabling it. No hosted CI result is claimed for the initial release.

## Releases

Bump pyproject.toml, plugin.json and the App Server client version together, update CHANGELOG, run tests and inspect an allowlisted source archive. Tag the reviewed commit and attach the source ZIP plus SHA-256 checksum. Never archive an installation directory wholesale.

MIT contributions are licensed under the repository's MIT license. Report vulnerabilities through the process in SECURITY.md rather than posting credentials in an issue.
