"""Small, durable SQLite store for consented caller memory."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


class CallerMemoryStore:
    """Persist the minimum Health Access profile needed between calls."""

    def __init__(self, database_path: Path | str | None = None) -> None:
        self.database_path = Path(
            database_path or Path(__file__).parents[1] / "data" / "caller_memory.db"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_table()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS caller_memory (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    language_preference TEXT,
                    facts TEXT NOT NULL,
                    last_interaction TEXT NOT NULL
                )
                """
            )

    def lookup(self, user_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id, name, language_preference, facts, last_interaction "
                "FROM caller_memory WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "language_preference": row["language_preference"],
            "facts": json.loads(row["facts"]),
            "last_interaction": row["last_interaction"],
        }

    def save(
        self,
        *,
        user_id: str,
        name: str,
        language_preference: str | None,
        facts: dict[str, str],
    ) -> dict[str, object]:
        """Upsert a caller profile after the caller has explicitly consented."""
        record = {
            "user_id": user_id.strip(),
            "name": name.strip()[:100],
            "language_preference": (language_preference or "").strip()[:50],
            "facts": {
                key: value.strip()[:200]
                for key, value in facts.items()
                if value and value.strip()
            },
            "last_interaction": datetime.now(UTC).isoformat(),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO caller_memory (
                    user_id, name, language_preference, facts, last_interaction
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    name = excluded.name,
                    language_preference = excluded.language_preference,
                    facts = excluded.facts,
                    last_interaction = excluded.last_interaction
                """,
                (
                    record["user_id"],
                    record["name"],
                    record["language_preference"],
                    json.dumps(record["facts"]),
                    record["last_interaction"],
                ),
            )
        return record
