"""Owner-confirmed project test-profile setup for safe, explicit write grants.

Only fixed, visible /usr/bin/python3 -m unittest commands, never custom shell.
Basic profile checks Python source syntax and *does not guarantee functional
correctness*. Actual project test suites should be used for write_test repairs.
"""
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

from .project_modes import ProjectModes, private_replace
from .project_sandbox import safe_root

SMOKE_DIR = "bridge_studio_tests"
SMOKE_NAME = "test_bridge_syntax_smoke.py"
SMOKE = '''"""Safe minimal Python syntax smoke; does NOT execute research modules.

This profile is a basic permission prerequisite, not a claim that scientific
algorithms, ROS nodes, simulations or integration tests are correct.
"""
import ast
from pathlib import Path
import unittest

class ProjectSyntaxCheck(unittest.TestCase):
    def test_top_level_python_sources_compile(self):
        root = Path(__file__).resolve().parent.parent
        checked = 0
        patterns = ("*.py", "*.yaml", "*.yml", "*.cpp", "*.hpp", "*.md", "*.toml")
        for pat in patterns:
            for source in sorted(root.glob(pat))[:25]:
                if source.is_symlink() or not source.is_file() or source.stat().st_size > 300000:
                    continue
                with self.subTest(file=source.name):
                    content = source.read_text(encoding="utf-8")
                    if source.suffix == ".py":
                        ast.parse(content, filename=str(source))
                    else:
                        self.assertTrue(content.strip(), "Empty project source")
                    checked += 1
        self.assertTrue(root.is_dir())
        self.assertGreater(checked, 0, "No inspectable top-level source file")
'''

def _authorized_path(root):
    root = Path(safe_root(root))
    if any(part in ('.ssh','.gnupg','.aws','.codex','.venv','.git')
           for part in root.parts):
        raise PermissionError("Credential directories cannot be test profiles")
    return root

def available_profiles(project):
    root = _authorized_path(project["root"])
    profiles = []
    for directory in ("tests", "test", "maintenance_tests"):
        folder = root / directory
        if folder.is_symlink() or not folder.is_dir():
            continue
        tests = [p for p in folder.iterdir() if p.is_file() and not p.is_symlink()
                 and p.name.startswith("test_") and p.suffix == ".py"]
        if tests:
            profiles.append({"id":directory, "label":"使用现有 unittest 测试 · "+directory,
                "description":f"使用 {len(tests)} 个候选测试文件；可能需要项目依赖。"})
    profiles.append({"id":"syntax_smoke",
         "label":"创建基础 Python 语法检查（适合先为 AirGym 开通受控写入）",
         "description":"检查顶层 Python 文件是否可解析；不会验证 RL/ROS 功能。"
                       "配置后仍需运行真正的项目测试。"})
    return profiles


class ProjectTestProfiles:
    def __init__(self, config_path, *, restart=None):
        self.modes = ProjectModes(config_path, restart=restart)

    def set(self, project_id, profile_id, *, confirmed=False):
        if confirmed is not True:
            raise PermissionError("Explicit local owner consent is required")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("Invalid project")
        if not isinstance(profile_id, str) or profile_id not in (
            "tests", "test", "maintenance_tests", "syntax_smoke"):
            raise ValueError("Unknown safe test profile")
        cfgpath = self.modes.path
        lock_path = cfgpath.with_name(cfgpath.name + ".mode.lock")
        if lock_path.is_symlink():
            raise PermissionError("Unsafe config lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        created_dir = created_file = None
        try:
            if (os.fstat(fd).st_uid != os.getuid()
                    or stat.S_IMODE(os.fstat(fd).st_mode) != 0o600):
                raise PermissionError("Unsafe lock file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = cfgpath.read_bytes()
            data = self.modes.read()
            project = next((x for x in data["projects"] if x["id"] == project_id), None)
            if project is None:
                raise ValueError("Unknown project")
            if project.get("enabled") is False:
                raise PermissionError("Enable project before editing its test profile")
            if project.get("file_write_enabled") is True:
                raise PermissionError("Lower existing write permission to read before changing tests")
            root = _authorized_path(project["root"])
            if profile_id == "syntax_smoke":
                testdir = root / SMOKE_DIR
                script = testdir / SMOKE_NAME
                if testdir.is_symlink() or script.exists() or script.is_symlink():
                    raise FileExistsError("Bridge syntax test folder/file already exists")
                if not testdir.exists():
                    testdir.mkdir(mode=0o700)
                    created_dir = testdir
                descriptor = os.open(script, os.O_CREAT | os.O_EXCL |
                                     os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                created_file = script
                with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                    f.write(SMOKE)
                    f.flush()
                    os.fsync(f.fileno())
                folder = SMOKE_DIR
            else:
                if profile_id not in {p["id"] for p in available_profiles(project)}:
                    raise ValueError("Selected unittest directory has no visible tests")
                folder = profile_id
            command = ["/usr/bin/python3", "-m", "unittest", "discover",
                       "-s", folder, "-v"]
            project["tests"] = [command]
            changed = (json.dumps(data, ensure_ascii=False, indent=2)+"\n").encode()
            if cfgpath.read_bytes() != raw:
                raise RuntimeError("Config changed concurrently")
            state = Path(data["state_dir"]).expanduser().absolute()
            backups = state / "test-profile-backups"
            if backups.is_symlink():
                raise PermissionError("Unsafe backup folder")
            backups.mkdir(mode=0o700, parents=False, exist_ok=True)
            if stat.S_IMODE(backups.stat().st_mode) != 0o700:
                raise PermissionError("Backup folder must be owner-only")
            back = backups / ("before-tests-"+uuid.uuid4().hex+".json")
            outfd = os.open(back, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(outfd,"wb") as f:
                f.write(raw);f.flush();os.fsync(f.fileno())
            try:
                private_replace(cfgpath, changed)
                self.modes.restart()
            except Exception:
                if cfgpath.read_bytes() == changed:
                    private_replace(cfgpath, raw)
                try:
                    self.modes.restart()
                except Exception:
                    pass
                raise
            return {"project_id":project_id, "command":command,
                    "profile":profile_id,"backup":str(back),
                    "scope":"syntax_only" if profile_id=="syntax_smoke" else "project_unittest"}
        except Exception:
            if created_file is not None:
                created_file.unlink(missing_ok=True)
            if created_dir is not None:
                try: created_dir.rmdir()
                except OSError: pass
            raise
        finally:
            os.close(fd)
