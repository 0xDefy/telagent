"""Capture selected Telegram channels as complete JSON message records."""

import argparse
import asyncio
from contextlib import closing
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
import tempfile
import threading

from telethon import TelegramClient, events, utils
from telethon.errors import RPCError
from settings import ROOT, atomic_json, read_config, load_credentials
from platform_utils import FileLock

LOG = logging.getLogger("telagent")
GUI_PROMPT = "TELAGENT_PROMPT:"


async def gui_answer(kind):
    print(GUI_PROMPT + json.dumps({"kind": kind}), flush=True)
    answer = await asyncio.to_thread(input)
    if answer == "__STOP__":
        raise RuntimeError("Login cancelled")
    return answer


def normalize_username(value):
    username = value.strip()
    username = re.sub(r"^(?:https?://)?t\.me/", "", username, flags=re.I)
    username = username.removeprefix("@").rstrip("/")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
        raise ValueError("Enter a public channel username, @username, or https://t.me/username")
    return username.lower()


def add_to_config(path, channel_id):
    # Read again to preserve unrelated preferences and the latest selection.
    config = load_config(path)
    if channel_id in config.get("channels", []):
        sync_channel_selection(path)
        return False
    config.setdefault("channels", []).append(channel_id)
    atomic_json(path, config)
    sync_channel_selection(path)
    return True


def channel_listing(channels):
    return [{"id": cid, "title": entity.title,
             "username": getattr(entity, "username", None)}
            for cid, entity in channels.items()]


async def add_channel(args, config, cache_path):
    username = normalize_username(args.username)
    entries = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else []
    found = next((entry for entry in entries
                  if (entry.get("username") or "").lower() == username), None)
    if found is None:
        if cache_path.exists():
            raise ValueError("Username not in the followed-channel cache. If you just followed or renamed it, "
                             "stop watch, run list-channels to refresh, then retry and restart watch.")
        session = args.config.resolve().parent / "data" / "telegram"
        if not session.with_suffix(".session").exists():
            raise ValueError("No saved Telegram login. Run list-channels once to sign in locally.")
        credentials = load_credentials(args.config)
        session_lock = FileLock(session.parent / "collector.lock")
        session_lock.__enter__()
        client = None
        try:
            client = TelegramClient(str(session), **credentials)
            # Deliberately avoid start()/async with, which can prompt for login.
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError("Saved login expired. Run list-channels to sign in again.")
            entity = await client.get_entity("@" + username)
            if not getattr(entity, "broadcast", False):
                raise ValueError("That username is not a broadcast channel")
            followed = {}
            async for dialog in client.iter_dialogs():
                if getattr(dialog.entity, "broadcast", False):
                    followed[dialog.id] = dialog.entity
            cid = utils.get_peer_id(entity)
            if cid not in followed:
                raise ValueError("Follow this channel in Telegram first, then retry. No channels were added.")
            entries = channel_listing(followed)
            atomic_json(cache_path, entries)
            found = next(entry for entry in entries if entry["id"] == cid)
        finally:
            try:
                if client:
                    await client.disconnect()
            finally:
                session_lock.__exit__()
    added = add_to_config(args.config, found["id"])
    print(f"{'Added' if added else 'Already selected'} @{username}: {found['id']}")
    print("Running collectors pick up this selection on the next message event.")


def load_config(path):
    return read_config(path)


def make_record(message, channel):
    username = getattr(channel, "username", None)
    channel_id = utils.get_peer_id(channel)
    return {
        "channel_id": channel_id,
        "channel_name": channel.title,
        "channel_username": username,
        "message_id": message.id,
        "published_at": message.date.isoformat() if message.date else None,
        "edited_at": message.edit_date.isoformat() if message.edit_date else None,
        "text": message.message or "",
        "message_url": f"https://t.me/{username}/{message.id}" if username else None,
        "grouped_id": str(message.grouped_id) if message.grouped_id else None,
        "raw": json.loads(message.to_json()),
    }


def init_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            channel_name TEXT,
            channel_username TEXT,
            published_at TEXT,
            edited_at TEXT,
            text TEXT NOT NULL,
            message_url TEXT,
            grouped_id TEXT,
            record_json TEXT NOT NULL,
            PRIMARY KEY (channel_id, message_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS messages_published_at ON messages(published_at)")
        db.execute("CREATE TABLE IF NOT EXISTS selected_channels (channel_id INTEGER PRIMARY KEY)")
        db.execute("""CREATE VIEW IF NOT EXISTS collective_messages AS
                      SELECT messages.* FROM messages
                      JOIN selected_channels USING (channel_id)""")


def database_path(config_path, config):
    return config_path.resolve().parent / Path(config.get("database_path", "data/messages.sqlite3"))


def sync_channel_selection(config_path):
    """Refresh the database's selected channel IDs from the local configuration."""
    database = database_path(config_path, load_config(config_path))
    init_database(database)
    with closing(sqlite3.connect(database, timeout=1)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        selected = set(load_config(config_path).get("channels", []))
        stored = {row[0] for row in db.execute("SELECT channel_id FROM selected_channels")}
        db.executemany("DELETE FROM selected_channels WHERE channel_id=?", ((cid,) for cid in stored - selected))
        db.executemany("INSERT INTO selected_channels VALUES (?)", ((cid,) for cid in selected - stored))
    return selected


def upsert_record(database, record):
    # Parameters keep arbitrary message content out of SQL syntax.
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("""INSERT INTO messages (
            channel_id, message_id, channel_name, channel_username, published_at,
            edited_at, text, message_url, grouped_id, record_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, message_id) DO UPDATE SET
            channel_name=excluded.channel_name,
            channel_username=excluded.channel_username,
            published_at=excluded.published_at,
            edited_at=excluded.edited_at,
            text=excluded.text,
            message_url=excluded.message_url,
            grouped_id=excluded.grouped_id,
            record_json=excluded.record_json
        """, (
            record["channel_id"], record["message_id"], record.get("channel_name"),
            record.get("channel_username"), record.get("published_at"),
            record.get("edited_at"), record["text"], record.get("message_url"),
            record.get("grouped_id"), json.dumps(record, ensure_ascii=False),
        ))


def sync_database(output, database, selected):
    count = 0
    for channel_id in selected:
        for path in sorted((output / str(channel_id)).glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["channel_id"] != channel_id:
                raise ValueError(f"Channel ID does not match folder: {path}")
            upsert_record(database, record)
            count += 1
    return count


def save_record(output, record, database=None):
    """Write JSON first, then upsert SQLite, including on identical redelivery."""
    changed = save_json(output, record)
    if database is not None:
        upsert_record(database, record)
    return changed


def save_json(output, record):
    """Atomically replace a message snapshot; identical redelivery is a no-op."""
    folder = output / str(record["channel_id"])
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{record['message_id']}.json"
    serialized = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") == serialized:
        return False
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    return True


async def run(args):
    if args.command in ("watch", "backfill", "list-channels"):
        with FileLock(args.config.resolve().parent / "data" / "collector.lock"):
            await run_collector(args)
    else:
        await run_collector(args)


async def run_collector(args):
    config = load_config(args.config)
    cache_path = args.config.resolve().parent / "data" / "channels.json"
    if args.command == "add-channel":
        await add_channel(args, config, cache_path)
        return
    output = Path(config.get("output_dir", "data/messages"))
    if not output.is_absolute():
        output = args.config.resolve().parent / output
    database = database_path(args.config, config)
    if args.command != "list-channels":
        selected = sync_channel_selection(args.config)
        if not selected and args.command != "sync-db":
            raise ValueError("No channels selected. Add IDs to config.json")
        count = sync_database(output, database, selected)
        LOG.info("Synced %s existing JSON records to %s", count, database)
        if args.command == "sync-db":
            return
    session = args.config.resolve().parent / "data" / "telegram"
    session.parent.mkdir(parents=True, exist_ok=True)
    credentials = load_credentials(args.config)
    client = TelegramClient(str(session), **credentials,
                            auto_reconnect=True, sequential_updates=True)
    if getattr(args, "gui", False):
        try:
            await client.start(phone=lambda: gui_answer("phone"),
                               code_callback=lambda: gui_answer("code"),
                               password=lambda: gui_answer("password"))
        except BaseException:
            await client.disconnect()
            raise
        loop = asyncio.get_running_loop()

        def stop_listener():
            # Daemon thread can exit with the worker even after connection loss.
            import sys
            for line in sys.stdin:
                if line.strip() == "__STOP__":
                    if not loop.is_closed():
                        asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
                    return

        threading.Thread(target=stop_listener, daemon=True).start()
    async with client:
        # Only broadcast channels already in this account's dialog list qualify.
        channels = {}
        async for dialog in client.iter_dialogs():
            if getattr(dialog.entity, "broadcast", False):
                channels[dialog.id] = dialog.entity
        atomic_json(cache_path, channel_listing(channels))
        if args.command == "list-channels":
            if getattr(args, "gui", False):
                print(f"Loaded {len(channels)} followed channels", flush=True)
            else:
                print(json.dumps(channel_listing(channels), ensure_ascii=False, indent=2))
            return
        selected = set(config.get("channels", []))
        if not selected:
            raise ValueError("No channels selected. Run list-channels and add IDs to config.json")
        missing = selected - channels.keys()
        if missing:
            raise ValueError(f"Selected IDs are not followed broadcast channels: {sorted(missing)}")
        allowed = {cid: channels[cid] for cid in selected}

        if args.command == "backfill":
            for cid, entity in allowed.items():
                count = 0
                async for message in client.iter_messages(entity, limit=args.limit):
                    count += save_record(output, make_record(message, entity), database)
                LOG.info("Saved %s records from %s", count, cid)
            return

        failure = []
        stored_selection = selected

        async def capture(event):
            nonlocal stored_selection
            try:
                # Reload on each event so local add-channel changes take effect
                # without opening another Telegram connection or restarting.
                current = set(load_config(args.config).get("channels", []))
                if current != stored_selection:
                    stored_selection = sync_channel_selection(args.config)
                if event.chat_id not in current or event.chat_id not in channels:
                    return
                if save_record(output, make_record(event.message, channels[event.chat_id]), database):
                    LOG.info("Saved channel=%s message=%s", event.chat_id, event.id)
            except Exception as exc:
                LOG.exception("Capture failed; stopping to avoid silently losing messages")
                failure.append(exc)
                await client.disconnect()

        client.add_event_handler(capture, events.NewMessage())
        client.add_event_handler(capture, events.MessageEdited())
        LOG.info("Monitoring %s selected channels (list reloads automatically). JSON: %s; SQLite: %s", len(allowed), output, database)
        await client.run_until_disconnected()
        if failure:
            raise RuntimeError("Capture stopped because a message could not be saved") from failure[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-channels", help="List followed broadcast channels for selection")
    add = sub.add_parser("add-channel", help="Resolve a public username and add it without login prompts")
    add.add_argument("username", help="@username, username, or https://t.me/username")
    sub.add_parser("watch", help="Save new messages and edits until stopped")
    sub.add_parser("sync-db", help="Import saved JSON into SQLite without Telegram login")
    history = sub.add_parser("backfill", help="Save recent messages from selected channels")
    history.add_argument("--limit", type=int, default=100, help="Recent messages per channel")
    args = parser.parse_args()
    if args.command == "backfill" and args.limit <= 0:
        parser.error("--limit must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError, sqlite3.Error, RPCError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
