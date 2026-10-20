#!/usr/bin/env python3
"""Bridge Studio Native — Qt Widgets desktop control center, no webview/browser.

Authentication: the current Ubuntu login user. Existing OAuth MCP remains
independent and enforces its own remote authorization. Qt is an optional desktop
dependency, loaded only on actual native launch.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.project_modes import ProjectModes, project_mode
from bridge.memory import MemoryStore


def config_path():
    return Path(os.environ.get("BRIDGE_CONFIG") or
                Path.home() / ".config/codex-local-bridge/config.json")


def status_color(value):
    return "#11836c" if value in ("succeeded", "running", "online", "write") else (
        "#bf7500" if value in ("queued", "read", "unknown") else "#ae4058")


def main():
    try:
        from PySide6.QtCore import Qt, QTimer, QSize
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        from PySide6.QtGui import QFont, QIcon
        from PySide6.QtWidgets import (
            QApplication, QWidget, QMainWindow, QHBoxLayout, QVBoxLayout,
            QLabel, QPushButton, QComboBox, QLineEdit, QTextEdit, QListWidget,
            QTableWidget, QTableWidgetItem, QAbstractItemView, QSplitter,
            QFileDialog, QInputDialog, QMessageBox, QStackedWidget,
            QFrame, QScrollArea, QFormLayout, QHeaderView, QCheckBox,
        )
    except ImportError as exc:
        raise SystemExit(
            "原生 Qt 依赖加载失败：" + str(exc) + "。首次安装请运行："
            " .venv/bin/python -m pip install -e '.[desktop]'") from exc

    from scripts.bridge_studio import Studio
    from bridge.core import Bridge
    from bridge.handoff import build_task_receipt, build_project_handoff
    from bridge.security import checked
    from bridge.filetools import _check_path, _read_at

    app = QApplication(sys.argv)
    app.setApplicationName("Bridge Studio")
    app.setOrganizationName("Codex Local Bridge")
    app.setStyle("Fusion")
    app.setFont(QFont("Noto Sans CJK SC", 10))
    root = Path(__file__).resolve().parents[1]
    icon = root / "docs/assets/bridge-studio-icon.svg"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))

    STYLE = """
    QWidget { color:#202c46; background:#f5f7fb; font-size:13px; }
    QMainWindow { background:#f5f7fb; }
    QFrame#sidebar { background:#141f3b; border-radius:0px; }
    QFrame#sidebar QLabel { color:#f0f3ff; background:transparent; }
    QPushButton#nav { text-align:left; padding:13px 16px; color:#aab7d2;
        background:transparent; border:0px; border-radius:11px; font-weight:600; }
    QPushButton#nav:hover { background:#25365e; color:#fff; }
    QPushButton#nav:checked { background:#485bc5; color:white; }
    QFrame#card { background:white; border:1px solid #e1e7f1; border-radius:14px; }
    QLabel#pageTitle { font-size:24px; font-weight:750; }
    QLabel#muted { color:#76839a; }
    QLabel#headline { font-size:18px; font-weight:700; }
    QPushButton { color:#253754; background:white; border:1px solid #cbd5e5;
        border-radius:9px; padding:9px 13px; font-weight:600; }
    QPushButton:hover { background:#edf1fc; border-color:#8999d5; }
    QPushButton#primary { color:white; background:#5366d5; border-color:#5366d5; }
    QPushButton#primary:hover { background:#4556c3; }
    QLineEdit, QTextEdit, QComboBox, QListWidget, QTableWidget {
        background:white; border:1px solid #d9e0ed; border-radius:9px;
        padding:6px; selection-background-color:#e5eaff; selection-color:#243569; }
    QTableWidget { gridline-color:#e8edf4; }
    QHeaderView::section { color:#5f6f8b; padding:10px; background:#edf1f8; border:0px; }
    QScrollBar:vertical { background:#f5f7fb; width:11px; }
    QScrollBar::handle:vertical { background:#c4cde2; border-radius:5px; }
    """

    class Workspace(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Bridge Studio · AI 协作工作台")
            self.resize(1320, 820)
            self.setMinimumSize(1020, 640)
            self.setStyleSheet(STYLE)
            self.studio = Studio(config_path())
            self.modes = ProjectModes(self.studio.config_path)
            self.bridge = None
            self.tabs = QStackedWidget()
            self.navs = []
            self.current_project = ""
            self.note_ids = []
            self.build()
            self.reload()
            self.timer = QTimer(self)
            self.timer.setInterval(8000)
            self.timer.timeout.connect(self.poll)
            self.timer.start()

        def btn(self, title, func, primary=False):
            w = QPushButton(title)
            if primary:
                w.setObjectName("primary")
            w.clicked.connect(func)
            return w

        def card(self, parent, title, subtitle=None):
            box = QFrame()
            box.setObjectName("card")
            layout = QVBoxLayout(box)
            layout.setContentsMargins(18, 17, 18, 17)
            header = QLabel(title)
            header.setObjectName("headline")
            layout.addWidget(header)
            if subtitle:
                hint = QLabel(subtitle)
                hint.setObjectName("muted")
                hint.setWordWrap(True)
                layout.addWidget(hint)
            parent.addWidget(box)
            return layout

        def panel(self, title, subtitle):
            panel = QWidget()
            outer = QVBoxLayout(panel)
            outer.setContentsMargins(24, 22, 24, 20)
            h = QLabel(title)
            h.setObjectName("pageTitle")
            outer.addWidget(h)
            sub = QLabel(subtitle)
            sub.setObjectName("muted")
            outer.addWidget(sub)
            outer.addSpacing(16)
            self.tabs.addWidget(panel)
            return outer

        def build(self):
            base = QWidget()
            self.setCentralWidget(base)
            columns = QHBoxLayout(base)
            columns.setContentsMargins(0, 0, 0, 0)
            columns.setSpacing(0)
            left = QFrame()
            left.setObjectName("sidebar")
            left.setFixedWidth(228)
            nav = QVBoxLayout(left)
            nav.setContentsMargins(20, 28, 20, 20)
            product = QLabel("◈  Bridge Studio")
            product.setStyleSheet("font-size:19px; font-weight:800;")
            nav.addWidget(product)
            subtitle = QLabel("PRIVATE AI WORKSPACE")
            subtitle.setStyleSheet("font-size:10px; color:#8797c0;")
            nav.addWidget(subtitle)
            nav.addSpacing(36)
            pages = ["⌂   工作台总览", "▤   项目与权限", "▶   Codex 任务",
                     "✦   长期共享记忆", "◎   文件浏览", "◉   连接与诊断"]
            for index, title in enumerate(pages):
                button = QPushButton(title)
                button.setObjectName("nav")
                button.setCheckable(True)
                button.clicked.connect(lambda _=False, i=index: self.go(i))
                self.navs.append(button)
                nav.addWidget(button)
            nav.addStretch()
            line = QLabel("LOCAL FIRST  ·  OFFLINE STORAGE")
            line.setStyleSheet("color:#7e90b7; font-size:10px;")
            nav.addWidget(line)
            columns.addWidget(left)
            columns.addWidget(self.tabs, 1)

            p = self.panel("工作台总览", "您的项目、Codex 任务和记忆都保存在本机。")
            row = QHBoxLayout()
            self.kpi = []
            for text in ("项目", "任务", "记忆", "本地 MCP"):
                c = self.card(row, text)
                value = QLabel("—")
                value.setStyleSheet("font-size:26px;font-weight:800;color:#4358ba;")
                c.addWidget(value)
                self.kpi.append(value)
            p.addLayout(row)
            actions = self.card(p, "开始工作", "直接选择项目、提交任务或打开共享记忆，不需要终端。")
            bar = QHBoxLayout()
            bar.addWidget(self.btn("添加项目", self.add_project, True))
            bar.addWidget(self.btn("发起 Codex 任务", lambda: self.go(2)))
            bar.addWidget(self.btn("搜索历史记忆", lambda: self.go(3)))
            actions.addLayout(bar)
            p.addStretch()

            p = self.panel("项目与权限", "权限在本机确认后生效。关闭项目会阻止远程 MCP 继续访问。")
            toolbar = QHBoxLayout()
            self.projects = QTableWidget(0, 4)
            self.projects.setHorizontalHeaderLabels(["项目", "本地目录", "权限档位", "状态"])
            self.projects.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            self.projects.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.projects.setEditTriggers(QAbstractItemView.NoEditTriggers)
            toolbar.addWidget(self.btn("➕ 添加本地项目", self.add_project, True))
            toolbar.addWidget(self.btn("刷新", self.reload))
            p.addLayout(toolbar)
            p.addWidget(self.projects, 1)
            line = QHBoxLayout()
            self.mode = QComboBox()
            self.mode.addItems(["关闭 · 不允许访问", "只读 · 浏览与分析", "读写 · 测试后写回"])
            line.addWidget(self.mode, 1)
            line.addWidget(self.btn("应用所选项目权限", self.change_permission, True))
            p.addLayout(line)
            note = QLabel("读写权限仅对已配置安全测试的项目开放；敏感文件仍需单独审批。")
            note.setObjectName("muted")
            p.addWidget(note)

            p = self.panel("Codex 任务", "查看实际测试和写回结果，并从桌面发起本地任务。")
            row = QHBoxLayout()
            self.task_project = QComboBox()
            row.addWidget(self.task_project, 2)
            self.task_mode = QComboBox()
            self.task_mode.addItems(["只读分析", "编程并测试"])
            row.addWidget(self.task_mode)
            row.addWidget(self.btn("提交任务", self.send_task, True))
            p.addLayout(row)
            self.prompt = QTextEdit()
            self.prompt.setPlaceholderText("告诉 Codex 要完成的任务，例如：检查代码结构、定位崩溃原因或修复指定函数……")
            self.prompt.setMaximumHeight(115)
            p.addWidget(self.prompt)
            row = QHBoxLayout()
            self.task_list = QListWidget()
            self.task_list.currentRowChanged.connect(self.show_task)
            self.task_detail = QTextEdit()
            self.task_detail.setReadOnly(True)
            row.addWidget(self.task_list, 2)
            row.addWidget(self.task_detail, 3)
            p.addLayout(row, 1)
            row2 = QHBoxLayout()
            row2.addWidget(self.btn("刷新任务", self.refresh_tasks))
            row2.addWidget(self.btn("复查任务状态", self.show_task_current))
            p.addLayout(row2)

            p = self.panel("长期共享记忆", "同一个 SQLite 数据库供网页 MCP 和 Bridge 委派的 Codex 使用。")
            row = QHBoxLayout()
            self.memory_project = QComboBox()
            self.memory_project.currentIndexChanged.connect(self.refresh_memory)
            self.memory_search = QLineEdit()
            self.memory_search.setPlaceholderText("搜索决策、问题、实验结论……")
            self.memory_search.returnPressed.connect(self.refresh_memory)
            row.addWidget(self.memory_project)
            row.addWidget(self.memory_search, 2)
            row.addWidget(self.btn("搜索", self.refresh_memory, True))
            p.addLayout(row)
            split = QHBoxLayout()
            self.memory_list = QListWidget()
            self.memory_list.currentRowChanged.connect(self.view_note)
            self.memory_text = QTextEdit()
            self.memory_text.setPlaceholderText("选择左侧记忆，或创建一条经过确认的项目知识。")
            split.addWidget(self.memory_list, 2)
            split.addWidget(self.memory_text, 3)
            p.addLayout(split, 1)
            tools = QHBoxLayout()
            tools.addWidget(self.btn("新建记忆", self.new_note))
            tools.addWidget(self.btn("保存修改", self.save_note, True))
            tools.addWidget(self.btn("归档记忆", self.archive_note))
            tools.addWidget(self.btn("查看交接上下文", self.handoff))
            p.addLayout(tools)
            self.selected_note = None

            p = self.panel("文件浏览", "仅浏览已授权项目中的文本文件，不展示隐藏凭据目录。")
            self.file_project = QComboBox()
            self.file_project.currentIndexChanged.connect(self.refresh_files)
            p.addWidget(self.file_project)
            box = QHBoxLayout()
            self.file_list = QListWidget()
            self.file_list.currentRowChanged.connect(self.show_file)
            self.file_preview = QTextEdit()
            self.file_preview.setReadOnly(True)
            box.addWidget(self.file_list, 2)
            box.addWidget(self.file_preview, 4)
            p.addLayout(box, 1)

            p = self.panel("连接与诊断", "真实检查本机 MCP 与 Tailscale 状态，无法确认的状态标记为未知。")
            self.health = QTextEdit()
            self.health.setReadOnly(True)
            p.addWidget(self.health, 1)
            p.addWidget(self.btn("重新检测连接", self.refresh_health, True))
            self.go(0)

        def go(self, i):
            self.tabs.setCurrentIndex(i)
            for index, button in enumerate(self.navs):
                button.setChecked(index == i)

        def alert(self, message, error=False):
            (QMessageBox.warning if error else QMessageBox.information)(
                self, "Bridge Studio", str(message))

        def active_id(self, combo):
            return combo.currentData()

        def reload(self):
            try:
                conf = self.studio.config()
                entries = conf.get("projects", [])
                self.projects.setRowCount(len(entries))
                for i, item in enumerate(entries):
                    mode = project_mode(item)
                    values = (item.get("name", item["id"]), item.get("root", ""),
                              {"off":"关闭", "read":"只读", "write":"读写"}[mode],
                              "已关闭" if mode == "off" else "已配置")
                    for j, value in enumerate(values):
                        self.projects.setItem(i, j, QTableWidgetItem(value))
                    self.projects.item(i, 0).setData(Qt.UserRole, item["id"])
                for combo in (self.task_project, self.memory_project, self.file_project):
                    old = combo.currentData()
                    combo.blockSignals(True)
                    combo.clear()
                    for item in entries:
                        if item.get("enabled") is False:
                            continue
                        combo.addItem(item.get("name", item["id"]), item["id"])
                    index = combo.findData(old)
                    combo.setCurrentIndex(index if index >= 0 else 0)
                    combo.blockSignals(False)
                self.kpi[0].setText(str(sum(x.get("enabled") is not False for x in entries)))
                self.refresh_tasks()
                self.refresh_memory()
                self.refresh_files()
                self.refresh_health()
            except Exception as exc:
                self.alert(exc, True)

        def add_project(self):
            folder = QFileDialog.getExistingDirectory(self, "选择本地项目目录")
            if not folder:
                return
            default = Path(folder).name.lower().replace("-", "_").replace(" ", "_")
            name, ok = QInputDialog.getText(self, "项目标识", "项目 ID（英文字母/数字/下划线）：", text=default)
            if not ok or not name:
                return
            if QMessageBox.question(self, "确认读取授权",
                 "将此目录添加到 ChatGPT 可读取的项目范围中？\n" + folder
                 ) != QMessageBox.Yes:
                return
            try:
                helper = self.studio.config_path.parent
                # Reuse the existing hardened local register helper, no shell.
                command = [sys.executable, str(root / "scripts/add_readonly_project.py"),
                           name, folder, "--yes"]
                result = subprocess.run(command, env=dict(os.environ, BRIDGE_CONFIG=str(self.studio.config_path)),
                                        text=True, capture_output=True, timeout=45, check=False)
                if result.returncode:
                    raise RuntimeError(result.stderr.strip() or "项目注册失败")
                self.reload()
            except Exception as exc:
                self.alert(exc, True)

        def change_permission(self):
            row = self.projects.currentRow()
            if row < 0:
                return self.alert("请先选中项目。")
            pid = self.projects.item(row, 0).data(Qt.UserRole)
            target = ("off", "read", "write")[self.mode.currentIndex()]
            if QMessageBox.question(
                self, "确认本机权限变更",
                f"项目：{pid}\n新权限：{target}\n\n此操作将重启 Bridge MCP 服务；"
                "写入权限只用于已配置测试的项目。是否继续？",
            ) != QMessageBox.Yes:
                return
            try:
                receipt = self.modes.change(pid, target, confirmed=True)
                self.reload()
                self.alert("权限修改成功：" + receipt["mode"])
            except Exception as exc:
                self.alert(str(exc), True)

        def send_task(self):
            pid = self.active_id(self.task_project)
            text = self.prompt.toPlainText().strip()
            if not pid or not text:
                return self.alert("请选择项目并输入任务内容。")
            if QMessageBox.question(self, "确认任务", "将任务提交到本地 Codex？") != QMessageBox.Yes:
                return
            try:
                bridge = Bridge(self.studio.config())
                mode = "read" if self.task_mode.currentIndex() == 0 else "write_test"
                result = bridge.submit(pid, text, mode)
                self.alert("已提交：" + result["id"][:12] + "\n状态：" + result["state"])
                self.refresh_tasks()
            except Exception as exc:
                self.alert(exc, True)

        def refresh_tasks(self):
            try:
                data = self.studio.tasks()
                self.task_records = data["items"]
                self.task_list.clear()
                for item in self.task_records:
                    self.task_list.addItem(
                        f"{item.get('state','unknown'):>9} · {item.get('project_id','')} · "
                        f"{str(item.get('id',''))[:12]}")
                self.kpi[1].setText(str(len(self.task_records)))
            except Exception:
                pass

        def show_task_current(self):
            self.show_task(self.task_list.currentRow())

        def show_task(self, index):
            if index < 0 or index >= len(getattr(self, "task_records", [])):
                return
            tid = self.task_records[index]["id"]
            try:
                data = self.studio.task_detail(tid)
                self.task_detail.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
            except Exception as exc:
                self.task_detail.setPlainText("读取任务详情失败：" + str(exc))

        def refresh_memory(self):
            pid = self.active_id(self.memory_project)
            self.notes = []
            self.memory_list.clear()
            self.selected_note = None
            if not pid:
                return
            try:
                term = self.memory_search.text().strip()
                self.notes = (self.studio.memory.search_notes(pid, term, limit=100)
                              if term else self.studio.memory.list_notes(pid, limit=100))
                for note in self.notes:
                    self.memory_list.addItem(note["title"])
                self.kpi[2].setText(str(len(self.notes)))
            except Exception:
                self.kpi[2].setText("?")

        def view_note(self, row):
            if 0 <= row < len(self.notes):
                self.selected_note = self.notes[row]
                self.memory_text.setPlainText(self.selected_note["body"])

        def new_note(self):
            self.selected_note = None
            self.memory_list.clearSelection()
            self.memory_text.clear()
            self.memory_text.setFocus()

        def save_note(self):
            pid = self.active_id(self.memory_project)
            if not pid:
                return
            existing = self.selected_note
            title = existing["title"] if existing else ""
            name, ok = QInputDialog.getText(self, "记忆标题", "输入知识/决策标题", text=title)
            if not ok or not name.strip():
                return
            try:
                keyword = existing["id"] if existing else None
                self.studio.memory.upsert_note(
                    pid, title=name.strip(), body=self.memory_text.toPlainText(),
                    kind=existing["kind"] if existing else "knowledge",
                    origin="studio", source="desktop-review",
                    provenance="Ubuntu 本地 Bridge Studio 原生桌面端操作",
                    note_id=keyword,
                    expected_revision=existing["revision"] if existing else None)
                self.refresh_memory()
            except Exception as exc:
                self.alert(exc, True)

        def archive_note(self):
            pid = self.active_id(self.memory_project)
            if not self.selected_note or not pid:
                return
            if QMessageBox.question(self, "归档记忆",
                    "归档当前记忆？这不会删除其他项目的记录。") != QMessageBox.Yes:
                return
            try:
                self.studio.memory.archive_note(
                    pid, self.selected_note["id"],
                    expected_revision=self.selected_note["revision"])
                self.refresh_memory()
            except Exception as exc:
                self.alert(exc, True)

        def handoff(self):
            pid = self.active_id(self.memory_project)
            if not pid:
                return
            try:
                bridge = Bridge(self.studio.config())
                context = build_project_handoff(bridge, pid, limit=6)
                self.memory_text.setPlainText(
                    json.dumps(context, indent=2, ensure_ascii=False))
            except Exception as exc:
                self.alert(exc, True)

        def refresh_files(self):
            pid = self.active_id(self.file_project)
            self.file_list.clear()
            self.file_preview.clear()
            self.file_paths = []
            if not pid:
                return
            try:
                conf = self.studio.config()
                item = next(p for p in conf["projects"] if p["id"] == pid)
                project_root = Path(item["root"]).resolve()
                # Bounded top-level preview; no implicit full disk scans.
                for file in sorted(project_root.iterdir()):
                    if len(self.file_paths) >= 100:
                        break
                    if file.name.startswith(".") or not file.is_file() or file.is_symlink():
                        continue
                    try:
                        checked(project_root, file.name, item["include"])
                    except ValueError:
                        continue
                    self.file_paths.append(file.name)
                    self.file_list.addItem(file.name)
            except Exception:
                pass

        def show_file(self, index):
            pid = self.active_id(self.file_project)
            if not pid or not 0 <= index < len(self.file_paths):
                return
            try:
                record = self.studio.project(pid)
                from bridge.filetools import _redact
                blob, _ = _read_at(Path(record["root"]), _check_path(self.file_paths[index]))
                if blob is None or len(blob) > 250_000:
                    raise ValueError("只支持不超过 250KB 的文本文件")
                text = blob.decode("utf-8")
                if _redact(text)[1]:
                    raise PermissionError("文件内容疑似含凭据，不显示")
                self.file_preview.setPlainText(text)
            except Exception as exc:
                self.file_preview.setPlainText("无法读取：" + str(exc))

        def refresh_health(self):
            import socket
            result = {}
            for port, name in ((8767,"MCP"), (8768,"旧版 Studio Web")):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        result[name] = "本地监听正常"
                except OSError:
                    result[name] = "未监听/未知"
            result["管理窗口"] = "Qt 原生窗口 · Ubuntu 当前登录用户"
            result["GitHub 同步"] = "按需执行 Git；项目记忆不上传云端"
            result["数据库"] = str(self.studio.memory.path)
            result["桥接服务"] = str(self.studio.config_path)
            result["记忆机制"] = "Bridge 任务自动保存状态 + 委派前项目检索"
            self.health.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            self.kpi[3].setText("在线" if result["MCP"] == "本地监听正常" else "未知")

        def poll(self):
            if self.tabs.currentIndex() in (0, 2):
                self.refresh_tasks()
            if self.tabs.currentIndex() == 5:
                self.refresh_health()

    # A second dock click activates the existing window instead of creating another.
    server_name = "bridge-studio-qt-owner-" + str(os.getuid())
    probe = QLocalSocket()
    probe.connectToServer(server_name)
    if probe.waitForConnected(180):
        probe.write(b"activate")
        probe.flush()
        probe.waitForBytesWritten(200)
        probe.disconnectFromServer()
        return 0
    listener = QLocalServer()
    # QLocalServer failures must be visible; never remove sockets owned by strangers.
    if not listener.listen(server_name):
        raise RuntimeError("原生窗口单实例服务已存在或不可用")
    window = Workspace()
    def activate():
        while listener.hasPendingConnections():
            peer = listener.nextPendingConnection()
            peer.readyRead.connect(lambda p=peer: p.readAll())
            peer.disconnectFromServer()
        window.showNormal()
        window.raise_()
        window.activateWindow()
    listener.newConnection.connect(activate)
    window.show()
    if os.environ.get("BRIDGE_STUDIO_SMOKE") == "1":
        QTimer.singleShot(350, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
