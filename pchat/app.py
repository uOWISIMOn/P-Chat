from __future__ import annotations

import asyncio
import os
from pathlib import Path

from . import APP_VERSION
from .client import PChatClient
from .commands import COMMANDS, CommandProcessor
from .config import ConfigManager
from .constants import DEFAULT_ROOM_NAME, HISTORY_DEFAULT_LIMIT
from .discovery import discover_rooms
from .election import ElectionManager
from .guide import GUIDE_VERSION, first_run_guide
from .models import RoomInfo
from .server import PChatServer
from .storage import Storage
from .tray import TrayNotifier
from .ui import ChatUI
from .utils import format_message_block, get_lan_ip, now_iso
from .wifi import connect_to_ssid, get_wifi_status, list_saved_visible_networks


class PChatApp:
    def __init__(self) -> None:
        self.config = ConfigManager()
        self.storage: Storage | None = None
        self.ui: ChatUI | None = None
        self.commands: CommandProcessor | None = None
        self.server: PChatServer | None = None
        self.client: PChatClient | None = None
        self.tray: TrayNotifier | None = None
        self.election: ElectionManager | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.host_monitor_task: asyncio.Task[None] | None = None
        self.room_epoch = 1
        self.host_id = ""
        self.mode = "Idle"
        self.stopping = False

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.config.load()
        self.storage = Storage(self.config.db_path)
        self.ui = ChatUI(self.config.history_path, COMMANDS)
        self._start_tray()
        self.commands = CommandProcessor(self)
        await self._ensure_username()
        await self._start_election()
        self._show_first_run_guide()
        self._show_start_banner()
        await self.discover_and_join_or_create()
        await self.ui.input_loop(self.commands.handle)
        await self.shutdown()

    async def _ensure_username(self) -> None:
        assert self.ui is not None
        if self.config.username:
            return
        self.ui.print("Welcome to P-Chat")
        self.ui.print("Please set your username:")
        while not self.config.username:
            username = (await self.ui.prompt_text("> ")).strip()
            if username:
                self.config.username = username[:32]
            else:
                self.ui.print("Username cannot be empty.")

    def _start_tray(self) -> None:
        assert self.ui is not None
        self.tray = TrayNotifier()
        if self.tray.start(on_quit=self._request_quit_from_tray):
            self.ui.print("[SYSTEM] Tray icon enabled. Minimize the window to hide P-Chat to tray.")
        elif self.tray.disabled_reason:
            self.ui.print(f"[SYSTEM] Tray icon disabled: {self.tray.disabled_reason}")

    def _request_quit_from_tray(self) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.quit()))

    def notify_unread(self) -> None:
        if self.tray is not None:
            self.tray.notify_unread()

    def clear_unread(self) -> None:
        if self.tray is not None:
            self.tray.clear_unread()

    async def _start_election(self) -> None:
        assert self.ui is not None
        self.election = ElectionManager(
            client_id=self.config.client_id,
            username=self.config.username,
            ui_print=self.ui.print,
            get_roster=self.get_roster,
            get_room_epoch=lambda: self.room_epoch,
            get_host_id=lambda: self.host_id,
            host_recently_seen=self.host_recently_seen,
            become_host=self.become_host_from_election,
            join_host=self.join_elected_host,
            reconnect=self.reconnect,
        )
        await self.election.start()

    def handle_host_failed(self, reason: str) -> None:
        if self.loop is None or self.mode != "Client" or self.election is None:
            return
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.election.trigger_election(reason)))

    def host_recently_seen(self) -> bool:
        if self.mode != "Client" or self.client is None:
            return False
        return self.client.host_recently_seen()

    def get_roster(self) -> list[dict[str, object]]:
        if self.mode == "Host" and self.server is not None:
            return self.server.roster()
        if self.mode == "Client" and self.client is not None and self.client.roster:
            return self.client.roster
        return [
            {
                "client_id": self.config.client_id,
                "username": self.config.username,
                "ip": get_lan_ip(),
                "control_port": self.election.port if self.election and self.election.enabled else 0,
                "joined_at_seq": 999999,
            }
        ]

    def _show_first_run_guide(self) -> None:
        assert self.ui is not None
        if self.config.first_run_guide_version >= GUIDE_VERSION:
            return
        self.ui.print("")
        self.ui.print(first_run_guide())
        self.ui.print("")
        self.config.mark_first_run_guide_seen()

    def _show_start_banner(self) -> None:
        assert self.ui is not None
        self.ui.print("====================================")
        self.ui.print("P-Chat")
        self.ui.print("Use /help for commands")
        self.ui.print("Checking network...")
        self.ui.print("====================================")

    async def discover_and_join_or_create(self) -> None:
        assert self.ui is not None
        if await self._scan_current_network_for_room():
            return
        await self._handle_no_room_found()

    async def _scan_current_network_for_room(self) -> bool:
        assert self.ui is not None
        status = await asyncio.to_thread(get_wifi_status)
        current = status.current_ssid or "unknown Wi-Fi"
        self.ui.print(f"[SYSTEM] Searching for room on {current}...")
        room = await self._discover_room()
        if room is None:
            return False
        return await self._join_discovered_room(room)

    async def _discover_room(self, timeout: float = 2.0) -> RoomInfo | None:
        rooms = await discover_rooms(timeout=timeout)
        if not rooms:
            return None
        return rooms[0]

    async def _join_discovered_room(self, room: RoomInfo) -> bool:
        assert self.ui is not None
        self.ui.print(f"[SYSTEM] Found room {room.room_name} at {room.host_ip}:{room.chat_port}.")
        try:
            await self.join_room(room)
            return True
        except Exception as exc:
            self.ui.print(f"[SYSTEM] Join failed: {exc}")
            return False

    async def _handle_no_room_found(self) -> None:
        assert self.ui is not None
        while True:
            choice = (
                await self.ui.prompt_text(
                    "No room found on current Wi-Fi.\n"
                    "1. Scan saved Wi-Fi networks for rooms (will disconnect current network)\n"
                    "2. Show saved Wi-Fi candidates and choose one manually\n"
                    "3. Skip Wi-Fi switching\n"
                    "> "
                )
            ).strip()
            if choice == "1":
                if await self._scan_saved_wifi_for_rooms():
                    return
                break
            if choice == "2":
                if await self._choose_wifi_and_scan():
                    return
                break
            if choice in {"3", "", "skip"}:
                break
            self.ui.print("[SYSTEM] Enter 1, 2, or 3.")
        answer = (await self.ui.prompt_text("Create one on current network? (y/n)\n> ")).strip().lower()
        if answer == "y":
            await self.create_room()
        else:
            self.ui.print("[SYSTEM] Not in a room. Type /reconnect or /create.")

    async def _scan_saved_wifi_for_rooms(self) -> bool:
        assert self.ui is not None
        status = await asyncio.to_thread(get_wifi_status)
        original_ssid = status.current_ssid
        candidates = await asyncio.to_thread(list_saved_visible_networks, exclude=original_ssid)
        if not candidates:
            self.ui.print("[SYSTEM] No other saved and visible Wi-Fi networks are available.")
            return False
        proceed = (
            await self.ui.prompt_text(
                "Switching Wi-Fi will interrupt your current network connection.\nContinue? (y/n)\n> "
            )
        ).strip().lower()
        if proceed != "y":
            return False
        for index, candidate in enumerate(candidates, start=1):
            self.ui.print(f"[SYSTEM] Trying Wi-Fi {index}/{len(candidates)}: {candidate.ssid}")
            result = await asyncio.to_thread(connect_to_ssid, candidate.ssid)
            self.ui.print(f"[SYSTEM] {result.message}")
            if not result.ok:
                continue
            room = await self._discover_room()
            if room is None:
                continue
            return await self._join_discovered_room(room)
        if original_ssid:
            self.ui.print(f"[SYSTEM] No room found. Reconnecting to {original_ssid}...")
            result = await asyncio.to_thread(connect_to_ssid, original_ssid)
            self.ui.print(f"[SYSTEM] {result.message}")
        self.ui.print("[SYSTEM] No room found on saved Wi-Fi candidates.")
        return False

    async def _choose_wifi_and_scan(self) -> bool:
        assert self.ui is not None
        status = await asyncio.to_thread(get_wifi_status)
        original_ssid = status.current_ssid
        candidates = await asyncio.to_thread(list_saved_visible_networks, exclude=original_ssid)
        if not candidates:
            self.ui.print("[SYSTEM] No other saved and visible Wi-Fi networks are available.")
            return False
        self.ui.print("[SYSTEM] Saved and visible Wi-Fi:")
        for index, candidate in enumerate(candidates, start=1):
            self.ui.print(f"{index}. {candidate.ssid}")
        choice = (await self.ui.prompt_text("Select Wi-Fi number, or 0 to cancel:\n> ")).strip()
        if not choice.isdigit():
            self.ui.print("[SYSTEM] Cancelled.")
            return False
        selected = int(choice)
        if selected == 0:
            return False
        if selected < 1 or selected > len(candidates):
            self.ui.print("[SYSTEM] Invalid selection.")
            return False
        target = candidates[selected - 1].ssid
        proceed = (
            await self.ui.prompt_text(f"Connect to {target}? This may disconnect your current network. (y/n)\n> ")
        ).strip().lower()
        if proceed != "y":
            return False
        result = await asyncio.to_thread(connect_to_ssid, target)
        self.ui.print(f"[SYSTEM] {result.message}")
        if not result.ok:
            return False
        room = await self._discover_room()
        if room is not None:
            return await self._join_discovered_room(room)
        self.ui.print(f"[SYSTEM] No room found on {target}.")
        if original_ssid:
            restore = (await self.ui.prompt_text(f"Reconnect to {original_ssid}? (y/n)\n> ")).strip().lower()
            if restore == "y":
                restore_result = await asyncio.to_thread(connect_to_ssid, original_ssid)
                self.ui.print(f"[SYSTEM] {restore_result.message}")
        return False

    async def join_room(self, room: RoomInfo) -> None:
        assert self.storage is not None and self.ui is not None
        if self.host_monitor_task is not None and self.host_monitor_task is not asyncio.current_task():
            self.host_monitor_task.cancel()
            try:
                await self.host_monitor_task
            except asyncio.CancelledError:
                pass
            self.host_monitor_task = None
        if self.server is not None:
            await self.server.stop()
            self.server = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        client = PChatClient(
            config=self.config,
            storage=self.storage,
            ui=self.ui,
            room=room,
            on_unread=self.notify_unread,
            on_host_failed=self.handle_host_failed,
            control_port=self.election.port if self.election and self.election.enabled else 0,
        )
        try:
            await client.connect()
        except Exception:
            await client.close()
            raise
        self.client = client
        self.mode = "Client"
        self.room_epoch = room.room_epoch
        self.host_id = room.host_id
        self.config.last_server_ip = room.host_ip

    async def create_room(self) -> None:
        assert self.storage is not None and self.ui is not None
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.server is not None:
            self.ui.print("[SYSTEM] Room is already hosted on this machine.")
            return
        self.server = PChatServer(
            config=self.config,
            storage=self.storage,
            ui=self.ui,
            room_name=DEFAULT_ROOM_NAME,
            on_unread=self.notify_unread,
            room_epoch=self.room_epoch,
        )
        try:
            await self.server.start()
        except Exception as exc:
            self.ui.print(f"[SYSTEM] Failed to create room: {exc}")
            if self.server is not None:
                await self.server.stop()
            self.server = None
            return
        self.mode = "Host"
        self.host_id = self.config.client_id
        self._start_host_monitor()
        self.ui.print(f"[SYSTEM] Hosting room '{DEFAULT_ROOM_NAME}' at {get_lan_ip()}:9000.")
        self.ui.print("[SYSTEM] Other users can join automatically by starting P-Chat on the same LAN.")

    def _start_host_monitor(self) -> None:
        if self.host_monitor_task is not None and not self.host_monitor_task.done():
            self.host_monitor_task.cancel()
        self.host_monitor_task = asyncio.create_task(self._host_split_brain_monitor())

    async def _host_split_brain_monitor(self) -> None:
        assert self.ui is not None
        while self.mode == "Host":
            await asyncio.sleep(10)
            try:
                rooms = await discover_rooms(timeout=1.5)
            except OSError:
                continue
            candidates = [room for room in rooms if room.host_id and room.host_id != self.config.client_id]
            if not candidates:
                continue
            best = candidates[0]
            current_key = (-self.room_epoch, self.config.client_id)
            best_key = (-best.room_epoch, best.host_id)
            if best_key < current_key:
                self.ui.print("[SYSTEM] Higher-priority Host detected. Rejoining to avoid split brain.")
                await self.join_room(best)
                return

    async def become_host_from_election(self, room_epoch: int) -> None:
        assert self.ui is not None
        self.ui.print("[SYSTEM] This client was elected as new Host.")
        self.room_epoch = room_epoch
        self.host_id = self.config.client_id
        if self.client is not None:
            await self.client.close()
            self.client = None
        await self.create_room()

    async def join_elected_host(
        self,
        host_ip: str,
        chat_port: int,
        http_port: int,
        room_epoch: int,
        host_id: str,
    ) -> None:
        assert self.ui is not None
        self.ui.print(f"[SYSTEM] Joining elected Host {host_ip}:{chat_port}.")
        room = RoomInfo(
            room_name=DEFAULT_ROOM_NAME,
            host_name="elected",
            host_ip=host_ip,
            chat_port=chat_port,
            http_port=http_port,
            announcement_version=0,
            version=APP_VERSION,
            room_epoch=room_epoch,
            host_id=host_id,
        )
        try:
            await self.join_room(room)
        except Exception as exc:
            self.ui.print(f"[SYSTEM] Failed to join elected Host: {exc}")
            await self.reconnect()

    async def send_chat(self, content: str) -> None:
        assert self.ui is not None
        if self.mode == "Host" and self.server is not None:
            await self.server.post_host_chat(content)
        elif self.mode == "Client" and self.client is not None:
            await self.client.send_chat(content)
        else:
            self.ui.print("[SYSTEM] Not in a room. Type /reconnect or /create.")

    async def sync(self) -> None:
        assert self.ui is not None
        if self.mode == "Client" and self.client is not None:
            await self.client.request_sync()
        elif self.mode == "Host":
            self.ui.print("[SYSTEM] Host storage is already local.")
        else:
            self.ui.print("[SYSTEM] Not connected.")

    async def change_nick(self, name: str) -> None:
        assert self.ui is not None
        name = name.strip()[:32]
        if not name:
            self.ui.print("Usage: /nick <name>")
            return
        old = self.config.username
        self.config.username = name
        if self.mode == "Client" and self.client is not None:
            await self.client.change_nick(name)
        elif self.mode == "Host":
            self.ui.print(f"[SYSTEM] {old} is now {name}.")

    async def undo(self) -> None:
        assert self.ui is not None
        if self.mode == "Host" and self.server is not None:
            await self.server.undo_host_message()
        elif self.mode == "Client" and self.client is not None:
            await self.client.undo()
        else:
            self.ui.print("[SYSTEM] Not connected.")

    async def show_users(self) -> None:
        assert self.ui is not None
        if self.mode == "Host" and self.server is not None:
            self.ui.print("[SYSTEM] Users: " + ", ".join(self.server.online_users()))
        elif self.mode == "Client" and self.client is not None:
            if self.client.users:
                self.ui.print("[SYSTEM] Users: " + ", ".join(self.client.users))
            await self.client.request_users()
        else:
            self.ui.print("[SYSTEM] Not connected.")

    def show_history(self, limit: int = HISTORY_DEFAULT_LIMIT) -> None:
        assert self.storage is not None and self.ui is not None
        rows = self.storage.recent_messages(limit)
        if not rows:
            self.ui.print("[SYSTEM] No local history.")
            return
        for row in rows:
            self.ui.print(format_message_block(row, current_user=self.config.username))

    def show_status(self) -> None:
        assert self.ui is not None
        wifi = get_wifi_status()
        server_addr = "-"
        http_addr = "-"
        users = 0
        room = "-"
        if self.mode == "Client" and self.client is not None:
            server_addr = f"{self.client.room.host_ip}:{self.client.room.chat_port}"
            http_addr = f"{self.client.room.host_ip}:{self.client.room.http_port}"
            users = len(self.client.users)
            room = self.client.room.room_name
        elif self.mode == "Host" and self.server is not None:
            ip = get_lan_ip()
            server_addr = f"{ip}:{self.server.chat_port}"
            http_addr = f"{ip}:{self.server.http_port}"
            users = len(self.server.online_users())
            room = self.server.room_name
        self.ui.print(f"Mode      : {self.mode}")
        self.ui.print(f"Username  : {self.config.username}")
        self.ui.print(f"Wi-Fi     : {wifi.current_ssid or 'unknown'}")
        self.ui.print(f"Server    : {server_addr}")
        self.ui.print(f"HTTP      : {http_addr}")
        self.ui.print(f"Room      : {room}")
        self.ui.print(f"Users     : {users}")
        self.ui.print(f"Epoch     : {self.room_epoch}")
        self.ui.print(f"Host ID   : {self.host_id or '-'}")
        self.ui.print(f"Translate : {self._translation_status_text()}")
        self.ui.print(f"Version   : {APP_VERSION}")

    def show_wifi(self) -> None:
        assert self.ui is not None
        status = get_wifi_status()
        candidates = list_saved_visible_networks(exclude=status.current_ssid)
        self.ui.print(f"Current Wi-Fi: {status.current_ssid or 'unknown'}")
        self.ui.print(f"Wi-Fi API    : {'ready' if status.available else 'unavailable'}")
        if status.message:
            self.ui.print(status.message)
        if candidates:
            self.ui.print("Saved + visible:")
            for candidate in candidates:
                self.ui.print(f"- {candidate.ssid}")
        else:
            self.ui.print("Saved + visible: none")

    def show_translate_status(self) -> None:
        assert self.ui is not None
        self.ui.print(f"Translate : {self._translation_status_text()}")
        if self.mode == "Idle":
            self.ui.print("[SYSTEM] Translation only affects local message display after joining or hosting a room.")

    def toggle_translate(self) -> None:
        if self.config.translation_enabled:
            self.translate_off()
        else:
            self.translate_on()

    def translate_on(self) -> None:
        assert self.ui is not None
        if self.mode == "Client" and self.client is not None:
            ok, message = self.client.set_translation_enabled(True)
            self.ui.print(f"[SYSTEM] {message}")
            return
        if self.mode == "Host" and self.server is not None:
            ok, message = self.server.set_translation_enabled(True)
            self.ui.print(f"[SYSTEM] {message}")
            return
        self.config.translation_enabled = True
        self.ui.print("[SYSTEM] Translation will apply after this machine joins or hosts a room.")

    def translate_off(self) -> None:
        assert self.ui is not None
        if self.mode == "Client" and self.client is not None:
            ok, message = self.client.set_translation_enabled(False)
            self.ui.print(f"[SYSTEM] {message}")
            return
        if self.mode == "Host" and self.server is not None:
            ok, message = self.server.set_translation_enabled(False)
            self.ui.print(f"[SYSTEM] {message}")
            return
        self.config.translation_enabled = False
        self.ui.print("[SYSTEM] Translation disabled.")

    def set_translate_key(self, api_key: str) -> None:
        assert self.ui is not None
        api_key = api_key.strip()
        if not api_key:
            self.ui.print("Usage: /translate key <API_KEY>")
            return
        self.config.translation_api_key = api_key
        self.ui.print("[SYSTEM] LibreTranslate API key saved to local config.")

    def clear_translate_key(self) -> None:
        assert self.ui is not None
        self.config.translation_api_key = ""
        self.ui.print("[SYSTEM] LibreTranslate API key cleared from local config.")

    def _translation_status_text(self) -> str:
        if self.mode == "Client" and self.client is not None:
            return self.client.translation_status()
        if self.mode == "Host" and self.server is not None:
            return self.server.translation_status()
        if self.config.translation_enabled:
            return "on (inactive until room join/host)"
        return "off"

    async def reconnect(self) -> None:
        await self.leave_room()
        await self.discover_and_join_or_create()

    async def leave_room(self) -> None:
        assert self.ui is not None
        if self.host_monitor_task is not None:
            self.host_monitor_task.cancel()
            try:
                await self.host_monitor_task
            except asyncio.CancelledError:
                pass
            self.host_monitor_task = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.server is not None:
            await self.server.stop()
            self.server = None
        self.mode = "Idle"
        self.ui.print("[SYSTEM] Left room.")

    def export_history(self) -> None:
        assert self.storage is not None and self.ui is not None
        path = self.config.exports_dir / f"pchat_export_{now_iso().replace(':', '-')}.txt"
        self.storage.export_messages(path)
        self.ui.print(f"[SYSTEM] Exported: {path}")

    def clear_screen(self) -> None:
        assert self.ui is not None
        self.ui.clear()

    async def quit(self) -> None:
        assert self.ui is not None
        self.stopping = True
        self.ui.stop()
        await self.shutdown()

    def announce_show(self) -> None:
        assert self.storage is not None and self.ui is not None
        announcement = self.storage.current_announcement()
        if not announcement:
            self.ui.print("[SYSTEM] No announcement.")
            return
        self.ui.print(f"[ANNOUNCEMENT v{announcement['version']}]")
        self.ui.print(str(announcement["content"]))

    async def announce_set(self, text: str) -> None:
        assert self.ui is not None
        if self.mode != "Host" or self.server is None:
            self.ui.print("[SYSTEM] Only Host can set announcements.")
            return
        if not text:
            self.ui.print("Usage: /announce set <text>")
            return
        await self.server.set_announcement(text)

    async def announce_rollback(self) -> None:
        assert self.ui is not None
        if self.mode != "Host" or self.server is None:
            self.ui.print("[SYSTEM] Only Host can roll back announcements.")
            return
        await self.server.rollback_announcement()

    def announce_history(self) -> None:
        assert self.storage is not None and self.ui is not None
        rows = self.storage.announcement_history()
        if not rows:
            self.ui.print("[SYSTEM] No announcement history.")
            return
        for row in rows:
            active = " *" if int(row.get("active", 0)) else ""
            self.ui.print(f"v{row['version']}{active} [{row['created_at']}] {row['created_by']}: {row['content']}")

    async def update_check_or_show(self, *, force_check: bool = False) -> None:
        assert self.storage is not None and self.ui is not None
        if self.mode == "Client" and self.client is not None:
            if force_check or self.client.latest_update is None:
                await self.client.check_update()
            else:
                self.client.show_update()
        elif self.mode == "Host":
            update = self.storage.latest_update()
            if update:
                self.ui.print(f"Version : {update.get('version')}")
                self.ui.print(f"File    : {update.get('file_name')} ({update.get('file_size')} bytes)")
                self.ui.print(f"SHA-256 : {update.get('sha256')}")
                self.ui.print(f"Notes   : {update.get('notes')}")
            else:
                self.ui.print("[SYSTEM] No update published.")
        else:
            self.ui.print("[SYSTEM] Not connected.")

    def update_notes(self) -> None:
        assert self.storage is not None and self.ui is not None
        if self.mode == "Client" and self.client is not None:
            self.client.show_update_notes()
            return
        update = self.storage.latest_update()
        self.ui.print(str(update.get("notes", "[SYSTEM] No update published.") if update else "[SYSTEM] No update published."))

    async def update_apply(self) -> None:
        assert self.ui is not None
        if self.mode == "Client" and self.client is not None:
            await self.client.apply_update()
        else:
            self.ui.print("[SYSTEM] /update apply is for clients.")

    async def update_publish(self, file_path: Path, version: str | None, notes: str | None) -> None:
        assert self.ui is not None
        if self.mode != "Host" or self.server is None:
            self.ui.print("[SYSTEM] Only Host can publish updates.")
            return
        await self.server.publish_update(file_path, version, notes)

    def show_help(self) -> None:
        assert self.ui is not None
        self.ui.print("P-Chat commands:")
        self.ui.print("/help, /guide, /sync, /nick <name>, /undo, /users, /history [n], /status")
        self.ui.print("/wifi, /translate on|off|status|key <API_KEY>|clear-key")
        self.ui.print("/reconnect, /create, /leave, /export, /clear, /quit")
        self.ui.print("/announce show | set <text> | rollback | history")
        self.ui.print("/update, /update check, /update notes, /update apply, /update publish <file> [version] [notes]")
        self.ui.print("/pc help, /pc guide, /pc status, /pc wifi, /pc version, /pc open-data")

    def show_guide(self) -> None:
        assert self.ui is not None
        self.ui.print(first_run_guide())

    def show_version(self) -> None:
        assert self.ui is not None
        self.ui.print(f"P-Chat {APP_VERSION}")

    def open_data_dir(self) -> None:
        assert self.ui is not None
        if os.name == "nt":
            os.startfile(str(self.config.base_dir))  # type: ignore[attr-defined]
        else:
            self.ui.print(str(self.config.base_dir))

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.server is not None:
            await self.server.stop()
            self.server = None
        if self.host_monitor_task is not None:
            self.host_monitor_task.cancel()
            try:
                await self.host_monitor_task
            except asyncio.CancelledError:
                pass
            self.host_monitor_task = None
        if self.storage is not None:
            self.storage.close()
            self.storage = None
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if self.election is not None:
            self.election.stop()
            self.election = None
        if self.ui is not None:
            self.ui.stop()
