"""ENTSO-E kiegyenlítő/szabályozási energia gyűjtő - Magyarország.

ÉLESÍTVE ÉS VALÓS ADATON VALIDÁLVA (2026-09-24). Az imbalance ár (A85)
működik és plauzibilis (HUF/kWh, ua. tartomány mint a MAVIR-adatnál).
A PICASSO (A67)/MARI (A60) aktivált ár (A84) egyelőre 0 pontot ad -
ez VÁRHATÓ, mert Magyarország csak 2026.10.01-jétől csatlakozik ezekhez
a platformokhoz, október 1. után érdemes újra ellenőrizni.

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

_COLLECTORS_DIR = Path(__file__).resolve().parent
if str(_COLLECTORS_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTORS_DIR))
from entsoe_common import HU_DOMAIN, PROJECT_ROOT, fetch_documents, parse_timeseries_points, sanitize_metric_name  # noqa: E402

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
    points = []
    for xml_text in fetch_documents(params):
        points.extend(parse_timeseries_points(xml_text))
    return points


def fetch_activated_balancing_prices(process_type: str, days_back: int = DAYS_BACK) -> list[tuple[datetime, dict]]:
    params = {
        "documentType": "A84",
        "processType": process_type,
        "controlArea_Domain": HU_DOMAIN,
        **_period_params(days_back),
    }
    points = []
    for xml_text in fetch_documents(params):
        points.extend(parse_timeseries_points(xml_text))
    return points


def get_live_imbalance_price(days_back: float = 1) -> list[tuple[datetime, float]]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés (HUF/kWh)."""
    points = fetch_imbalance_prices(days_back)
    result = []
    for ts, fields in points:
        for field_name, value in fields.items():
            if field_name.lower().endswith("_price.amount"):
                result.append((ts, value / 1000))
    return sorted(result)


# Az ENTSO-E válaszban a price-mezők (pl. 'imbalance_Price.amount',
# feltehetően 'activation_Price.amount' is A84-nél) HUF/MWh-ban jönnek
# (currency_Unit.name=HUF, price_Measure_Unit.name=MWH - ellenőrizve
# valós adaton 2026-09-24-én) - HUF/kWh-ra váltjuk a MAVIR-adatokkal
# való összevethetőség miatt.
PRICE_FIELD_SUFFIXES = ("_price.amount",)


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
                is_price = field_name.lower().endswith(PRICE_FIELD_SUFFIXES)
                measurement_rows.append(
                    {
                        "timestamp_utc": ts.isoformat(),
                        "source": SOURCE_NAME,
                        "scope": "system",
                        "asset_id": "",
                        "metric": sanitize_metric_name(prefix, field_name),
                        "value": value / 1000 if is_price else value,
                        "unit": "HUF/kWh" if is_price else "",
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
