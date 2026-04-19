from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .utils import now_iso


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  message_uuid TEXT NOT NULL UNIQUE,
                  sender TEXT NOT NULL,
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  withdrawn INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS announcements (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  version INTEGER NOT NULL,
                  content TEXT NOT NULL,
                  created_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS updates (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  version TEXT NOT NULL,
                  notes TEXT NOT NULL,
                  file_name TEXT NOT NULL,
                  file_size INTEGER NOT NULL,
                  sha256 TEXT NOT NULL,
                  published_at TEXT NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def add_message(
        self,
        message_uuid: str,
        sender: str,
        content: str,
        created_at: str,
        withdrawn: int = 0,
    ) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO messages
                (message_uuid, sender, content, created_at, withdrawn)
                VALUES (?, ?, ?, ?, ?)
                """,
                (message_uuid, sender, content, created_at, int(withdrawn)),
            )
            if cur.rowcount == 0:
                self.conn.execute(
                    "UPDATE messages SET withdrawn = ? WHERE message_uuid = ?",
                    (int(withdrawn), message_uuid),
                )
                return False
            return True

    def mark_withdrawn(self, message_uuid: str) -> dict[str, Any] | None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE messages SET withdrawn = 1 WHERE message_uuid = ?", (message_uuid,))
            return self.get_message(message_uuid)

    def withdraw_last_by_sender(self, sender: str) -> dict[str, Any] | None:
        with self._lock, self.conn:
            row = self.conn.execute(
                """
                SELECT * FROM messages
                WHERE sender = ? AND withdrawn = 0
                ORDER BY id DESC LIMIT 1
                """,
                (sender,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE messages SET withdrawn = 1 WHERE message_uuid = ?", (row["message_uuid"],))
            return self.get_message(str(row["message_uuid"]))

    def get_message(self, message_uuid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM messages WHERE message_uuid = ?", (message_uuid,)).fetchone()
            return self._row_to_dict(row)

    def recent_messages(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM (
                  SELECT * FROM messages ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def all_messages(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM messages ORDER BY id ASC").fetchall()
            return [dict(row) for row in rows]

    def max_announcement_version(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM announcements").fetchone()
            return int(row["version"] if row else 0)

    def current_announcement(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT * FROM announcements
                WHERE active = 1
                ORDER BY version DESC, id DESC LIMIT 1
                """
            ).fetchone()
            return self._row_to_dict(row)

    def set_announcement(self, content: str, created_by: str) -> dict[str, Any]:
        with self._lock, self.conn:
            version = self.max_announcement_version() + 1
            self.conn.execute("UPDATE announcements SET active = 0")
            self.conn.execute(
                """
                INSERT INTO announcements (version, content, created_by, created_at, active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (version, content, created_by, now_iso()),
            )
            return self.current_announcement() or {}

    def upsert_announcement(
        self,
        version: int,
        content: str,
        created_by: str,
        created_at: str,
        active: int = 1,
    ) -> dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute("UPDATE announcements SET active = 0")
            existing = self.conn.execute(
                "SELECT id FROM announcements WHERE version = ? ORDER BY id DESC LIMIT 1",
                (int(version),),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO announcements (version, content, created_by, created_at, active)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (int(version), content, created_by, created_at, int(active)),
                )
            else:
                self.conn.execute(
                    """
                    UPDATE announcements
                    SET content = ?, created_by = ?, created_at = ?, active = ?
                    WHERE id = ?
                    """,
                    (content, created_by, created_at, int(active), existing["id"]),
                )
            return self.current_announcement() or {}

    def rollback_announcement(self, created_by: str) -> dict[str, Any] | None:
        with self._lock:
            active = self.current_announcement()
            if active is None:
                return None
            previous = self.conn.execute(
                """
                SELECT * FROM announcements
                WHERE version < ? ORDER BY version DESC, id DESC LIMIT 1
                """,
                (active["version"],),
            ).fetchone()
            if previous is None:
                return None
            # Create a new version with the previous content so clients always see a version change.
            return self.set_announcement(str(previous["content"]), created_by)

    def announcement_history(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM announcements ORDER BY version ASC, id ASC").fetchall()
            return [dict(row) for row in rows]

    def latest_update(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT * FROM updates
                WHERE active = 1
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            return self._row_to_dict(row)

    def publish_update(
        self,
        version: str,
        notes: str,
        file_name: str,
        file_size: int,
        sha256: str,
    ) -> dict[str, Any]:
        with self._lock, self.conn:
            self.conn.execute("UPDATE updates SET active = 0")
            self.conn.execute(
                """
                INSERT INTO updates
                (version, notes, file_name, file_size, sha256, published_at, active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (version, notes, file_name, int(file_size), sha256, now_iso()),
            )
            return self.latest_update() or {}

    def export_messages(self, export_path: Path) -> Path:
        rows = self.all_messages()
        with export_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                content = "<message withdrawn>" if int(row.get("withdrawn", 0)) else str(row.get("content", ""))
                fh.write(f"[{row.get('created_at', '')}] {row.get('sender', '?')}: {content}\n")
        return export_path
