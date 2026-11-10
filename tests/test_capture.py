import json
import asyncio
from contextlib import closing
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from telagent import (save_record, init_database, sync_database, normalize_username,
                      add_channel, atomic_json)


class CaptureTests(unittest.TestCase):
    def test_username_inputs_and_invalid_links(self):
        for value in ("ExampleChannel", "@ExampleChannel", "https://t.me/ExampleChannel/"):
            self.assertEqual(normalize_username(value), "examplechannel")
        for value in ("https://evil.example/channel", "https://t.me/+invite", "name/123", ""):
            with self.assertRaises(ValueError):
                normalize_username(value)

    def test_add_cached_channel_without_login_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            settings = {"api_id": 1234, "api_hash": "test", "channels": [], "custom": "keep"}
            atomic_json(config, settings)
            cache = root / "channels.json"
            atomic_json(cache, [{"id": -100123, "title": "Example", "username": "ExampleChannel"}])
            args = SimpleNamespace(config=config, username="@ExampleChannel")
            with patch("telagent.TelegramClient") as client, patch("builtins.print"):
                asyncio.run(add_channel(args, settings, cache))
                asyncio.run(add_channel(args, settings, cache))
                client.assert_not_called()
                args.username = "@unknownchannel"
                with self.assertRaises(ValueError):
                    asyncio.run(add_channel(args, settings, cache))
                client.assert_not_called()
            result = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(result["channels"], [-100123])
            self.assertEqual(result["custom"], "keep")

    def test_database_edits_channel_identity_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "messages"
            database = root / "messages.sqlite3"
            init_database(database)
            record = {"channel_id": -100123, "message_id": 42,
                      "text": "नमस्ते 'quoted' 🌍", "raw": {"entities": []}}
            # A JSON-only write (or interrupted dual write) is repaired on sync.
            save_record(output, record)
            excluded = dict(record, channel_id=-100999)
            save_record(output, excluded)
            self.assertEqual(sync_database(output, database, {-100123}), 1)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertFalse(save_record(output, record, database))
            record["text"] = "Edited 🌍"
            save_record(output, record, database)
            save_record(output, excluded, database)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
                row = db.execute("SELECT text, record_json FROM messages WHERE channel_id=?",
                                 (-100123,)).fetchone()
            self.assertEqual(row[0], record["text"])
            self.assertEqual(json.loads(row[1]), record)

    def test_redelivery_and_edit_keep_one_complete_unicode_record(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = {"channel_id": -100123, "message_id": 42,
                      "text": "नमस्ते 🌍\nFull message", "raw": {"entities": []}}
            self.assertTrue(save_record(output, record))
            self.assertFalse(save_record(output, record))
            record["text"] = "Edited 🌍"
            self.assertTrue(save_record(output, record))
            files = list(output.rglob("*.*"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text(encoding="utf-8")), record)


if __name__ == "__main__":
    unittest.main()
