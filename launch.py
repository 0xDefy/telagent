"""Portable launcher: run with Python, or use the OS-specific double-click script."""

from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent
    windows = sys.platform == "win32"
    python = root / ".venv" / ("Scripts/pythonw.exe" if windows else "bin/python")
    if not python.exists():
        python = Path(sys.executable)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if windows else {}
    return subprocess.call([str(python), str(root / "gui.py")], cwd=root, **options)


if __name__ == "__main__":
    sys.exit(main())
