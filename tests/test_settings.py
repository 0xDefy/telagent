import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
from settings import atomic_json, read_config, load_credentials, save_credentials


class SettingsTests(unittest.TestCase):
    def test_migration_preserves_preferences_and_removes_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            config = Path(directory) / "config.json"
            atomic_json(config, {"api_id": 1234, "api_hash": "a" * 32,
                                 "channels": [-100123], "output_dir": "custom"})
            result = read_config(config)
            self.assertEqual(result, {"channels": [-100123], "output_dir": "custom"})
            self.assertEqual(json.loads(config.read_text(encoding="utf-8")), result)
            self.assertEqual(load_credentials(config), {"api_id": 1234, "api_hash": "a" * 32})
            self.assertEqual(read_config(config), result)
            if os.name != "nt":
                self.assertEqual(config.with_name(".env").stat().st_mode & 0o777, 0o600)

    def test_existing_env_and_process_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            config = Path(directory) / "config.json"
            env = config.with_name(".env")
            env.write_text("# keep\nOTHER='untouched'\nTELEGRAM_API_ID='5678'\n", encoding="utf-8")
            atomic_json(config, {"api_id": 1234, "api_hash": "a" * 32, "channels": []})
            read_config(config)
            self.assertEqual(load_credentials(config)["api_id"], 5678)
            self.assertEqual(dotenv_values(env)["OTHER"], "untouched")
            with patch.dict(os.environ, {"TELEGRAM_API_ID": "9999"}):
                self.assertEqual(load_credentials(config)["api_id"], 9999)
            save_credentials(config, 1234, "b" * 32)
            self.assertEqual(load_credentials(config), {"api_id": 1234, "api_hash": "b" * 32})
            self.assertNotIn("api_hash", read_config(config))

    def test_bad_credentials_do_not_appear_in_errors(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            config = Path(directory) / "config.json"
            save_credentials(config, "private-invalid-value", "private-hash-value")
            with self.assertRaises(ValueError) as error:
                load_credentials(config)
            self.assertNotIn("private-invalid-value", str(error.exception))
            save_credentials(config, 1234, "private-hash-value")
            with self.assertRaises(ValueError) as error:
                load_credentials(config)
            self.assertNotIn("private-hash-value", str(error.exception))
