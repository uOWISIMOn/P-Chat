from __future__ import annotations

import os
import re
import subprocess
import time

from .models import WifiCandidate, WifiStatus, WifiSwitchResult


def _run_netsh(args: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["netsh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, output
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def get_current_ssid() -> str:
    if os.name != "nt":
        return ""
    code, output = _run_netsh(["wlan", "show", "interfaces"])
    if code != 0:
        return ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if re.match(r"^SSID\s*:", line, flags=re.IGNORECASE):
            return line.split(":", 1)[1].strip()
    return ""


def list_saved_profiles() -> list[str]:
    if os.name != "nt":
        return []
    code, output = _run_netsh(["wlan", "show", "profiles"])
    if code != 0:
        return []
    profiles: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = re.search(r"(?:All User Profile|Current User Profile)\s*:\s*(.+)$", line, flags=re.IGNORECASE)
        if match is None:
            continue
        ssid = match.group(1).strip()
        if ssid and ssid not in profiles:
            profiles.append(ssid)
    return profiles


def list_visible_networks() -> list[str]:
    if os.name != "nt":
        return []
    code, output = _run_netsh(["wlan", "show", "networks", "mode=bssid"])
    if code != 0:
        return []
    visible: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", line, flags=re.IGNORECASE)
        if match is None:
            continue
        ssid = match.group(1).strip()
        if ssid and ssid not in visible:
            visible.append(ssid)
    return visible


def list_saved_visible_networks(*, exclude: str = "") -> list[WifiCandidate]:
    exclude_key = exclude.strip().casefold()
    saved = {ssid.casefold(): ssid for ssid in list_saved_profiles()}
    visible = {ssid.casefold(): ssid for ssid in list_visible_networks()}
    common_keys = [key for key in visible if key in saved and key != exclude_key]
    return [
        WifiCandidate(ssid=visible[key], saved=True, visible=True)
        for key in sorted(common_keys, key=lambda item: visible[item].casefold())
    ]


def profile_exists(ssid: str) -> bool:
    if os.name != "nt":
        return False
    return ssid.casefold() in {profile.casefold() for profile in list_saved_profiles()}


def get_wifi_status() -> WifiStatus:
    if os.name != "nt":
        return WifiStatus(
            current_ssid="",
            available=False,
            message="Wi-Fi check is only available on Windows.",
        )
    current = get_current_ssid()
    if not current:
        return WifiStatus(
            current_ssid="",
            available=True,
            message="No active Wi-Fi SSID detected.",
        )
    return WifiStatus(
        current_ssid=current,
        available=True,
        message="",
    )


def connect_to_ssid(ssid: str) -> WifiSwitchResult:
    if os.name != "nt":
        return WifiSwitchResult(False, "Wi-Fi switching is only available on Windows.")
    if not profile_exists(ssid):
        return WifiSwitchResult(False, f'Windows has no saved Wi-Fi profile named "{ssid}".')
    code, output = _run_netsh(["wlan", "connect", f"name={ssid}"])
    if code != 0:
        return WifiSwitchResult(False, output.strip() or "netsh wlan connect failed.")
    time.sleep(4)
    current = get_current_ssid()
    if current == ssid:
        return WifiSwitchResult(True, f"Connected to {ssid}.")
    return WifiSwitchResult(False, f"Connection command was sent, but current Wi-Fi is {current or 'unknown'}.")
