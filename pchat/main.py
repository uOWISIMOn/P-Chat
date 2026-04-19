from __future__ import annotations

import asyncio

from .app import PChatApp
from .utils import setup_console_encoding


def main() -> None:
    setup_console_encoding()
    try:
        asyncio.run(PChatApp().run())
    except KeyboardInterrupt:
        print("\nP-Chat closed.")
