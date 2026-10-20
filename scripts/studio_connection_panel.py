"""Native Qt connection center with bounded, read-only checks.

The owner can explicitly restart only the known Bridge user service.
No hidden OAuth/token manipulations or automatic Tailscale login.
"""
import json
from pathlib import Path
import subprocess

from PySide6.QtCore import QThread, Signal, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QAbstractItemView, QTextEdit)

from bridge.connection_health import ConnectionDiagnostics

LABELS = {
    "bridge_config": "本机配置", "bridge_service": "Bridge 后端",
    "local_mcp": "本地 MCP", "codex_engine": "Codex 引擎",
    "codex_memory": "Codex 共享记忆", "memory_database": "SQLite 数据",
    "tailscale": "Tailscale", "remote_access": "ChatGPT 远程连接",
}
STATES = {"ready": "● 正常", "attention": "● 需处理", "unknown": "○ 未验证"}


class DiagnosticWorker(QThread):
    done = Signal(object)
    error = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.done.emit(self.operation())
        except Exception as exc:
            self.error.emit(str(exc))


class ConnectionPanel(QWidget):
    status_changed = Signal(object)

    def __init__(self, config_path, *, on_codex_connect=None, parent=None):
        super().__init__(parent)
        self.config_path = Path(config_path)
        self.on_codex_connect = on_codex_connect
        self.worker = None
        self.last_report = None
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        intro = QLabel(
            "自动检测本地配置、Bridge 服务、Codex、SQLite、Tailscale。"
            "注意：本地端口正常不代表 ChatGPT 的 OAuth 已连接。"
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        body.addWidget(intro)
        buttons = QHBoxLayout()
        self.controls = []
        def add(label, handler):
            button = QPushButton(label)
            button.clicked.connect(handler)
            self.controls.append(button)
            buttons.addWidget(button)
        add("↻ 检测全部连接", self.refresh)
        add("重启 Bridge 服务", self.restart_bridge)
        if on_codex_connect:
            add("连接 VS Code Codex 记忆", on_codex_connect)
        body.addLayout(buttons)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["组件", "诊断状态", "发现与建议"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        body.addWidget(self.table, 3)
        self.selected_detail = QTextEdit()
        self.selected_detail.setReadOnly(True)
        self.selected_detail.setMaximumHeight(125)
        self.selected_detail.setPlaceholderText("点击组件查看完整诊断建议。")
        self.table.itemSelectionChanged.connect(self.show_detail)
        body.addWidget(self.selected_detail, 1)
        self.status = QLabel("尚未检查")
        self.status.setObjectName("muted")
        body.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(60000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        # Delay initial subprocess probes until the Qt event loop settles.
        QTimer.singleShot(650, self.refresh)

    def _job(self, operation, success):
        if self.worker is not None:
            return
        for ctrl in self.controls:
            ctrl.setEnabled(False)
        self.worker = DiagnosticWorker(operation, self)
        self.worker.done.connect(success)
        self.worker.error.connect(lambda e: self.status.setText("诊断失败：" + e))
        def end():
            for ctrl in self.controls:
                ctrl.setEnabled(True)
            self.worker.deleteLater()
            self.worker = None
        self.worker.finished.connect(end)
        self.worker.start()

    def refresh(self):
        self.status.setText("正在检查本机连接……")
        self._job(lambda: ConnectionDiagnostics(self.config_path).inspect(), self.display_report)

    def display_report(self, report):
        self.last_report = report
        checks = report["checks"]
        self.table.setRowCount(len(LABELS))
        for index, (key, name) in enumerate(LABELS.items()):
            value = checks.get(key, {"status": "unknown", "detail": "无数据",
                                    "action": "重新检查"})
            for column, content in enumerate((name, STATES.get(value["status"], "○ 未验证"),
                    value["detail"])):
                item = QTableWidgetItem(content)
                item.setData(0x0100, key)
                self.table.setItem(index, column, item)
            self.table.resizeRowToContents(index)
        summary = report["summary"]
        self.status.setText(
            "最近检查：" + report["checked_at"] + " UTC   ·   " +
            str(summary["ready"]) + " 项正常 / " +
            str(summary["attention"]) + " 项待处理 / " +
            str(summary["unknown"]) + " 项待验证")
        self.status_changed.emit(report)

    def show_detail(self):
        row = self.table.currentRow()
        if row < 0 or not self.last_report:
            return
        item = self.table.item(row, 0)
        key = item.data(0x0100) if item else ""
        value = self.last_report["checks"].get(key)
        if value:
            self.selected_detail.setPlainText(
                LABELS.get(key, key) + "\n\n" + value["detail"] +
                "\n\n建议操作：" + value["action"])

    def restart_bridge(self):
        if QMessageBox.question(
            self, "确认重启 Bridge",
            "只重启 codex-local-bridge.service 用户服务。"
            "\n运行中的 Codex 任务可能被中断；不更改 Tailscale、OAuth、项目权限。"
            "\n确定继续吗？"
        ) != QMessageBox.Yes:
            return
        # Fail closed when tasks remain non-terminal; don't break active research.
        try:
            from bridge.core import Bridge, TERMINAL
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            pending = [x for x in Bridge(cfg).store.all("task")
                       if x.get("state") not in TERMINAL]
            if pending:
                raise RuntimeError("检测到 " + str(len(pending)) +
                                   " 个未完成任务，拒绝自动重启。")
        except Exception as exc:
            QMessageBox.warning(self, "Bridge Studio", str(exc))
            return
        def action():
            result = subprocess.run(
                ["systemctl", "--user", "restart", "codex-local-bridge.service"],
                check=False, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=20)
            if result.returncode:
                raise RuntimeError("Bridge 用户服务重启失败，请检查 systemd 用户日志。")
            return True
        def done(_):
            self.status.setText("Bridge 已重启，正在重新检查……")
            QTimer.singleShot(200, self.refresh)
        self._job(action, done)
