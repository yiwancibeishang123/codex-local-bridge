"""Debian package manifest validation without building PyInstaller or modifying host."""
import importlib.util
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/build_deb.py"
SPEC=importlib.util.spec_from_file_location("build_bridge_deb",SCRIPT)
mod=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

@unittest.skipUnless(platform.system()=="Linux" and
                     subprocess.run(["dpkg-deb","--version"],capture_output=True).returncode==0,
                     "dpkg-deb unavailable")
class DebManifestTests(unittest.TestCase):
    def test_builds_no_root_deb_and_does_not_include_user_config(self):
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp)
            bundle=d/"prebuilt"
            bundle.mkdir()
            binary=bundle/"bridge-studio"
            binary.write_text("#!/bin/sh\necho hello\n")
            binary.chmod(0o755)
            (bundle/"_internal").mkdir()
            (bundle/"_internal/resource.txt").write_text("bundle-data")
            artifact=mod.create_package(ROOT,d/"out",prebuilt=bundle)
            self.assertTrue(artifact.is_file())
            manifest=subprocess.check_output(["dpkg-deb","--contents",str(artifact)],text=True)
            control=subprocess.check_output(["dpkg-deb","--field",str(artifact),"Package"],text=True)
            self.assertEqual(control.strip(),"bridge-studio-native")
            self.assertIn("./opt/bridge-studio/bridge-studio",manifest)
            self.assertIn("./usr/share/applications/bridge-studio-native.desktop",manifest)
            self.assertIn("./usr/share/icons/hicolor/scalable/apps/bridge-studio-native.svg",manifest)
            self.assertNotIn(".config/codex-local-bridge",manifest)
            self.assertNotIn(".local/share/codex-local-bridge",manifest)
            self.assertNotIn("postinst",manifest)
            self.assertNotIn("OAuth",manifest)

if __name__=="__main__":
    unittest.main()
