from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


COMMANDS = [
    "/help",
    "/guide",
    "/sync",
    "/nick",
    "/undo",
    "/users",
    "/history",
    "/status",
    "/wifi",
    "/reconnect",
    "/create",
    "/leave",
    "/export",
    "/clear",
    "/quit",
    "/announce",
    "/update",
    "/pc",
]


def split_args(text: str) -> list[str]:
    try:
        return [part.strip('"') for part in shlex.split(text, posix=False)]
    except ValueError:
        return text.split()


class CommandProcessor:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        clear_unread = getattr(self.app, "clear_unread", None)
        if clear_unread is not None:
            clear_unread()
        if not line.startswith("/"):
            await self.app.send_chat(line)
            return

        lower = line.lower()
        if lower in {"/help", "/pc help"}:
            self.app.show_help()
        elif lower in {"/guide", "/pc guide"}:
            self.app.show_guide()
        elif lower == "/sync":
            await self.app.sync()
        elif lower.startswith("/nick "):
            await self.app.change_nick(line.split(maxsplit=1)[1].strip())
        elif lower == "/undo":
            await self.app.undo()
        elif lower == "/users":
            await self.app.show_users()
        elif lower.startswith("/history"):
            self.app.show_history(self._history_limit(line))
        elif lower in {"/status", "/pc status"}:
            self.app.show_status()
        elif lower in {"/wifi", "/pc wifi"}:
            self.app.show_wifi()
        elif lower == "/reconnect":
            await self.app.reconnect()
        elif lower == "/create":
            await self.app.create_room()
        elif lower == "/leave":
            await self.app.leave_room()
        elif lower == "/export":
            self.app.export_history()
        elif lower == "/clear":
            self.app.clear_screen()
        elif lower == "/quit":
            await self.app.quit()
        elif lower == "/pc version":
            self.app.show_version()
        elif lower == "/pc open-data":
            self.app.open_data_dir()
        elif lower == "/announce show":
            self.app.announce_show()
        elif lower.startswith("/announce set "):
            await self.app.announce_set(line[len("/announce set ") :].strip())
        elif lower == "/announce rollback":
            await self.app.announce_rollback()
        elif lower == "/announce history":
            self.app.announce_history()
        elif lower in {"/update", "/update check"}:
            await self.app.update_check_or_show(force_check=lower == "/update check")
        elif lower == "/update notes":
            self.app.update_notes()
        elif lower == "/update apply":
            await self.app.update_apply()
        elif lower.startswith("/update publish "):
            await self._publish_update(line)
        else:
            self.app.ui.print("[SYSTEM] Unknown command. Type /help.")

    def _history_limit(self, line: str) -> int:
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            return max(1, min(500, int(parts[1].strip())))
        return 20

    async def _publish_update(self, line: str) -> None:
        args = split_args(line[len("/update publish ") :])
        if not args:
            self.app.ui.print("Usage: /update publish <file> [version] [notes]")
            return
        file_path = Path(args[0])
        version = args[1] if len(args) >= 2 else None
        notes = " ".join(args[2:]) if len(args) >= 3 else None
        await self.app.update_publish(file_path, version, notes)
