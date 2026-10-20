# Codex Local Bridge

Plan in ChatGPT. Let a locally authenticated Codex author code, run configured sandboxed tests, and return real diffs and results. Keep your VS Code setup.

See the illustrated [Chinese README](../README.md) for the complete guide.

## What you get

- One OAuth-protected HTTPS MCP endpoint and ten project/file/task tools.
- Official Codex App Server authenticated with an existing ChatGPT account; no API-key fallback or automatic credit purchases.
- Local, revocable project grants for ordinary file changes; risky operations still require separate local approval or are denied.
- SQLite history, saved threads, SHA conflict checks, path restrictions, backups and audit.
- Network-isolated, read-only test snapshots using bubblewrap.
- An isolated maintenance workspace; changes do not deploy themselves.

**Check platform access first.** Codex subscription access and ChatGPT custom MCP/write access are separate entitlements. The maintainer's working account is not a guarantee for every Plus account. Consult [current ChatGPT requirements](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt), [Codex plan access](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan), and [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel). No paid upgrade is assumed by this project.

## Install

Tested on Ubuntu 22.04, Python 3.10. Install `python3-venv`, Git and bubblewrap first.

```bash
git clone https://github.com/yiwancibeishang123/codex-local-bridge.git
cd codex-local-bridge
python3 scripts/install-engine.py  # optional, pinned official CLI 0.162.1
bash scripts/install.sh
~/.local/bin/codex-local-bridge login
~/.local/bin/codex-local-bridge doctor
systemctl --user enable --now codex-local-bridge
```

Log into Tailscale, enable HTTPS/Funnel as required, and run `tailscale funnel --bg 8767` after inspecting existing mappings. Set `public_url` to the returned HTTPS origin, restart Bridge, then connect ChatGPT to `<origin>/mcp` using OAuth, scope `bridge`, and the exact approved callback. Complete local `approve-auth` consent. Refresh tool discovery: ten tools should be visible.

The install path must stay in place. Existing config and systemd units are preserved; verify an old unit is not pointing at another checkout.

## Authorize one project

The default demo is read-only. Locally configure `allowed_modes: ["read", "write_test"]`, `file_write_enabled: true`, `project_authorization: true`, narrow include patterns and fixed tests. Add an exact systemd `ReadWritePaths` for projects outside the state directory, reload/restart, then:

```bash
codex-local-bridge grant-project demo
# Review scope and type AUTHORIZE demo in a local interactive terminal.
codex-local-bridge project-policy demo
codex-local-bridge revoke-project demo
```

A project grant persists until revoked, expired or invalidated by scope changes. It does not override ChatGPT's own confirmation requirements. See the full [authorization policy](PROJECT_AUTHORIZATION.md).

## Expected results

A real disposable project was fixed in two turns on the same Codex thread: add passed one test, then multiply passed two tests; source changes were applied. A no-edit task passed its configured tests with `diff=""` and `changes_applied=false`. These are summarized observations, not a claim that CI reproduces a ChatGPT browser session.

`state=succeeded`, `tests_passed` and `changes_applied` are separate. Direct file tools do not run tests. Read `path`, `content`, and raw-byte `sha256` before conflict-safe edits; use `ABSENT` only for a new file.

## Test and contribute

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
# Explicit opt-in, requires an existing local ChatGPT Codex login:
BRIDGE_LIVE_CODEX=1 .venv/bin/python -m pytest tests/test_live_codex.py -q -s
```

Hosted CI is not yet enabled: the publishing credential lacks workflow scope. An opt-in [CI template](ci/README.md) is included; the README badge records local test results.

Read [validation](VALIDATION.md), [security](SECURITY.md), [contribution guidelines](../CONTRIBUTING.md) and [changelog](../CHANGELOG.md). MIT licensed, independent community project. No credentials, local project content or private runtime databases are included.
