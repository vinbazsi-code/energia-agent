"""Közös SQLite tárolási réteg a projekt összes gyűjtőjéhez és a dashboardhoz.

Egyetlen általános, hosszú formátumú (long/tidy) 'measurements' táblát használ,
hogy új adatforrás vagy eszköz (pl. akkumulátor) felvétele csak konfiguráció
legyen, ne séma-változás - lásd CLAUDE.md.md "Alapelvek".

Egy sor egy adott időpillanathoz, adott forráshoz és metrikához tartozó
egyetlen mérőszámot ír le. Rendszer-/portfólió-szintű adatnál asset_id = ''.
"""

import sqlite3
from collections.abc import Iterable
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "energia.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    timestamp_utc TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,           -- 'system' | 'portfolio' | 'asset'
    asset_id TEXT NOT NULL DEFAULT '',  -- '' ha nem eszköz-szintű adat
    metric TEXT NOT NULL,
    value REAL,
    unit TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (timestamp_utc, source, scope, asset_id, metric)
);
CREATE INDEX IF NOT EXISTS idx_measurements_metric_time ON measurements(metric, timestamp_utc);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    model TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_insights_generated_at ON insights(generated_at);
"""

UPSERT_SQL = """
INSERT INTO measurements (timestamp_utc, source, scope, asset_id, metric, value, unit, collected_at)
VALUES (:timestamp_utc, :source, :scope, :asset_id, :metric, :value, :unit, :collected_at)
ON CONFLICT(timestamp_utc, source, scope, asset_id, metric)
DO UPDATE SET value = excluded.value, unit = excluded.unit, collected_at = excluded.collected_at
"""


def connect() -> sqlite3.Connection:
    """Megnyitja (és szükség esetén létrehozza) az adatbázist és a sémát."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def upsert_measurements(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Beszúr vagy frissít mérési sorokat. Visszaadja a feldolgozott sorok számát."""
    rows = list(rows)
    if not rows:
        return 0
    conn.executemany(UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


def save_insight(conn: sqlite3.Connection, generated_at: str, model: str, content: str,
                  period_start: str | None = None, period_end: str | None = None) -> int:
    """Elment egy LLM-generált piaci összefoglalót/stratégiajavaslatot. Visszaadja az új sor id-jét."""
    cur = conn.execute(
        "INSERT INTO insights (generated_at, period_start, period_end, model, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (generated_at, period_start, period_end, model, content),
    )
    conn.commit()
    return cur.lastrowid


def get_latest_insight(conn: sqlite3.Connection) -> dict | None:
    cur = conn.execute(
        "SELECT id, generated_at, period_start, period_end, model, content "
        "FROM insights ORDER BY generated_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        return None
    keys = ["id", "generated_at", "period_start", "period_end", "model", "content"]
    return dict(zip(keys, row))
