"""Native UI for genuine ChatGPT web↔local Bridge collaboration evidence.

Copies task instructions to clipboard; does not impersonate ChatGPT, secretly
change project files or claim that local writes prove remote OAuth.
"""
from html import escape
from secrets import token_hex

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit,
    QHeaderView, QApplication, QAbstractItemView
)
from PySide6.QtCore import Qt

from bridge.collaboration import CollaborationEvidence

class WebCollaborationPanel(QWidget):
    def __init__(self, config_provider, parent=None):
        super().__init__(parent)
        self.config_provider=config_provider
        self.setObjectName("webCollaboration")
        body=QVBoxLayout(self)
        body.setContentsMargins(0,0,0,0)
        headline=QLabel("ChatGPT 网页端 ⇄ 本地 Ubuntu ⇄ Codex")
        headline.setStyleSheet("font-size:18px;font-weight:700;color:#314b87")
        body.addWidget(headline)
        subtitle=QLabel(
          "这里显示的是本地执行证据。网页端是否真的连通，以本对话中实际调用 MCP 并写回为准。")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        body.addWidget(subtitle)
        tools=QHBoxLayout()
        self.project=QComboBox()
        self.project.currentIndexChanged.connect(self.refresh)
        tools.addWidget(self.project,2)
        copy=QPushButton("复制网页端验收指令")
        copy.clicked.connect(self.copy_prompt)
        tools.addWidget(copy)
        reload=QPushButton("刷新执行记录")
        reload.clicked.connect(self.refresh)
        tools.addWidget(reload)
        body.addLayout(tools)
        self.status=QLabel("选择项目以查看连接与授权状态。")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size:14px; font-weight:600;color:#42588a;padding:10px")
        body.addWidget(self.status)
        self.help=QLabel(
            "验证步骤：复制测试指令 → 在 ChatGPT 网页对话中发送 → "
            "ChatGPT 直接读取文件，具备写权限时创建一份无害的探针文件 → "
            "回到这里刷新，核对写入记录。普通项目文件不会自动修改。")
        self.help.setWordWrap(True)
        self.help.setObjectName("muted")
        body.addWidget(self.help)
        self.activity=QTableWidget(0,4)
        self.activity.setHorizontalHeaderLabels(["时间顺序","操作类型","目标文件 / 任务","实际结果"])
        self.activity.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch)
        self.activity.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        self.activity.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.activity.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.activity.verticalHeader().setVisible(False)
        body.addWidget(self.activity,3)
        self.foot=QLabel("")
        self.foot.setObjectName("muted")
        self.foot.setWordWrap(True)
        body.addWidget(self.foot)

    def populate(self, projects):
        current=self.project.currentData()
        self.project.blockSignals(True)
        self.project.clear()
        for p in projects:
            if p.get("enabled") is not False:
                self.project.addItem(p.get("name",p["id"]),p["id"])
        index=self.project.findData(current)
        if index>=0:self.project.setCurrentIndex(index)
        self.project.blockSignals(False)
        self.refresh()

    def refresh(self):
        pid=self.project.currentData()
        self.activity.setRowCount(0)
        if not pid:
            self.status.setText("请先添加并启用一个项目。")
            return
        try:
            ev=CollaborationEvidence(self.config_provider()).overview(pid)
            grants="已授权读写" if ev["write_authorized"] else (
                "已配置但未激活写入授权" if ev["write_configured"] else "只读")
            self.status.setText(
                "● 本地项目可读取     ·     写入：" + grants +
                "     ·     安全测试：" + ("已配置" if ev["safe_tests"] else "未配置"))
            events=[]
            for p in ev["recent_writes"]:
                events.append(("文件写入","本地文件："+p["path"],
                               "已写入" if p["status"]=="applied" else p["status"]))
            for task in ev["recent_tasks"]:
                tests=task.get("tests_passed")
                changes=task.get("changes_applied")
                result=("测试通过" if tests is True else "测试失败" if tests is False else "未验证")
                if changes is True:result+=" · 已写回"
                elif changes is False:result+=" · 未写回"
                events.append(("Codex 任务",str(task.get("id",""))[:12], result))
            for index,(kind,item,result) in enumerate(events[:24]):
                self.activity.insertRow(index)
                for col,value in enumerate((str(index+1),kind,item,result)):
                    self.activity.setItem(index,col,QTableWidgetItem(value))
            self.foot.setText(
                f"近况：{len(ev['recent_tasks'])} 条任务、"
                f"{len(ev['recent_writes'])} 条本地文件提案。"
                "本地记录本身不能证明 ChatGPT OAuth 已连通；必须配合网页端读写并核对提案状态。")
        except Exception as exc:
            self.status.setText("暂时无法读取本地协作记录。")
            self.foot.setText("检查项目访问权限与本地 SQLite 数据库。"+str(exc)[:150])

    def copy_prompt(self):
        pid=self.project.currentData()
        if not pid:
            return
        config=self.config_provider()
        item=next((x for x in config["projects"] if x["id"]==pid),None)
        if not item:return
        sample=(item.get("context_files") or [])
        filename=sample[0] if sample else ""
        suffix=token_hex(4)
        marker="WEB_BRIDGE_PROOF_"+suffix
        probe="bridge_web_probe_"+suffix+".py"
        msg=(
          "请使用已连接的 Codex Local Bridge MCP，针对项目 "+pid+
          " 执行真实的 ChatGPT 网页端→本地 Ubuntu 测试。"
          "先调用 list_projects，明确回复当前项目真实的授权状态。"
          "接着调用 list_project_files 和 read_project_file，"
          +("读取 "+filename+"；" if filename else "任选一个授权 UTF-8 文件读取并报告 SHA-256；")+
          "如果且仅如果该项目已经授权自动写入，请调用 request_write_file "
          "在项目根目录新建 "+probe+"，expected_sha256='ABSENT'，"
          "内容严格为 # Bridge web-only acceptance probe\\n"
          "WEB_PROOF = '"+marker+"'\\n。"
          "然后调用 read_project_file 核验内容和 SHA-256，并调用 get_write_status 核实状态。"
          "不得修改或删除其他项目文件。"
          "如果项目是只读则跳过写入，并明确指出需要在本机 Bridge Studio 授权。"
          "最后分别报告：网页读取成功与否、网页写入是否确实 applied、"
          "路径、SHA-256 与提案编号。只使用实际 MCP 工具返回值，不做推测。"
        )
        QApplication.clipboard().setText(msg)
        self.foot.setText("验收指令已复制。粘贴到 ChatGPT 网页端对话执行，再点击「刷新执行记录」。"
                          "验收探针属于普通 .py 文件，可在验证后自行清理。")
