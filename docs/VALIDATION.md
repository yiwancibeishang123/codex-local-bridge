# Validation and limitations

## Evidence from the local deployment

The public source includes the reviewed patch deployed on 2026-10-19 and rechecked on 2026-10-20. Personal account identifiers, local paths, endpoint hostnames and raw acceptance data are intentionally not published.

| Check | Observed result | Boundary |
|---|---|---|
| Full candidate pytest with BRIDGE_LIVE_CODEX=1 | 71 passed; 6 subtests passed | Included real Codex, not just mocked outputs |
| Real Codex two-turn disposable project | 1 test then 2 tests, exit 0, same thread, auto-writeback | Uses existing authenticated Codex and isolated fixture roots |
| Actual ChatGPT host no-edit task after deployment | diff empty, configured 5 tests exit 0, changes_applied=false, no empty backup | This was a real host task call |
| Actual ChatGPT host ordinary write after deployment | Configured 5 tests exit 0, changes_applied=true | Wrote only an isolated maintenance acceptance document |
| Public HTTPS OAuth/file diagnostic | Refresh 200; anonymous MCP 401; ten tools; structured/text content equal; valid path/content/SHA | Independent HTTP client, not browser evidence |
| Persistent project authorization | Same token and grant continued across service restart | Does not prove whole-machine restart or indefinite authorization |
| Negative checks | Stale SHA, traversal and read-only-project writes rejected; dangerous edits separately gated | Rule-based redaction is not comprehensive DLP |
| Deployment preservation | Security-module hashes, config, unit, grant records and prior task history retained; DB integrity OK | Raw backups remain local and private |

The original ordinary-write attempt hit the account's Codex usage limit and made no changes. Retrying with the same account after the reset succeeded. No API key, paid upgrade or credit purchase was used.

## Reproduce

```bash
.venv/bin/python -m pytest -q
# With an existing local Bridge config and ChatGPT-authenticated Codex:
BRIDGE_LIVE_CODEX=1 .venv/bin/python -m pytest tests/test_live_codex.py -q -s
# With your own running HTTPS/OAuth endpoint:
.venv/bin/python scripts/check-connection.py
```

Without opt-in, one real-model test is skipped intentionally. CI never receives personal auth files and cannot establish a ChatGPT browser session. Some fixture tests bypass the engine or a test runner to isolate orchestration behavior; comments label those cases explicitly. Real sandbox behavior is covered separately. Never equate a green CI badge with a browser end-to-end check.

## Not established

- Universal custom-MCP write access for all ChatGPT plans or workspaces.
- A permanent login guarantee, full seven-day refresh-token lifecycle, or whole-machine reboot acceptance.
- Windows/macOS execution, arbitrary shells, GPU/robotics runtimes or automatic dependency installation.
- A third-party security audit, exhaustive credential detection or crash-atomic multi-file writes.
- Unrestricted autonomous production deployments. Maintenance changes still require deployment review.

The local test result above records the original deployed candidate; the public repository's own test results are recorded below. Hosted CI is not enabled because the publishing credential has no workflow scope; its configuration is supplied as an opt-in template.


## Public source verification — v0.3.2

The freshly exported public source was installed into a new venv and tested on
2026-10-20 with `BRIDGE_LIVE_CODEX=1 .venv/bin/python -m pytest -q`:
**71 passed, 6 subtests passed in 23.51 seconds**. This included actual Codex
turns using the maintainer's existing local login; no credential was copied into
the public tree. Shell syntax, relative Markdown links, SVG XML and plugin
manifest checks also passed. No hosted CI run is claimed. The optional CI template uses no credentials.
