from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .constants import DATA_DIR_NAME


def executable_dir() -> Path:
    """Return the directory beside pc.exe, or beside main.py in dev mode."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def data_dir() -> Path:
    return executable_dir() / DATA_DIR_NAME


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_uuid() -> str:
    return str(uuid.uuid4())


def format_time(iso_text: str) -> str:
    try:
        return datetime.fromisoformat(iso_text).strftime("%H:%M")
    except ValueError:
        return iso_text[:5] if iso_text else "--:--"


def format_message(row: dict[str, Any], *, current_user: str | None = None) -> str:
    content = "<message withdrawn>" if int(row.get("withdrawn", 0)) else str(row.get("content", ""))
    timestamp = format_time(str(row.get("created_at", "")))
    sender = str(row.get("sender", "?"))
    if current_user and sender == current_user:
        return f"[{timestamp}] [you] {sender}"
    return f"[{timestamp}] {sender}: {content}"


def format_message_block(row: dict[str, Any], *, current_user: str | None = None) -> str:
    return format_message(row, current_user=current_user) + "\n"


def ensure_hidden_on_windows(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        FILE_ATTRIBUTE_HIDDEN = 0x02
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:
            return
        ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        # Hiding is best-effort; program data remains in the same directory either way.
        return


def setup_console_encoding() -> None:
    """Use UTF-8 console I/O on Windows so bundled Chinese text is not mojibake."""
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.SetConsoleCP(65001)
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        return


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_lan_ip() -> str:
    """Best-effort local LAN address detection."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def get_local_ip_for_peer(peer_ip: str) -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((peer_ip, 1))
            return str(sock.getsockname()[0])
    except OSError:
        return get_lan_ip()


async def write_json(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    writer.write(data.encode("utf-8"))
    await writer.drain()


async def read_json(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    line = await reader.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def safe_filename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch not in '<>:"/\\|?*').strip()
    return cleaned or "file"
