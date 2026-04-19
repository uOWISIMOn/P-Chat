from __future__ import annotations

import os
from pathlib import Path
from typing import Awaitable, Callable

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout


class ChatUI:
    def __init__(self, history_path: Path, commands: list[str]) -> None:
        self.history_path = history_path
        self.commands = commands
        self.session = PromptSession(
            history=FileHistory(str(history_path)),
            completer=WordCompleter(commands, ignore_case=True),
            complete_while_typing=False,
        )
        self.stopped = False

    def print(self, text: str = "") -> None:
        print_formatted_text(text)

    def print_lines(self, lines: list[str]) -> None:
        for line in lines:
            self.print(line)

    async def prompt_text(self, prompt: str = "> ") -> str:
        with patch_stdout():
            return await self.session.prompt_async(prompt)

    async def input_loop(self, handler: Callable[[str], Awaitable[None]]) -> None:
        while not self.stopped:
            try:
                with patch_stdout():
                    line = await self.session.prompt_async("> ")
            except (EOFError, KeyboardInterrupt):
                self.stopped = True
                break
            await handler(line)

    def clear(self) -> None:
        os.system("cls" if os.name == "nt" else "clear")

    def stop(self) -> None:
        self.stopped = True
