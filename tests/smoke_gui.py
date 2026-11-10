"""Create the actual window using isolated settings; no Telegram connection."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    gui.CONFIG = root / "config.json"
    gui.CACHE = root / "data" / "channels.json"
    app = gui.CollectorApp()
    app.update_idletasks()
    app.update()
    assert app.process is None
    assert len(app.tabs.tabs()) == 4
    app.destroy()
print("Desktop window smoke check passed")
