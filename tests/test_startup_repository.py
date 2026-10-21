"""Offline startup/repository regressions: no live Bot or API requests."""
import ast
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_weflow_patch", ROOT / "scripts/install_weflow_patch.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class StartupRepositoryTests(unittest.TestCase):
    def test_no_clock_module_or_import(self):
        self.assertFalse((ROOT / "core/system_time.py").exists())
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertNotIn("core.system_time", imports)
        names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
        for name in ("set_startup_date", "restore_automatic_time", "_restore_automatic_time_if_needed"):
            self.assertNotIn(name, names)

    def test_startup_has_no_clock_commands(self):
        for relative in ("main.py", "start.bat", "start_weflow.bat", "setup_wizard.py", "setup_wizard.bat", "core/weflow_headless.py"):
            text = (ROOT / relative).read_text(encoding="utf-8").lower()
            for command in ("set-date", "w32tm", "set_startup_date", "restore_automatic_time", "setsystemtime"):
                self.assertNotIn(command, text, relative)

    def test_first_setup_does_not_require_webui(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        setup = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_launch_weflow_setup_wizard")
        self.assertNotIn("python -m webui", ast.get_source_segment(source, setup))
        self.assertIn("setup_wizard.bat", ast.get_source_segment(source, setup))

    def test_webui_and_temporary_files_ignored(self):
        for path in ("webui/server.py", "temp/diagnostic.json", "docs/WEBUI_DESIGN.md", "weflow-core/.weflow-data/local.json"):
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
            self.assertEqual(result.returncode, 0, path)

    def test_regression_tests_are_tracked_source(self):
        result = subprocess.run(["git", "check-ignore", "-q", "tests/test_startup_repository.py"], cwd=ROOT)
        self.assertEqual(result.returncode, 1)

    def test_agpl_and_original_attribution(self):
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("oldicecube", (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "LICENSES/project-legacy-MIT.txt").is_file())
        self.assertTrue((ROOT / "LICENSES/CC-BY-NC-SA-4.0.txt").is_file())

    def test_native_hash_and_architecture(self):
        data = installer.DEFAULT_DLL.read_bytes()
        installer.validate_patch(data)
        self.assertEqual(hashlib.sha256(data).hexdigest(), installer.PATCH_SHA256)

    def test_shutdown_imports_real_sender_stop_function(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "core.wechat_sender"]
        self.assertTrue(any(alias.name == "stop_server" for n in imports for alias in n.names))

    def test_installer_backs_up_and_replaces_before_retry(self):
        data = installer.DEFAULT_DLL.read_bytes()
        original = b"test original native component"
        with tempfile.TemporaryDirectory(prefix="wechatbot-patch-test-") as directory:
            target = Path(directory) / "resources/wcdb_api.dll"
            target.parent.mkdir()
            target.write_bytes(original)
            with patch.dict(installer.os.environ, {"LOCALAPPDATA": directory}), patch.object(installer, "assert_not_loaded"):
                backup = installer.install(target, data, allow_version_change=True)
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(target.read_bytes(), data)
                self.assertIsNone(installer.install(target, data))
                self.assertEqual(backup.read_bytes(), original)

    def test_tampered_native_payload_rejected(self):
        with self.assertRaises(ValueError):
            installer.validate_patch(b"untrusted DLL")

    def test_install_is_idempotent(self):
        data = installer.DEFAULT_DLL.read_bytes()
        with tempfile.TemporaryDirectory(prefix="wechatbot-patch-test-") as directory:
            target = Path(directory) / "wcdb_api.dll"
            target.write_bytes(data)
            with patch.object(installer, "assert_not_loaded", side_effect=AssertionError("must not check processes")):
                self.assertIsNone(installer.install(target, data))

    def test_unknown_existing_version_rejected(self):
        data = installer.DEFAULT_DLL.read_bytes()
        with tempfile.TemporaryDirectory(prefix="wechatbot-patch-test-") as directory:
            target = Path(directory) / "wcdb_api.dll"
            target.write_bytes(b"unknown existing version")
            with self.assertRaisesRegex(ValueError, "Unknown DLL version"):
                installer.install(target, data)
            self.assertEqual(target.read_bytes(), b"unknown existing version")

    def test_loaded_native_library_is_not_replaced(self):
        data = installer.DEFAULT_DLL.read_bytes()
        with tempfile.TemporaryDirectory(prefix="wechatbot-patch-test-") as directory:
            target = Path(directory) / "wcdb_api.dll"
            target.write_bytes(b"test original")
            with patch.object(installer, "assert_not_loaded", side_effect=RuntimeError("DLL is loaded")):
                with self.assertRaisesRegex(RuntimeError, "DLL is loaded"):
                    installer.install(target, data, allow_version_change=True)
            self.assertEqual(target.read_bytes(), b"test original")


if __name__ == "__main__":
    unittest.main()
