"""Időjárás gyűjtő (Balassagyarmat) - Open-Meteo API.

Az Open-Meteo teljesen ingyenes, nem igényel API-kulcsot vagy
regisztrációt (lásd open-meteo.com). A PV-termelés becsléséhez
releváns mezőket gyűjti: hőmérséklet, felhőzet, globál- és direkt
napsugárzás, óránkénti bontásban, múlt+jövő ablakkal.

    https://api.open-meteo.com/v1/forecast
        ?latitude=48.0787&longitude=19.2988
        &hourly=temperature_2m,cloud_cover,shortwave_radiation,direct_radiation
        &timezone=UTC&past_days=7&forecast_days=7

Megjegyzés: ez NEM MAVIR-adat, hanem a Balassagyarmat-i portfólióhoz
(a két KÁT-os napelempark helyszínéhez) kötött, helyszín-specifikus
kontextus-adat - scope='portfolio'.
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import storage  # noqa: E402
from http_utils import get_with_retry  # noqa: E402

API_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "energia-agent-collector/0.1 (kontakt: vinbazsi@gmail.com)"
SOURCE_NAME = "OPEN_METEO"

# Balassagyarmat, ahol a két KÁT-os napelempark + akkumulátor van (lásd CLAUDE.md.md)
LATITUDE = 48.0787
LONGITUDE = 19.2988

HOURLY_FIELDS = ["temperature_2m", "cloud_cover", "shortwave_radiation", "direct_radiation"]
METRIC_MAP = {
    "temperature_2m": ("weather_temp_c", "°C"),
    "cloud_cover": ("weather_cloud_cover_pct", "%"),
    "shortwave_radiation": ("weather_shortwave_radiation_w_m2", "W/m2"),
    "direct_radiation": ("weather_direct_radiation_w_m2", "W/m2"),
}

LOG_DIR = PROJECT_ROOT / "logs"
PAST_DAYS = 7
FORECAST_DAYS = 7

log = logging.getLogger("weather")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "weather.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def fetch_forecast(past_days: int = PAST_DAYS, forecast_days: int = FORECAST_DAYS) -> dict:
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": ",".join(HOURLY_FIELDS),
        "timezone": "UTC",
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    resp = get_with_retry(f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT}, timeout=30)
    return resp.json()


def get_live_forecast(past_days: int = 2, forecast_days: int = 3) -> list[tuple[datetime, dict]]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés."""
    payload = fetch_forecast(past_days, forecast_days)
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    result = []
    for i, t in enumerate(times):
        ts = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        values = {field: hourly[field][i] for field in HOURLY_FIELDS if field in hourly}
        result.append((ts, values))
    return result


def collect(past_days: int = PAST_DAYS, forecast_days: int = FORECAST_DAYS) -> int:
    payload = fetch_forecast(past_days, forecast_days)
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    log.info("Letöltve: %d óra (múlt %d nap + előrejelzés %d nap)", len(times), past_days, forecast_days)

    collected_at = datetime.now(timezone.utc)
    collected_iso = collected_at.isoformat()
    measurement_rows = []
    for i, t in enumerate(times):
        ts = datetime.fromisoformat(t).replace(tzinfo=timezone.utc).isoformat()
        for field in HOURLY_FIELDS:
            if field not in hourly:
                continue
            value = hourly[field][i]
            if value is None:
                continue
            metric, unit = METRIC_MAP[field]
            measurement_rows.append(
                {
                    "timestamp_utc": ts,
                    "source": SOURCE_NAME,
                    "scope": "portfolio",
                    "asset_id": "",
                    "metric": metric,
                    "value": value,
                    "unit": unit,
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
