"""Read-only, project-scoped evidence of ChatGPT web-to-local collaboration.

Never reads stored proposed content or full prompts. No claim of ChatGPT OAuth
success is made from checking only local SQLite records.
"""
from pathlib import Path
import os
import sqlite3
import stat

from .core import Bridge


class CollaborationEvidence:
    def __init__(self, config):
        self.bridge = Bridge(config)
        self.state = self.bridge.store.directory

    def overview(self, project_id):
        project = self.bridge.project(project_id)
        auth = self.bridge.authorization.describe(project_id)
        possible = project.get("file_write_enabled") is True
        tests = project.get("tests") or []
        tasks = [x for x in self.bridge.store.all("task") if x.get("project_id") == project_id]
        latest = list(reversed(tasks[-30:]))
        records = []
        for task in latest:
            records.append({
                "id": task.get("id", ""), "state": task.get("state", "unknown"),
                "mode": task.get("mode", "read"),
                "execution_succeeded": task.get("execution_succeeded"),
                "tests_passed": task.get("tests_passed"),
                "changes_applied": task.get("changes_applied"),
            })
        proposals = []
        dbfile = self.state / "workspace.sqlite3"
        if dbfile.exists():
            st=dbfile.lstat()
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode)!=0o600):
                raise PermissionError("Workspace audit database is not private")
            with sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True,
                                 timeout=3) as db:
                db.row_factory = sqlite3.Row
                try:
                    entries=db.execute(
                        "SELECT id,path,status,created_at,applied_at FROM proposals "
                        "WHERE project_id=? ORDER BY created_at DESC LIMIT 25",
                        (project_id,)).fetchall()
                except sqlite3.OperationalError:
                    entries=[]
                proposals=[dict(x) for x in entries]
        return {
            "project": project_id, "read_capable": True,
            "write_configured": possible and "write_test" in project.get("allowed_modes", []),
            "write_authorized": auth["effective_access"]=="automatic_code_write",
            "safe_tests": bool(tests), "test_commands": len(tests),
            "recent_tasks": records[:12], "recent_writes": proposals[:12],
            "note": "Local records are not a remote OAuth check; verify real ChatGPT web calls separately."
        }
