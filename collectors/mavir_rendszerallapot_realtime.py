"""MAVIR rendszerállapot - közel valós idejű OPERATÍV becslés gyűjtő.

A hivatalos kiegyenlítő energia elszámolási adatok (lásd
collectors/mavir_kiegyenlito.py) kb. 5-6 napos csúszással érhetők csak
el, mert elszámolási minőségű mérési adatra épülnek. Ez a gyűjtő egy
attól FÜGGETLEN, közel valós idejű proxy-t tölt le a MAVIR rtdwweb
chart-rendszeréből (chart_id=1000727: "Kiegyenlítő célú igénybevételek
- NÜKSZ szerinti rendszerállapot meghatározásához"), a "NÜKSZ 3.1.
szerinti rendszerállapot szerinti teljesítmény (MW)" sort.

FONTOS: ez NEM azonos a mavir_kiegyenlito.py-ban gyűjtött, elszámolási
minőségű "Rendszer-irány (kWh)" értékkel - más mértékegység (MW
pillanatnyi teljesítmény, nem kWh negyedórás energia) és operatív,
nem hivatalos elszámolási adat. Csak tájékozódásra, közel valós idejű
becslésként való megjelenítésre alkalmas.
"""

import io
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import storage  # noqa: E402
from http_utils import get_with_retry  # noqa: E402

CHART_ID = "1000727"
CHART_EXPORT_URL = f"https://rtdwweb.mavir.hu/rtdwweb/webuser/chart/{CHART_ID}/export"
USER_AGENT = "energia-agent-collector/0.1 (kontakt: vinbazsi@gmail.com)"
COLUMN_NAME = "NÜKSZ 3.1. szerinti rendszerállapot szerinti teljesítmény (MW)"
SOURCE_NAME = "MAVIR_RTDWWEB_1000727"

RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"

log = logging.getLogger("mavir_rendszerallapot_realtime")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "mavir_rendszerallapot_realtime.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def fetch_chart_xlsx(hours_back: int = 48) -> bytes:
    now = datetime.now(timezone.utc)
    from_ms = int((now - timedelta(hours=hours_back)).timestamp() * 1000)
    to_ms = int(now.timestamp() * 1000)
    url = (
        f"{CHART_EXPORT_URL}?exportType=xlsx&fromTime={from_ms}&toTime={to_ms}"
        f"&periodType=min&period=15"
    )
    resp = get_with_retry(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    content_type = resp.headers.get("Content-Type", "")
    if "spreadsheet" not in content_type:
        raise RuntimeError(f"Váratlan Content-Type ({content_type}) ettől: {url}")
    return resp.content


def parse_chart_xlsx(xlsx_bytes: bytes) -> list[tuple[datetime, float]]:
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


def get_live_rendszerallapot(hours_back: float = 6) -> list[tuple[datetime, float]]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés."""
    return parse_chart_xlsx(fetch_chart_xlsx(hours_back))


def collect() -> int:
    xlsx_bytes = fetch_chart_xlsx()
    log.info("Letöltve: chart %s export (%d KB)", CHART_ID, len(xlsx_bytes) // 1024)

    rows = parse_chart_xlsx(xlsx_bytes)
    log.info("Feldolgozott sorok: %d", len(rows))

    collected_at = datetime.now(timezone.utc)
    measurement_rows = [
        {
            "timestamp_utc": ts.isoformat(),
            "source": SOURCE_NAME,
            "scope": "system",
            "asset_id": "",
            "metric": "rendszerallapot_realtime_mw",
            "value": value,
            "unit": "MW",
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
