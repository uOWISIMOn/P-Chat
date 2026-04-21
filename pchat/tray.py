from __future__ import annotations

import os
import threading
from typing import Callable


class TrayNotifier:
    def __init__(self) -> None:
        self.icon = None
        self.thread: threading.Thread | None = None
        self.monitor_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.on_quit: Callable[[], None] | None = None
        self.enabled = False
        self.disabled_reason = ""
        self._normal_image = None
        self._unread_image = None

    def start(self, on_quit: Callable[[], None] | None = None) -> bool:
        if os.name != "nt":
            self.disabled_reason = "Tray icon is only supported on Windows."
            return False
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            self.disabled_reason = "Tray icon requires: pip install pystray Pillow"
            return False

        self.on_quit = on_quit
        self._normal_image = self._make_icon(Image, ImageDraw, "#5f6b7a")
        self._unread_image = self._make_icon(Image, ImageDraw, "#ff8a00")
        menu = pystray.Menu(
            pystray.MenuItem("Show P-Chat", self._show_clicked, default=True),
            pystray.MenuItem("Hide to tray", self._hide_clicked),
            pystray.MenuItem("Quit", self._quit_clicked),
        )
        self.icon = pystray.Icon("P-Chat", self._normal_image, "P-Chat", menu)
        self.thread = threading.Thread(target=self.icon.run, name="pchat-tray", daemon=True)
        self.thread.start()
        self.monitor_thread = threading.Thread(target=self._monitor_minimize, name="pchat-tray-monitor", daemon=True)
        self.monitor_thread.start()
        self.enabled = True
        return True

    def notify_unread(self) -> None:
        if not self.enabled or self.icon is None or self._unread_image is None:
            return
        self.icon.icon = self._unread_image
        self.icon.title = "P-Chat - new message"

    def clear_unread(self) -> None:
        if not self.enabled or self.icon is None or self._normal_image is None:
            return
        self.icon.icon = self._normal_image
        self.icon.title = "P-Chat"

    def stop(self) -> None:
        self.stop_event.set()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        self.enabled = False

    def hide_window(self) -> None:
        hwnd = self._console_window()
        if hwnd:
            self._show_window(hwnd, 0)

    def show_window(self) -> None:
        hwnd = self._console_window()
        if hwnd:
            self._show_window(hwnd, 5)
            self._show_window(hwnd, 9)
            try:
                import ctypes

                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        self.clear_unread()

    def _show_clicked(self, icon: object, item: object) -> None:
        self.show_window()

    def _hide_clicked(self, icon: object, item: object) -> None:
        self.hide_window()

    def _quit_clicked(self, icon: object, item: object) -> None:
        if self.on_quit is not None:
            self.on_quit()
        self.stop()

    def _monitor_minimize(self) -> None:
        while not self.stop_event.wait(0.5):
            hwnd = self._console_window()
            if hwnd and self._is_minimized(hwnd):
                self.hide_window()
            elif hwnd and self._is_visible(hwnd) and self._is_foreground(hwnd):
                self.clear_unread()

    def _console_window(self) -> int:
        try:
            import ctypes

            return int(ctypes.windll.kernel32.GetConsoleWindow())
        except Exception:
            return 0

    def _show_window(self, hwnd: int, command: int) -> None:
        try:
            import ctypes

            ctypes.windll.user32.ShowWindow(hwnd, command)
        except Exception:
            pass

    def _is_minimized(self, hwnd: int) -> bool:
        try:
            import ctypes

            return bool(ctypes.windll.user32.IsIconic(hwnd))
        except Exception:
            return False

    def _is_visible(self, hwnd: int) -> bool:
        try:
            import ctypes

            return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    def _is_foreground(self, hwnd: int) -> bool:
        try:
            import ctypes

            return int(ctypes.windll.user32.GetForegroundWindow()) == hwnd
        except Exception:
            return False

    def _make_icon(self, image_module: object, draw_module: object, color: str) -> object:
        image = image_module.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = draw_module.Draw(image)
        draw.rounded_rectangle((8, 10, 56, 46), radius=10, fill=color)
        draw.polygon([(24, 46), (32, 56), (38, 46)], fill=color)
        draw.ellipse((18, 24, 24, 30), fill="white")
        draw.ellipse((30, 24, 36, 30), fill="white")
        draw.ellipse((42, 24, 48, 30), fill="white")
        return image
