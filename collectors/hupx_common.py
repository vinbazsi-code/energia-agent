"""Közös segédfüggvények a HUPX Labs API-t használó gyűjtőkhöz."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from http_utils import get_with_retry  # noqa: E402

API_BASE = "https://labs.hupx.hu/data/v1"
USER_AGENT = "energia-agent-collector/0.1 (kontakt: vinbazsi@gmail.com)"
CET_FIXED = timezone(timedelta(hours=1))  # a HUPX QH-indexelés (DAM, IDA) mindig fix CET, DST-től függetlenül


def qh_to_utc(delivery_day: str, qh: int) -> datetime:
    """'2026-09-24T00:00:00Z' + ProductQH (1-96) -> a negyedóra kezdete UTC-ben, fix CET indexelés alapján."""
    y, m, d = (int(x) for x in delivery_day[:10].split("-"))
    local_start = datetime(y, m, d, tzinfo=CET_FIXED) + timedelta(minutes=15 * (qh - 1))
    return local_start.astimezone(timezone.utc)


def fetch(endpoint: str, date_field: str, day, region: str = "HU") -> list[dict]:
    """Egy nap lekérdezése egy HUPX Labs dataset-ből (pl. endpoint='idc_quarterhourly', date_field='DeliveryDate')."""
    day_str = day.isoformat()
    url = f"{API_BASE}/{endpoint}?filter={date_field}__gte__{day_str},{date_field}__lte__{day_str},Region__in__{region}"
    resp = get_with_retry(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    payload = resp.json()
    if payload.get("message") != "Success":
        raise RuntimeError(f"Váratlan API válasz ({endpoint}, {day_str}): {payload}")
    return payload.get("data", [])
