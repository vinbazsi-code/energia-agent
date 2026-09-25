"""ENTSO-E erőmű-üzemzavar (REMIT/UMM - Unavailability of Generation Units) gyűjtő.

Magyarországi (10YHU-MAVIR----U) tervezett és nem tervezett erőmű-
kiesés bejelentéseket kér le az ENTSO-E Transparency Platformról:
    - A77 (Unavailability of Generation Units) - egyedileg nevesített,
      nagyobb (jellemzően >=100 MW) erőművek
    - A78 (Unavailability of Production Units) - kisebb, aggregált
      termelési egységek
mindkettő businessType A53 (tervezett karbantartás) és A54 (nem
tervezett kiesés) bontásban.

Miért fontos kereskedési szempontból: egy nagyobb erőmű váratlan
kiesése azonnali rendszerszintű szűkösséget okozhat (hirtelen
árugrás, felszabályozási igény) - ez az egyetlen forrás a rendszerben,
ami az árváltozás OKÁT (nem csak a következményét) mutatja meg,
és a tervezett kiesések (A53) néhány nappal ELŐRE ismertek, tehát
előretekintő jelzésként is használhatók.

Az ENTSO-E kiesés-dokumentum szerkezete ELTÉR a többi (A85/A84)
dokumentumtípusétól - nem egyszerű időpont->érték sorozat, hanem
minden <TimeSeries> egy ESEMÉNYT (egy adott erőmű egy kiesését)
ír le, benne az érintett erőmű nevével/névleges teljesítményével
(nominalP) és az elérhető (üzemelő) teljesítmény időbeli alakulásával
(Available_Period/Point - a kiesés mértéke = nominalP - quantity).
Ezért ehhez KÜLÖN, dedikált XML-parser kell (parse_outage_documents),
nem a generikus entsoe_common.parse_timeseries_points.

A mentés mintája: minden eseményhez KÉT mérési sort írunk a
measurements táblába - egyet a kiesés KEZDETÉN (value=kiesett_mw),
egyet a VÉGÉN (value=0, a kapacitás helyreállt) - így egy egyszerű
lépcsős vonaldiagram (asset_id=erőmű neve szerint csoportosítva)
helyesen mutatja, mikor mennyi kapacitás volt kiesve, séma-bővítés
nélkül, a meglévő generikus measurements-táblával.
"""

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

_COLLECTORS_DIR = Path(__file__).resolve().parent
if str(_COLLECTORS_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTORS_DIR))
from entsoe_common import HU_DOMAIN, PROJECT_ROOT, _strip_ns, fetch_documents  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
import storage  # noqa: E402

SOURCE_NAME = "ENTSOE_OUTAGES"
LOG_DIR = PROJECT_ROOT / "logs"
DAYS_BACK = 3
DAYS_FORWARD = 7  # a tervezett (A53) kiesések előre bejelentettek

log = logging.getLogger("entsoe_outages")

# (documentType, businessType) kombinációk - lásd fejléc-komment.
# FONTOS: az (A78, A53) kombináció (kisebb/aggregált egység, tervezett)
# HTTP 400-at ad Magyarországra (ellenőrizve 2026-09-25-én) - úgy tűnik,
# a MAVIR nem publikál ilyet ezen a végponton, ezért szándékosan KIHAGYVA
# (a get_with_retry úgyis 4x újrapróbálná feleslegesen, ~30 mp-et pazarolva).
DOC_BUSINESS_TYPES = (
    ("A77", "A53"),  # nagyobb erőmű, tervezett
    ("A77", "A54"),  # nagyobb erőmű, nem tervezett
    ("A78", "A54"),  # kisebb/aggregált egység, nem tervezett
)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "entsoe_outages.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def _period_params(days_back: int, days_forward: int) -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    end = now + timedelta(days=days_forward)
    fmt = "%Y%m%d%H%M"
    return {"periodStart": start.strftime(fmt), "periodEnd": end.strftime(fmt)}


RESOLUTION_TO_TIMEDELTA = {
    "PT1M": timedelta(minutes=1),
    "PT15M": timedelta(minutes=15),
    "PT30M": timedelta(minutes=30),
    "PT60M": timedelta(hours=1),
}


def _parse_dt(date_text: str, time_text: str) -> datetime:
    # pl. '2026-09-22' + '01:01:00Z' -> 2026-09-22T01:01:00+00:00
    time_clean = time_text.rstrip("Z")
    return datetime.strptime(f"{date_text}T{time_clean}", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def parse_outage_documents(xml_text: str) -> list[dict]:
    """Egy A77/A78 kiesés-dokumentumot esemény-listává alakít.

    Minden esemény: {resource_name, business_type, start, end, nominal_mw, unavailable_mw}
    - unavailable_mw None, ha nominalP nem szerepel a dokumentumban (ritka, de előfordulhat).
    """
    root = ElementTree.fromstring(xml_text)
    events = []

    for ts_el in root:
        if _strip_ns(ts_el.tag) != "TimeSeries":
            continue

        business_type = None
        resource_name = None
        nominal_mw = None
        avail_start = None
        avail_resolution = None
        points: list[tuple[int, float]] = []
        start_dt = None
        end_dt = None
        start_date = start_time = end_date = end_time = None

        for child in ts_el:
            tag = _strip_ns(child.tag)
            if tag == "businessType":
                business_type = child.text.strip() if child.text else None
            elif tag == "production_RegisteredResource.name":
                resource_name = child.text.strip() if child.text else None
            elif tag == "production_RegisteredResource.pSRType.powerSystemResources.nominalP":
                try:
                    nominal_mw = float(child.text.strip())
                except (TypeError, ValueError):
                    nominal_mw = None
            elif tag == "start_DateAndOrTime.date":
                start_date = child.text.strip() if child.text else None
            elif tag == "start_DateAndOrTime.time":
                start_time = child.text.strip() if child.text else None
            elif tag == "end_DateAndOrTime.date":
                end_date = child.text.strip() if child.text else None
            elif tag == "end_DateAndOrTime.time":
                end_time = child.text.strip() if child.text else None
            elif tag == "Available_Period":
                for sub in child:
                    sub_tag = _strip_ns(sub.tag)
                    if sub_tag == "timeInterval":
                        for s in sub:
                            if _strip_ns(s.tag) == "start":
                                avail_start = datetime.strptime(s.text.strip(), "%Y-%m-%dT%H:%MZ").replace(
                                    tzinfo=timezone.utc
                                )
                    elif sub_tag == "resolution":
                        avail_resolution = RESOLUTION_TO_TIMEDELTA.get(sub.text.strip())
                    elif sub_tag == "Point":
                        pos = None
                        qty = None
                        for p in sub:
                            p_tag = _strip_ns(p.tag)
                            if p_tag == "position":
                                pos = int(p.text.strip())
                            elif p_tag == "quantity":
                                qty = float(p.text.strip())
                        if pos is not None and qty is not None:
                            points.append((pos, qty))

        if start_date and start_time:
            start_dt = _parse_dt(start_date, start_time)
        if end_date and end_time:
            end_dt = _parse_dt(end_date, end_time)

        if resource_name is None or start_dt is None or end_dt is None:
            continue  # hiányos esemény, kihagyjuk

        if nominal_mw is not None and points and avail_start is not None and avail_resolution is not None:
            # a legkisebb elérhető (available) mennyiség adja a legnagyobb kiesést -
            # egy esemény alatt ez jellemzően egyetlen pont, de több pont esetén a minimumot vesszük
            min_available = min(qty for _pos, qty in points)
            unavailable_mw = round(nominal_mw - min_available, 3)
        else:
            unavailable_mw = None

        events.append(
            {
                "resource_name": resource_name,
                "business_type": business_type,
                "start": start_dt,
                "end": end_dt,
                "nominal_mw": nominal_mw,
                "unavailable_mw": unavailable_mw,
            }
        )

    return events


def _dedupe_events(events: list[dict]) -> list[dict]:
    """Az ENTSO-E ugyanazt a kiesés-bejelentést TÖBBSZÖR is visszaadhatja (a REMIT
    üzenet minden korábbi revízióját külön dokumentumként adja vissza a lekérdezett
    időszakra) - ellenőrizve 2026-09-25-én valós adaton: pl. a Paksi Atomerőmű egy
    eseménye 18-szor szerepelt azonos (erőmű, kezdet, vég, kiesett MW) adatokkal, ami
    18x-osan felduzzasztotta az összesített kiesett teljesítményt. Ez a függvény az
    azonos (erőmű, kezdet, vég, kiesett_mw) négyest egyetlen eseményre vonja össze."""
    seen = {}
    for ev in events:
        key = (ev["resource_name"], ev["start"], ev["end"], ev["unavailable_mw"])
        seen[key] = ev
    return list(seen.values())


def fetch_outage_events(days_back: int = DAYS_BACK, days_forward: int = DAYS_FORWARD) -> list[dict]:
    all_events = []
    for doc_type, biz_type in DOC_BUSINESS_TYPES:
        params = {
            "documentType": doc_type,
            "businessType": biz_type,
            "biddingZone_Domain": HU_DOMAIN,
            **_period_params(days_back, days_forward),
        }
        try:
            docs = fetch_documents(params)
        except Exception as e:
            log.warning("Kihagyva (%s/%s): %s", doc_type, biz_type, e)
            continue
        for xml_text in docs:
            try:
                all_events.extend(parse_outage_documents(xml_text))
            except ElementTree.ParseError as e:
                log.warning("XML parse hiba (%s/%s): %s", doc_type, biz_type, e)
        log.info("%s/%s: %d dokumentum", doc_type, biz_type, len(docs))
    deduped = _dedupe_events(all_events)
    log.info("Deduplikálás: %d nyers esemény -> %d egyedi esemény", len(all_events), len(deduped))
    return deduped


def get_live_outages(days_back: float = 3, days_forward: float = 7) -> list[dict]:
    """Kényelmi függvény a dashboardnak: közvetlen, adatbázis nélküli élő lekérdezés."""
    return fetch_outage_events(int(days_back), int(days_forward))


def collect(days_back: int = DAYS_BACK, days_forward: int = DAYS_FORWARD) -> int:
    events = fetch_outage_events(days_back, days_forward)
    log.info("Összesen %d üzemzavar/karbantartás esemény", len(events))

    collected_at = datetime.now(timezone.utc)
    collected_iso = collected_at.isoformat()
    measurement_rows = []
    for ev in events:
        if ev["unavailable_mw"] is None:
            continue  # nominalP nélkül nem tudjuk számszerűsíteni a kiesés méretét
        asset_id = ev["resource_name"]
        metric = "kiesett_kapacitas_mw"
        # lépcsős jelalak: kezdéskor felugrik a kiesett MW-ra, végén visszaesik 0-ra
        measurement_rows.append(
            {
                "timestamp_utc": ev["start"].isoformat(),
                "source": SOURCE_NAME,
                "scope": "plant",
                "asset_id": asset_id,
                "metric": metric,
                "value": ev["unavailable_mw"],
                "unit": "MW",
                "collected_at": collected_iso,
            }
        )
        measurement_rows.append(
            {
                "timestamp_utc": ev["end"].isoformat(),
                "source": SOURCE_NAME,
                "scope": "plant",
                "asset_id": asset_id,
                "metric": metric,
                "value": 0.0,
                "unit": "MW",
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
