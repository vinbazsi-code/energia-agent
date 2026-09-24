"""MAVIR által megfigyelhető tárolók (akkumulátorok) energiaforgalmazása.

Az összes MAVIR-nak látható energiatároló aggregált töltés/kisütés
teljesítménye, MW-ban, negyedórás bontásban - a rtdwweb
chart-rendszerből (chart_id=22361, "MAVIR által megfigyelhető tárolók
energiaforgalmazása", a
mavir.hu/web/mavir/mavir-altal-megfigyelheto-tarolok-energiaforgalmazasa
oldal alapértelmezett chartja).

FIGYELEM: ez ORSZÁGOS/piaci szintű aggregátum (az összes MAVIR-nak
látható tároló együtt), NEM a Balassagyarmat-i saját akkumulátoroké -
csak referenciaként/benchmark-ként hasznos, hogy lásd, a piac többi
tárolója mikor tölt/süt ki.

Az oszlopnevek validálva 2026-09-24-én valós exporton (a chart
legendája alapján megsejtett nevek pontosan egyeztek).
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

CHART_ID = "22361"
CHART_EXPORT_URL = f"https://rtdwweb.mavir.hu/rtdwweb/webuser/chart/{CHART_ID}/export"
USER_AGENT = "energia-agent-collector/0.1 (kontakt: vinbazsi@gmail.com)"
SOURCE_NAME = "MAVIR_RTDWWEB_22361"

# Excel oszlopnév -> (metric név) - a chart legendája alapján, MÉG NEM validálva valós exporton
COLUMN_METRIC_MAP = {
    "Energiatárolók - Összes megfigyelhető beépített teljesítmény - kitárolás [MW]": "tarolo_osszes_kapacitas_kitarolas_mw",
    "Energiatárolók - Összes megfigyelhető beépített teljesítmény - betárolás [MW]": "tarolo_osszes_kapacitas_betarolas_mw",
    "Energiatárolók - Megfigyelt beépített teljesítmény - kitárolás [MW]": "tarolo_megfigyelt_kapacitas_kitarolas_mw",
    "Energiatárolók - Megfigyelt beépített teljesítmény - betárolás [MW]": "tarolo_megfigyelt_kapacitas_betarolas_mw",
    "Energiatárolók - Mért tény kitárolás [MW]": "tarolo_teny_kitarolas_mw",
    "Energiatárolók - Mért tény betárolás [MW]": "tarolo_teny_betarolas_mw",
}

LOG_DIR = PROJECT_ROOT / "logs"

log = logging.getLogger("mavir_tarolok")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "mavir_tarolok.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def fetch_chart_xlsx(hours_back: float = 48) -> bytes:
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


def parse_chart_xlsx(xlsx_bytes: bytes) -> list[tuple[datetime, dict]]:
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb.worksheets[0]

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise RuntimeError("Üres export - nincs adat a chartban")
    header = {str(name).strip(): idx for idx, name in enumerate(rows[0]) if name}
    log.info("Talált fejléc: %s", list(header.keys()))

    col_indices = {}
    for col_name, metric in COLUMN_METRIC_MAP.items():
        idx = header.get(col_name)
        if idx is None:
            log.warning("Nem található oszlop: '%s' - kihagyva (a sejtett oszlopnév eltérhet a valóditól!)", col_name)
            continue
        col_indices[metric] = idx

    results = []
    for row in rows[1:]:
        if not row or row[0] is None:
            continue
        ts_local = datetime.strptime(str(row[0]), "%Y.%m.%d %H:%M:%S %z")
        ts_utc = ts_local.astimezone(timezone.utc)
        values = {}
        for metric, idx in col_indices.items():
            if row[idx] is not None:
                values[metric] = float(row[idx])
        if values:
            results.append((ts_utc, values))
    return results


def get_live_tarolok(hours_back: float = 24) -> list[tuple[datetime, dict]]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés."""
    return parse_chart_xlsx(fetch_chart_xlsx(hours_back))


def collect(hours_back: float = 48) -> int:
    xlsx_bytes = fetch_chart_xlsx(hours_back)
    log.info("Letöltve: chart %s export (%d KB)", CHART_ID, len(xlsx_bytes) // 1024)

    rows = parse_chart_xlsx(xlsx_bytes)
    log.info("Feldolgozott sorok: %d", len(rows))

    collected_at = datetime.now(timezone.utc)
    collected_iso = collected_at.isoformat()
    measurement_rows = []
    for ts, values in rows:
        for metric, value in values.items():
            measurement_rows.append(
                {
                    "timestamp_utc": ts.isoformat(),
                    "source": SOURCE_NAME,
                    "scope": "system",
                    "asset_id": "",
                    "metric": metric,
                    "value": value,
                    "unit": "MW",
                    "collected_at": collected_iso,
                }
            )

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
