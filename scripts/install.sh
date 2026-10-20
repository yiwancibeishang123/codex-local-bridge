#!/usr/bin/env bash
set -euo pipefail
umask 077
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
for executable in python3 git bwrap; do
  if ! command -v "$executable" >/dev/null; then
    echo "Missing: $executable. No system software will be installed automatically. See README prerequisites."
    exit 1
  fi
done

# VS Code bundles the official Codex executable but does not add it to PATH.
# Prefer an explicit PATH binary, then discover the newest bundled executable.
codex_bin="$HOME/.local/share/codex-local-bridge/engine-cli/0.162.1/bin/codex"
if [[ ! -x "$codex_bin" ]]; then codex_bin="$(command -v codex || true)"; fi
if [[ -z "$codex_bin" ]]; then
  while IFS= read -r candidate; do
    if [[ -x "$candidate" ]]; then codex_bin="$candidate"; break; fi
  done < <(find "$HOME/.vscode/extensions" "$HOME/.vscode-server/extensions" \
      -type f -path '*/openai.chatgpt-*/bin/*/codex' -print 2>/dev/null | sort -V -r)
fi
if [[ -z "$codex_bin" ]]; then
  echo "Missing Codex executable. Keep the VS Code extension and expose its bundled binary, or install the official CLI; see README prerequisites."
  exit 1
fi
if ! command -v tailscale >/dev/null; then
  echo "Tailscale is not installed. Continuing with localhost setup; install/login to Tailscale before ChatGPT web connection."
fi
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
mkdir -p "$HOME/.config/codex-local-bridge" "$HOME/.local/share/codex-local-bridge" "$HOME/codex-bridge-demo"
mkdir -p "$HOME/.local/bin"
ln -sfn "$project_dir/.venv/bin/codex-local-bridge" "$HOME/.local/bin/codex-local-bridge"
if [[ ! -f "$HOME/.config/codex-local-bridge/config.json" ]]; then
  default_url="http://127.0.0.1:8767"
  if command -v tailscale >/dev/null; then default_url=""; fi
  if [[ -n "$default_url" ]]; then
    read -r -p "Tailscale HTTPS origin (optional, press Enter for localhost): " bridge_public_url
    bridge_public_url="${bridge_public_url:-$default_url}"
  else
    read -r -p "Paste the Tailscale HTTPS origin (https://device.tailnet.ts.net): " bridge_public_url
  fi
  BRIDGE_SETUP_URL="$bridge_public_url" CODEX_BIN="$codex_bin" .venv/bin/python - <<'PY'
import json,os,shutil
from pathlib import Path
from bridge.cli import DEFAULT_FLAGS
from urllib.parse import urlparse
url=os.environ['BRIDGE_SETUP_URL'].rstrip('/')
u=urlparse(url)
if not u.hostname or u.path or u.query or (u.scheme!='https' and not (u.scheme=='http' and u.hostname in ('127.0.0.1','localhost'))):
 raise SystemExit('Expected a Tailscale HTTPS origin or http://127.0.0.1 for local setup')
config=json.loads(Path('config.example.json').read_text())
config.update(state_dir=str(Path.home()/'.local/share/codex-local-bridge'),public_url=url,
              codex_command=[os.environ.get('CODEX_BIN') or shutil.which('codex') or '',*DEFAULT_FLAGS])
config['projects'][0]['root']=str(Path.home()/'codex-bridge-demo')
p=Path.home()/'.config/codex-local-bridge/config.json';p.write_text(json.dumps(config,indent=2));p.chmod(0o600)
PY
fi
mkdir -p "$HOME/.config/systemd/user"
if [[ -f "$HOME/.config/systemd/user/codex-local-bridge.service" ]]; then
  echo "Existing systemd unit preserved. New template at scripts/codex-local-bridge.service.in"
else
  BRIDGE_SETUP_ROOT="$project_dir" .venv/bin/python - <<'PY'
import os
from pathlib import Path
root=os.environ['BRIDGE_SETUP_ROOT']
if any(x in root for x in ('\n','"','%','\\')): raise SystemExit('Unsupported install path')
text=Path('scripts/codex-local-bridge.service.in').read_text().replace('@ROOT@',root)
(Path.home()/'.config/systemd/user/codex-local-bridge.service').write_text(text)
PY
fi
if command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null; then
  :
else
  echo "Warning: user systemd is unavailable in this session; unit was written but not loaded."
fi
if [[ ! -d "$HOME/codex-bridge-demo/.git" ]]; then .venv/bin/codex-local-bridge init-demo; fi
echo "Installed. Keep this directory in place. No public exposure has been enabled."
echo "Next: $HOME/.local/bin/codex-local-bridge login"
echo "Then: $HOME/.local/bin/codex-local-bridge doctor"
echo "Start: systemctl --user enable --now codex-local-bridge"
echo "After reviewing README: tailscale funnel --bg 8767"
