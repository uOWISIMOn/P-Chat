from __future__ import annotations

import os
import re
import subprocess
import time

from .models import WifiStatus, WifiSwitchResult


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


def profile_exists(ssid: str) -> bool:
    if os.name != "nt":
        return False
    code, output = _run_netsh(["wlan", "show", "profiles"])
    if code != 0:
        return False
    return ssid.lower() in output.lower()


def get_wifi_status(target_ssid: str) -> WifiStatus:
    if os.name != "nt":
        return WifiStatus(
            current_ssid="",
            target_ssid=target_ssid,
            connected=False,
            available=False,
            message="Wi-Fi check is only available on Windows.",
        )
    current = get_current_ssid()
    if not current:
        return WifiStatus(
            current_ssid="",
            target_ssid=target_ssid,
            connected=False,
            available=True,
            message="No active Wi-Fi SSID detected.",
        )
    return WifiStatus(
        current_ssid=current,
        target_ssid=target_ssid,
        connected=current == target_ssid,
        available=True,
        message="",
    )


def connect_to_ssid(target_ssid: str) -> WifiSwitchResult:
    if os.name != "nt":
        return WifiSwitchResult(False, "Wi-Fi switching is only available on Windows.")
    if not profile_exists(target_ssid):
        return WifiSwitchResult(False, f'Windows has no saved Wi-Fi profile named "{target_ssid}".')
    code, output = _run_netsh(["wlan", "connect", f"name={target_ssid}"])
    if code != 0:
        return WifiSwitchResult(False, output.strip() or "netsh wlan connect failed.")
    time.sleep(4)
    current = get_current_ssid()
    if current == target_ssid:
        return WifiSwitchResult(True, f"Connected to {target_ssid}.")
    return WifiSwitchResult(False, f"Connection command was sent, but current Wi-Fi is {current or 'unknown'}.")
