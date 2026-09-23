"""ENTSO-E kiegyenlítő/szabályozási energia gyűjtő - Magyarország.

MÉG NEM ÉLESÍTETT, VALÓS ADATON NEM TESZTELT VÁZ - a felhasználó API
tokenjére vár (lásd collectors/entsoe_common.py fejléce a regisztrációs
folyamatért). Amint van érvényes ENTSOE_API_TOKEN környezeti változó,
ez a szkript kipróbálható és a szükséges finomítások (mezőnevek,
metrika-elnevezések) elvégezhetők a valós válasz alapján.

Lekérdezi Magyarországra (10YHU-MAVIR----U):
    - A85 Imbalance prices (kiegyenlítő energia egységár, ENTSO-E oldalon)
    - A84 Activated balancing prices, processType A67 (Central Selection
      aFRR - ez felel meg a PICASSO platformnak)
    - A84 Activated balancing prices, processType A60 (Scheduled
      activation mFRR - ez felel meg a MARI platformnak)

Ezek célja NEM a MAVIR-adatok kiváltása, hanem KERESZTELLENŐRZÉS és a
PICASSO/MARI (európai, összehangolt aFRR/mFRR piactér) perspektíva
hozzáadása a már meglévő magyar (MAVIR) nézethez.
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from entsoe_common import HU_DOMAIN, PROJECT_ROOT, fetch_xml, parse_timeseries_points, sanitize_metric_name

sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
import storage  # noqa: E402

SOURCE_NAME = "ENTSOE_BALANCING"
LOG_DIR = PROJECT_ROOT / "logs"
DAYS_BACK = 3

log = logging.getLogger("entsoe_balancing")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "entsoe_balancing.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def _period_params(days_back: int) -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    fmt = "%Y%m%d%H%M"
    return {"periodStart": start.strftime(fmt), "periodEnd": now.strftime(fmt)}


def fetch_imbalance_prices(days_back: int = DAYS_BACK) -> list[tuple[datetime, dict]]:
    params = {
        "documentType": "A85",
        "controlArea_Domain": HU_DOMAIN,
        **_period_params(days_back),
    }
    xml_text = fetch_xml(params)
    return parse_timeseries_points(xml_text)


def fetch_activated_balancing_prices(process_type: str, days_back: int = DAYS_BACK) -> list[tuple[datetime, dict]]:
    params = {
        "documentType": "A84",
        "processType": process_type,
        "controlArea_Domain": HU_DOMAIN,
        **_period_params(days_back),
    }
    xml_text = fetch_xml(params)
    return parse_timeseries_points(xml_text)


def collect(days_back: int = DAYS_BACK) -> int:
    collected_at = datetime.now(timezone.utc)
    collected_iso = collected_at.isoformat()
    measurement_rows = []

    sources = (
        ("imbalance", lambda: fetch_imbalance_prices(days_back)),
        ("afrr_picasso", lambda: fetch_activated_balancing_prices("A67", days_back)),  # PICASSO
        ("mfrr_mari", lambda: fetch_activated_balancing_prices("A60", days_back)),  # MARI
    )

    for prefix, fetcher in sources:
        try:
            points = fetcher()
        except Exception as e:
            log.warning("Kihagyva (%s): %s", prefix, e)
            continue
        log.info("%s: %d pont", prefix, len(points))
        for ts, fields in points:
            for field_name, value in fields.items():
                measurement_rows.append(
                    {
                        "timestamp_utc": ts.isoformat(),
                        "source": SOURCE_NAME,
                        "scope": "system",
                        "asset_id": "",
                        "metric": sanitize_metric_name(prefix, field_name),
                        "value": value,
                        "unit": "",  # a mezőnév tartalmazza, mit jelent (ár/mennyiség) - finomítandó valós adaton
                        "collected_at": collected_iso,
                    }
                )

    if not measurement_rows:
        log.warning("Nincs beírható sor - lásd a fenti figyelmeztetéseket (pl. hiányzó token).")
        return 0

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
