#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ ! -f "$HOME/.config/codex-local-bridge/config.json" && -z "${BRIDGE_CONFIG:-}" ]]; then
  echo "原 Codex Local Bridge 尚未配置。请先安装稳定版 Bridge。" >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "未找到本地 Python 虚拟环境：请先按 Bridge README 完成初次安装。" >&2
  exit 1
fi
echo "正在安装 Bridge Studio 原生 Qt 桌面依赖（首次安装需要下载 PySide6）..."
.venv/bin/python -m pip install -e '.[desktop]'
echo "正在配置 Ubuntu 应用程序菜单..."
python3 scripts/studio_desktop.py install
echo "完成。以后按 Super 键搜索 Bridge Studio，点击图标即可打开。"
