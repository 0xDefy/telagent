# Telagent

A local Telegram channel collector with a desktop GUI and CLI. Select channels
you follow and save complete message text and metadata to JSON and SQLite.
Built with Python, Telethon, and Tkinter for **Windows, macOS, and Linux**.

## Install

Use Python **3.10 or newer** with Tkinter. Python installers from
[python.org](https://www.python.org/downloads/) for Windows/macOS include Tcl/Tk.
Linux may package it separately: on Debian/Ubuntu install `python3-tk` and
`python3-venv`. The GUI needs a desktop/display; the CLI can run without one.

One-time setup in the project directory:

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**macOS / Linux:**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x 'Open Telagent.command' open-telagent.sh
```

Virtual environments are OS-specific. Recreate `.venv` after cloning on another
computer; do not copy a Windows environment to macOS/Linux.

## Open the desktop app

| System | Launcher |
| --- | --- |
| Windows | Double-click `Open Telagent.vbs` or `Open Telagent.cmd` |
| macOS | Double-click `Open Telagent.command` in Finder |
| Linux | Run `open-telagent.sh`; file-manager behavior varies by desktop |
| Any supported OS | Run `python launch.py` (or `python3 launch.py`) |

The portable launcher prefers the project's `.venv`. No terminal commands are
needed for daily use after setup. Desktop shortcuts are local to the computer;
the committed launchers contain no absolute user paths.

### First connection

1. Get your `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org),
   under **API development tools**.
2. Enter them in **Settings**, then **Save connection settings**. Credentials go
   into `.env`, never into `config.json`.
3. Click **Connect / refresh channels**. Enter your phone number, Telegram login
   code, and two-step password in local dialogs if needed.
4. Double-click channel rows to toggle monitoring, or add a username/link.
5. Click **Start monitoring**.

Existing settings and saved Telegram sessions are reused. Phone numbers, codes,
and two-step passwords are not saved to configuration or `.env`. The Telegram
session in `data/` permits account access: keep it private.

### Features

- **Channels:** choose followed channels, add usernames/links, and change selection
  while monitoring. Stop and refresh the list after following new channels or
  username changes. Selection changes do not erase old records.
- **History & storage:** import the latest N messages per selected channel, or sync
  existing JSON into SQLite. Stop monitoring before these tasks.
- **Messages:** search text/channel names, inspect full JSON, export a record, and
  open storage folders. The latest 500 matches are shown; older records remain in
  the database. The view refreshes every three seconds.
- **Activity:** task logs, connection status, and Start/Stop controls.

Keep the app open for capture. Closing it stops its worker. File locks prevent
two GUIs or two network collectors from using the same project/session.

## Credentials and configuration

You can alternatively copy `.env.example` to `.env` and fill it in:

```dotenv
TELEGRAM_API_ID=your_numeric_api_id
TELEGRAM_API_HASH=your_32_character_api_hash
```

Process environment variables take precedence over `.env`. Files are loaded from
the configuration directory, regardless of the terminal's working directory. The
GUI uses the project root. CLI `--config /path/to/config.json` uses `.env`, `data/`,
and relative storage paths alongside that config.

`config.json` contains non-credential preferences and private channel selections:

```json
{
  "channels": [-1001234567890],
  "output_dir": "data/messages",
  "database_path": "data/messages.sqlite3"
}
```

The GUI creates it when needed; you can also copy `config.example.json`.
Legacy API credentials are automatically migrated from JSON into `.env`, then
removed from JSON. Existing `.env` values and unrelated preferences are preserved.
`.env` is plaintext, not encrypted. When the app writes it on Unix, its file
permissions are restricted to the current user.

## CLI (optional)

Use `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on macOS/Linux.
These examples assume the environment's Python is on PATH:

```sh
python telagent.py list-channels
python telagent.py add-channel '@channel_username'
python telagent.py watch
python telagent.py backfill --limit 100
python telagent.py sync-db
```

Username lookup accepts `username`, `@username`, and `https://t.me/username`.
The followed-channel cache (`data/channels.json`) permits local lookup without
network or login. Unknown cached names require a refresh. With no cache, the CLI
can resolve through an existing authorized session without login prompts. It never
joins channels. Private channels without public usernames can be selected by ID.
`sync-db` needs no Telegram credentials or login.

## Storage and recovery

Atomic JSON files live at `data/messages/<channel_id>/<message_id>.json`. Text and
captions are preserved without summarization. Top-level fields cover channel,
message ID, timestamps, text, public link, and album group ID. `raw` preserves the
complete Telegram message object, including available formatting, reply/forward,
and media metadata. Album parts share `grouped_id` but are separate records.

SQLite defaults to `data/messages.sqlite3`. The `messages` table has searchable
columns and complete `record_json`. The primary key `(channel_id, message_id)`
prevents duplicates and keeps IDs from different channels separate. Edits update
the same file and row. An index on `published_at` supports date queries.

```sql
SELECT channel_name, message_id, published_at, text
FROM messages
ORDER BY published_at DESC
LIMIT 20;
```

JSON is written before SQLite; they are not one transaction. A write failure stops
capture. On startup, selected channels' saved JSON repairs missing database writes.
`sync-db` can also import/repair offline.

Attachments are metadata only: binary files are not downloaded or OCR-transcribed.
Capture runs while the app is open. Import recent history after downtime, but
missed edits, deletions, and earlier versions are not guaranteed recoverable.
Deletions are not mirrored to storage.

## Sharing on GitHub

Commit source, tests, launchers, `.env.example`, `config.example.json`, and docs.
`.gitignore` excludes populated `.env` files, `config.json`, `data/`, Telegram
sessions, databases, virtual environments, logs, caches, and local shortcuts.
Keep exported messages private too; prefer the default `data/` directory.

Before committing, inspect staged filenames and diffs. Never upload credentials,
session files, channel caches, or message records. Ignore rules do not remove
secrets already committed to an existing repository's history. The app never
publishes to GitHub automatically.

## Verification

```sh
python -m unittest discover -s tests -v
python tests/smoke_gui.py
```

Tests cover persistence, deduplication/edits, username handling, environment
migration and overrides, login protocol, search, and cross-process locks. The GUI
smoke check creates a real window with isolated settings and no Telegram access.
GitHub Actions is configured to run tests on Windows/macOS/Linux with Python
3.10/3.13, plus GUI smoke checks on Windows and Linux (using Xvfb on Linux).
Live Telegram checks require your local session.

References: [Telethon](https://docs.telethon.dev/en/stable/),
[Tkinter](https://docs.python.org/3/library/tkinter.html),
[python-dotenv](https://bbc2.github.io/python-dotenv/).
