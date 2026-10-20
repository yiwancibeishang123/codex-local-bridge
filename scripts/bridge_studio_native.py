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
from bridge.desktop_profile import config_location, remember_config


def config_path():
    return config_location()


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
    from scripts.studio_upgrade_panel import UpgradePanel
    from scripts.studio_connection_panel import ConnectionPanel
    from scripts.studio_web_panel import WebCollaborationPanel

    app = QApplication(sys.argv)
    app.setApplicationName("Bridge Studio")
    app.setOrganizationName("Codex Local Bridge")
    app.setStyle("Fusion")
    app.setFont(QFont("Noto Sans CJK SC", 10))
    # First-run selection for an already deployed Bridge with a nondefault config.
    if not config_path().is_file():
        QMessageBox.information(
            None, "Bridge Studio 首次使用",
            "未找到已经配置的 Codex Local Bridge。\\n"
            "如果你之前部署过 Bridge，请选择该服务的 config.json。\\n"
            "此操作不会重置 Codex 登录、授权或数据库。"
        )
        selected, _ = QFileDialog.getOpenFileName(
            None, "选择已有的 Bridge config.json",
            str(Path.home()), "Bridge config (config.json *.json)"
        )
        if not selected:
            return 0
        try:
            remember_config(selected)
        except Exception as exc:
            QMessageBox.warning(None, "Bridge Studio", "配置文件未通过安全检查：" + str(exc))
            return 1
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
            self.memory_switch_worker = None
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
                     "✦   长期共享记忆", "◎   文件浏览", "◉   连接与诊断",
                     "↻   升级与恢复", "⌁   网页端协作"]
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
            toolbar.addWidget(self.btn("配置安全测试", self.configure_project_tests))
            toolbar.addWidget(self.btn("刷新", self.reload))
            p.addLayout(toolbar)
            p.addWidget(self.projects, 1)
            line = QHBoxLayout()
            self.mode = QComboBox()
            self.mode.addItems(["关闭 · 不允许访问", "只读 · 浏览与分析", "读写 · 测试后写回"])
            line.addWidget(self.mode, 1)
            line.addWidget(self.btn("应用所选项目权限", self.change_permission, True))
            p.addLayout(line)
            note = QLabel("开通读写前先点「配置安全测试」。可选项目自带的 unittest，或创建基础 Python 语法自检（不代表算法通过）。")
            note.setObjectName("muted")
            p.addWidget(note)
            self.projects.itemSelectionChanged.connect(self.sync_selected_mode)

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
            row2.addWidget(self.btn("续接选中任务", self.continue_selected))
            row2.addWidget(self.btn("重试失败任务", self.retry_selected))
            row2.addWidget(self.btn("取消未完成任务", self.cancel_selected))
            p.addLayout(row2)

            p = self.panel("长期共享记忆", "同一个 SQLite 数据库供网页 MCP 和 Bridge 委派的 Codex 使用。")
            row = QHBoxLayout()
            self.memory_project = QComboBox()
            self.memory_project.currentIndexChanged.connect(self.refresh_memory)
            self.memory_project.currentIndexChanged.connect(self.refresh_memory_setting)
            self.memory_search = QLineEdit()
            self.memory_search.setPlaceholderText("搜索决策、问题、实验结论……")
            self.memory_search.returnPressed.connect(self.refresh_memory)
            row.addWidget(self.memory_project)
            row.addWidget(self.memory_search, 2)
            row.addWidget(self.btn("搜索", self.refresh_memory, True))
            p.addLayout(row)
            self.auto_memory_switch = QCheckBox("自动保存本项目 Bridge 任务结果，并在后续任务中读取共享记忆")
            self.auto_memory_switch.toggled.connect(self.change_auto_memory_setting)
            p.addWidget(self.auto_memory_switch)
            note = QLabel("该开关仅作用于 Bridge 管理的任务；独立 VS Code Codex 需启用并调用记忆 MCP。")
            note.setObjectName("muted")
            p.addWidget(note)
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
            tools.addWidget(self.btn("连接 VS Code Codex 记忆", self.connect_local_codex))
            p.addLayout(tools)
            self.selected_note = None

            p = self.panel("文件浏览", "仅浏览已授权项目中的文本文件，不展示隐藏凭据目录。")
            self.file_project = QComboBox()
            self.file_project.currentIndexChanged.connect(self.reset_files)
            p.addWidget(self.file_project)
            directory = QHBoxLayout()
            self.file_folder = QLineEdit(".")
            self.file_folder.setReadOnly(True)
            directory.addWidget(self.file_folder, 1)
            directory.addWidget(self.btn("返回上级", self.file_up))
            directory.addWidget(self.btn("刷新目录", self.refresh_files))
            p.addLayout(directory)
            box = QHBoxLayout()
            self.file_list = QListWidget()
            self.file_list.currentRowChanged.connect(self.show_file)
            self.file_list.itemDoubleClicked.connect(self.open_file_item)
            self.file_preview = QTextEdit()
            self.file_preview.setReadOnly(True)
            box.addWidget(self.file_list, 2)
            box.addWidget(self.file_preview, 4)
            p.addLayout(box, 1)

            p = self.panel("连接与诊断", "逐项检查本机运行状况，并给出可操作的修复建议。")
            self.connection_panel = ConnectionPanel(
                self.studio.config_path,
                on_codex_connect=self.connect_local_codex,
                parent=self,
            )
            self.connection_panel.status_changed.connect(self.on_connection_status)
            p.addWidget(self.connection_panel, 1)

            p = self.panel("升级与恢复", "图形化管理后端升级、数据一致性备份与故障回退。")
            self.upgrade_panel = UpgradePanel(self.studio.config_path, self)
            p.addWidget(self.upgrade_panel, 1)

            p = self.panel("网页端协作", "在 ChatGPT 对话中进行真正的本地读写，桌面端查看写回结果与 Codex 任务证据。")
            self.web_panel = WebCollaborationPanel(self.studio.config, self)
            p.addWidget(self.web_panel, 1)
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
                self.refresh_memory_setting()
                self.refresh_files()
                self.refresh_health()
                self.web_panel.populate(entries)
            except Exception as exc:
                self.alert(exc, True)

        def sync_selected_mode(self):
            row = self.projects.currentRow()
            if row < 0:
                return
            project_id = self.projects.item(row, 0).data(Qt.UserRole)
            current = next((p for p in self.studio.config()["projects"]
                            if p["id"] == project_id), None)
            if current is not None:
                self.mode.setCurrentIndex({"off":0,"read":1,"write":2}[project_mode(current)])

        def selected_task(self):
            row = self.task_list.currentRow()
            entries = getattr(self, "task_records", [])
            return entries[row] if 0 <= row < len(entries) else None

        def continue_selected(self):
            task = self.selected_task()
            if not task:
                return self.alert("请选择需要续接的历史任务。")
            prompt = self.prompt.toPlainText().strip()
            if not prompt:
                return self.alert("请在上方输入续接任务的要求。")
            if QMessageBox.question(self,"续接 Codex","从已有会话继续工作？") != QMessageBox.Yes:
                return
            try:
                bridge = Bridge(self.studio.config())
                result = bridge.submit(task["project_id"], prompt,
                                       task.get("mode", "read"), parent_id=task["id"])
                self.alert("已续接任务：" + result["id"][:12])
                self.refresh_tasks()
            except Exception as exc:
                self.alert(exc, True)

        def retry_selected(self):
            task = self.selected_task()
            if not task:
                return self.alert("请选择任务。")
            if QMessageBox.question(self,"重新执行","重试该失败/中断任务？") != QMessageBox.Yes:
                return
            try:
                result = Bridge(self.studio.config()).retry(task["id"])
                self.alert("已重新排队：" + result["id"][:12])
                self.refresh_tasks()
            except Exception as exc:
                self.alert(exc, True)

        def cancel_selected(self):
            task = self.selected_task()
            if not task:
                return self.alert("请选择任务。")
            if QMessageBox.question(self,"取消任务","取消这个尚未结束的任务？") != QMessageBox.Yes:
                return
            try:
                Bridge(self.studio.config()).cancel(task["id"])
                self.refresh_tasks()
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
                from scripts.add_readonly_project import main as register_local
                from scripts.studio_upgrade_panel import Operation
                from contextlib import redirect_stdout
                import io
                if getattr(self, "registration_worker", None) is not None:
                    return self.alert("当前还有一个项目正在注册。")
                def register():
                    # Import inside bundled PyInstaller app, no separate Python
                    # script path required. The safe helper performs a backup
                    # and a verified service restart before returning.
                    out = io.StringIO()
                    with redirect_stdout(out):
                        register_local([name, folder, "--yes"], config_file=self.studio.config_path)
                    return {"id": name, "summary": out.getvalue()[-900:]}
                self.registration_worker = Operation(register, self)
                self.registration_worker.success.connect(
                    lambda _result: self.reload())
                self.registration_worker.failure.connect(
                    lambda message: self.alert(message, True))
                def clean():
                    self.registration_worker.deleteLater()
                    self.registration_worker = None
                self.registration_worker.finished.connect(clean)
                self.registration_worker.start()
            except Exception as exc:
                self.alert(exc, True)

        def configure_project_tests(self):
            """GUI-selected safe suite, never allow arbitrary shell commands."""
            from bridge.project_tests import ProjectTestProfiles, available_profiles
            from scripts.studio_upgrade_panel import Operation
            row = self.projects.currentRow()
            if row < 0:
                return self.alert("请先选择要配置测试的项目，例如 airgym_yingjie。")
            pid = self.projects.item(row, 0).data(Qt.UserRole)
            record = next((p for p in self.studio.config()["projects"] if p["id"] == pid), None)
            if not record:
                return self.alert("当前项目不存在。", True)
            try:
                from bridge.core import TERMINAL
                pending = [t for t in Bridge(self.studio.config()).store.all("task")
                           if t.get("state") not in TERMINAL]
                if pending:
                    return self.alert("仍有正在运行的 Codex 任务；请结束任务后再设置测试。")
                profiles = available_profiles(record)
                labels = [p["label"] for p in profiles]
                choice, ok = QInputDialog.getItem(
                    self, "设置项目安全测试", "为 "+pid+" 选择测试方案：",
                    labels, 0, False)
                if not ok:
                    return
                chosen = profiles[labels.index(choice)]
                if QMessageBox.question(
                    self, "确认测试方案",
                    "测试方案：" + chosen["label"] + "\n\n" + chosen["description"] +
                    "\n\n会修改本机项目测试配置并重启 Bridge。"
                    "\n轻量语法测试不代替 AirGym/ROS/PPO 的真实功能测试。是否继续？"
                ) != QMessageBox.Yes:
                    return
                if getattr(self, "test_profile_worker", None) is not None:
                    return self.alert("已有测试设置正在执行。")
                job = Operation(
                    lambda: ProjectTestProfiles(self.studio.config_path).set(
                        pid,chosen["id"],confirmed=True), self)
                self.test_profile_worker = job
                job.success.connect(lambda result: (
                    self.reload(),
                    self.alert("测试方案已保存。现在选择「读写」并应用权限。"
                               "\n注意：基础语法检查不代表科研算法的功能正确。")))
                job.failure.connect(lambda err: self.alert(err, True))
                def done():
                    self.test_profile_worker = None
                    job.deleteLater()
                job.finished.connect(done)
                job.start()
            except Exception as exc:
                self.alert(exc, True)

        def change_permission(self):
            row = self.projects.currentRow()
            if row < 0:
                return self.alert("请先选中项目。")
            pid = self.projects.item(row, 0).data(Qt.UserRole)
            target = ("off", "read", "write")[self.mode.currentIndex()]
            if target == "write":
                project = next((p for p in self.studio.config()["projects"]
                                if p["id"] == pid), None)
                if not project or not project.get("tests"):
                    self.alert("当前项目尚未配置安全测试。我将打开测试方案选择器。")
                    self.configure_project_tests()
                    return
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
                self.task_detail.setHtml(self.render_task_summary(data))
            except Exception as exc:
                self.task_detail.setPlainText("读取任务详情失败：" + str(exc))

        @staticmethod
        def render_task_summary(data):
            """Human-readable evidence. Never render raw prompts, logs or JSON."""
            from html import escape
            state = {"queued":"排队中", "running":"执行中",
                     "awaiting_approval":"待本机审批",
                     "succeeded":"已完成", "failed":"执行失败",
                     "cancelled":"已取消","interrupted":"中断"}
            def flag(val):
                return "通过" if val is True else "未通过" if val is False else "尚未验证"
            title = escape(state.get(data.get("state"),"未知状态"))
            rows = [
                ("所属项目", escape(str(data.get("project_id","未知")))),
                ("任务状态", title),
                ("执行结果", flag(data.get("execution_succeeded"))),
                ("测试结果", flag(data.get("tests_passed"))),
                ("文件写回", "已应用" if data.get("changes_applied") is True
                            else "未修改" if data.get("changes_applied") is False else "尚未确认"),
                ("Git 提交", flag(data.get("source_commit_created"))),
                ("实际修改文件数", str(data.get("file_count",0))),
            ]
            blocks = [
                "<h2 style='color:#2c4269'>"+title+"</h2>",
                "<p style='color:#72819a'>"+escape(str(data.get("id",""))[:12])+"</p>"
            ]
            blocks += ["<p><b>"+k+"：</b> "+v+"</p>" for k,v in rows]
            tests = data.get("tests",[])
            if tests:
                blocks.append("<h3>沙箱测试</h3>")
                for i,item in enumerate(tests):
                    blocks.append("<p>测试 "+str(i+1)+"："+
                        flag(item.get("passed"))+
                        (" · 网络隔离" if item.get("sandboxed") else "")+"</p>")
            files=data.get("files",[])
            if files:
                blocks.append("<h3>涉及文件</h3>")
                blocks += ["<p>• "+escape(str(path))+"</p>" for path in files[:20]]
            blocks.append("<p style='color:#78879a'>本页只展示已保存的执行事实；"
                          "没有测试通过不等于任务一定失败，成功执行也不等于完成写回。</p>")
            return "".join(blocks)

        def refresh_memory_setting(self):
            from bridge.memory_preferences import AutoMemoryPreferences
            pid = self.active_id(self.memory_project)
            self.auto_memory_switch.blockSignals(True)
            try:
                if not pid:
                    self.auto_memory_switch.setEnabled(False)
                    self.auto_memory_switch.setChecked(False)
                    return
                self.auto_memory_switch.setChecked(
                    AutoMemoryPreferences(self.studio.config_path).read(pid))
                self.auto_memory_switch.setEnabled(True)
            except Exception:
                self.auto_memory_switch.setEnabled(False)
            finally:
                self.auto_memory_switch.blockSignals(False)

        def change_auto_memory_setting(self, enabled):
            from bridge.memory_preferences import AutoMemoryPreferences
            from scripts.studio_upgrade_panel import Operation
            pid = self.active_id(self.memory_project)
            if not pid:
                return
            if self.memory_switch_worker is not None:
                self.refresh_memory_setting()
                return
            # Preferences restart the service; do not interrupt active Codex tasks.
            try:
                from bridge.core import Bridge, TERMINAL
                pending = [t for t in Bridge(self.studio.config()).store.all("task")
                           if t.get("state") not in TERMINAL]
                if pending:
                    raise RuntimeError("当前还有未结束的 Codex 任务，拒绝重启 Bridge。")
            except Exception as exc:
                self.alert(exc, True)
                self.refresh_memory_setting()
                return
            word = "启用" if enabled else "关闭"
            if QMessageBox.question(
                self, "确认自动记忆偏好",
                word + "项目 " + pid + " 的 Bridge 自动任务记忆？\\n"
                "该操作将更新本机私有配置并重启 Bridge 用户服务。"
                "已经存在的历史记忆不会被删除。"
            ) != QMessageBox.Yes:
                self.refresh_memory_setting()
                return
            try:
                job = Operation(
                    lambda: AutoMemoryPreferences(self.studio.config_path).set(
                        pid, bool(enabled), confirmed=True), self)
                self.memory_switch_worker = job
                self.auto_memory_switch.setEnabled(False)
                job.success.connect(
                    lambda result: self.alert("自动记忆设置已更新：" +
                                              ("启用" if result["enabled"] else "关闭")))
                job.failure.connect(lambda error: self.alert(error, True))
                def done():
                    self.memory_switch_worker = None
                    job.deleteLater()
                    self.refresh_memory_setting()
                job.finished.connect(done)
                job.start()
            except Exception as exc:
                self.alert(exc, True)
                self.refresh_memory_setting()

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

        def connect_local_codex(self):
            """Owner opt-in: uses official local Codex MCP config manager."""
            from bridge.codex_connect import autodetect_backend_python, register_codex_memory
            backend = autodetect_backend_python(root=root)
            if not backend:
                backend, _ = QFileDialog.getOpenFileName(
                    self, "选择已安装 Bridge 后端的 Python (.venv/bin/python)",
                    str(Path.home()), "Python interpreter (python python3 python3.*);;All files (*)")
            if not backend:
                return
            if QMessageBox.question(
                self, "连接独立 VS Code Codex",
                "将为当前 Ubuntu 用户的 Codex 注册本地记忆 MCP。\n"
                "仅允许访问已授权项目的 SQLite 记忆，不会提供任意文件写入权限。\n"
                "注册前会备份已有 Codex 设置；之后需要重新载入 VS Code Codex。\n\n"
                "确定继续？",
            ) != QMessageBox.Yes:
                return
            try:
                result = register_codex_memory(
                    self.studio.config(), self.studio.config_path, backend,
                    confirmed=True)
                self.alert("记忆工具已注册。重新启动 VS Code Codex，检查 MCP 工具列表。\n"
                           + "Codex 独立会话需要主动调用记忆工具；不能自动抓取全部聊天。")
            except Exception as exc:
                self.alert(str(exc), True)

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

        def reset_files(self):
            self.file_folder.setText(".")
            self.refresh_files()

        def file_up(self):
            old = self.file_folder.text().strip() or "."
            self.file_folder.setText(str(Path(old).parent))
            self.refresh_files()

        def open_file_item(self, item):
            row = self.file_list.row(item)
            if 0 <= row < len(self.file_paths):
                relative, is_dir = self.file_paths[row]
                if is_dir:
                    self.file_folder.setText(relative)
                    self.refresh_files()
                else:
                    self.show_file(row)

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
                root = Path(item["root"]).resolve(strict=True)
                relative = self.file_folder.text().strip() or "."
                target = root.joinpath(relative)
                # No symlinks anywhere in the relative path; no directory traversal.
                if (relative.startswith("/") or ".." in Path(relative).parts or
                    any((root / Path(*Path(relative).parts[:i])).is_symlink()
                        for i in range(1, len(Path(relative).parts)+1))):
                    raise PermissionError("路径不属于授权项目")
                target = target.resolve(strict=True)
                if not target.is_relative_to(root) or not target.is_dir():
                    raise PermissionError("目录越界")
                for item_path in sorted(target.iterdir(), key=lambda x:x.name.casefold()):
                    if len(self.file_paths) >= 250:
                        break
                    if item_path.name.startswith(".") or item_path.is_symlink():
                        continue
                    rel = item_path.relative_to(root).as_posix()
                    if item_path.is_dir():
                        self.file_paths.append((rel, True))
                        self.file_list.addItem("📁  " + item_path.name)
                        continue
                    if not item_path.is_file():
                        continue
                    try:
                        checked(root, rel, item["include"])
                    except ValueError:
                        continue
                    self.file_paths.append((rel, False))
                    self.file_list.addItem("📄  " + item_path.name)
            except Exception as exc:
                self.file_preview.setPlainText("无法列出目录：" + str(exc))

        def show_file(self, index):
            pid = self.active_id(self.file_project)
            if not pid or not 0 <= index < len(self.file_paths):
                return
            relative, is_dir = self.file_paths[index]
            if is_dir:
                self.file_preview.setPlainText("双击文件夹进入：" + relative)
                return
            try:
                record = self.studio.project(pid)
                from bridge.filetools import _redact
                blob, _ = _read_at(Path(record["root"]), _check_path(relative))
                if blob is None or len(blob) > 250_000:
                    raise ValueError("只支持不超过 250KB 的 UTF-8 文本文件")
                content = blob.decode("utf-8")
                if _redact(content)[1]:
                    raise PermissionError("文件内容疑似含凭据，不显示")
                self.file_preview.setPlainText(content)
            except Exception as exc:
                self.file_preview.setPlainText("无法读取：" + str(exc))

        def on_connection_status(self, report):
            checks = report.get("checks", {})
            status = checks.get("local_mcp", {}).get("status")
            self.kpi[3].setText(
                "在线" if status == "ready"
                else "离线" if status == "attention" else "未验证"
            )

        def refresh_health(self):
            # Refreshes are scheduled by ConnectionPanel every 60 seconds;
            # a fast 8-second poll must not spawn repeated Codex/TS processes.
            if self.connection_panel.last_report:
                self.on_connection_status(self.connection_panel.last_report)

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
