"""Create the actual window using isolated settings; no Telegram connection."""
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui
from telagent import init_database, upsert_record


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    gui.CONFIG = root / "config.json"
    gui.CACHE = root / "data" / "channels.json"
    database = root / "messages.sqlite3"
    gui.atomic_json(gui.CONFIG, {"channels": [-100123], "database_path": str(database)})
    gui.atomic_json(gui.CACHE, [
        {"id": -100123, "title": "Release notes", "username": "release_notes"},
        {"id": -100124, "title": "Community", "username": "community"},
    ])
    init_database(database)
    body = "Release notes are ready. " + "Full message content. " * 30
    upsert_record(database, {"channel_id": -100123, "message_id": 1,
                             "channel_name": "Release notes", "text": body})
    upsert_record(database, {"channel_id": -100124, "message_id": 1,
                             "channel_name": "Community", "text": "Community is ready."})
    app = gui.CollectorApp()
    app.update_idletasks()
    app.update()
    assert app.process is None
    assert len(app.tabs.tabs()) == 4
    assert app.cget("background") == gui.BG
    assert app.detail.cget("background") == gui.PANEL
    assert app.message_scope.get() == gui.COMBINED
    assert app.query_active is None  # No archive queries until Messages opens.
    app.channel_search.set("@community")
    assert app.channel_tree.get_children() == ("-100124",)

    app.tabs.select(app.messages_tab)
    app.search.set("old search")
    app.refresh_messages()
    app.search.set("ready")
    deadline = time.monotonic() + 5
    while not app.message_tree.get_children() and time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)
    assert app.message_tree.get_children() == ("-100123:1",)
    assert len(app.message_tree.item("-100123:1", "values")[2]) == 180
    assert app.detail_json is None
    app.message_tree.selection_set("-100123:1")
    app.update()
    assert body in app.detail.get("1.0", "end")
    app.reader_mode.set("JSON")
    app.render_message()
    assert '"message_id": 1' in app.detail.get("1.0", "end")
    app.message_scope.set("Community (-100124)")
    app.scope_picker.event_generate("<<ComboboxSelected>>")
    deadline = time.monotonic() + 5
    while app.message_tree.get_children() != ("-100124:1",) and time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)
    assert app.message_tree.get_children() == ("-100124:1",)
    assert app.detail_json is None
    app.search.set("no matching message")
    deadline = time.monotonic() + 5
    while app.message_tree.get_children() and time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)
    assert not app.message_tree.get_children()
    assert app.detail_json is None
    app.destroy()
print("Desktop window smoke check passed")
