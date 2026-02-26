from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import NormalizedMessage, SummaryState


class AgentDB:
    def __init__(self, db_path: str = "agent.db") -> None:
        self.db_path = Path(db_path)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    text TEXT NOT NULL,
                    reply_to TEXT,
                    attachments_json TEXT NOT NULL,
                    UNIQUE(group_name, external_id)
                );

                CREATE TABLE IF NOT EXISTS summary_state (
                    group_name TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    last_summary_ts TEXT,
                    last_message_ts TEXT
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    summary_text TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(summary_state)").fetchall()}
            if "last_message_ts" not in columns:
                conn.execute("ALTER TABLE summary_state ADD COLUMN last_message_ts TEXT")

    def save_messages(self, messages: list[NormalizedMessage]) -> int:
        inserted = 0
        with self._conn() as conn:
            for msg in messages:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO messages (
                        group_name, external_id, author, ts, text, reply_to, attachments_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        msg.group_name,
                        msg.external_id,
                        msg.author,
                        msg.timestamp.isoformat(),
                        msg.text,
                        msg.reply_to,
                        json.dumps(msg.attachments, ensure_ascii=False),
                    ),
                )
                inserted += int(cur.rowcount > 0)
        return inserted

    def load_messages_since(self, group_name: str, since_ts: datetime | None) -> list[NormalizedMessage]:
        query = """
            SELECT group_name, external_id, author, ts, text, reply_to, attachments_json
            FROM messages
            WHERE group_name = ?
        """
        params: list[object] = [group_name]
        if since_ts is not None:
            query += " AND ts > ?"
            params.append(since_ts.isoformat())
        query += " ORDER BY ts ASC"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            NormalizedMessage(
                group_name=row["group_name"],
                external_id=row["external_id"],
                author=row["author"],
                timestamp=datetime.fromisoformat(row["ts"]),
                text=row["text"],
                reply_to=row["reply_to"],
                attachments=json.loads(row["attachments_json"]),
            )
            for row in rows
        ]

    def load_state(self, group_name: str) -> tuple[SummaryState, datetime | None]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state_json, last_message_ts FROM summary_state WHERE group_name = ?",
                (group_name,),
            ).fetchone()

        if row is None:
            return SummaryState(), None

        raw_state = json.loads(row["state_json"])
        state = SummaryState(
            decisions=raw_state.get("decisions", []),
            pending=raw_state.get("pending", []),
            risks=raw_state.get("risks", []),
            current_status=raw_state.get("current_status", "Sem status consolidado"),
        )
        last_message_ts = row["last_message_ts"]
        return state, (datetime.fromisoformat(last_message_ts) if last_message_ts else None)

    def save_summary(
        self,
        group_name: str,
        summary_text: str,
        message_count: int,
        state: SummaryState,
        last_message_ts: datetime,
    ) -> None:
        now = datetime.now().isoformat()
        state_json = json.dumps(
            {
                "decisions": state.decisions,
                "pending": state.pending,
                "risks": state.risks,
                "current_status": state.current_status,
            },
            ensure_ascii=False,
        )

        with self._conn() as conn:
            conn.execute(
                "INSERT INTO summaries (group_name, created_at, message_count, summary_text) VALUES (?, ?, ?, ?)",
                (group_name, now, message_count, summary_text),
            )
            conn.execute(
                """
                INSERT INTO summary_state (group_name, state_json, last_summary_ts, last_message_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_name)
                DO UPDATE SET
                    state_json=excluded.state_json,
                    last_summary_ts=excluded.last_summary_ts,
                    last_message_ts=excluded.last_message_ts
                """,
                (group_name, state_json, now, last_message_ts.isoformat()),
            )

    def get_latest_summary(self, group_name: str) -> dict[str, str | int] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT created_at, message_count, summary_text
                FROM summaries
                WHERE group_name = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (group_name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "created_at": row["created_at"],
            "message_count": int(row["message_count"]),
            "summary_text": row["summary_text"],
        }
