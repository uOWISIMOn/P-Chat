from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .constants import APP_VERSION, CONFIG_FILE, DATABASE_FILE
from .guide import GUIDE_VERSION
from .utils import data_dir, ensure_hidden_on_windows, new_uuid


class ConfigManager:
    def __init__(self) -> None:
        self.base_dir = data_dir()
        self.config_path = self.base_dir / CONFIG_FILE
        self.db_path = self.base_dir / DATABASE_FILE
        self.certs_dir = self.base_dir / "certs"
        self.downloads_dir = self.base_dir / "downloads"
        self.exports_dir = self.base_dir / "exports"
        self.logs_dir = self.base_dir / "logs"
        self.updates_dir = self.base_dir / "updates"
        self.history_path = self.base_dir / "command_history.txt"
        self.data: dict[str, Any] = {}

    def ensure_dirs(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        ensure_hidden_on_windows(self.base_dir)
        for path in [
            self.certs_dir,
            self.downloads_dir,
            self.exports_dir,
            self.logs_dir,
            self.updates_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def default_config(self) -> dict[str, Any]:
        return {
            "username": "",
            "last_server_ip": "",
            "last_seen_announcement_version": 0,
            "last_seen_program_version": APP_VERSION,
            "first_run_guide_version": 0,
            "client_id": "",
        }

    def load(self) -> dict[str, Any]:
        self.ensure_dirs()
        if not self.config_path.exists():
            self.data = self.default_config()
            self.data["client_id"] = new_uuid()
            self.save()
            return self.data
        try:
            with self.config_path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (json.JSONDecodeError, OSError):
            loaded = {}
        self.data = self.default_config()
        self.data.update({k: v for k, v in loaded.items() if k in self.data})
        if not str(self.data.get("client_id", "")).strip():
            self.data["client_id"] = new_uuid()
        self.save()
        return self.data

    def save(self) -> None:
        self.ensure_dirs()
        with self.config_path.open("w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)

    @property
    def username(self) -> str:
        return str(self.data.get("username", "")).strip()

    @username.setter
    def username(self, value: str) -> None:
        self.data["username"] = value.strip()
        self.save()

    @property
    def last_seen_announcement_version(self) -> int:
        return int(self.data.get("last_seen_announcement_version", 0) or 0)

    @last_seen_announcement_version.setter
    def last_seen_announcement_version(self, value: int) -> None:
        self.data["last_seen_announcement_version"] = int(value)
        self.save()

    @property
    def last_seen_program_version(self) -> str:
        return str(self.data.get("last_seen_program_version", APP_VERSION))

    @last_seen_program_version.setter
    def last_seen_program_version(self, value: str) -> None:
        self.data["last_seen_program_version"] = value
        self.save()

    @property
    def last_server_ip(self) -> str:
        return str(self.data.get("last_server_ip", ""))

    @last_server_ip.setter
    def last_server_ip(self, value: str) -> None:
        self.data["last_server_ip"] = value
        self.save()

    @property
    def first_run_guide_version(self) -> int:
        return int(self.data.get("first_run_guide_version", 0) or 0)

    def mark_first_run_guide_seen(self) -> None:
        self.data["first_run_guide_version"] = GUIDE_VERSION
        self.save()

    @property
    def client_id(self) -> str:
        value = str(self.data.get("client_id", "")).strip()
        if not value:
            value = new_uuid()
            self.data["client_id"] = value
            self.save()
        return value
