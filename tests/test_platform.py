from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from platform_utils import FileLock, open_folder, worker_python, worker_options


class PlatformTests(unittest.TestCase):
    def test_lock_rejects_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "collector.lock"
            code = "from pathlib import Path; from platform_utils import FileLock; import sys; "
            code += "lock=FileLock(Path(sys.argv[1])); lock.__enter__(); lock.__exit__()"
            with FileLock(lock_path):
                result = subprocess.run([sys.executable, "-c", code, str(lock_path)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Another Telagent instance", result.stderr)
            result = subprocess.run([sys.executable, "-c", code, str(lock_path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_folder_open_on_mac_and_linux_handles_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "folder with spaces"
            for platform, command in (("darwin", "open"), ("linux", "xdg-open")):
                with patch("platform_utils.sys.platform", platform), patch("platform_utils.subprocess.Popen") as popen:
                    open_folder(folder)
                    self.assertEqual(popen.call_args.args[0], [command, str(folder)])
                    self.assertNotIn("shell", popen.call_args.kwargs)

    def test_native_worker_uses_current_environment(self):
        self.assertTrue(worker_python().exists())
        options = worker_options()
        self.assertTrue("creationflags" in options or options.get("start_new_session"))
