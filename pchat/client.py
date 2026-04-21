from __future__ import annotations

import asyncio
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import APP_VERSION
from .config import ConfigManager
from .constants import CHAT_ACK_TIMEOUT, CONTROL_PORT, HOST_TIMEOUT
from .models import RoomInfo
from .storage import Storage
from .tls_util import create_client_ssl_context
from .translate import TranslationError, TranslatorClient
from .ui import ChatUI
from .utils import contains_chinese, format_message_block, format_translation_block, new_uuid, now_iso, read_json, sha256_file, write_json


class PChatClient:
    def __init__(
        self,
        *,
        config: ConfigManager,
        storage: Storage,
        ui: ChatUI,
        room: RoomInfo,
        on_unread: Callable[[], None] | None = None,
        on_host_failed: Callable[[str], None] | None = None,
        control_port: int = CONTROL_PORT,
    ) -> None:
        self.config = config
        self.storage = storage
        self.ui = ui
        self.room = room
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.receiver_task: asyncio.Task[None] | None = None
        self.connected_event = asyncio.Event()
        self.users: list[str] = []
        self.latest_update: dict[str, Any] | None = None
        self.roster: list[dict[str, Any]] = []
        self.room_epoch = room.room_epoch
        self.host_id = room.host_id
        self.joined_at_seq = 999999
        self.last_host_seen = asyncio.get_event_loop().time()
        self.pending_acks: dict[str, asyncio.Future[bool]] = {}
        self.control_port = control_port
        self.initial_sync_done = False
        self.closed = False
        self.on_unread = on_unread
        self.on_host_failed = on_host_failed
        self.watchdog_task: asyncio.Task[None] | None = None
        self.translator = TranslatorClient(config)
        self.translation_cache: dict[str, str] = {}
        self.translation_tasks: set[asyncio.Task[None]] = set()

    async def connect(self) -> None:
        ssl_context = create_client_ssl_context()
        self.reader, self.writer = await asyncio.open_connection(
            self.room.host_ip,
            self.room.chat_port,
            ssl=ssl_context,
            server_hostname=None,
        )
        await self._send(
            {
                "type": "hello",
                "username": self.config.username,
                "client_id": self.config.client_id,
                "control_port": self.control_port,
                "known_room_epoch": self.room_epoch,
                "known_announcement_version": self.config.last_seen_announcement_version,
                "program_version": APP_VERSION,
            }
        )
        self.receiver_task = asyncio.create_task(self._receiver_loop())
        self.watchdog_task = asyncio.create_task(self._host_watchdog_loop())
        await asyncio.wait_for(self.connected_event.wait(), timeout=8)

    async def close(self) -> None:
        self.closed = True
        try:
            if self.writer is not None:
                await self._send({"type": "leave"})
                self.writer.close()
                await self.writer.wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass
        if self.receiver_task is not None and self.receiver_task is not asyncio.current_task():
            self.receiver_task.cancel()
            try:
                await self.receiver_task
            except asyncio.CancelledError:
                pass
        if self.watchdog_task is not None and self.watchdog_task is not asyncio.current_task():
            self.watchdog_task.cancel()
            try:
                await self.watchdog_task
            except asyncio.CancelledError:
                pass
        tasks = list(self.translation_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def send_chat(self, content: str) -> None:
        message_uuid = new_uuid()
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending_acks[message_uuid] = future
        await self._send(
            {
                "type": "chat",
                "message_uuid": message_uuid,
                "content": content,
                "created_at": now_iso(),
            }
        )
        try:
            await asyncio.wait_for(future, timeout=CHAT_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            self.pending_acks.pop(message_uuid, None)
            self.ui.print("[SYSTEM] Message was not acknowledged by Host. Checking failover.")
            self._notify_host_failed("chat_ack_timeout")

    async def request_sync(self) -> None:
        await self._send(
            {
                "type": "sync_request",
                "known_announcement_version": self.config.last_seen_announcement_version,
            }
        )

    async def change_nick(self, username: str) -> None:
        await self._send({"type": "nick", "username": username})

    async def undo(self) -> None:
        await self._send({"type": "undo"})

    async def request_users(self) -> None:
        await self._send({"type": "users_request"})

    async def check_update(self) -> None:
        await self._send({"type": "update_check"})

    def show_update(self) -> None:
        if not self.latest_update:
            self.ui.print("[SYSTEM] No update info. Type /update check.")
            return
        update = self.latest_update
        self.ui.print(f"Version : {update.get('version')}")
        self.ui.print(f"File    : {update.get('file_name')} ({update.get('file_size')} bytes)")
        self.ui.print(f"SHA-256 : {update.get('sha256')}")
        self.ui.print("Type /update notes for notes, /update apply to download.")

    def show_update_notes(self) -> None:
        if not self.latest_update:
            self.ui.print("[SYSTEM] No update info. Type /update check.")
            return
        self.ui.print(str(self.latest_update.get("notes", "")))

    async def apply_update(self) -> None:
        if not self.latest_update:
            self.ui.print("[SYSTEM] No update info. Type /update check.")
            return
        try:
            dest = await asyncio.to_thread(self._download_update, self.latest_update)
        except Exception as exc:
            self.ui.print(f"[SYSTEM] Update download failed: {exc}")
            return
        self.ui.print(f"[SYSTEM] Update downloaded and verified: {dest}")
        self.ui.print("[SYSTEM] Close P-Chat and manually replace pc.exe with the downloaded file.")

    async def _receiver_loop(self) -> None:
        assert self.reader is not None
        try:
            while not self.closed:
                payload = await read_json(self.reader)
                if payload is None:
                    break
                await self._handle_payload(payload)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            pass
        finally:
            if not self.closed:
                self.ui.print("[SYSTEM] Disconnected from Host. Starting failover check.")
                self._notify_host_failed("connection_lost")

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "welcome":
            self.ui.print(f"[SYSTEM] Joined room: {payload.get('room_name', self.room.room_name)}")
            self.host_id = str(payload.get("host_id") or payload.get("client_id") or self.host_id)
            self.room_epoch = int(payload.get("room_epoch", self.room_epoch) or self.room_epoch)
            self.joined_at_seq = int(payload.get("joined_at_seq", self.joined_at_seq) or self.joined_at_seq)
            self.last_host_seen = asyncio.get_running_loop().time()
            self.connected_event.set()
        elif kind == "chat_ack":
            message_uuid = str(payload.get("message_uuid", ""))
            future = self.pending_acks.pop(message_uuid, None)
            if future is not None and not future.done():
                future.set_result(bool(payload.get("ok", False)))
        elif kind == "host_ping":
            self.host_id = str(payload.get("host_id") or self.host_id)
            self.room_epoch = int(payload.get("room_epoch", self.room_epoch) or self.room_epoch)
            self.last_host_seen = asyncio.get_running_loop().time()
        elif kind == "roster_update":
            self.roster = [dict(item) for item in payload.get("roster", [])]
            self.host_id = str(payload.get("host_id") or self.host_id)
            self.room_epoch = int(payload.get("room_epoch", self.room_epoch) or self.room_epoch)
        elif kind == "sync_response":
            self._handle_sync(payload)
        elif kind == "chat":
            self._store_and_display_message(payload)
        elif kind == "message_withdrawn":
            row = self.storage.mark_withdrawn(str(payload.get("message_uuid", "")))
            self.ui.print(format_message_block(row, current_user=self.config.username) if row else "[SYSTEM] A message was withdrawn.")
        elif kind == "announcement_update":
            self._handle_announcement(payload)
        elif kind == "update_available":
            self._handle_update(payload, notify=True)
        elif kind == "update_info":
            update = payload.get("update")
            if update:
                self._handle_update(update, notify=False)
                self.show_update()
            else:
                self.latest_update = None
                self.ui.print("[SYSTEM] No update available.")
        elif kind == "users":
            self.users = [str(item) for item in payload.get("users", [])]
            self.ui.print("[SYSTEM] Users: " + ", ".join(self.users))
        elif kind == "system":
            self._display_system(payload)
        elif kind == "error":
            self.ui.print(f"[SYSTEM] {payload.get('message', 'Unknown error')}")

    def host_recently_seen(self) -> bool:
        return asyncio.get_running_loop().time() - self.last_host_seen <= HOST_TIMEOUT

    async def _host_watchdog_loop(self) -> None:
        while not self.closed:
            await asyncio.sleep(1.0)
            if self.connected_event.is_set() and not self.host_recently_seen():
                self.ui.print("[SYSTEM] Host heartbeat timed out.")
                self._notify_host_failed("host_timeout")
                return

    def _notify_host_failed(self, reason: str) -> None:
        if self.on_host_failed is not None:
            self.on_host_failed(reason)

    def _handle_sync(self, payload: dict[str, Any]) -> None:
        messages = [dict(item) for item in payload.get("messages", [])]
        for message in messages:
            self.storage.add_message(
                str(message.get("message_uuid", "")),
                str(message.get("sender", "")),
                str(message.get("content", "")),
                str(message.get("created_at", "")),
                int(message.get("withdrawn", 0)),
            )
        announcement = payload.get("announcement")
        if isinstance(announcement, dict):
            self._handle_announcement(announcement)
        update = payload.get("update")
        if isinstance(update, dict):
            self._handle_update(update, notify=False)
        if not self.initial_sync_done:
            if messages:
                self.ui.print("[SYSTEM] Recent messages:")
                for message in messages:
                    self.ui.print(format_message_block(message, current_user=self.config.username))
                    self._schedule_translation(message)
            else:
                self.ui.print("[SYSTEM] No recent messages.")
            self.initial_sync_done = True
        else:
            self.ui.print(f"[SYSTEM] Sync complete. {len(messages)} recent messages checked.")

    def _store_and_display_message(self, payload: dict[str, Any]) -> None:
        inserted = self.storage.add_message(
            str(payload.get("message_uuid", "")),
            str(payload.get("sender", "")),
            str(payload.get("content", "")),
            str(payload.get("created_at", "")),
            int(payload.get("withdrawn", 0)),
        )
        if not inserted:
            return
        self.ui.print(format_message_block(payload, current_user=self.config.username))
        self._schedule_translation(payload)
        if str(payload.get("sender", "")) != self.config.username and self.on_unread is not None:
            self.on_unread()

    def _schedule_translation(self, payload: dict[str, Any]) -> None:
        if not self.config.translation_enabled:
            return
        if str(payload.get("sender", "")) == self.config.username:
            return
        content = str(payload.get("content", ""))
        if not content or int(payload.get("withdrawn", 0)) or not contains_chinese(content):
            return
        message_uuid = str(payload.get("message_uuid", ""))
        if not message_uuid:
            return
        cached = self.translation_cache.get(message_uuid)
        if cached:
            self.ui.print(format_translation_block(cached))
            return
        task = asyncio.create_task(self._translate_and_display(message_uuid, content))
        self.translation_tasks.add(task)
        task.add_done_callback(self.translation_tasks.discard)

    async def _translate_and_display(self, message_uuid: str, content: str) -> None:
        try:
            translated = await asyncio.to_thread(self.translator.translate_zh_to_ja, content)
        except TranslationError as exc:
            self.ui.print(f"[SYSTEM] Translation failed: {exc}")
            return
        except Exception as exc:
            self.ui.print(f"[SYSTEM] Translation failed: {exc}")
            return
        self.translation_cache[message_uuid] = translated
        self.ui.print(format_translation_block(translated))

    def translation_status(self) -> str:
        if not self.config.translation_enabled:
            return "off"
        if not self.translator.configured():
            return "on (missing API URL)"
        return "on (LibreTranslate zh -> ja)"

    def set_translation_enabled(self, enabled: bool) -> tuple[bool, str]:
        if enabled and not self.translator.configured():
            return False, "LibreTranslate API URL is not configured."
        self.config.translation_enabled = enabled
        return True, f"Translation {'enabled' if enabled else 'disabled'}."

    def _handle_announcement(self, payload: dict[str, Any]) -> None:
        version = int(payload.get("version", 0) or 0)
        if version <= 0:
            return
        self.storage.upsert_announcement(
            version=version,
            content=str(payload.get("content", "")),
            created_by=str(payload.get("created_by", "Host")),
            created_at=str(payload.get("created_at", now_iso())),
            active=int(payload.get("active", 1)),
        )
        if version != self.config.last_seen_announcement_version:
            self.config.last_seen_announcement_version = version
            self.ui.print("")
            self.ui.print(f"[ANNOUNCEMENT v{version}]")
            self.ui.print(str(payload.get("content", "")))
            self.ui.print("")

    def _handle_update(self, payload: dict[str, Any], notify: bool) -> None:
        self.latest_update = dict(payload)
        required = {"version", "notes", "file_name", "file_size", "sha256"}
        if required.issubset(self.latest_update):
            self.storage.publish_update(
                str(self.latest_update["version"]),
                str(self.latest_update["notes"]),
                str(self.latest_update["file_name"]),
                int(self.latest_update["file_size"]),
                str(self.latest_update["sha256"]),
            )
        if notify:
            self.ui.print(f"[SYSTEM] New version available: {payload.get('version')}")
            self.ui.print("Type /update to view details.")

    def _display_system(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        if event == "user_joined":
            self.ui.print(f"[SYSTEM] {payload.get('username')} joined.")
        elif event == "user_left":
            self.ui.print(f"[SYSTEM] {payload.get('username')} left.")
        elif event == "nick_changed":
            self.ui.print(f"[SYSTEM] {payload.get('old_username')} is now {payload.get('username')}.")
        elif event == "hourly_time":
            self.ui.print(f"[SYSTEM] {payload.get('text', 'Top of the hour.')}")
        else:
            self.ui.print(f"[SYSTEM] {event}")

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.writer is None:
            self.ui.print("[SYSTEM] Not connected.")
            return
        await write_json(self.writer, payload)

    def _download_update(self, update: dict[str, Any]) -> Path:
        file_name = str(update["file_name"])
        url = str(update.get("url") or f"http://{self.room.host_ip}:{self.room.http_port}/{file_name}")
        dest = self.config.downloads_dir / file_name
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as fh:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
        expected_size = int(update["file_size"])
        actual_size = tmp.stat().st_size
        if actual_size != expected_size:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"File size mismatch. Expected {expected_size}, got {actual_size}.")
        actual_sha = sha256_file(tmp)
        if actual_sha.lower() != str(update["sha256"]).lower():
            tmp.unlink(missing_ok=True)
            raise RuntimeError("SHA-256 mismatch.")
        tmp.replace(dest)
        return dest
