"""Read-only, owner-local connection diagnostics for Bridge Studio.

Never perform OAuth login, make unauthenticated claims of remote availability,
invoke unknown project commands, read chat transcripts or display credentials.
All checks are bounded and can be called from a Qt background worker.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
from urllib.parse import urlparse

SERVICE = "codex-local-bridge.service"


def _check_config(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise PermissionError("Bridge config symlink denied")
    info = path.stat()
    if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise PermissionError("Bridge config must be private 0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("state_dir"), str):
        raise ValueError("Bridge config schema not recognized")
    return value


class ConnectionDiagnostics:
    def __init__(self, config_path, *, run=None, connect=None, lookup=None):
        self.config_path = Path(config_path).expanduser().absolute()
        self.run = run or subprocess.run
        self.connect = connect or socket.create_connection
        self.lookup = lookup or shutil.which

    def _command(self, argv, timeout=3):
        try:
            result = self.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout,
                              check=False)
            return result.returncode, result.stdout[:30000]
        except (OSError, subprocess.TimeoutExpired):
            return -1, ""

    def _tcp(self, port):
        try:
            with self.connect(("127.0.0.1", port), timeout=0.6):
                return True
        except OSError:
            return False

    @staticmethod
    def _entry(status, detail, action):
        return {"status": status, "detail": detail, "action": action}

    def inspect(self):
        result = {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "checks": {},
            "summary": {"ready": 0, "attention": 0, "unknown": 0},
            "remote_oauth_verified": False,
            "public_https_verified": False,
        }
        rows = result["checks"]
        try:
            cfg = _check_config(self.config_path)
        except (OSError, ValueError, PermissionError, json.JSONDecodeError):
            rows["bridge_config"] = self._entry(
                "attention", "未发现有效的当前用户私有 Bridge 配置。",
                "在 Ubuntu 本机检查配置文件和 0600 权限；不需要重新登录 Codex。")
            for name in ("bridge_service", "local_mcp", "codex_engine",
                         "codex_memory", "memory_database", "tailscale",
                         "remote_access"):
                rows[name] = self._entry(
                    "unknown", "需要先确认 Bridge 配置。", "先完成 Bridge 本地安装或选择已有配置。")
            result["summary"]["attention"] = 1
            result["summary"]["unknown"] = 7
            return result

        rows["bridge_config"] = self._entry(
            "ready", "本地配置文件权限正确，可读取项目设置。",
            "如需迁移配置，请先做快照，不要上传配置文件。")

        port = cfg.get("port", 8767)
        if type(port) is not int or not 1024 <= port <= 65535:
            rows["local_mcp"] = self._entry("attention", "MCP 端口配置无效。",
                                            "在本地修复 Bridge 配置并重启服务。")
        else:
            up = self._tcp(port)
            rows["local_mcp"] = self._entry(
                "ready" if up else "attention",
                "127.0.0.1 的 MCP TCP 监听可连接。" if up else "MCP 本地监听未响应。",
                "正常情况下无须操作。" if up else "检查 Bridge 用户服务状态和本机日志。")

        if self.lookup("systemctl"):
            rc, _ = self._command(["systemctl", "--user", "is-active", "--quiet", SERVICE])
            rows["bridge_service"] = self._entry(
                "ready" if rc == 0 else "attention",
                "systemd 用户服务处于活动状态。" if rc == 0 else "Bridge 用户服务未处于活动状态。",
                "可在连接中心明确确认后重启 Bridge 用户服务。")
        else:
            rows["bridge_service"] = self._entry(
                "unknown", "此环境没有 systemctl。",
                "在 Ubuntu 用户会话中检查后台服务。")

        command = cfg.get("codex_command")
        binary = command[0] if isinstance(command, list) and command and isinstance(command[0], str) else None
        if binary:
            candidate = Path(binary).expanduser()
            available = (candidate.is_file() and os.access(candidate, os.X_OK)
                         if candidate.is_absolute() else bool(self.lookup(binary)))
        else:
            available = False
        rows["codex_engine"] = self._entry(
            "ready" if available else "attention",
            "Codex 可执行文件已找到；登录与模型调用仍未验证。" if available
            else "Codex 可执行文件路径无效或不存在。",
            "确认 Codex 可执行文件和 VS Code 扩展安装，登录状态需另做真实测试。")

        state = Path(cfg["state_dir"]).expanduser().absolute()
        if (state.is_symlink() or not state.is_dir()
                or state.stat().st_uid != os.getuid()
                or stat.S_IMODE(state.stat().st_mode) != 0o700):
            rows["memory_database"] = self._entry(
                "attention", "Bridge 状态目录缺失或权限不安全。",
                "检查状态目录属于当前用户且权限为 0700。")
        else:
            files = (state / "bridge.sqlite3", state / "studio.sqlite3")
            ok = True
            for file in files:
                try:
                    st = file.lstat()
                    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                            or stat.S_IMODE(st.st_mode) != 0o600):
                        ok = False
                        continue
                    with sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
                        if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                            ok = False
                except (OSError, sqlite3.DatabaseError):
                    ok = False
            rows["memory_database"] = self._entry(
                "ready" if ok else "attention",
                "本地任务和共享记忆数据库检查通过。" if ok
                else "本地数据库缺失、权限不安全或快速检查失败。",
                "先通过升级与恢复页面创建/验证备份，切勿直接删除数据库。")

        # Checking Codex MCP registration cannot verify VS Code extension
        # auto-call behavior or memory contents.
        if binary and available:
            rc, txt = self._command([binary, "mcp", "get", "bridge_memory", "--json"])
            registered = rc == 0
            rows["codex_memory"] = self._entry(
                "ready" if registered else "attention",
                "Codex 已注册 bridge_memory；独立会话是否实际调用尚未验证。" if registered
                else "Codex 尚未注册本地共享记忆 MCP。",
                "可点击“连接 VS Code Codex 记忆”完成注册，再重新加载扩展。")
        else:
            rows["codex_memory"] = self._entry(
                "unknown", "Codex 可执行文件未就绪，无法读取 MCP 注册状态。",
                "先安装或定位 Codex 可执行文件。")

        ts = self.lookup("tailscale")
        if ts:
            rc, output = self._command([ts, "status", "--json"], timeout=4)
            try:
                status = json.loads(output) if rc == 0 else {}
            except (ValueError, TypeError):
                status = {}
            running = status.get("BackendState") == "Running"
            rows["tailscale"] = self._entry(
                "ready" if running else "attention",
                "Tailscale 本地服务已运行。" if running
                else "Tailscale 未登录、已停止，或状态不可读取。",
                "打开 Tailscale/Clash 检查连接，避免盲目重置登录。")
        else:
            rows["tailscale"] = self._entry(
                "attention", "当前环境找不到 tailscale 命令。",
                "确认 Tailscale 已安装，Snap 安装可从终端单独检查。")

        url = cfg.get("public_url")
        parsed = urlparse(url) if isinstance(url, str) else None
        https = bool(parsed and parsed.scheme == "https" and parsed.hostname)
        if https and rows["tailscale"]["status"] == "ready":
            rows["remote_access"] = self._entry(
                "unknown", "已配置 HTTPS 地址并检测到 Tailscale；公网 Funnel、OAuth、ChatGPT 连接尚未实测。",
                "打开 ChatGPT 插件连接检查工具列表；只有真实远端调用能确认连接。")
        else:
            rows["remote_access"] = self._entry(
                "unknown", "公网 HTTPS / ChatGPT OAuth 尚未验证。",
                "先修复 Tailscale 和本地 MCP，再在 ChatGPT 检查已连接插件。")
        for item in rows.values():
            result["summary"][item["status"]] = result["summary"].get(item["status"], 0) + 1
        return result
