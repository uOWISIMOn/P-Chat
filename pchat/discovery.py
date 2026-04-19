from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, Callable

from .constants import APP_ID, APP_VERSION, DISCOVERY_MAGIC, DISCOVERY_PORT
from .models import RoomInfo
from .utils import get_local_ip_for_peer


async def discover_rooms(timeout: float = 2.0, port: int = DISCOVERY_PORT) -> list[RoomInfo]:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    try:
        sock.bind(("", 0))
        await loop.sock_sendto(sock, DISCOVERY_MAGIC.encode("utf-8"), ("255.255.255.255", port))
        rooms: list[RoomInfo] = []
        seen: set[tuple[str, int]] = set()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                data, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if payload.get("type") != "discover_response" or payload.get("app") != APP_ID:
                continue
            if not payload.get("host_ip"):
                payload["host_ip"] = addr[0]
            key = (str(payload.get("host_ip")), int(payload.get("chat_port", 0)))
            if key in seen:
                continue
            seen.add(key)
            rooms.append(RoomInfo.from_dict(payload))
        rooms.sort(key=lambda item: (-item.room_epoch, item.host_id))
        return rooms
    finally:
        sock.close()


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, response_factory: Callable[[str], dict[str, Any]]) -> None:
        self.response_factory = response_factory
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.decode("utf-8", errors="ignore").strip() != DISCOVERY_MAGIC:
            return
        response = self.response_factory(addr[0])
        encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
        if self.transport is not None:
            self.transport.sendto(encoded, addr)


class DiscoveryResponder:
    def __init__(
        self,
        *,
        room_name: str,
        host_name: str,
        chat_port: int,
        http_port: int,
        get_announcement_version: Callable[[], int],
        get_room_epoch: Callable[[], int],
        get_host_id: Callable[[], str],
        port: int = DISCOVERY_PORT,
    ) -> None:
        self.room_name = room_name
        self.host_name = host_name
        self.chat_port = chat_port
        self.http_port = http_port
        self.get_announcement_version = get_announcement_version
        self.get_room_epoch = get_room_epoch
        self.get_host_id = get_host_id
        self.port = port
        self.transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(self._make_response),
            local_addr=("0.0.0.0", self.port),
            allow_broadcast=True,
            reuse_port=False,
        )

    def _make_response(self, peer_ip: str) -> dict[str, Any]:
        return {
            "type": "discover_response",
            "app": APP_ID,
            "version": APP_VERSION,
            "room_name": self.room_name,
            "host_name": self.host_name,
            "host_ip": get_local_ip_for_peer(peer_ip),
            "chat_port": self.chat_port,
            "http_port": self.http_port,
            "announcement_version": self.get_announcement_version(),
            "room_epoch": self.get_room_epoch(),
            "host_id": self.get_host_id(),
        }

    def stop(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
