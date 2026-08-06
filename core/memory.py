"""
Memory: the part that makes Jarvis feel like it learns.

Worth being precise about what "learning" means here, because the word carries
more weight than the technology deserves. Nothing in this file makes the
underlying model smarter — its weights are fixed and untouchable. What this
does is accumulate context, so the same model gets better material to work
with every time you talk to it.

In practice that is indistinguishable from learning. An assistant that knows
your mother is "Mum" in your contacts, that you leave for college at 8:40, and
that you always want the text rather than the phone call, is one that behaves
as though it learned those things. It just learned them into a database instead
of into a neural network.

Three kinds of memory live here:

  conversation  every turn, for short-term continuity
  facts         durable things about you, deduplicated by key
  episodes      what Jarvis did and how it went, so mistakes are visible
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("jarvis.memory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_time
    ON conversation(created_at DESC);

-- Facts are keyed so that re-learning something overwrites rather than
-- accumulates. Without this, asking "who is mum" ten times would put ten
-- copies of the answer into the context window.
CREATE TABLE IF NOT EXISTS facts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'inferred',
    confidence REAL NOT NULL DEFAULT 0.5,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT    NOT NULL,
    params      TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_action
    ON episodes(action, created_at DESC);
"""


class Memory:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- conversation ----

    def remember_turn(self, role: str, content: str) -> None:
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be user or assistant, got {role!r}")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversation (role, content, created_at) VALUES (?, ?, ?)",
                (role, content, time.time()),
            )

    def recent_turns(self, limit: int = 20) -> list[dict[str, str]]:
        """The last `limit` turns, oldest first, ready to hand to the brain."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM conversation "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # ---- facts ----

    def learn(
        self,
        key: str,
        value: str,
        source: str = "inferred",
        confidence: float = 0.5,
    ) -> None:
        """Store or update a durable fact.

        A fact you stated outright should not be silently overwritten by one
        the model guessed at, so a lower-confidence write loses to a
        higher-confidence existing value.
        """
        key = key.strip().lower()
        if not key:
            raise ValueError("fact key cannot be empty")

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT confidence FROM facts WHERE key = ?", (key,)
            ).fetchone()

            if existing and existing["confidence"] > confidence:
                log.debug("keeping higher-confidence value for %r", key)
                return

            conn.execute(
                "INSERT INTO facts (key, value, source, confidence, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "  value=excluded.value, source=excluded.source, "
                "  confidence=excluded.confidence, updated_at=excluded.updated_at",
                (key, value, source, confidence, time.time()),
            )

    def recall(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM facts WHERE key = ?", (key.strip().lower(),)
            ).fetchone()
        return row["value"] if row else None

    def forget(self, key: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM facts WHERE key = ?", (key.strip().lower(),))
            return cur.rowcount > 0

    def all_facts(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM facts ORDER BY confidence DESC, updated_at DESC"
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def context_block(self, limit: int = 40) -> str:
        """Facts rendered for the system prompt.

        Capped deliberately: as the fact table grows, sending all of it would
        crowd out the actual conversation and cost more every single turn.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM facts "
                "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        if not rows:
            return ""
        return "\n".join(f"- {r['key']}: {r['value']}" for r in rows)

    # ---- episodes ----

    def record_episode(
        self,
        action: str,
        params: dict[str, Any],
        outcome: str,
        detail: str = "",
    ) -> None:
        """Log what was attempted and how it went.

        This is the raw material for genuine self-improvement: if ui_tap on a
        particular app fails nine times out of ten, that pattern is visible
        here, and it is the honest evidence a self-edit proposal should be
        built on rather than the model's hunch.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO episodes (action, params, outcome, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (action, json.dumps(params, default=str), outcome, detail, time.time()),
            )

    def failure_rates(self, minimum_attempts: int = 3) -> list[dict[str, Any]]:
        """Actions that fail often enough to be worth fixing."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT action, "
                "       COUNT(*) AS attempts, "
                "       SUM(CASE WHEN outcome != 'ok' THEN 1 ELSE 0 END) AS failures "
                "FROM episodes GROUP BY action HAVING attempts >= ? "
                "ORDER BY (CAST(failures AS REAL) / attempts) DESC",
                (minimum_attempts,),
            ).fetchall()

        return [
            {
                "action": r["action"],
                "attempts": r["attempts"],
                "failures": r["failures"],
                "rate": r["failures"] / r["attempts"],
            }
            for r in rows
        ]
