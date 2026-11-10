"""Native desktop interface for the Telegram collector (Python/Tkinter)."""

from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import webbrowser

from telagent import (ROOT, GUI_PROMPT, atomic_json, load_config,
                      normalize_username, add_to_config)
from settings import read_config, read_credentials, save_credentials, load_credentials
from platform_utils import FileLock, worker_python, worker_options, kill_worker, open_folder

CONFIG = ROOT / "config.json"
CACHE = ROOT / "data" / "channels.json"
BG = "#101827"
PANEL = "#182438"
TEXT = "#edf3fb"
MUTED = "#a5b5ce"
ACCENT = "#68d8c3"


def read_settings():
    return read_config(CONFIG)


def configured_path(settings, key, default):
    path = Path(settings.get(key, default))
    return path if path.is_absolute() else ROOT / path


def query_messages(database, search=""):
    if not database.exists():
        return 0, []
    # Read-only connections never create a second accidental database.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True,
                                timeout=0.5)) as db:
        total = db.execute("SELECT count(*) FROM messages").fetchone()[0]
        needle = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = db.execute("""SELECT channel_name, message_id, published_at, text,
                             record_json FROM messages
                             WHERE text LIKE ? ESCAPE '\\' OR channel_name LIKE ? ESCAPE '\\'
                             ORDER BY published_at DESC, channel_id, message_id DESC LIMIT 500""",
                          (needle, needle)).fetchall()
    return total, rows


class CollectorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Telagent — Telegram Collector")
        self.geometry("1120x800")
        self.minsize(960, 700)
        self.configure(bg=BG)
        self.process = None
        self.job = None
        self.stopping = False
        self.closing_app = False
        self.events = queue.Queue()
        self.records = {}
        self.last_rows = None
        self.channel_rows = []
        self.settings = read_settings()
        self.status = tk.StringVar(value="Ready · monitoring stopped")
        self.summary = tk.StringVar()
        self.message_count = tk.StringVar(value="No messages yet")
        self.search = tk.StringVar()
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
        self.refresh_messages()
        self.after(120, self.poll_events)
        self.after(3000, self.auto_refresh)
        self.protocol("WM_DELETE_WINDOW", self.close_app)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=(self.ui_font, 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=(self.ui_font, 24, "bold"))
        style.configure("Heading.TLabel", font=(self.ui_font, 15, "bold"))
        style.configure("TButton", background="#263a54", foreground=TEXT,
                        padding=(13, 9), borderwidth=0)
        style.map("TButton", background=[("active", "#36516f"), ("disabled", "#1d2b3e")],
                  foreground=[("disabled", "#6f829e")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#0a2923")
        style.map("Accent.TButton", background=[("active", "#91ebda"), ("disabled", "#254a47")])
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT,
                        insertcolor=TEXT, padding=8)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(22, 12), background=PANEL, foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", "#263a54")],
                  foreground=[("selected", TEXT)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=34, borderwidth=0)
        style.configure("Treeview.Heading", background="#22344c", foreground=TEXT,
                        font=(self.ui_font, 10, "bold"), padding=7)
        style.map("Treeview", background=[("selected", "#31526a")])
        style.configure("TLabelframe", background=BG, bordercolor="#30415b")
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED)

    def _build(self):
        header = ttk.Frame(self, padding=(26, 22, 26, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Telagent", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="  /  Your Telegram archive", style="Muted.TLabel").pack(side="left", pady=(10, 0))
        self.stop_button = ttk.Button(header, text="Stop", command=self.stop_job, state="disabled")
        self.stop_button.pack(side="right", padx=(8, 0))
        self.start_button = ttk.Button(header, text="Start monitoring", style="Accent.TButton",
                                       command=lambda: self.start_job("watch"))
        self.start_button.pack(side="right")
        strip = ttk.Frame(self, padding=(28, 0, 28, 18))
        strip.pack(fill="x")
        ttk.Label(strip, textvariable=self.status, foreground=ACCENT).pack(side="left")
        ttk.Label(strip, textvariable=self.summary, style="Muted.TLabel").pack(side="right")

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=26, pady=(0, 12))
        self.channels_tab = ttk.Frame(self.tabs, padding=18)
        self.messages_tab = ttk.Frame(self.tabs, padding=18)
        self.settings_tab = ttk.Frame(self.tabs, padding=24)
        self.activity_tab = ttk.Frame(self.tabs, padding=18)
        for frame, name in ((self.channels_tab, "Channels"), (self.messages_tab, "Messages"),
                            (self.settings_tab, "Settings"), (self.activity_tab, "Activity")):
            self.tabs.add(frame, text=name)
        self._channels_ui()
        self._messages_ui()
        self._settings_ui()
        self.log = tk.Text(self.activity_tab, bg=PANEL, fg=MUTED, relief="flat",
                           font=(self.mono_font, 10), wrap="word", state="disabled", padx=12, pady=12)
        self.log.pack(fill="both", expand=True)
        ttk.Label(self, text="Stored locally • JSON + SQLite • Keep this app open while monitoring",
                  style="Muted.TLabel").pack(pady=(0, 13))

    def _channels_ui(self):
        frame = self.channels_tab
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 9))
        ttk.Label(row, text="Choose what to follow", style="Heading.TLabel").pack(side="left")
        self.refresh_button = ttk.Button(row, text="Connect / refresh channels",
                                         command=self.refresh_from_telegram)
        self.refresh_button.pack(side="right")
        ttk.Label(frame, text="Only channels marked Monitoring are saved. Double-click a row to toggle it.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 14))
        add = ttk.Frame(frame)
        add.pack(fill="x", pady=(0, 14))
        entry = ttk.Entry(add, textvariable=self.username)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry.bind("<Return>", lambda _: self.add_username())
        ttk.Button(add, text="Add @username or link", command=self.add_username).pack(side="right")
        self.channel_tree = self.tree(frame, (("state", "Status", 125), ("title", "Channel", 340),
                                              ("username", "Username", 220), ("id", "Channel ID", 180)))
        self.channel_tree.bind("<Double-1>", lambda _: self.toggle_channels())
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=12)
        ttk.Button(buttons, text="Toggle monitoring for selected", command=self.toggle_channels).pack(side="left")
        ttk.Label(buttons, text="Changes apply while monitoring is running.", style="Muted.TLabel").pack(side="left", padx=15)
        history = ttk.LabelFrame(frame, text="History & storage", padding=12)
        history.pack(fill="x")
        ttk.Label(history, text="Recent messages per selected channel").pack(side="left")
        ttk.Entry(history, textvariable=self.limit, width=8).pack(side="left", padx=10)
        self.backfill_button = ttk.Button(history, text="Import history", command=self.backfill)
        self.backfill_button.pack(side="left", padx=(0, 8))
        self.sync_button = ttk.Button(history, text="Sync JSON to database", command=lambda: self.start_job("sync-db"))
        self.sync_button.pack(side="left")

    def tree(self, parent, columns):
        holder = ttk.Frame(parent)
        holder.pack(fill="both", expand=True)
        tree = ttk.Treeview(holder, columns=[c[0] for c in columns], show="headings", selectmode="extended", height=5)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=65)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return tree

    def _messages_ui(self):
        row = ttk.Frame(self.messages_tab)
        row.pack(fill="x", pady=(0, 12))
        ttk.Entry(row, textvariable=self.search).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(row, text="Search / refresh", command=self.refresh_messages).pack(side="left")
        self.search.trace_add("write", lambda *_: self.refresh_messages())
        self.message_tree = self.tree(self.messages_tab, (("channel", "Channel", 200),
                                      ("date", "Published (UTC)", 190), ("text", "Message preview", 580)))
        self.message_tree.bind("<<TreeviewSelect>>", self.show_message)
        ttk.Label(self.messages_tab, textvariable=self.message_count, style="Muted.TLabel").pack(anchor="w", pady=8)
        self.detail = tk.Text(self.messages_tab, height=9, bg=PANEL, fg=TEXT, relief="flat",
                              font=(self.mono_font, 10), wrap="word", state="disabled", padx=10, pady=10)
        self.detail.pack(fill="x")
        buttons = ttk.Frame(self.messages_tab)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Export selected JSON", command=self.export_message).pack(side="left")
        ttk.Button(buttons, text="Open JSON folder", command=self.open_output).pack(side="left", padx=8)
        ttk.Button(buttons, text="Open database folder", command=self.open_database_folder).pack(side="left")

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
            selected = set(self.settings.get("channels", []))
            entries = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else []
            known = {entry["id"] for entry in entries}
            entries += [{"id": cid, "title": "Refresh to load channel name", "username": None}
                        for cid in selected - known]
            entries.sort(key=lambda e: (e["id"] not in selected, e["title"].lower()))
            self.channel_rows = entries
            focus = set(self.channel_tree.selection())
            self.channel_tree.delete(*self.channel_tree.get_children())
            for entry in entries:
                cid = entry["id"]
                self.channel_tree.insert("", "end", iid=str(cid), values=(
                    "Monitoring" if cid in selected else "Available", entry["title"],
                    "@" + entry["username"] if entry.get("username") else "Private / no username", cid))
            self.channel_tree.selection_set([str(e["id"]) for e in entries if str(e["id"]) in focus])
            self.summary.set(f"{len(selected)} selected  ·  {len(entries)} followed channels")
        except (ValueError, OSError) as exc:
            self.append_log(f"Could not load channel list: {exc}")

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
            self.refresh_channels()
            self.channel_tree.selection_set(str(entry["id"]))
            self.channel_tree.see(str(entry["id"]))
            self.username.set("")
            self.append_log(f"{'Added' if added else 'Already selected'} @{username} ({entry['id']})")
        except (ValueError, OSError) as exc:
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
            if command != "list-channels" and not config.get("channels"):
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
            while True:
                kind, proc, value = self.events.get_nowait()
                if proc is not self.process:
                    continue
                if kind == "exit":
                    job, stopped = self.job, self.stopping
                    self.process, self.job = None, None
                    proc.stdout.close()
                    proc.stdin.close()
                    self.set_busy(False)
                    self.status.set("Monitoring stopped" if stopped else
                                    ("Ready · task complete" if value == 0 else "Task failed · see Activity"))
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
                    if "INFO Monitoring " in value:
                        self.status.set("Live · saving messages to JSON + SQLite")
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

    def refresh_messages(self):
        try:
            database = configured_path(read_settings(), "database_path", "data/messages.sqlite3")
            total, rows = query_messages(database, self.search.get())
            self.message_count.set(f"{total:,} stored messages · showing {len(rows):,} most recent matches")
            if rows == self.last_rows:
                return
            self.last_rows = rows
            selected = self.message_tree.selection()
            old = self.records.get(selected[0]) if selected else None
            self.message_tree.delete(*self.message_tree.get_children())
            self.records = {}
            for row in rows:
                record = json.loads(row[4])
                key = f"{record['channel_id']}:{record['message_id']}"
                self.records[key] = record
                self.message_tree.insert("", "end", iid=key, values=(row[0], (row[2] or "")[:19].replace("T", " "),
                                          (row[3] or "[media / service message]").replace("\n", " ")[:180]))
                if old and record["channel_id"] == old["channel_id"] and record["message_id"] == old["message_id"]:
                    self.message_tree.selection_set(key)
            if old is None or not self.message_tree.selection():
                self.detail.configure(state="normal")
                self.detail.delete("1.0", "end")
                self.detail.insert("end", "Select a message to inspect its full JSON record.")
                self.detail.configure(state="disabled")
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.message_count.set(f"Could not read database: {exc}")

    def show_message(self, _=None):
        selection = self.message_tree.selection()
        if not selection:
            return
        record = self.records.get(selection[0])
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", json.dumps(record, ensure_ascii=False, indent=2))
        self.detail.configure(state="disabled")

    def export_message(self):
        selection = self.message_tree.selection()
        if not selection:
            messagebox.showinfo("Export JSON", "Select a message first.", parent=self)
            return
        record = self.records[selection[0]]
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
