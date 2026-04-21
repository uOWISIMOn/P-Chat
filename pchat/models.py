from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class RoomInfo:
    room_name: str
    host_name: str
    host_ip: str
    chat_port: int
    http_port: int
    announcement_version: int
    version: str
    room_epoch: int = 1
    host_id: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RoomInfo":
        return cls(
            room_name=str(payload.get("room_name", "main")),
            host_name=str(payload.get("host_name", "")),
            host_ip=str(payload.get("host_ip", "")),
            chat_port=int(payload.get("chat_port", 9000)),
            http_port=int(payload.get("http_port", 9100)),
            announcement_version=int(payload.get("announcement_version", 0)),
            version=str(payload.get("version", "0.0.0")),
            room_epoch=int(payload.get("room_epoch", 1) or 1),
            host_id=str(payload.get("host_id", "")),
        )


@dataclass(slots=True)
class WifiStatus:
    current_ssid: str
    available: bool
    message: str = ""


@dataclass(slots=True)
class WifiSwitchResult:
    ok: bool
    message: str


@dataclass(slots=True)
class WifiCandidate:
    ssid: str
    saved: bool = True
    visible: bool = True
