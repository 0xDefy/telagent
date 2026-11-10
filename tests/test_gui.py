from contextlib import redirect_stdout
import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gui import query_messages, configured_path, re_api_hash
from telagent import gui_answer, GUI_PROMPT, init_database, upsert_record


class DesktopTests(unittest.TestCase):
    def test_search_preserves_unicode_and_treats_wildcards_literally(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "messages.sqlite3"
            init_database(database)
            for mid, text in enumerate(("नमस्ते 100%", "1000", "under_score", "underXscore")):
                upsert_record(database, {"channel_id": -100123, "message_id": mid,
                                        "channel_name": "Example", "text": text,
                                        "published_at": "2026-11-10T12:00:00+00:00"})
            total, rows = query_messages(database, "100%")
            self.assertEqual(total, 4)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][3], "नमस्ते 100%")
            self.assertEqual(len(query_messages(database, "under_")[1]), 1)
            self.assertEqual(len(query_messages(database, "Example")[1]), 4)
            self.assertEqual(query_messages(Path(directory) / "missing.sqlite3"), (0, []))

    def test_login_bridge_never_outputs_answer(self):
        output = io.StringIO()
        with patch("builtins.input", return_value="private-answer"), redirect_stdout(output):
            self.assertEqual(asyncio.run(gui_answer("password")), "private-answer")
        self.assertEqual(output.getvalue().strip(), GUI_PROMPT + json.dumps({"kind": "password"}))
        with patch("builtins.input", return_value="__STOP__"), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                asyncio.run(gui_answer("code"))

    def test_credential_validation_and_existing_paths(self):
        self.assertTrue(re_api_hash("a" * 32))
        self.assertFalse(re_api_hash("YOUR_API_HASH"))
        self.assertFalse(re_api_hash("z" * 32))
        path = Path(tempfile.gettempdir()) / "example.sqlite3"
        self.assertEqual(configured_path({"db": str(path)}, "db", "ignored"), path)


if __name__ == "__main__":
    unittest.main()
