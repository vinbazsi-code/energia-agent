"""HUPX Day-Ahead Market (DAM) árfigyelő gyűjtő.

A HUPX Labs publikus, hitelesítés nélkül elérhető JSON API-jából (nincs
hivatalos dokumentáció-link nélküli scraping, ez egy rendes REST API)
tölti le a magyar (HU) day-ahead aukciós órás/negyedórás árakat:

    https://labs.hupx.hu/data/v1/dam_aggregated_trading_data_15min
        ?filter=DeliveryDay__gte__{date},DeliveryDay__lte__{date},Region__in__HU

A HUPX day-ahead aukció a szállítási napot MEGELŐZŐ napon zárul (kb.
dél körül), tehát ez a gyűjtő tipikusan a "holnapi" napra már ismert
árakat is látja - ez az egyetlen ELŐRETEKINTŐ adatforrás a rendszerben
(a MAVIR-adatok mind visszatekintőek).

FONTOS: a "Quarter hour" (ProductQH, 1-96) a HUPX dokumentációja szerint
mindig CET (UTC+1) szerint van indexelve, FÜGGETLENÜL a nyári/téli
időszámítástól (ez a szokásos EU day-ahead piaci konvenció, hogy mindig
pontosan 96 negyedóra legyen egy napban, ne 92/100 a DST-váltás miatt).

Az ár EUR/MWh-ban van (nincs HUF-átváltás beépítve).
"""

import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import storage  # noqa: E402
from http_utils import get_with_retry  # noqa: E402

API_URL = "https://labs.hupx.hu/data/v1/dam_aggregated_trading_data_15min"
USER_AGENT = "energia-agent-collector/0.1 (kontakt: vinbazsi@gmail.com)"
CET_FIXED = timezone(timedelta(hours=1))  # a HUPX QH-indexelés mindig fix CET, DST-től függetlenül
SOURCE_NAME = "HUPX_DAM"
REGION = "HU"

LOG_DIR = PROJECT_ROOT / "logs"
DAYS_BACK = 3  # ennyi napra megyünk vissza minden futáskor (finalizálás/korrekció miatt)
DAYS_FORWARD = 1  # a "holnapi" ismert nap

log = logging.getLogger("hupx_dam")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "hupx_dam.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def qh_to_utc(delivery_day: str, qh: int) -> datetime:
    """'2026-09-24T00:00:00Z' + ProductQH -> az adott negyedóra kezdete UTC-ben (fix CET indexelés alapján)."""
    y, m, d = (int(x) for x in delivery_day[:10].split("-"))
    local_start = datetime(y, m, d, tzinfo=CET_FIXED) + timedelta(minutes=15 * (qh - 1))
    return local_start.astimezone(timezone.utc)


def fetch_day(day: date) -> list[dict]:
    day_str = day.isoformat()
    url = f"{API_URL}?filter=DeliveryDay__gte__{day_str},DeliveryDay__lte__{day_str},Region__in__{REGION}"
    resp = get_with_retry(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    payload = resp.json()
    if payload.get("message") != "Success":
        raise RuntimeError(f"Váratlan API válasz ({day_str}): {payload}")
    return payload.get("data", [])


def fetch_range(days_back: int, days_forward: int) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    days = [today + timedelta(days=offset) for offset in range(-days_back, days_forward + 1)]

    all_rows: list[dict] = []
    for day in days:
        try:
            rows = fetch_day(day)
        except Exception as e:
            log.warning("Nap kihagyva (%s): %s", day, e)
            continue
        if rows:
            log.info("%s: %d negyedórás ár", day, len(rows))
        all_rows.extend(rows)
    return all_rows


def get_live_prices(days_back: int = 1, days_forward: int = 1) -> list[tuple[datetime, float]]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés."""
    rows = fetch_range(days_back, days_forward)
    return [(qh_to_utc(r["DeliveryDay"], int(r["ProductQH"])), r["Price"]) for r in rows]


def collect(days_back: int = DAYS_BACK, days_forward: int = DAYS_FORWARD) -> int:
    all_rows = fetch_range(days_back, days_forward)
    collected_at = datetime.now(timezone.utc)
    collected_iso = collected_at.isoformat()
    measurement_rows = []
    for row in all_rows:
        ts = qh_to_utc(row["DeliveryDay"], int(row["ProductQH"])).isoformat()
        measurement_rows.append(
            {
                "timestamp_utc": ts,
                "source": SOURCE_NAME,
                "scope": "system",
                "asset_id": "",
                "metric": "hupx_dam_price_eur_mwh",
                "value": row["Price"],
                "unit": "EUR/MWh",
                "collected_at": collected_iso,
            }
        )
        if row.get("Volume") is not None:
            measurement_rows.append(
                {
                    "timestamp_utc": ts,
                    "source": SOURCE_NAME,
                    "scope": "system",
                    "asset_id": "",
                    "metric": "hupx_dam_volume_mw",
                    "value": row["Volume"],
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
