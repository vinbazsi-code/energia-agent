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
