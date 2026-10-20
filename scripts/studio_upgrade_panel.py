"""Local-only graphical maintenance page. No API, root shell or browser admin."""
import json
from pathlib import Path
import subprocess
from html import escape

from PySide6.QtCore import QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QComboBox, QTextEdit,
                               QFileDialog, QMessageBox)

from bridge.backend_upgrade import BackendUpgrade
from bridge.snapshots import StateSnapshots


class Operation(QThread):
    success = Signal(object)
    failure = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.success.emit(self.fn())
        except Exception as exc:
            self.failure.emit(str(exc))


class UpgradePanel(QWidget):
    def __init__(self, config_path, parent=None):
        super().__init__(parent)
        self.config_path = Path(config_path)
        self.running = None
        self.setObjectName("upgradeWidget")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        intro = QLabel("① 检查后端   ② 创建私有备份   ③ 选择受信任的本地版本升级   ④ 出现问题时恢复")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        outer.addWidget(intro)
        self.buttons = []

        def button(text, action):
            btn = QPushButton(text)
            btn.clicked.connect(action)
            self.buttons.append(btn)
            return btn

        row = QHBoxLayout()
        row.addWidget(button("检查后端", self.refresh))
        row.addWidget(button("立即备份", self.backup))
        row.addWidget(button("选择新版后端", self.pick_backend))
        row.addWidget(button("打开桌面 .deb", self.pick_deb))
        outer.addLayout(row)

        bar = QHBoxLayout()
        self.snapshots = QComboBox()
        self.snapshots.setMinimumWidth(260)
        bar.addWidget(self.snapshots, 1)
        bar.addWidget(button("验证备份", self.verify))
        bar.addWidget(button("恢复所选备份", self.restore))
        outer.addLayout(bar)

        self.message = QTextEdit()
        self.message.setReadOnly(True)
        self.message.setPlaceholderText("当前本地后端、备份版本、升级和恢复结果将在这里显示。")
        outer.addWidget(self.message, 1)
        warning = QLabel("桌面软件与 MCP 后端分别升级。数据、配置和项目权限仍保存在 Ubuntu 本机；升级不会上传到 GitHub。")
        warning.setObjectName("muted")
        warning.setWordWrap(True)
        outer.addWidget(warning)
        self.refresh()

    def manager(self):
        return BackendUpgrade(self.config_path)

    def idle(self):
        # Stop upgrades/restores when a task is still queued or running.
        from bridge.core import Bridge, TERMINAL
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        pending = [t for t in Bridge(cfg).store.all("task")
                   if t.get("state") not in TERMINAL]
        if pending:
            raise RuntimeError("当前还有 " + str(len(pending)) +
                               " 个待执行或运行中的任务，请先结束或取消。")

    def report(self, obj):
        """Friendly status notes instead of dumping implementation JSON."""
        if not isinstance(obj, dict):
            self.message.append(str(obj))
            return
        if "id" in obj:
            self.message.append("备份编号：" + str(obj["id"])[:16])
        if "files" in obj:
            value = obj["files"]
            self.message.append("已保存：" + "、".join(value if isinstance(value, list) else value.keys()))
        if "candidate" in obj:
            info = obj["candidate"]
            self.message.append("待安装版本：" + str(info.get("version","未知")))
            self.message.append("代码提交：" + str(info.get("revision","未知")))
        if "version" in obj:
            self.message.append("当前版本：" + str(obj["version"]))
        if "restored" in obj:
            self.message.append("已恢复备份：" + str(obj["restored"])[:16])
        if "verified" in obj:
            self.message.append("验证结果：" + ("成功" if obj["verified"] else "未通过"))
        if "snapshot" in obj:
            self.message.append("可回退备份：" + str(obj["snapshot"])[:16])
        if not any(k in obj for k in ("id","files","candidate","version","restored","verified","snapshot")):
            self.message.append("操作已完成，可检查下方状态。")

    def alert(self, message):
        QMessageBox.warning(self, "Bridge Studio", str(message))

    def refresh(self):
        try:
            manager = self.manager()
            old = self.snapshots.currentData()
            backups = manager.snapshots.list()
            self.snapshots.clear()
            for backup in backups:
                mark = "✓" if backup.get("verified") else "⚠"
                self.snapshots.addItem(mark + " " + backup["id"][:16], backup["id"])
            index = self.snapshots.findData(old)
            if index >= 0:
                self.snapshots.setCurrentIndex(index)
            state = manager.current()
            runtime = state["working_directory"]
            self.message.setHtml(
                "<h2 style='color:#25375a'>Bridge 工作状态</h2>"
                "<p><b style='color:#177a60'>● 已发现本机后端服务</b></p>"
                "<p><b>当前后端位置</b><br>" + escape(runtime) + "</p>"
                "<p><b>可恢复的本机备份</b><br>"
                + str(len(backups)) + " 份</p>"
                "<p><b>数据存储位置</b><br>"
                + escape(str(manager.snapshots.state)) + "</p>"
                "<p style='color:#758198'>此处只验证本机管理信息。"
                "要确认 ChatGPT 网页端可用，请到“网页端协作”页进行真实读写测试。</p>")
        except Exception as exc:
            self.message.setPlainText("无法读取后端： " + str(exc))

    def run_job(self, title, fn, next_step=None):
        if self.running is not None:
            return self.alert("维护操作正在执行，请勿重复点击。")
        self.message.append("\n◷ " + title + "……")
        for button in self.buttons:
            button.setEnabled(False)
        worker = Operation(fn, self)
        self.running = worker
        def success(result):
            self.message.append("✓ " + title + "：")
            self.report(result)
            if next_step:
                next_step(result)
        def fail(reason):
            self.message.append("✕ " + title + "：" + reason)
            self.alert(reason)
        def cleanup():
            for button in self.buttons:
                button.setEnabled(True)
            self.running = None
            worker.deleteLater()
            # Do not overwrite the completion report with an automatic refresh.
            self._refresh_backups()
        worker.success.connect(success)
        worker.failure.connect(fail)
        worker.finished.connect(cleanup)
        worker.start()

    def _refresh_backups(self):
        try:
            old = self.snapshots.currentData()
            items = self.manager().snapshots.list()
            self.snapshots.clear()
            for item in items:
                mark = "✓" if item.get("verified") else "⚠"
                self.snapshots.addItem(mark + " " + item["id"][:16], item["id"])
            pos = self.snapshots.findData(old)
            if pos >= 0:
                self.snapshots.setCurrentIndex(pos)
        except Exception:
            pass

    def backup(self):
        self.run_job("SQLite 一致性备份",
                     lambda: StateSnapshots(self.config_path).create())

    def verify(self):
        sid = self.snapshots.currentData()
        if not sid:
            return self.alert("请选择备份")
        self.run_job("备份 SHA256 与 SQLite 完整性验证",
                     lambda: {"id": sid, "verified": True,
                              "manifest": StateSnapshots(self.config_path).verify(sid)})

    def restore(self):
        sid = self.snapshots.currentData()
        if not sid:
            return self.alert("请选择备份")
        try:
            self.idle()
        except Exception as exc:
            return self.alert(exc)
        if QMessageBox.question(
            self, "确认数据恢复",
            "将停止本地 Bridge 服务，从选定时间点恢复配置、SQLite 和用户服务文件。"
            "\n恢复前会另存一份当前状态以便后悔。"
            "\n请确认全部 Codex 任务已经结束。\n\n确定恢复 " + sid[:16] + " 吗？"
        ) != QMessageBox.Yes:
            return
        self.run_job("恢复数据并重新验证 MCP",
                     lambda: self.manager().rollback(sid, confirmed=True))

    def pick_backend(self):
        path = QFileDialog.getExistingDirectory(self, "选择已在本地安装的新 Bridge Git 检出")
        if not path:
            return
        try:
            self.idle()
        except Exception as exc:
            return self.alert(exc)
        self.run_job("候选后端安全预检",
                     lambda: self.manager().preflight(path),
                     next_step=lambda plan: self.confirm_backend(path, plan))

    def confirm_backend(self, path, plan):
        info = plan["candidate"]
        if QMessageBox.question(
            self, "确认受控后端升级",
            "受信任 Git 检出：" + info["root"] +
            "\n版本：" + info["version"] + "  提交：" + info["revision"] +
            "\n\n下一步将保存私有备份、切换用户 systemd 服务、"
            "验证本地监听；失败时尝试自动回滚。"
            "\n\n只有确认你信任这个版本及其依赖后，才允许升级。"
        ) != QMessageBox.Yes:
            return
        # Executed asynchronously to keep the Qt UI responsive.
        # next_step is called from the main thread on the signal delivery.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self.run_job(
            "后端升级和回滚保护", lambda: self.manager().install(path, confirmed=True)))

    def pick_deb(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择可信任的 Bridge Studio .deb", str(Path.home() / "Downloads"),
            "Debian packages (*.deb)")
        if not filename:
            return
        try:
            package = Path(filename).resolve(strict=True)
            if not package.is_file() or package.stat().st_size > 600_000_000:
                raise ValueError("非法或过大的安装包")
            result = subprocess.run(
                ["dpkg-deb", "--field", str(package), "Package", "Version", "Architecture"],
                capture_output=True, text=True, timeout=8)
            fields = result.stdout.splitlines()
            if (result.returncode != 0 or len(fields) < 3 or
                fields[0].strip() != "bridge-studio-native" or
                fields[2].strip() != "amd64"):
                raise ValueError("不是 Bridge Studio Ubuntu amd64 安装包")
            if QMessageBox.question(
                self, "启动 Ubuntu 软件安装器",
                "包名：" + fields[0] + "\n版本：" + fields[1] +
                "\n\n注意：包名验证不代表数字签名验证。仅安装来自自己信任的 GitHub 构建。"
                "\n是否调用 Ubuntu 软件安装器？"
            ) != QMessageBox.Yes:
                return
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(package))):
                raise RuntimeError("未找到可处理 .deb 的 Ubuntu 软件安装器")
        except Exception as exc:
            self.alert(exc)
