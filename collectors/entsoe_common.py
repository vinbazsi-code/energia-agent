"""Közös segédfüggvények az ENTSO-E Transparency Platform RESTful API-hoz.

ÉLESÍTVE ÉS VALÓS ADATON VALIDÁLVA (2026-09-24). A használathoz egy
ingyenes, de regisztrációhoz kötött security token kell (lásd
transparency.entsoe.eu, email a transparency@entsoe.eu címre "Restful
API access" tárggyal, kb. 3 munkanapos jóváhagyás, utána Account
Settings alatt tokent generálni).

API alap: https://web-api.tp.entsoe.eu/api
A token SOSEM kerül a kódba/git-be - a ENTSOE_API_TOKEN környezeti
változóból olvassuk.

Magyarország EIC/domain kódja: 10YHU-MAVIR----U

Releváns dokumentum-típusok (IEC 62325 szabvány, ENTSO-E doksi alapján):
    A83 = Activated balancing quantities (aktivált szabályozási energia mennyiség)
    A84 = Activated balancing prices (aktivált szabályozási energia ár)
    A85 = Imbalance prices (kiegyenlítő energia/imbalance árak)
    A86 = Imbalance volume (imbalance mennyiség)

processType (melyik szabályozási termék/platform):
    A67 = Central Selection aFRR  <- ez felel meg a PICASSO platformnak
    A68 = Local Selection aFRR
    A60 = Scheduled activation mFRR  <- ez felel meg a MARI platformnak
    A61 = Direct activation mFRR

businessType:
    A96 = aFRR
    A97 = mFRR

A válasz XML (IEC 62325 market document), nem JSON - lásd parse_timeseries_points().
Nagyobb/több dokumentumos válasznál a szerver ZIP-be csomagolva küldi az
XML(eke)t - ezt a fetch_documents() automatikusan kicsomagolja.

Validált mértékegység (2026-09-24, valós adaton): az A85 imbalance ár
HUF/MWh-ban jön (currency_Unit.name=HUF, price_Measure_Unit.name=MWH) -
lásd entsoe_balancing.py, ahol HUF/kWh-ra váltjuk a MAVIR-adatokkal
való összevethetőség miatt. A85 (imbalance) adat MŰKÖDIK Magyarországra.

FONTOS, kollégai (Krausz Tamás) visszajelzés alapján felfedezett csapda
(2026-09-24): az A85 dokumentum negyedóránként KÉT külön TimeSeries-t ad
vissza, `imbalance_Price.category` A04 és A05 kóddal (a Point-okon belül
külön-külön mezőként). Valós adaton összevetve: A04 következetesen
plauzibilis, a MAVIR-adattal egyező tartományú (~130-170k HUF/MWh),
míg A05 többnyire 0, de időnként irreális kiugrásokat ad (pl. -495000,
-173517 HUF/MWh) - ez feltehetően a 2022 előtti kétáras rendszer
maradványa egy formai kötelező mezőként, miközben Magyarország azóta
egyáras (b=0, s=0) módszertant használ. A entsoe_balancing.py ezért
KIZÁRÓLAG az A04 kategóriájú pontokat használja a kiegyenlítő árhoz -
lásd IMBALANCE_PRICE_VALID_CATEGORY. PICASSO/MARI csatlakozás után
(2026.10.01-től) várhatóan megszűnik ez a kettősség - érdemes utána
újra ellenőrizni, hogy még mindig szükséges-e a szűrés.
A84 (aFRR/mFRR aktivált ár) PICASSO (A67)/MARI (A60) processType-tal
2026-09-24-én még 0 pontot ad vissza - ez VÁRHATÓ, mert Magyarország
csak 2026.10.01-jétől csatlakozik ezekhez a platformokhoz (lásd
mavir.hu "MARI-PICASSO csatlakozás" hírek) - október 1. után érdemes
újra tesztelni.
"""

import io
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from http_utils import get_with_retry  # noqa: E402

API_BASE = "https://web-api.tp.entsoe.eu/api"
USER_AGENT = "energia-agent-collector/0.1 (kontakt: vinbazsi@gmail.com)"
HU_DOMAIN = "10YHU-MAVIR----U"

RESOLUTION_TO_TIMEDELTA = {
    "PT15M": timedelta(minutes=15),
    "PT30M": timedelta(minutes=30),
    "PT60M": timedelta(hours=1),
    "P1D": timedelta(days=1),
}


class MissingTokenError(RuntimeError):
    pass


def get_token() -> str:
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        raise MissingTokenError(
            "Hiányzik az ENTSOE_API_TOKEN környezeti változó. Regisztrálj a "
            "transparency.entsoe.eu-n, kérj RESTful API hozzáférést, majd add meg "
            "a tokent: $env:ENTSOE_API_TOKEN = '...' (PowerShell) mielőtt futtatod."
        )
    return token


def fetch_documents(params: dict) -> list[str]:
    """Egy ENTSO-E API hívás, a securityTokent automatikusan hozzáadva.

    Az ENTSO-E válasza néha sima XML, néha (jellemzően több/nagyobb
    dokumentumnál) egy ZIP-be csomagolt XML-eket ad vissza - ez a
    függvény mindkét esetet kezeli, és mindig a benne található
    XML-dokumentum(ok) szövegét adja vissza listaként.
    """
    token = get_token()
    query_params = {**params, "securityToken": token}
    query = "&".join(f"{k}={v}" for k, v in query_params.items())
    url = f"{API_BASE}?{query}"
    resp = get_with_retry(url, headers={"User-Agent": USER_AGENT}, timeout=30)

    if resp.content[:2] == b"PK":  # ZIP magic bytes
        docs = []
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".xml"):
                    docs.append(zf.read(name).decode("utf-8"))
        return docs

    return [resp.text]


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def parse_timeseries_points(xml_text: str) -> list[tuple[datetime, dict]]:
    """Generikus IEC 62325 TimeSeries/Period/Point parser.

    Minden Point-hoz visszaadja az UTC időpontját és az alatta talált
    összes mezőt (tag_name -> szám/szöveg érték) - így nem kell előre
    pontosan ismerni az A84/A85/A83 dokumentumok különböző mezőneveit
    (pl. 'imbalance_Price.amount' vs 'activation_Price.amount').

    FIGYELEM: ez a parser még nem lett valós ENTSO-E válaszon leellenőrizve
    (token hiányában) - amint van adat, validálni kell a kimenetet!
    """
    root = ElementTree.fromstring(xml_text)
    results: list[tuple[datetime, dict]] = []

    for period in root.iter():
        if _strip_ns(period.tag) != "Period":
            continue

        interval_start = None
        resolution_td = None
        points: list[tuple[int, dict]] = []

        for child in period:
            tag = _strip_ns(child.tag)
            if tag == "timeInterval":
                for sub in child:
                    if _strip_ns(sub.tag) == "start":
                        interval_start = datetime.strptime(sub.text.strip(), "%Y-%m-%dT%H:%MZ").replace(
                            tzinfo=timezone.utc
                        )
            elif tag == "resolution":
                resolution_td = RESOLUTION_TO_TIMEDELTA.get(child.text.strip())
            elif tag == "Point":
                position = None
                fields = {}
                for sub in child:
                    sub_tag = _strip_ns(sub.tag)
                    if sub_tag == "position":
                        position = int(sub.text.strip())
                    else:
                        fields[sub_tag] = sub.text.strip() if sub.text else None
                if position is not None:
                    points.append((position, fields))

        if interval_start is None or resolution_td is None:
            continue

        for position, fields in points:
            ts = interval_start + (position - 1) * resolution_td
            parsed_fields = {}
            for k, v in fields.items():
                if v is None:
                    continue
                try:
                    parsed_fields[k] = float(v)
                except ValueError:
                    # nem szám (pl. kategória-/státuszkód, mint 'imbalance_Price.category'=A04/A05) -
                    # a hívónak kellhet a szűréshez, ezért szövegként megtartjuk, nem dobjuk el
                    parsed_fields[k] = v
            if parsed_fields:
                results.append((ts, parsed_fields))

    return results


_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


def sanitize_metric_name(prefix: str, field_name: str) -> str:
    name = f"{prefix}_{field_name}".lower().replace(".", "_")
    return _SANITIZE_RE.sub("_", name).strip("_")
