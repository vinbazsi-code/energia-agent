"""MAVIR hálózati frekvencia gyűjtő - ez a ténylegesen ÉLŐ adatforrás.

A rtdwweb chart-rendszer 4444-es chartja (Frekvencia oldal) 1 perces
bontásban publikálja a hálózati frekvenciát (Hz), gyakorlatilag a
jelen pillanatig - nincs benne az elszámolási adatokra jellemző
napos csúszás. A fetch/parse függvényeket a dashboard.py is importálja,
hogy közvetlenül, adatbázis nélkül tudjon "élő" nézetet mutatni.
"""

import io
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openpyxl
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import storage  # noqa: E402

CHART_ID = "4444"
CHART_EXPORT_URL = f"https://rtdwweb.mavir.hu/rtdwweb/webuser/chart/{CHART_ID}/export"
USER_AGENT = "energia-agent-collector/0.1 (kontakt: vinbazsi@gmail.com)"
COLUMN_NAME = "Hálózati frekvencia"
SOURCE_NAME = "MAVIR_RTDWWEB_4444"

RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"

log = logging.getLogger("mavir_frekvencia")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "mavir_frekvencia.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def fetch_frequency_xlsx(hours_back: float = 1) -> bytes:
    """1 perces bontású frekvencia export az elmúlt `hours_back` órára."""
    now = datetime.now(timezone.utc)
    from_ms = int((now - timedelta(hours=hours_back)).timestamp() * 1000)
    to_ms = int(now.timestamp() * 1000)
    url = (
        f"{CHART_EXPORT_URL}?exportType=xlsx&fromTime={from_ms}&toTime={to_ms}"
        f"&periodType=min&period=1"
    )
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "spreadsheet" not in content_type:
        raise RuntimeError(f"Váratlan Content-Type ({content_type}) ettől: {url}")
    return resp.content


def parse_frequency_xlsx(xlsx_bytes: bytes) -> list[tuple[datetime, float]]:
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb.worksheets[0]

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise RuntimeError("Üres export - nincs adat a chartban")
    header = {str(name).strip(): idx for idx, name in enumerate(rows[0]) if name}
    value_col = header.get(COLUMN_NAME)
    if value_col is None:
        raise RuntimeError(f"Nem található a(z) '{COLUMN_NAME}' oszlop - megváltozott a chart formátuma?")

    results = []
    for row in rows[1:]:
        if not row or row[0] is None:
            continue
        ts_local = datetime.strptime(str(row[0]), "%Y.%m.%d %H:%M:%S %z")
        ts_utc = ts_local.astimezone(timezone.utc)
        value = row[value_col]
        if value is None:
            continue
        results.append((ts_utc, float(value)))
    return results


def get_live_frequency(hours_back: float = 1) -> list[tuple[datetime, float]]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés."""
    return parse_frequency_xlsx(fetch_frequency_xlsx(hours_back))


def collect() -> int:
    xlsx_bytes = fetch_frequency_xlsx(hours_back=6)
    log.info("Letöltve: chart %s export (%d KB)", CHART_ID, len(xlsx_bytes) // 1024)

    rows = parse_frequency_xlsx(xlsx_bytes)
    log.info("Feldolgozott sorok: %d", len(rows))

    collected_at = datetime.now(timezone.utc)
    measurement_rows = [
        {
            "timestamp_utc": ts.isoformat(),
            "source": SOURCE_NAME,
            "scope": "system",
            "asset_id": "",
            "metric": "frekvencia_hz",
            "value": value,
            "unit": "Hz",
            "collected_at": collected_at.isoformat(),
        }
        for ts, value in rows
    ]

    conn = storage.connect()
    try:
        n = storage.upsert_measurements(conn, measurement_rows)
        log.info("Adatbázisba írva/frissítve: %d mérési sor -> %s", n, storage.DB_PATH)
    finally:
        conn.close()

    return n


def main() -> int:
    setup_logging()
    try:
        collect()
    except Exception:
        log.exception("A gyűjtés hibával leállt")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
