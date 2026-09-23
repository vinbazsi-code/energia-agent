"""HUPX Intraday Continuous (IDC) piac árfigyelő gyűjtő.

A HUPX Labs publikus API-jából (nincs hitelesítés az olvasáshoz) tölti
le a folyamatos kereskedésű intraday piac negyedórás összesített
adatait: forgalommal súlyozott átlagár (VWAP) és kereskedett mennyiség.

    https://labs.hupx.hu/data/v1/idc_quarterhourly
        ?filter=DeliveryDate__gte__{date},DeliveryDate__lte__{date},Region__in__HU

Itt a 'DeliveryDate' mező már közvetlenül UTC időpont (nincs szükség a
DAM/IDA-nál használt fix-CET átszámításra).
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_COLLECTORS_DIR = Path(__file__).resolve().parent
if str(_COLLECTORS_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTORS_DIR))
from hupx_common import PROJECT_ROOT, fetch  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
import storage  # noqa: E402

ENDPOINT = "idc_quarterhourly"
DATE_FIELD = "DeliveryDate"
SOURCE_NAME = "HUPX_IDC"

LOG_DIR = PROJECT_ROOT / "logs"
DAYS_BACK = 3
DAYS_FORWARD = 1

log = logging.getLogger("hupx_idc")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "hupx_idc.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def fetch_range(days_back: int, days_forward: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    days = [today + timedelta(days=offset) for offset in range(-days_back, days_forward + 1)]

    all_rows: list[dict] = []
    for day in days:
        try:
            rows = fetch(ENDPOINT, DATE_FIELD, day)
        except Exception as e:
            log.warning("Nap kihagyva (%s): %s", day, e)
            continue
        if rows:
            log.info("%s: %d negyedórás sor", day, len(rows))
        all_rows.extend(rows)
    return all_rows


def get_live_prices(days_back: int = 1, days_forward: int = 1) -> list[tuple[datetime, float]]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés (VWAP ár)."""
    rows = fetch_range(days_back, days_forward)
    result = []
    for r in rows:
        if r.get("PriceVWAPLast") is None:
            continue
        ts = datetime.fromisoformat(r["DeliveryDate"].replace("Z", "+00:00"))
        result.append((ts, r["PriceVWAPLast"]))
    return sorted(result)


def collect(days_back: int = DAYS_BACK, days_forward: int = DAYS_FORWARD) -> int:
    all_rows = fetch_range(days_back, days_forward)
    collected_at = datetime.now(timezone.utc)
    collected_iso = collected_at.isoformat()

    measurement_rows = []
    for row in all_rows:
        ts = datetime.fromisoformat(row["DeliveryDate"].replace("Z", "+00:00")).isoformat()
        if row.get("PriceVWAPLast") is not None:
            measurement_rows.append(
                {
                    "timestamp_utc": ts,
                    "source": SOURCE_NAME,
                    "scope": "system",
                    "asset_id": "",
                    "metric": "hupx_idc_vwap_price_eur_mwh",
                    "value": row["PriceVWAPLast"],
                    "unit": "EUR/MWh",
                    "collected_at": collected_iso,
                }
            )
        if row.get("VolumeTotalTraded") is not None:
            measurement_rows.append(
                {
                    "timestamp_utc": ts,
                    "source": SOURCE_NAME,
                    "scope": "system",
                    "asset_id": "",
                    "metric": "hupx_idc_volume_traded_mwh",
                    "value": row["VolumeTotalTraded"],
                    "unit": "MWh",
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
