#!/usr/bin/env python3
"""Build a self-contained *optional* Ubuntu .deb of the Qt desktop app.

Requires a Linux build host with Python, PySide6, PyInstaller, dpkg-deb.
Never touches user's Bridge, OAuth token, systemd grants or SQLite. The .deb
ships desktop UI only; the separately installed MCP bridge remains authoritative.
"""
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

VERSION = "0.5.0~alpha4"
PACKAGE = "bridge-studio-native"

def exec_checked(argv):
    subprocess.run(argv, check=True)

def create_package(root, output, *, prebuilt=None, version=VERSION):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if not root.joinpath("scripts/bridge_studio_native.py").is_file():
        raise FileNotFoundError("Native Qt program missing")
    if platform.system() != "Linux":
        raise RuntimeError("Linux host required")
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    if arch != "amd64":
        raise RuntimeError("Currently only Ubuntu amd64 builds are tested")
    with tempfile.TemporaryDirectory(prefix="bridge-studio-deb-") as work:
        tmp = Path(work)
        dist = tmp / "dist"
        if prebuilt:
            binary_dir = Path(prebuilt).resolve()
            if not (binary_dir / "bridge-studio").is_file():
                raise ValueError("Prebuilt tree must contain bridge-studio executable")
        else:
            exec_checked([
                sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                "--onedir", "--windowed", "--name", "bridge-studio",
                "--collect-submodules", "bridge",
                "--hidden-import", "scripts.bridge_studio",
                "--hidden-import", "scripts.add_readonly_project",
                "--hidden-import", "scripts.studio_connection_panel",
                "--hidden-import", "bridge.connection_health",
                "--hidden-import", "bridge.desktop_profile",
                "--add-data", str(root / "docs/assets/bridge-studio-icon.svg") + ":docs/assets",
                "--distpath", str(dist), "--workpath", str(tmp / "work"),
                "--specpath", str(tmp / "spec"),
                str(root / "scripts/bridge_studio_native.py"),
            ])
            binary_dir = dist / "bridge-studio"
        package = tmp / "pkg"
        app = package / "opt/bridge-studio"
        shutil.copytree(binary_dir, app, symlinks=False)
        (package / "DEBIAN").mkdir()
        (package / "usr/share/applications").mkdir(parents=True)
        (package / "usr/share/icons/hicolor/scalable/apps").mkdir(parents=True)
        (package / "usr/share/doc/bridge-studio-native").mkdir(parents=True)
        control = (
            "Package: " + PACKAGE + "\n"
            "Version: " + version + "\n"
            "Section: devel\nPriority: optional\nArchitecture: amd64\n"
            "Maintainer: Bridge Studio contributors <noreply@github.com>\n"
            "Depends: libc6, libglib2.0-0, libgl1, libegl1, libxcb-cursor0\n"
            "Description: Native local-first Qt desktop interface for Codex Local Bridge\n"
            " Standalone desktop UI; requires an existing user-managed Bridge config.\n"
            " OAuth, service grants and SQLite data are not modified by installation.\n"
        )
        (package / "DEBIAN/control").write_text(control, encoding="utf-8")
        desktop = (
            "[Desktop Entry]\nType=Application\nName=Bridge Studio\n"
            "Comment=Local ChatGPT and Codex workspace\n"
            "Exec=/opt/bridge-studio/bridge-studio\n"
            "Icon=bridge-studio-native\nTerminal=false\n"
            "Categories=Development;Utility;\nStartupNotify=true\n"
        )
        (package / "usr/share/applications/bridge-studio-native.desktop").write_text(desktop)
        shutil.copy2(root / "docs/assets/bridge-studio-icon.svg",
                     package / "usr/share/icons/hicolor/scalable/apps/bridge-studio-native.svg")
        shutil.copy2(root / "LICENSE",
                     package / "usr/share/doc/bridge-studio-native/LICENSE")
        for path in package.rglob("*"):
            if path.is_file() and path.name not in ("bridge-studio",):
                # PyInstaller files may include shared .so libraries with execute perms:
                # keep original mode for binaries; only restrict package metadata.
                if path.parent.name == "DEBIAN":
                    path.chmod(0o644)
        output.mkdir(parents=True, exist_ok=True)
        dest = output / f"{PACKAGE}_{version}_amd64.deb"
        exec_checked(["dpkg-deb", "--build", "--root-owner-group", str(package), str(dest)])
        exec_checked(["dpkg-deb", "--field", str(dest), "Package", "Version"])
        print("Created:", dest)
        print("Size (bytes):", dest.stat().st_size)
        return dest

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="release")
    parser.add_argument("--prebuilt", default=None)
    args = parser.parse_args(argv)
    create_package(Path(__file__).resolve().parents[1], args.output, prebuilt=args.prebuilt)

if __name__ == "__main__":
    main()
