#!/usr/bin/env python3
"""Install once, then open Bridge Studio from the Ubuntu Applications menu."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler

MARKER = "Managed by Bridge Studio Desktop"
PORT = 8768
URL = "http://127.0.0.1:8768/"
TITLE = "Bridge · 本地工作室"


def safe_write(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PermissionError("拒绝覆盖非普通文件：" + str(path))
    fd, name = tempfile.mkstemp(prefix=".bridge-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def quoted(value):
    """Quote a Desktop Entry Exec argument, not a shell command."""
    s = str(value)
    if any(c in s for c in ("\n", "\r", "\0", chr(96))):
        raise ValueError("不支持路径中的控制字符")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


class Desktop:
    def __init__(self, root=None, home=None, env=None):
        self.env = dict(os.environ if env is None else env)
        self.home = Path(home or Path.home()).resolve()
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve()
        self.conf = Path(self.env.get("XDG_CONFIG_HOME") or self.home / ".config")
        self.data = Path(self.env.get("XDG_DATA_HOME") or self.home / ".local/share")
        self.bridge_config = Path(self.env.get("BRIDGE_CONFIG") or
                                  self.home / ".config/codex-local-bridge/config.json")
        self.manifest = self.conf / "bridge-studio/desktop.json"
        self.service = self.conf / "systemd/user/bridge-studio.service"
        self.menu = self.data / "applications/bridge-studio.desktop"
        self.key_menu = self.data / "applications/bridge-studio-key.desktop"
        self.launcher = self.home / ".local/bin/bridge-studio-open"
        self.key_launcher = self.home / ".local/bin/bridge-studio-key"
        self.app = self.root / "scripts/bridge_studio.py"
        self.script = self.root / "scripts/studio_desktop.py"
        self.icon = self.root / "docs/assets/bridge-studio-icon.svg"

    def state(self):
        for p in (self.script, self.app, self.root / "bridge/memory.py", self.icon):
            if not p.is_file():
                raise FileNotFoundError("程序文件缺失：" + str(p))
        if self.bridge_config.is_symlink() or not self.bridge_config.is_file():
            raise FileNotFoundError("未找到已部署的 Bridge 配置：" + str(self.bridge_config))
        info = self.bridge_config.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Bridge 配置文件必须为当前用户私有")
        obj = json.loads(self.bridge_config.read_text(encoding="utf-8"))
        state = Path(obj["state_dir"]).expanduser().resolve()
        if not state.is_dir() or not state.is_relative_to(self.home):
            raise PermissionError("Bridge 状态目录必须位于当前用户主目录")
        return state

    def check_owned(self, p):
        if p.is_symlink() or (p.exists() and MARKER not in p.read_text(encoding="utf-8")):
            raise FileExistsError("已有非本产品的同名文件：" + str(p))

    def assets(self, state):
        def unit_path(p):
            s = str(p).replace("%", "%%")
            if any(c in s for c in ("\n", "\r", "\0", '"')):
                raise ValueError("systemd 路径不安全")
            return '"' + s.replace("\\", "\\\\") + '"'
        unit = f"""# {MARKER}
[Unit]
Description=Bridge Studio local control center
After=graphical-session.target
[Service]
Type=simple
WorkingDirectory={unit_path(self.root)}
ExecStart=/usr/bin/python3 {unit_path(self.app)}
Environment="BRIDGE_CONFIG={str(self.bridge_config).replace('%','%%').replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"
Environment=BRIDGE_STUDIO_DESKTOP=1
Environment=PORT={PORT}
UMask=0077
Restart=on-failure
RestartSec=4
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={unit_path(state)} {unit_path(self.bridge_config.parent)}
PrivateTmp=true
NoNewPrivileges=true
[Install]
WantedBy=default.target
"""
        def desktop(name, executable):
            return f"""[Desktop Entry]
# {MARKER}
Type=Application
Version=1.0
Name={name}
Name[zh_CN]={name}
Comment=本地 AI 项目、任务、记忆与连接管理
Exec={quoted(executable)}
Icon={self.icon}
Terminal=false
Categories=Development;Utility;
StartupNotify=true
"""
        def sh(action):
            path = str(self.script)
            if "'" in path or any(c in path for c in ("\n", "\r", "\0")):
                raise ValueError("安装路径不支持单引号或控制字符")
            return ("#!/bin/sh\n# " + MARKER + "\nexec /usr/bin/python3 '" +
                    path + "' " + action + ' "$@"\n')
        return {
            self.service: (unit, 0o600),
            self.menu: (desktop("Bridge Studio", self.launcher), 0o644),
            self.key_menu: (desktop("Bridge Studio · 查看登录密钥", self.key_launcher), 0o644),
            self.launcher: (sh("open"), 0o700),
            self.key_launcher: (sh("key"), 0o700),
        }

    def systemctl(self, *args):
        return subprocess.run(["systemctl", "--user", *args],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=12)

    def install(self, autostart=False, launch=True, dry_run=False):
        state = self.state()
        assets = self.assets(state)
        for p in assets:
            self.check_owned(p)
        if self.manifest.exists():
            self.check_manifest()
            current = json.loads(self.manifest.read_text(encoding="utf-8"))
            if (current["root"] != str(self.root) or
                    current.get("config_path", str(self.bridge_config.resolve())) !=
                    str(self.bridge_config.resolve())):
                raise ValueError("桌面版已使用其他源码/配置路径安装；请先从原目录卸载")
        if dry_run:
            print("将安装桌面图标、启动器、用户服务（不需要 sudo）：")
            for p in (*assets, self.manifest):
                print(" -", p)
            return
        for p, (content, mode) in assets.items():
            safe_write(p, content, mode)
        safe_write(self.manifest, json.dumps({
            "managed_by": MARKER, "root": str(self.root),
            "config_path": str(self.bridge_config.resolve()), "schema": 1,
        }, ensure_ascii=False), 0o600)
        print("✓ 已创建 Ubuntu 应用菜单入口和图标")
        if not (state / "studio-ui.key").exists():
            self.show_key()  # Local owner sees the key once; never put it into URLs.
        if autostart:
            try:
                self.systemctl("daemon-reload")
                result = self.systemctl("enable", "--now", "bridge-studio.service")
                if result.returncode:
                    print("提示：用户 systemd 暂不可用；点击图标时仍可启动")
                else:
                    print("✓ 已启用用户登录后的后台自启动")
            except (OSError, subprocess.TimeoutExpired):
                print("提示：用户 systemd 不可用；点击图标时仍可启动")
        if launch:
            self.open()
        else:
            print("以后直接从 Ubuntu 应用菜单点击 Bridge Studio 即可使用。")

    def check_manifest(self):
        if self.manifest.is_symlink() or not self.manifest.is_file():
            raise RuntimeError("桌面版尚未安装")
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        if data.get("managed_by") != MARKER or data.get("schema") != 1:
            raise PermissionError("桌面安装记录不可信")
        return data

    def as_installed(self):
        """Reconstruct both code and Bridge config paths without inherited shell env."""
        data = self.check_manifest()
        root = data.get("root")
        config = data.get("config_path") or str(self.bridge_config)
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise PermissionError("桌面程序路径不可信")
        if not isinstance(config, str) or not Path(config).is_absolute():
            raise PermissionError("Bridge 配置路径不可信")
        env = dict(self.env, BRIDGE_CONFIG=config)
        return Desktop(root=root, home=self.home, env=env)

    @staticmethod
    def listening():
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=.4):
                return True
        except OSError:
            return False

    @staticmethod
    def ready():
        try:
            # Never let Clash/proxy variables redirect a localhost UI probe.
            with build_opener(ProxyHandler({})).open(URL, timeout=1.5) as response:
                return response.status == 200 and TITLE.encode() in response.read(16384)
        except (OSError, ValueError):
            return False

    def environment(self):
        return dict(self.env, BRIDGE_CONFIG=str(self.bridge_config),
                    BRIDGE_STUDIO_DESKTOP="1", PORT=str(PORT))

    def open(self):
        """Launch a real Qt desktop window, never a browser or webview."""
        installed = self.as_installed()
        if installed.root != self.root or installed.bridge_config != self.bridge_config:
            return installed.open()
        self.state()
        native = self.root / "scripts/bridge_studio_native.py"
        python = self.root / ".venv/bin/python"
        if not native.is_file():
            raise FileNotFoundError("缺少原生 Bridge Studio 程序文件")
        if not python.is_file():
            raise FileNotFoundError("未找到项目虚拟环境，请先按原版说明安装桌面依赖")
        result = subprocess.run(
            [str(python), "-c", "import PySide6.QtWidgets"], capture_output=True,
            text=True, timeout=10, check=False)
        if result.returncode:
            raise RuntimeError("需要安装 PySide6：.venv/bin/python -m pip install -e '.[desktop]'")
        subprocess.Popen(
            [str(python), str(native)],
            cwd=self.root, env=self.environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        print("✓ 已启动 Bridge Studio 原生桌面窗口")

    def show_key(self):
        self.state()
        r = subprocess.run(["/usr/bin/python3", str(self.app), "--show-key"],
                           env=self.environment(), capture_output=True, text=True, timeout=12)
        key = r.stdout.strip()
        if r.returncode or len(key) != 64:
            raise RuntimeError("无法读取 Studio 管理密钥")
        message = ("Bridge Studio 本机登录密钥（请勿转发或存入 GitHub）：\n\n" +
                   key + "\n\n复制后粘贴到浏览器登录界面。")
        gui = shutil.which("zenity")
        if gui and (self.env.get("DISPLAY") or self.env.get("WAYLAND_DISPLAY")):
            try:
                # Secret goes over stdin; never expose it as a process argument.
                subprocess.run([gui, "--text-info", "--title=Bridge Studio 登录密钥",
                                "--width=650", "--height=270"], input=message, text=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        if sys.stdout.isatty():
            print(message)
        else:
            print("无图形密钥窗口；请在 Ubuntu 本机终端运行 python3 scripts/studio_desktop.py key")

    def uninstall(self, dry_run=False):
        self.check_manifest()
        items = self.assets(self.state())
        for p in items:
            self.check_owned(p)
        if dry_run:
            print("将移除启动入口和 systemd 服务；不会删除项目、数据库或凭据")
            return
        try:
            self.systemctl("disable", "--now", "bridge-studio.service")
        except (OSError, subprocess.TimeoutExpired):
            pass
        for p in items:
            p.unlink(missing_ok=True)
        self.manifest.unlink(missing_ok=True)
        try:
            self.systemctl("daemon-reload")
        except (OSError, subprocess.TimeoutExpired):
            pass
        print("✓ 已卸载桌面快捷入口；原 MCP 与 SQLite 数据保持不变")

    def doctor(self):
        self.check_manifest()
        state = self.state()
        print(json.dumps({
            "installed": self.menu.is_file(),
            "studio_local_http": self.ready(),
            "port_conflict": self.listening() and not self.ready(),
            "memory_database": (state / "studio.sqlite3").is_file(),
            "login_key_present": (state / "studio-ui.key").is_file(),
            "public_funnel_exposure": "not_checked",
        }, ensure_ascii=False, indent=2))


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cmd = cli.add_subparsers(dest="action", required=True)
    a = cmd.add_parser("install", help="首次安装：创建应用图标并可选自动启动")
    a.add_argument("--web-backend-autostart", action="store_true",
                   help="可选：同时启动旧版网页后台，原生应用不需要")
    a.add_argument("--no-launch", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    for action in ("open", "key", "doctor", "uninstall"):
        p = cmd.add_parser(action)
        if action == "uninstall":
            p.add_argument("--dry-run", action="store_true")
    args = cli.parse_args(argv)
    app = Desktop()
    if args.action == "install":
        app.install(args.web_backend_autostart, not args.no_launch, args.dry_run)
    elif args.action == "open":
        app.as_installed().open()
    elif args.action == "key":
        app.as_installed().show_key()
    elif args.action == "doctor":
        app.as_installed().doctor()
    else:
        app.as_installed().uninstall(args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Bridge Studio：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
