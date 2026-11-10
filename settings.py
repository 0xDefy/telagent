"""Local settings and secret migration; never serialize credentials into JSON."""

import json
import os
from pathlib import Path
import tempfile

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parent
SECRET_KEYS = {"api_id": "TELEGRAM_API_ID", "api_hash": "TELEGRAM_API_HASH"}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def save_credentials(config_path, api_id, api_hash):
    env_path = config_path.resolve().parent / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    for name, value in (("TELEGRAM_API_ID", api_id), ("TELEGRAM_API_HASH", api_hash)):
        set_key(str(env_path), name, str(value), quote_mode="always")
    if os.name != "nt":
        env_path.chmod(0o600)


def migrate_credentials(config_path):
    """Persist legacy credentials before removing them; preserve existing .env keys."""
    if not config_path.exists():
        return
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not any(key in config for key in SECRET_KEYS):
        return
    env_path = config_path.resolve().parent / ".env"
    existing = dotenv_values(env_path, interpolate=False) if env_path.exists() else {}
    for key, env_key in SECRET_KEYS.items():
        value = config.get(key)
        if value and value != "YOUR_API_HASH" and not existing.get(env_key):
            set_key(str(env_path), env_key, str(value), quote_mode="always")
    if env_path.exists() and os.name != "nt":
        env_path.chmod(0o600)
    atomic_json(config_path, {key: value for key, value in config.items() if key not in SECRET_KEYS})


def read_config(path):
    migrate_credentials(path)
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "channels": [], "output_dir": "data/messages", "database_path": "data/messages.sqlite3"}
    channels = config.get("channels", [])
    if not isinstance(channels, list) or any(type(c) is not int for c in channels):
        raise ValueError("channels must be a list of numeric IDs")
    return config


def read_credentials(config_path):
    env_path = config_path.resolve().parent / ".env"
    values = dotenv_values(env_path, interpolate=False) if env_path.exists() else {}
    return {key: os.environ.get(env_key, values.get(env_key) or "")
            for key, env_key in SECRET_KEYS.items()}


def load_credentials(config_path):
    values = read_credentials(config_path)
    try:
        api_id = int(values["api_id"])
        if api_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Set a positive TELEGRAM_API_ID in .env or Settings") from None
    api_hash = values["api_hash"]
    if len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
        raise ValueError("Set a 32-character hexadecimal TELEGRAM_API_HASH in .env or Settings")
    return {"api_id": api_id, "api_hash": api_hash}
