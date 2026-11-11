"""Native desktop interface for the Telegram collector (Python/Tkinter)."""

from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import webbrowser

from telagent import (ROOT, GUI_PROMPT, atomic_json, load_config,
                      normalize_username, add_to_config, sync_channel_selection)
from settings import read_config, read_credentials, save_credentials, load_credentials
from platform_utils import FileLock, worker_python, worker_options, kill_worker, open_folder

CONFIG = ROOT / "config.json"
CACHE = ROOT / "data" / "channels.json"
BG = "#12151e"
PANEL = "#1b202d"
TEXT = "#e8edf7"
MUTED = "#a9b5cb"
ACCENT = "#9b9aff"
BORDER = "#30394e"
RAIL = "#0e1119"
BUTTON = "#665de0"
COMBINED = "All selected channels"
ARCHIVE = "All archived messages"


def read_settings():
    return read_config(CONFIG)


def configured_path(settings, key, default):
    path = Path(settings.get(key, default))
    return path if path.is_absolute() else ROOT / path


def query_messages(database, search="", scope=None, *, previews=False):
    if not database.exists():
        return 0, []
    # Read-only connections never create a second accidental database.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True,
                                timeout=0.5)) as db:
        db.execute("BEGIN")
        source = "collective_messages" if scope == COMBINED else "messages"
        filters = []
        params = []
        if isinstance(scope, int):
            filters.append("channel_id=?")
            params.append(scope)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        total = db.execute(f"SELECT count(*) FROM {source}{where}", params).fetchone()[0]
        needle = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        columns = "substr(text, 1, 180), channel_id, edited_at" if previews else "text, record_json"
        if search:
            filters.append("(text LIKE ? ESCAPE '\\' OR channel_name LIKE ? ESCAPE '\\')")
            params.extend((needle, needle))
        where = " WHERE " + " AND ".join(filters) if filters else ""
        rows = db.execute(f"""SELECT channel_name, message_id, published_at, {columns} FROM {source}{where}
                              ORDER BY published_at DESC, channel_id, message_id DESC LIMIT 500""",
                          params).fetchall()
    return total, rows


class CollectorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Telagent — Telegram Collector")
        self.geometry("1160x720")
        self.minsize(970, 640)
        self.configure(bg=BG)
        self.process = None
        self.job = None
        self.stopping = False
        self.closing_app = False
        self.events = queue.Queue()
        self.message_rows = {}
        self.message_db = None
        self.query_active = None
        self.query_pending = None
        self.search_timer = None
        self.detail_json = None
        self.detail_key = None
        self.reader_mode = tk.StringVar(value="Message")
        self.channel_rows = []
        self.selected_channels = set()
        self.channel_search = tk.StringVar()
        self.settings = read_settings()
        self.status = tk.StringVar(value="Monitoring paused")
        self.summary = tk.StringVar()
        self.message_count = tk.StringVar(value="No messages yet")
        self.search = tk.StringVar()
        self.message_scope = tk.StringVar(value=COMBINED)
        self.message_scopes = {COMBINED: COMBINED, ARCHIVE: None}
        self.username = tk.StringVar()
        self.limit = tk.StringVar(value="100")
        credentials = read_credentials(CONFIG)
        self.api_id = tk.StringVar(value=str(credentials["api_id"]))
        self.api_hash = tk.StringVar(value=credentials["api_hash"])
        import tkinter.font as tkfont
        available = set(tkfont.families(self))
        self.ui_font = next((name for name in ("Segoe UI", "Helvetica Neue", "DejaVu Sans")
                             if name in available), "TkDefaultFont")
        self.mono_font = next((name for name in ("Consolas", "Menlo", "DejaVu Sans Mono")
                               if name in available), "TkFixedFont")
        self._style()
        self._build()
        self.refresh_channels()
        self.after(120, self.poll_events)
        self.after(3000, self.auto_refresh)
        self.protocol("WM_DELETE_WINDOW", self.close_app)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=(self.ui_font, 10), background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=(self.ui_font, 25, "bold"))
        style.configure("Heading.TLabel", font=(self.ui_font, 13, "bold"))
        style.configure("Rail.TFrame", background=RAIL)
        style.configure("Brand.TLabel", background=RAIL, foreground=TEXT,
                        font=(self.ui_font, 21, "bold"))
        style.configure("Card.TFrame", background=PANEL)
        style.configure("Card.TLabel", background=PANEL)
        style.configure("TButton", background=PANEL, foreground=TEXT, padding=(13, 9),
                        bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL, relief="flat")
        style.map("TButton", background=[("disabled", BG), ("active", "#2a3247")],
                  foreground=[("disabled", "#8591a9")])
        style.configure("Accent.TButton", background=BUTTON, foreground="#ffffff",
                        bordercolor=BUTTON, lightcolor=BUTTON, darkcolor=BUTTON)
        style.map("Accent.TButton", background=[("disabled", "#252c3e"), ("active", "#5952c9")],
                  foreground=[("disabled", "#8591a9"), ("!disabled", "#ffffff")])
        for name, background, foreground in (("Nav", RAIL, "#cad4e6"),
                                             ("ActiveNav", "#343c6b", TEXT)):
            style.configure(f"{name}.TButton", anchor="w", padding=(14, 11),
                            background=background, foreground=foreground,
                            bordercolor=background, lightcolor=background, darkcolor=background)
            style.map(f"{name}.TButton", background=[("active", "#343c6b")],
                      foreground=[("active", TEXT)])
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT,
                        padding=8, bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL)
        style.map("TEntry", bordercolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=PANEL, foreground=TEXT,
                        arrowcolor=MUTED, padding=8, bordercolor=BORDER,
                        lightcolor=PANEL, darkcolor=PANEL)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", "#363966")
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        style.configure("TScrollbar", background="#30384d", troughcolor=BG,
                        arrowcolor=MUTED, bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.configure("Workspace.TNotebook", background=BG, borderwidth=0, tabmargins=0,
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.layout("Workspace.TNotebook.Tab", [])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=36, borderwidth=0, relief="flat")
        style.configure("Treeview.Heading", background="#242b3c", foreground=MUTED,
                        bordercolor=BORDER, lightcolor="#242b3c", darkcolor="#242b3c",
                        font=(self.ui_font, 10, "bold"), padding=8)
        style.map("Treeview", background=[("selected", "#363966")],
                  foreground=[("selected", TEXT)])

    def _build(self):
        rail = ttk.Frame(self, style="Rail.TFrame", padding=(16, 22))
        rail.pack(side="left", fill="y")
        ttk.Label(rail, text="Telagent", style="Brand.TLabel").pack(anchor="w", padx=10, pady=(0, 24))
        workspace = ttk.Frame(self, padding=(24, 20, 24, 16))
        workspace.pack(side="left", fill="both", expand=True)

        header = ttk.Frame(workspace)
        header.pack(fill="x", pady=(0, 16))
        self.page_title = tk.StringVar(value="Channels")
        ttk.Label(header, textvariable=self.page_title, style="Title.TLabel").pack(side="left")
        self.stop_button = ttk.Button(header, text="Stop", command=self.stop_job, state="disabled")
        self.stop_button.pack(side="right", padx=(8, 0))
        self.start_button = ttk.Button(header, text="Start monitoring", style="Accent.TButton",
                                       command=lambda: self.start_job("watch"))
        self.start_button.pack(side="right")
        status = ttk.Frame(workspace)
        status.pack(fill="x", pady=(0, 14))
        ttk.Label(status, textvariable=self.status, foreground=ACCENT).pack(side="left")
        ttk.Label(status, textvariable=self.summary, style="Muted.TLabel").pack(side="right")
        ttk.Separator(workspace).pack(fill="x", pady=(0, 14))

        self.tabs = ttk.Notebook(workspace, style="Workspace.TNotebook")
        self.tabs.pack(fill="both", expand=True)
        self.channels_tab = ttk.Frame(self.tabs)
        self.messages_tab = ttk.Frame(self.tabs)
        self.settings_tab = ttk.Frame(self.tabs)
        self.activity_tab = ttk.Frame(self.tabs)
        self.navigation = {}
        for frame, name in ((self.channels_tab, "Channels"), (self.messages_tab, "Messages"),
                            (self.activity_tab, "Activity"), (self.settings_tab, "Settings")):
            self.tabs.add(frame, text=name)
            button = ttk.Button(rail, text=name, width=14, style="Nav.TButton",
                                command=lambda page=frame: self.tabs.select(page))
            button.pack(fill="x", pady=3)
            self.navigation[str(frame)] = (name, button)
        self._channels_ui()
        self._messages_ui()
        self._settings_ui()
        self.log = self.text_panel(self.activity_tab, font=(self.mono_font, 10))
        self.tabs.bind("<<NotebookTabChanged>>", self.page_changed)
        self.page_changed()
        shortcut = "Command" if self.tk.call("tk", "windowingsystem") == "aqua" else "Control"
        self.bind(f"<{shortcut}-f>", self.focus_search)

    def page_changed(self, _=None):
        current = self.tabs.select()
        for page, (name, button) in self.navigation.items():
            button.configure(style="ActiveNav.TButton" if page == current else "Nav.TButton")
            if page == current:
                self.page_title.set(name)
        if current == str(self.messages_tab):
            self.refresh_messages()

    def focus_search(self, _):
        if self.tabs.select() == str(self.channels_tab):
            entry = self.channel_search_entry
        elif self.tabs.select() == str(self.messages_tab):
            entry = self.message_search_entry
        else:
            return
        entry.focus_set()
        entry.selection_range(0, "end")
        return "break"

    def text_panel(self, parent, **options):
        holder = ttk.Frame(parent, style="Card.TFrame")
        holder.pack(fill="both", expand=True)
        widget = tk.Text(holder, bg=PANEL, fg=TEXT, relief="flat", wrap="word",
                         state="disabled", padx=14, pady=14, **options)
        scroll = ttk.Scrollbar(holder, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        widget.pack(side="left", fill="both", expand=True)
        return widget

    def _channels_ui(self):
        frame = self.channels_tab
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="Find a channel", width=17).pack(side="left", padx=(0, 10))
        self.channel_search_entry = ttk.Entry(row, textvariable=self.channel_search)
        self.channel_search_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.channel_search.trace_add("write", lambda *_: self.render_channels())
        self.refresh_button = ttk.Button(row, text="Connect / refresh channels",
                                         command=self.refresh_from_telegram)
        self.refresh_button.pack(side="right")
        add = ttk.Frame(frame)
        add.pack(fill="x", pady=(0, 12))
        ttk.Label(add, text="@username or link", width=17).pack(side="left", padx=(0, 10))
        entry = ttk.Entry(add, textvariable=self.username)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry.bind("<Return>", lambda _: self.add_username())
        ttk.Button(add, text="Add channel", command=self.add_username).pack(side="right")

        footer = ttk.Frame(frame)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        history = ttk.Frame(footer)
        history.pack(fill="x")
        ttk.Label(history, text="History per channel").pack(side="left")
        ttk.Entry(history, textvariable=self.limit, width=6).pack(side="left", padx=8)
        self.backfill_button = ttk.Button(history, text="Import history", command=self.backfill)
        self.backfill_button.pack(side="left", padx=(0, 8))
        self.sync_button = ttk.Button(history, text="Sync JSON to database",
                                      command=lambda: self.start_job("sync-db"))
        self.sync_button.pack(side="left")
        self.toggle_button = ttk.Button(history, text="Toggle monitoring", command=self.toggle_channels,
                                        state="disabled")
        self.toggle_button.pack(side="right")
        self.channel_hint = tk.StringVar(value="Double-click a channel to toggle monitoring.")
        ttk.Label(footer, textvariable=self.channel_hint, style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        self.channel_tree = self.tree(frame, (("state", "Status", 105), ("title", "Channel", 260),
                                              ("username", "Username", 170), ("id", "Channel ID", 134)))
        self.channel_tree.tag_configure("monitored", foreground="#91dbc1", background="#1d302f")
        self.channel_tree.bind("<Double-1>", self.toggle_clicked_channel)
        self.channel_tree.bind("<space>", lambda _: self.toggle_channels())
        self.channel_tree.bind("<<TreeviewSelect>>", self.channel_selection_changed)

    def toggle_clicked_channel(self, event):
        row = self.channel_tree.identify_row(event.y)
        if row and self.channel_tree.identify_region(event.x, event.y) == "cell":
            self.channel_tree.selection_set(row)
            self.toggle_channels()
            return "break"

    def channel_selection_changed(self, _=None):
        self.toggle_button.configure(state="normal" if self.channel_tree.selection() else "disabled")

    def tree(self, parent, columns, *, selectmode="extended"):
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        tree = ttk.Treeview(holder, columns=[c[0] for c in columns], show="headings",
                            selectmode=selectmode, height=5)
        for key, label, width in columns:
            tree.heading(key, text=label, anchor="w")
            tree.column(key, width=width, minwidth=65)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return tree

    def _messages_ui(self):
        row = ttk.Frame(self.messages_tab)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="Channel").pack(side="left", padx=(0, 8))
        self.scope_picker = ttk.Combobox(row, textvariable=self.message_scope, width=25,
                                         state="readonly", values=tuple(self.message_scopes))
        self.scope_picker.pack(side="left", padx=(0, 14))
        self.scope_picker.bind("<<ComboboxSelected>>", lambda _: self.refresh_messages())
        ttk.Label(row, text="Search").pack(side="left", padx=(0, 8))
        self.message_search_entry = ttk.Entry(row, textvariable=self.search)
        self.message_search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.message_search_entry.bind("<Escape>", lambda _: self.search.set(""))
        ttk.Button(row, text="Refresh", command=self.refresh_messages).pack(side="left")
        self.search.trace_add("write", self.schedule_search)

        footer = ttk.Frame(self.messages_tab)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Label(footer, textvariable=self.message_count, style="Muted.TLabel").pack(side="left")
        ttk.Button(footer, text="JSON folder", command=self.open_output).pack(side="right")
        ttk.Button(footer, text="Database folder", command=self.open_database_folder).pack(side="right", padx=8)

        panes = ttk.Panedwindow(self.messages_tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        listing = ttk.Frame(panes, padding=(0, 0, 8, 0))
        reader = ttk.Frame(panes, style="Card.TFrame")
        panes.add(listing, weight=3)
        panes.add(reader, weight=2)
        self.message_tree = self.tree(listing, (("channel", "Channel", 126),
                                              ("date", "Date (UTC)", 116),
                                              ("text", "Preview", 160)), selectmode="browse")
        self.message_tree.bind("<<TreeviewSelect>>", self.show_message)

        bar = ttk.Frame(reader, style="Card.TFrame", padding=(12, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="Reader", style="Card.TLabel").pack(side="left", fill="x", expand=True)
        self.reader_buttons = {}
        for mode in ("JSON", "Message"):
            button = ttk.Button(bar, text=mode, command=lambda value=mode: self.set_reader_mode(value))
            button.pack(side="right", padx=(4, 0))
            self.reader_buttons[mode] = button
        actions = ttk.Frame(reader, style="Card.TFrame", padding=(12, 8))
        actions.pack(side="bottom", fill="x")
        self.export_button = ttk.Button(actions, text="Export JSON", command=self.export_message, state="disabled")
        self.export_button.pack(side="right")
        self.detail = self.text_panel(reader, font=(self.ui_font, 11), width=27)
        self.detail.tag_configure("title", font=(self.ui_font, 14, "bold"))
        self.detail.tag_configure("date", foreground=MUTED, spacing3=12)
        self.render_message()


    def _settings_ui(self):
        frame = self.settings_tab
        ttk.Label(frame, text="Telegram connection", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Credentials are stored in your local .env file. Your saved login is reused.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 22))
        ttk.Label(frame, text="API ID").pack(anchor="w")
        self.id_entry = ttk.Entry(frame, textvariable=self.api_id, width=52)
        self.id_entry.pack(anchor="w", pady=(6, 16))
        ttk.Label(frame, text="API hash").pack(anchor="w")
        self.hash_entry = ttk.Entry(frame, textvariable=self.api_hash, show="•", width=52)
        self.hash_entry.pack(anchor="w", pady=(6, 20))
        self.save_button = ttk.Button(frame, text="Save connection settings", command=self.save_settings)
        self.save_button.pack(anchor="w")
        ttk.Button(frame, text="Open Telegram API setup", command=lambda: webbrowser.open("https://my.telegram.org")).pack(anchor="w", pady=12)
        ttk.Label(frame, text="Use Connect / refresh channels to sign in or refresh your followed channels.\n"
                  "If needed, the app asks for your phone number, login code, and two-step password.\n"
                  "Codes and passwords are used only for login and are never saved to configuration.\n\n"
                  "Attachments are stored as metadata. Binary media files are not downloaded.\n"
                  "Monitoring captures new posts and edits while the app is running.\n"
                  "Use Import history to retrieve recent messages after downtime.",
                  style="Muted.TLabel", justify="left", wraplength=820).pack(anchor="w", pady=20)

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {text.rstrip()}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 1200:
            self.log.delete("1.0", "200.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def save_settings(self):
        try:
            if self.process:
                raise ValueError("Stop the current task before changing connection settings.")
            settings = read_settings()
            api_id = int(self.api_id.get().strip())
            api_hash = self.api_hash.get().strip()
            if api_id <= 0 or not re_api_hash(api_hash):
                raise ValueError("Enter a positive API ID and a 32-character hexadecimal API hash.")
            save_credentials(CONFIG, api_id, api_hash)
            atomic_json(CONFIG, settings)
            self.settings = settings
            self.status.set("Credentials saved to .env")
            if any(key in os.environ for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")):
                messagebox.showinfo("Environment override", "System environment variables take precedence over .env. "
                                    "Update or unset them to use the values saved here.", parent=self)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Connection settings", str(exc), parent=self)

    def refresh_channels(self):
        try:
            self.settings = read_settings()
            selected = sync_channel_selection(CONFIG)
            entries = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else []
            known = {entry["id"] for entry in entries}
            entries += [{"id": cid, "title": "Refresh to load channel name", "username": None}
                        for cid in selected - known]
            entries.sort(key=lambda e: (e["id"] not in selected, e["title"].lower()))
            self.channel_rows = entries
            self.selected_channels = selected
            self.render_channels()
            self.refresh_message_scopes()
            self.summary.set(f"{len(selected)} selected  ·  {len(entries)} followed channels")
            self.refresh_messages()
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.append_log(f"Could not load channel list: {exc}")

    def refresh_message_scopes(self):
        current = self.message_scopes.get(self.message_scope.get(), COMBINED)
        scopes = {COMBINED: COMBINED, ARCHIVE: None}
        names = {entry["id"]: entry["title"] for entry in self.channel_rows}
        database = configured_path(self.settings, "database_path", "data/messages.sqlite3")
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)) as db:
            for (cid,) in db.execute("SELECT DISTINCT channel_id FROM messages"):
                if cid not in names:
                    name, = db.execute("SELECT channel_name FROM messages WHERE channel_id=? LIMIT 1", (cid,)).fetchone()
                    names[cid] = name or str(cid)
        for cid, name in sorted(names.items(), key=lambda item: item[1].casefold()):
            scopes[f"{name} ({cid})"] = cid
        self.message_scopes = scopes
        self.scope_picker.configure(values=tuple(scopes))
        self.message_scope.set(next((label for label, value in scopes.items() if value == current), COMBINED))

    def render_channels(self):
        needle = self.channel_search.get().strip().casefold().lstrip("@")
        rows = {}
        for entry in self.channel_rows:
            cid, title = entry["id"], entry["title"]
            username = entry.get("username") or ""
            if needle not in f"{title} {username} {cid}".casefold():
                continue
            monitored = cid in self.selected_channels
            rows[str(cid)] = (("Selected" if monitored else "Available", title,
                               f"@{username}" if username else "Private", str(cid)),
                              ("monitored",) if monitored else ())
        self.update_tree(self.channel_tree, rows)
        if not self.channel_rows:
            hint = "Connect to Telegram to load followed channels."
        elif not rows:
            hint = "No channels match your search."
        else:
            hint = "Double-click a channel to toggle monitoring."
        self.channel_hint.set(hint)
        self.channel_selection_changed()

    def update_tree(self, tree, rows):
        current = tree.get_children()
        for key in current:
            if key not in rows:
                tree.delete(key)
        for key, (values, tags) in rows.items():
            if not tree.exists(key):
                tree.insert("", "end", iid=key, values=values, tags=tags)
            else:
                item = tree.item(key)
                if tuple(map(str, item["values"])) != values or tuple(item["tags"]) != tags:
                    tree.item(key, values=values, tags=tags)
        if tree.get_children() != tuple(rows):
            tree.set_children("", *rows)

    def toggle_channels(self):
        ids = self.channel_tree.selection()
        if not ids:
            return
        try:
            config = load_config(CONFIG)
            selected = set(config.get("channels", []))
            for cid in ids:
                if int(cid) in selected:
                    selected.remove(int(cid))
                else:
                    selected.add(int(cid))
            config["channels"] = sorted(selected)
            atomic_json(CONFIG, config)
            self.refresh_channels()
        except (ValueError, OSError) as exc:
            messagebox.showerror("Channel selection", str(exc), parent=self)

    def add_username(self):
        try:
            username = normalize_username(self.username.get())
            entries = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else []
            entry = next((e for e in entries if (e.get("username") or "").lower() == username), None)
            if entry is None:
                raise ValueError("Channel not found in your followed-channel list. Follow it in Telegram, "
                                 "then click Connect / refresh channels and try again.")
            added = add_to_config(CONFIG, entry["id"])
            self.channel_search.set("")
            self.refresh_channels()
            self.channel_tree.selection_set(str(entry["id"]))
            self.channel_tree.see(str(entry["id"]))
            self.username.set("")
            self.append_log(f"{'Added' if added else 'Already selected'} @{username} ({entry['id']})")
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror("Add channel", str(exc), parent=self)

    def refresh_from_telegram(self):
        if self.process:
            messagebox.showinfo("Refresh channels", "Stop monitoring before refreshing your followed-channel list.", parent=self)
            return
        self.start_job("list-channels")

    def backfill(self):
        try:
            count = int(self.limit.get())
            if count <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Import history", "Enter a positive message count.", parent=self)
            return
        self.start_job("backfill", "--limit", str(count))

    def start_job(self, command, *extra):
        if self.process:
            messagebox.showinfo("Task already running", "Stop the current task first.", parent=self)
            return
        try:
            config = load_config(CONFIG)
            if command != "sync-db":
                load_credentials(CONFIG)
            if command in ("watch", "backfill") and not config.get("channels"):
                raise ValueError("Choose at least one channel first.")
            python = worker_python()
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen([str(python), "-u", str(ROOT / "telagent.py"),
                                     "--config", str(CONFIG), "--gui", command, *extra],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1, cwd=ROOT, env=env,
                                    **worker_options())
            self.process, self.job = proc, command
            self.stopping = False
            self.set_busy(True)
            self.status.set({"watch": "Connecting to Telegram…", "backfill": "Importing history…",
                             "list-channels": "Loading followed channels…", "sync-db": "Syncing database…"}[command])
            self.append_log(f"Started {command}")
            threading.Thread(target=self.read_worker, args=(proc,), daemon=True).start()
        except (ValueError, OSError) as exc:
            messagebox.showerror("Unable to start", str(exc), parent=self)
            if not CONFIG.exists():
                self.tabs.select(self.settings_tab)

    def read_worker(self, proc):
        for line in proc.stdout:
            self.events.put(("line", proc, line.rstrip()))
        self.events.put(("exit", proc, proc.wait()))

    def set_busy(self, busy):
        for widget in (self.start_button, self.refresh_button, self.backfill_button,
                       self.sync_button, self.save_button, self.id_entry, self.hash_entry):
            widget.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def poll_events(self):
        try:
            for _ in range(60):
                kind, proc, value = self.events.get_nowait()
                if kind == "messages":
                    self.finish_message_query(proc, value)
                    continue
                if proc is not self.process:
                    continue
                if kind == "exit":
                    job, stopped = self.job, self.stopping
                    self.process, self.job = None, None
                    proc.stdout.close()
                    proc.stdin.close()
                    self.set_busy(False)
                    self.status.set("Monitoring paused" if stopped else
                                    ("Task complete" if value == 0 else "Task failed · see Activity"))
                    self.append_log(f"{job} {'stopped' if stopped else 'finished'} (exit {value})")
                    self.refresh_channels()
                    self.refresh_messages()
                    if self.closing_app:
                        self.destroy()
                        return
                elif value.startswith(GUI_PROMPT):
                    if not self.stopping:
                        self.login_prompt(json.loads(value[len(GUI_PROMPT):])["kind"])
                else:
                    self.append_log(value)
                    if "INFO Monitoring " in value and not self.stopping:
                        self.status.set("Live · saving to JSON and SQLite")
        except queue.Empty:
            pass
        self.after(120, self.poll_events)

    def login_prompt(self, kind):
        prompts = {"phone": "Phone number with country code (for example +91…):",
                   "code": "Enter the login code sent by Telegram:",
                   "password": "Enter your Telegram two-step verification password:"}
        answer = simpledialog.askstring("Telegram sign in", prompts[kind], parent=self,
                                        show="*" if kind in ("code", "password") else None)
        if answer is None:
            self.stop_job()
        elif self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(answer.strip() + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                self.append_log("Connection closed during login. Please try again.")

    def stop_job(self):
        if not self.process or self.stopping:
            return
        self.stopping = True
        self.status.set("Stopping…")
        self.stop_button.configure(state="disabled")
        proc = self.process
        try:
            proc.stdin.write("__STOP__\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        self.after(5000, lambda: self.force_stop(proc))

    def force_stop(self, proc):
        if proc.poll() is None:
            threading.Thread(target=kill_worker, args=(proc,), daemon=True).start()

    def schedule_search(self, *_):
        if self.search_timer is not None:
            self.after_cancel(self.search_timer)
        self.search_timer = self.after(180, self.run_search)

    def run_search(self):
        self.search_timer = None
        self.refresh_messages()

    def refresh_messages(self):
        if self.closing_app or self.tabs.select() != str(self.messages_tab):
            return
        try:
            database = configured_path(read_settings(), "database_path", "data/messages.sqlite3")
        except (OSError, ValueError) as exc:
            self.message_count.set(f"Could not load storage settings: {exc}")
            return
        request = (database, *self.message_filter())
        if self.query_active is not None:
            self.query_pending = request if request != self.query_active else None
        else:
            self.start_message_query(request)

    def start_message_query(self, request):
        self.query_active = request
        threading.Thread(target=self.read_message_previews, args=(request,), daemon=True).start()

    def message_filter(self):
        scope = self.message_scopes.get(self.message_scope.get(), COMBINED)
        selection = tuple(sorted(self.selected_channels)) if scope == COMBINED else ()
        return self.search.get().strip(), scope, selection

    def read_message_previews(self, request):
        try:
            result = query_messages(*request[:3], previews=True)
        except (sqlite3.Error, OSError, ValueError) as exc:
            result = exc
        self.events.put(("messages", request, result))

    def finish_message_query(self, request, result):
        pending = self.query_pending
        self.query_active = self.query_pending = None
        if self.closing_app or self.tabs.select() != str(self.messages_tab):
            return
        current = self.message_filter()
        if pending is not None and pending[1:] == current:
            self.start_message_query(pending)
            return
        database, search, scope, _ = request
        if request[1:] != current:
            return
        if isinstance(result, Exception):
            self.message_count.set(f"Could not read database: {result}")
            return

        total, rows = result
        empty = "No selected channels" if scope == COMBINED and not self.selected_channels else "No messages in this feed"
        self.message_count.set(f"{len(rows):,} shown  /  {total:,} in this feed" if rows else
                               ("No matching messages" if search and total else empty))
        by_id = {f"{row[4]}:{row[1]}": row for row in rows}
        if by_id == self.message_rows and database == self.message_db:
            return
        selection = self.message_tree.selection()
        selected = selection[0] if selection else None
        refresh_detail = (database != self.message_db or
                          self.message_rows.get(selected) != by_id.get(selected))
        self.message_db = database
        self.message_rows = by_id
        self.update_tree(self.message_tree, {
            key: ((row[0] or "", (row[2] or "")[:16].replace("T", " "),
                   (row[3] or "[Media or service message]").replace("\n", " ")), ())
            for key, row in by_id.items()
        })
        if refresh_detail:
            self.detail_key = None
            self.show_message()

    def show_message(self, _=None):
        selection = self.message_tree.selection()
        key = selection[0] if selection else None
        if key == self.detail_key and (key is not None or self.detail_json is None):
            return
        self.detail_key = key
        self.detail_json = None
        row = self.message_rows.get(key)
        if row is not None:
            try:
                uri = self.message_db.resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True, timeout=0.05)) as db:
                    result = db.execute("SELECT record_json FROM messages WHERE channel_id=? AND message_id=?",
                                        (row[4], row[1])).fetchone()
                if result is not None:
                    self.detail_json = json.loads(result[0])
            except (sqlite3.Error, OSError, ValueError):
                self.detail_key = None
        self.render_message()

    def set_reader_mode(self, mode):
        self.reader_mode.set(mode)
        self.render_message()

    def render_message(self):
        record = self.detail_json
        mode = self.reader_mode.get()
        for name, button in self.reader_buttons.items():
            button.configure(style="Accent.TButton" if name == mode else "TButton")
        self.export_button.configure(state="normal" if record else "disabled")
        font = self.mono_font if mode == "JSON" else self.ui_font
        self.detail.configure(state="normal", font=(font, 11))
        self.detail.delete("1.0", "end")
        if record is None:
            self.detail.insert("end", "Select a message to read it here.")
        elif mode == "JSON":
            self.detail.insert("end", json.dumps(record, ensure_ascii=False, indent=2))
        else:
            channel = record.get("channel_name") or "Unknown channel"
            date = (record.get("published_at") or "")[:19].replace("T", " ")
            body = record.get("text") or "This message contains media or service metadata. Choose JSON to inspect it."
            self.detail.insert("end", f"{channel}\n", "title")
            if date:
                self.detail.insert("end", f"{date} UTC\n", "date")
            self.detail.insert("end", body)
            if record.get("message_url"):
                self.detail.insert("end", f"\n\n{record['message_url']}")
        self.detail.configure(state="disabled")

    def export_message(self):
        record = self.detail_json
        if record is None:
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                    initialfile=f"{record['channel_id']}_{record['message_id']}.json",
                    filetypes=[("JSON", "*.json")])
        if path:
            try:
                atomic_json(Path(path), record)
                self.append_log("Exported selected message")
            except OSError as exc:
                messagebox.showerror("Export JSON", str(exc), parent=self)


    def open_output(self):
        self.open_folder(configured_path(read_settings(), "output_dir", "data/messages"))

    def open_database_folder(self):
        self.open_folder(configured_path(read_settings(), "database_path", "data/messages.sqlite3").parent)

    def open_folder(self, path):
        try:
            open_folder(path)
        except OSError as exc:
            messagebox.showerror("Open folder", str(exc), parent=self)

    def auto_refresh(self):
        if not self.closing_app:
            self.refresh_messages()
            self.after(3000, self.auto_refresh)

    def close_app(self):
        self.closing_app = True
        if self.process:
            self.stop_job()
        else:
            self.destroy()


def re_api_hash(value):
    return len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)


def main():
    with FileLock(ROOT / "data" / "gui.lock"):
        CollectorApp().mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        messagebox.showerror("Telagent", f"Unable to open Telagent: {exc}")
        raise
