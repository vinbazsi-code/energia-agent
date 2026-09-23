"""HUPX Intraday Auction (IDA) piac árfigyelő gyűjtő.

A HUPX Labs publikus API-jából (nincs hitelesítés az olvasáshoz) tölti
le a napon belüli aukciós piac negyedórás árait. Naponta 3 aukciós kör
fut (IDA1, IDA2, IDA3), mindegyiknek külön ára/mennyisége/státusza van
(egy adott negyedórára az adott kör lehet 'deleted' is, ha nem volt rá
kereskedés/ajánlat).

    https://labs.hupx.hu/data/v1/ida_quarterhourly
        ?filter=DeliveryDay__gte__{date},DeliveryDay__lte__{date},Region__in__HU

Ugyanaz a fix-CET ProductQH indexelés, mint a DAM-nál (lásd hupx_common.py).
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_COLLECTORS_DIR = Path(__file__).resolve().parent
if str(_COLLECTORS_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTORS_DIR))
from hupx_common import PROJECT_ROOT, fetch, qh_to_utc  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
import storage  # noqa: E402

ENDPOINT = "ida_quarterhourly"
DATE_FIELD = "DeliveryDay"
SOURCE_NAME = "HUPX_IDA"
ROUNDS = (1, 2, 3)

LOG_DIR = PROJECT_ROOT / "logs"
DAYS_BACK = 3
DAYS_FORWARD = 1

log = logging.getLogger("hupx_ida")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "hupx_ida.log", encoding="utf-8")
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


def get_live_prices(round_no: int = 1, days_back: int = 1, days_forward: int = 1) -> list[tuple[datetime, float]]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés egy adott IDA körre."""
    rows = fetch_range(days_back, days_forward)
    price_key = f"PriceIDA{round_no}"
    result = []
    for r in rows:
        if r.get(price_key) is None:
            continue
        ts = qh_to_utc(r["DeliveryDay"], int(r["ProductQH"]))
        result.append((ts, r[price_key]))
    return sorted(result)


def collect(days_back: int = DAYS_BACK, days_forward: int = DAYS_FORWARD) -> int:
    all_rows = fetch_range(days_back, days_forward)
    collected_at = datetime.now(timezone.utc)
    collected_iso = collected_at.isoformat()

    measurement_rows = []
    for row in all_rows:
        ts = qh_to_utc(row["DeliveryDay"], int(row["ProductQH"])).isoformat()
        for r in ROUNDS:
            price = row.get(f"PriceIDA{r}")
            volume = row.get(f"VolumeIDA{r}")
            if price is not None:
                measurement_rows.append(
                    {
                        "timestamp_utc": ts,
                        "source": SOURCE_NAME,
                        "scope": "system",
                        "asset_id": "",
                        "metric": f"hupx_ida{r}_price_eur_mwh",
                        "value": price,
                        "unit": "EUR/MWh",
                        "collected_at": collected_iso,
                    }
                )
            if volume is not None:
                measurement_rows.append(
                    {
                        "timestamp_utc": ts,
                        "source": SOURCE_NAME,
                        "scope": "system",
                        "asset_id": "",
                        "metric": f"hupx_ida{r}_volume_mw",
                        "value": volume,
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
