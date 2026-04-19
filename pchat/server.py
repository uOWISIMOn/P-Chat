from __future__ import annotations

import asyncio
import re
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import APP_VERSION
from .config import ConfigManager
from .constants import CHAT_PORT, DEFAULT_ROOM_NAME, HOST_PING_INTERVAL, HTTP_PORT, SYNC_MESSAGE_LIMIT
from .discovery import DiscoveryResponder
from .http_update import UpdateHttpServer
from .storage import Storage
from .tls_util import create_server_ssl_context
from .ui import ChatUI
from .utils import (
    format_message_block,
    get_lan_ip,
    get_local_ip_for_peer,
    new_uuid,
    now_iso,
    safe_filename,
    sha256_file,
    write_json,
    read_json,
)


@dataclass(slots=True)
class ClientSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    username: str
    peer_ip: str
    client_id: str
    control_port: int
    joined_at_seq: int
    last_message_uuid: str | None = None


class PChatServer:
    def __init__(
        self,
        *,
        config: ConfigManager,
        storage: Storage,
        ui: ChatUI,
        room_name: str = DEFAULT_ROOM_NAME,
        chat_port: int = CHAT_PORT,
        http_port: int = HTTP_PORT,
        on_unread: Callable[[], None] | None = None,
        room_epoch: int = 1,
    ) -> None:
        self.config = config
        self.storage = storage
        self.ui = ui
        self.room_name = room_name
        self.chat_port = chat_port
        self.http_port = http_port
        self.server: asyncio.AbstractServer | None = None
        self.discovery: DiscoveryResponder | None = None
        self.http_server: UpdateHttpServer | None = None
        self.clients: dict[int, ClientSession] = {}
        self.host_last_message_uuid: str | None = None
        self.on_unread = on_unread
        self.room_epoch = room_epoch
        self.host_id = self.config.client_id
        self._next_join_seq = 1
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        try:
            ssl_context = create_server_ssl_context(self.config.certs_dir)
            self.server = await asyncio.start_server(
                self._handle_client,
                host="0.0.0.0",
                port=self.chat_port,
                ssl=ssl_context,
            )
            self.discovery = DiscoveryResponder(
                room_name=self.room_name,
                host_name=socket.gethostname(),
                chat_port=self.chat_port,
                http_port=self.http_port,
                get_announcement_version=self.storage.max_announcement_version,
                get_room_epoch=lambda: self.room_epoch,
                get_host_id=lambda: self.host_id,
            )
            await self.discovery.start()
            self.http_server = UpdateHttpServer(self.config.updates_dir, port=self.http_port)
            await asyncio.to_thread(self.http_server.start)
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stopping = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        for session in list(self.clients.values()):
            await self._close_session(session)
        self.clients.clear()
        if self.discovery is not None:
            self.discovery.stop()
            self.discovery = None
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.http_server is not None:
            await asyncio.to_thread(self.http_server.stop)
            self.http_server = None

    def online_users(self) -> list[str]:
        return [self.config.username, *[session.username for session in self.clients.values()]]

    def roster(self) -> list[dict[str, Any]]:
        return [
            {
                "client_id": self.config.client_id,
                "username": self.config.username,
                "ip": get_lan_ip(),
                "control_port": 0,
                "joined_at_seq": 0,
                "last_seen": now_iso(),
            },
            *[
                {
                    "client_id": session.client_id,
                    "username": session.username,
                    "ip": session.peer_ip,
                    "control_port": session.control_port,
                    "joined_at_seq": session.joined_at_seq,
                    "last_seen": now_iso(),
                }
                for session in self.clients.values()
            ],
        ]

    async def post_host_chat(self, content: str) -> None:
        message = {
            "type": "chat",
            "message_uuid": new_uuid(),
            "sender": self.config.username,
            "content": content,
            "created_at": now_iso(),
            "withdrawn": 0,
        }
        self.storage.add_message(
            message["message_uuid"],
            message["sender"],
            message["content"],
            message["created_at"],
            0,
        )
        self.host_last_message_uuid = message["message_uuid"]
        self.ui.print(format_message_block(message, current_user=self.config.username))
        await self._broadcast(message)

    async def undo_host_message(self) -> None:
        row = None
        if self.host_last_message_uuid:
            existing = self.storage.get_message(self.host_last_message_uuid)
            if existing and not int(existing.get("withdrawn", 0)):
                row = self.storage.mark_withdrawn(self.host_last_message_uuid)
        if row is None:
            row = self.storage.withdraw_last_by_sender(self.config.username)
        if row is None:
            self.ui.print("[SYSTEM] No message to withdraw.")
            return
        self.host_last_message_uuid = None
        self.ui.print(format_message_block(row, current_user=self.config.username))
        await self._broadcast({"type": "message_withdrawn", "message_uuid": row["message_uuid"]})

    async def set_announcement(self, content: str) -> None:
        announcement = self.storage.set_announcement(content, self.config.username)
        self._display_announcement(announcement)
        await self._broadcast({"type": "announcement_update", **announcement})

    async def rollback_announcement(self) -> None:
        announcement = self.storage.rollback_announcement(self.config.username)
        if announcement is None:
            self.ui.print("[SYSTEM] No previous announcement to roll back to.")
            return
        self._display_announcement(announcement)
        await self._broadcast({"type": "announcement_update", **announcement})

    async def publish_update(self, source: Path, version: str | None = None, notes: str | None = None) -> None:
        if not source.exists() or not source.is_file():
            self.ui.print(f"[SYSTEM] Update file not found: {source}")
            return
        file_name = safe_filename(source.name)
        dest = self.config.updates_dir / file_name
        try:
            if source.resolve() != dest.resolve():
                shutil.copy2(source, dest)
        except OSError as exc:
            self.ui.print(f"[SYSTEM] Failed to copy update file: {exc}")
            return
        version = version or self._version_from_filename(file_name)
        notes = notes or f"Published update package: {file_name}"
        sha256 = sha256_file(dest)
        update = self.storage.publish_update(version, notes, file_name, dest.stat().st_size, sha256)
        payload = {"type": "update_available", **self._update_payload(update)}
        self.ui.print(f"[SYSTEM] New version available: {version}")
        self.ui.print("Type /update to view details.")
        await self._broadcast(payload)

    def latest_update_payload(self, peer_ip: str | None = None) -> dict[str, Any] | None:
        update = self.storage.latest_update()
        return self._update_payload(update, peer_ip) if update else None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else ""
        session: ClientSession | None = None
        try:
            hello = await asyncio.wait_for(read_json(reader), timeout=10)
            if not hello or hello.get("type") != "hello":
                return
            username = str(hello.get("username") or "Guest").strip()[:32] or "Guest"
            client_id = str(hello.get("client_id") or new_uuid())
            control_port = int(hello.get("control_port", 0) or 0)
            joined_at_seq = self._next_join_seq
            self._next_join_seq += 1
            session = ClientSession(
                reader=reader,
                writer=writer,
                username=username,
                peer_ip=peer_ip,
                client_id=client_id,
                control_port=control_port,
                joined_at_seq=joined_at_seq,
            )
            self.clients[id(writer)] = session
            await self._send(
                session,
                {
                    "type": "welcome",
                    "room_name": self.room_name,
                    "server_version": APP_VERSION,
                    "host_name": socket.gethostname(),
                    "http_port": self.http_port,
                    "client_id": self.host_id,
                    "host_id": self.host_id,
                    "room_epoch": self.room_epoch,
                    "joined_at_seq": joined_at_seq,
                },
            )
            await self._send_initial_sync(session, int(hello.get("known_announcement_version", 0) or 0))
            await self._send_users(session)
            await self._broadcast_system("user_joined", username=username, exclude=session)
            await self._broadcast_users()
            await self._broadcast_roster()
            self.ui.print(f"[SYSTEM] {username} joined.")

            while not self._stopping:
                payload = await read_json(reader)
                if payload is None:
                    break
                keep = await self._handle_payload(session, payload)
                if not keep:
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            if session is not None:
                self.clients.pop(id(writer), None)
                if not self._stopping:
                    await self._broadcast_system("user_left", username=session.username, exclude=session)
                    await self._broadcast_users()
                    await self._broadcast_roster()
                    self.ui.print(f"[SYSTEM] {session.username} left.")
            await self._close_writer(writer)

    async def _handle_payload(self, session: ClientSession, payload: dict[str, Any]) -> bool:
        kind = payload.get("type")
        if kind == "chat":
            await self._handle_chat(session, payload)
        elif kind == "sync_request":
            await self._send_initial_sync(session, int(payload.get("known_announcement_version", 0) or 0))
        elif kind == "nick":
            await self._handle_nick(session, payload)
        elif kind == "undo":
            await self._handle_undo(session)
        elif kind == "users_request":
            await self._send_users(session)
        elif kind == "update_check":
            await self._send(session, {"type": "update_info", "update": self.latest_update_payload(session.peer_ip)})
        elif kind == "leave":
            return False
        else:
            await self._send_error(session, f"Unknown message type: {kind}")
        return True

    async def _handle_chat(self, session: ClientSession, payload: dict[str, Any]) -> None:
        content = str(payload.get("content", "")).strip()
        if not content:
            return
        message = {
            "type": "chat",
            "message_uuid": str(payload.get("message_uuid") or new_uuid()),
            "sender": session.username,
            "content": content,
            "created_at": str(payload.get("created_at") or now_iso()),
            "withdrawn": 0,
        }
        inserted = self.storage.add_message(
            message["message_uuid"],
            message["sender"],
            message["content"],
            message["created_at"],
            0,
        )
        if not inserted:
            return
        await self._send(session, {"type": "chat_ack", "message_uuid": message["message_uuid"], "ok": True})
        session.last_message_uuid = message["message_uuid"]
        self.ui.print(format_message_block(message, current_user=self.config.username))
        if self.on_unread is not None:
            self.on_unread()
        await self._broadcast(message)

    async def _handle_nick(self, session: ClientSession, payload: dict[str, Any]) -> None:
        new_name = str(payload.get("username") or "").strip()[:32]
        if not new_name:
            await self._send_error(session, "Usage: /nick <name>")
            return
        old_name = session.username
        session.username = new_name
        await self._broadcast_system("nick_changed", username=new_name, old_username=old_name)
        await self._broadcast_users()
        await self._broadcast_roster()

    async def _handle_undo(self, session: ClientSession) -> None:
        row = None
        if session.last_message_uuid:
            existing = self.storage.get_message(session.last_message_uuid)
            if existing and not int(existing.get("withdrawn", 0)):
                row = self.storage.mark_withdrawn(session.last_message_uuid)
        if row is None:
            row = self.storage.withdraw_last_by_sender(session.username)
        if row is None:
            await self._send_error(session, "No message to withdraw.")
            return
        session.last_message_uuid = None
        self.ui.print(format_message_block(row, current_user=self.config.username))
        await self._broadcast({"type": "message_withdrawn", "message_uuid": row["message_uuid"]})

    async def _send_initial_sync(self, session: ClientSession, known_announcement_version: int) -> None:
        announcement = self.storage.current_announcement()
        announcement_payload = None
        if announcement and int(announcement["version"]) != known_announcement_version:
            announcement_payload = announcement
        await self._send(
            session,
            {
                "type": "sync_response",
                "messages": self.storage.recent_messages(SYNC_MESSAGE_LIMIT),
                "announcement": announcement_payload,
                "update": self.latest_update_payload(session.peer_ip),
            },
        )

    async def _broadcast_users(self) -> None:
        await self._broadcast({"type": "users", "users": self.online_users()})

    async def _broadcast_roster(self) -> None:
        await self._broadcast(
            {
                "type": "roster_update",
                "roster": self.roster(),
                "room_epoch": self.room_epoch,
                "host_id": self.host_id,
            }
        )

    async def _send_users(self, session: ClientSession) -> None:
        await self._send(session, {"type": "users", "users": self.online_users()})

    async def _heartbeat_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(HOST_PING_INTERVAL)
            await self._broadcast(
                {
                    "type": "host_ping",
                    "host_id": self.host_id,
                    "room_epoch": self.room_epoch,
                    "created_at": now_iso(),
                }
            )

    async def _broadcast_system(self, event: str, exclude: ClientSession | None = None, **extra: Any) -> None:
        await self._broadcast({"type": "system", "event": event, "created_at": now_iso(), **extra}, exclude=exclude)

    async def _broadcast(self, payload: dict[str, Any], exclude: ClientSession | None = None) -> None:
        tasks = [
            self._send(session, payload)
            for session in list(self.clients.values())
            if exclude is None or id(session.writer) != id(exclude.writer)
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send(self, session: ClientSession, payload: dict[str, Any]) -> None:
        try:
            await write_json(session.writer, payload)
        except (ConnectionError, OSError, RuntimeError):
            self.clients.pop(id(session.writer), None)

    async def _send_error(self, session: ClientSession, message: str) -> None:
        await self._send(session, {"type": "error", "message": message})

    async def _close_session(self, session: ClientSession) -> None:
        await self._close_writer(session.writer)

    async def _close_writer(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass

    def _display_announcement(self, announcement: dict[str, Any]) -> None:
        self.ui.print("")
        self.ui.print(f"[ANNOUNCEMENT v{announcement.get('version')}]")
        self.ui.print(str(announcement.get("content", "")))
        self.ui.print("")

    def _update_payload(self, update: dict[str, Any], peer_ip: str | None = None) -> dict[str, Any]:
        payload = dict(update)
        host = get_local_ip_for_peer(peer_ip) if peer_ip else get_lan_ip()
        payload["url"] = f"http://{host}:{self.http_port}/{payload['file_name']}"
        return payload

    def _version_from_filename(self, file_name: str) -> str:
        match = re.search(r"(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?)", file_name)
        return match.group(1) if match else APP_VERSION
