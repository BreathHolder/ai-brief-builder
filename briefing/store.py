"""SQLite cache: fetched articles, first-seen dates, translations, source health."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url_canon   TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    feed_url    TEXT NOT NULL,
    title       TEXT,
    text        TEXT,
    published   TEXT,
    author      TEXT,
    first_seen  TEXT NOT NULL,
    fetched_at  TEXT
);
CREATE TABLE IF NOT EXISTS translations (
    url_canon         TEXT PRIMARY KEY,
    original_language TEXT NOT NULL,
    title_en          TEXT,
    text_en           TEXT,
    model             TEXT,
    at                TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed_baselines (
    feed_url TEXT PRIMARY KEY,
    at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_runs (
    run_id    TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status    TEXT NOT NULL,
    items     INTEGER NOT NULL,
    error     TEXT,
    at        TEXT NOT NULL,
    PRIMARY KEY (run_id, source_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(articles)")}
        if "skip_reason" not in cols:
            with self.db:
                self.db.execute("ALTER TABLE articles ADD COLUMN skip_reason TEXT")

    def close(self) -> None:
        self.db.close()

    # -- articles -------------------------------------------------------
    def get_article(self, url_canon: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM articles WHERE url_canon = ?", (url_canon,)).fetchone()

    def save_article(
        self,
        *,
        url_canon: str,
        url: str,
        source_id: str,
        feed_url: str,
        title: str | None,
        text: str | None,
        published: datetime | None,
        author: str | None,
        fetched: bool,
        skip_reason: str | None = None,
    ) -> str:
        """Upsert an article; returns its first_seen timestamp (kept from the first insert)."""
        existing = self.get_article(url_canon)
        first_seen = existing["first_seen"] if existing else _now()
        with self.db:
            self.db.execute(
                """INSERT INTO articles (url_canon, url, source_id, feed_url, title, text, published, author, first_seen, fetched_at, skip_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url_canon) DO UPDATE SET
                     title = COALESCE(excluded.title, articles.title),
                     text = COALESCE(excluded.text, articles.text),
                     published = COALESCE(excluded.published, articles.published),
                     author = COALESCE(excluded.author, articles.author),
                     fetched_at = COALESCE(excluded.fetched_at, articles.fetched_at),
                     skip_reason = COALESCE(excluded.skip_reason, articles.skip_reason)""",
                (
                    url_canon, url, source_id, feed_url, title, text,
                    published.isoformat() if published else None, author, first_seen,
                    _now() if fetched else None, skip_reason,
                ),
            )
        return first_seen

    # -- scrape baselines -------------------------------------------------
    def has_baseline(self, feed_url: str) -> bool:
        return self.db.execute("SELECT 1 FROM feed_baselines WHERE feed_url = ?", (feed_url,)).fetchone() is not None

    def set_baseline(self, feed_url: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO feed_baselines (feed_url, at) VALUES (?, ?)", (feed_url, _now()))

    # -- translations ---------------------------------------------------
    def get_translation(self, url_canon: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM translations WHERE url_canon = ?", (url_canon,)).fetchone()

    def save_translation(self, url_canon: str, original_language: str, title_en: str, text_en: str, model: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO translations VALUES (?, ?, ?, ?, ?, ?)",
                (url_canon, original_language, title_en, text_en, model, _now()),
            )

    # -- source health --------------------------------------------------
    def record_source_run(self, run_id: str, source_id: str, status: str, items: int, error: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO source_runs VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, source_id, status, items, error, _now()),
            )

    def recent_source_runs(self, source_id: str, n: int) -> list[sqlite3.Row]:
        with closing(self.db.execute(
            "SELECT * FROM source_runs WHERE source_id = ? ORDER BY run_id DESC LIMIT ?", (source_id, n)
        )) as cur:
            return cur.fetchall()
