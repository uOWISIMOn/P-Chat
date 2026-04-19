from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from .constants import APP_ID, CHAT_PORT, CONTROL_PORT, ELECTION_TIMEOUT, HTTP_PORT
from .utils import get_lan_ip, new_uuid, now_iso


class _ElectionProtocol(asyncio.DatagramProtocol):
    def __init__(self, manager: "ElectionManager") -> None:
        self.manager = manager

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.manager.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        asyncio.create_task(self.manager.handle_datagram(payload, addr))


class ElectionManager:
    def __init__(
        self,
        *,
        client_id: str,
        username: str,
        ui_print: Callable[[str], None],
        get_roster: Callable[[], list[dict[str, Any]]],
        get_room_epoch: Callable[[], int],
        get_host_id: Callable[[], str],
        host_recently_seen: Callable[[], bool],
        become_host: Callable[[int], Awaitable[None]],
        join_host: Callable[[str, int, int, int, str], Awaitable[None]],
        reconnect: Callable[[], Awaitable[None]],
        port: int = CONTROL_PORT,
    ) -> None:
        self.client_id = client_id
        self.username = username
        self.ui_print = ui_print
        self.get_roster = get_roster
        self.get_room_epoch = get_room_epoch
        self.get_host_id = get_host_id
        self.host_recently_seen = host_recently_seen
        self.become_host = become_host
        self.join_host = join_host
        self.reconnect = reconnect
        self.port = port
        self.enabled = False
        self.transport: asyncio.DatagramTransport | None = None
        self.pending_votes: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.election_running = False

    async def start(self) -> bool:
        loop = asyncio.get_running_loop()
        try:
            await loop.create_datagram_endpoint(
                lambda: _ElectionProtocol(self),
                local_addr=("0.0.0.0", self.port),
            )
        except OSError as exc:
            self.ui_print(f"[SYSTEM] Election disabled: UDP control port {self.port} unavailable ({exc}).")
            return False
        self.enabled = True
        return True

    def stop(self) -> None:
        self.enabled = False
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        for queue in self.pending_votes.values():
            queue.put_nowait({"type": "election_vote", "vote": "reject", "reason": "stopping"})
        self.pending_votes.clear()

    async def handle_datagram(self, payload: dict[str, Any], addr: tuple[str, int]) -> None:
        if payload.get("app") != APP_ID:
            return
        kind = payload.get("type")
        if kind == "election_propose":
            await self._handle_proposal(payload, addr)
        elif kind == "election_vote":
            election_id = str(payload.get("election_id", ""))
            queue = self.pending_votes.get(election_id)
            if queue is not None:
                await queue.put(payload)
        elif kind == "election_result":
            await self._handle_result(payload)
        elif kind == "host_probe":
            await self._send(
                {
                    "type": "host_probe_response",
                    "ok": self.host_recently_seen(),
                    "client_id": self.client_id,
                },
                addr[0],
                int(payload.get("reply_port", self.port) or self.port),
            )

    async def trigger_election(self, reason: str) -> None:
        if not self.enabled:
            self.ui_print("[SYSTEM] Election unavailable. Reconnecting.")
            await self.reconnect()
            return
        if self.election_running:
            return
        self.election_running = True
        try:
            await self._run_election(reason)
        finally:
            self.election_running = False

    async def _run_election(self, reason: str) -> None:
        roster = self.get_roster()
        self_member = self._self_member(roster)
        reachable: dict[str, dict[str, Any]] = {self.client_id: self_member}
        election_id = new_uuid()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.pending_votes[election_id] = queue
        epoch = self.get_room_epoch()
        self.ui_print(f"[SYSTEM] Host unavailable ({reason}). Starting election.")
        try:
            for member in roster:
                if member.get("client_id") == self.client_id:
                    continue
                control_port = int(member.get("control_port", 0) or 0)
                ip = str(member.get("ip", ""))
                if not ip or control_port <= 0:
                    continue
                await self._send(
                    {
                        "type": "election_propose",
                        "election_id": election_id,
                        "room_epoch": epoch,
                        "suspected_host_id": self.get_host_id(),
                        "proposer_id": self.client_id,
                        "reply_port": self.port,
                        "roster": roster,
                        "created_at": now_iso(),
                    },
                    ip,
                    control_port,
                )
            deadline = asyncio.get_running_loop().time() + ELECTION_TIMEOUT
            rejected = False
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    vote = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                voter_id = str(vote.get("client_id", ""))
                if vote.get("vote") == "reject":
                    rejected = True
                    self.ui_print(f"[SYSTEM] Election rejected by {vote.get('username', voter_id)}.")
                    break
                if vote.get("vote") == "accept" and voter_id:
                    reachable[voter_id] = {
                        "client_id": voter_id,
                        "username": vote.get("username", ""),
                        "ip": vote.get("ip", ""),
                        "control_port": int(vote.get("control_port", 0) or 0),
                        "joined_at_seq": int(vote.get("joined_at_seq", 999999) or 999999),
                    }
            if rejected:
                await self.reconnect()
                return
            winner = min(reachable.values(), key=lambda item: (int(item.get("joined_at_seq", 999999)), str(item.get("client_id", ""))))
            new_epoch = epoch + 1
            await self._broadcast_result(roster, winner, new_epoch)
            if winner.get("client_id") == self.client_id:
                await self.become_host(new_epoch)
            else:
                await asyncio.sleep(1.0)
                await self.join_host(
                    str(winner.get("ip", "")),
                    CHAT_PORT,
                    HTTP_PORT,
                    new_epoch,
                    str(winner.get("client_id", "")),
                )
        finally:
            self.pending_votes.pop(election_id, None)

    async def _handle_proposal(self, payload: dict[str, Any], addr: tuple[str, int]) -> None:
        reply_port = int(payload.get("reply_port", self.port) or self.port)
        election_id = str(payload.get("election_id", ""))
        member = self._self_member(list(payload.get("roster", []) or self.get_roster()))
        if self.host_recently_seen():
            vote = "reject"
            reason = "host_alive"
        else:
            vote = "accept"
            reason = "host_unreachable"
        await self._send(
            {
                "type": "election_vote",
                "election_id": election_id,
                "vote": vote,
                "reason": reason,
                "client_id": self.client_id,
                "username": self.username,
                "ip": member.get("ip") or get_lan_ip(),
                "control_port": self.port if self.enabled else 0,
                "joined_at_seq": int(member.get("joined_at_seq", 999999) or 999999),
            },
            addr[0],
            reply_port,
        )

    async def _handle_result(self, payload: dict[str, Any]) -> None:
        winner_id = str(payload.get("winner_id", ""))
        new_epoch = int(payload.get("room_epoch", self.get_room_epoch() + 1) or (self.get_room_epoch() + 1))
        if winner_id == self.client_id:
            await self.become_host(new_epoch)
            return
        host_ip = str(payload.get("host_ip", ""))
        if not host_ip:
            await self.reconnect()
            return
        await asyncio.sleep(1.0)
        await self.join_host(
            host_ip,
            int(payload.get("chat_port", CHAT_PORT) or CHAT_PORT),
            int(payload.get("http_port", HTTP_PORT) or HTTP_PORT),
            new_epoch,
            winner_id,
        )

    async def _broadcast_result(self, roster: list[dict[str, Any]], winner: dict[str, Any], new_epoch: int) -> None:
        payload = {
            "type": "election_result",
            "winner_id": winner.get("client_id"),
            "host_ip": winner.get("ip") or get_lan_ip(),
            "chat_port": CHAT_PORT,
            "http_port": HTTP_PORT,
            "room_epoch": new_epoch,
            "host_id": winner.get("client_id"),
            "created_at": now_iso(),
        }
        for member in roster:
            control_port = int(member.get("control_port", 0) or 0)
            ip = str(member.get("ip", ""))
            if not ip or control_port <= 0 or member.get("client_id") == self.client_id:
                continue
            await self._send(payload, ip, control_port)

    def _self_member(self, roster: list[dict[str, Any]]) -> dict[str, Any]:
        for member in roster:
            if member.get("client_id") == self.client_id:
                return dict(member)
        return {
            "client_id": self.client_id,
            "username": self.username,
            "ip": get_lan_ip(),
            "control_port": self.port if self.enabled else 0,
            "joined_at_seq": 999999,
        }

    async def _send(self, payload: dict[str, Any], ip: str, port: int) -> None:
        if self.transport is None:
            return
        payload = {"app": APP_ID, **payload}
        self.transport.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), (ip, port))
