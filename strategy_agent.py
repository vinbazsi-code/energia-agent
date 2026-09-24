"""Összefogó (LLM-alapú) stratégia-agent.

Ez az EGYETLEN LLM-hívást tartalmazó komponens a rendszerben - minden
más gyűjtő (collectors/*.py) egyszerű, LLM-hívás nélküli Python-szkript
(lásd CLAUDE.md.md "Alapelvek"). Ez az agent az összes már összegyűjtött
adatból (MAVIR rendszerirány/ár, HUPX DAM/IDC/IDA, időjárás) egy írott
magyar nyelvű piaci összefoglalót és konkrét töltés/kisütés-stratégia-
javaslatot készít a Balassagyarmat-i akkumulátorokhoz (2×1,2 MWh,
parkonként ~490 kW betáplálási korlát).

Használat:
    $env:ANTHROPIC_API_KEY = "..."   (soha ne kerüljön kódba/git-be)
    python strategy_agent.py

Az eredmény az adatbázis 'insights' táblájába kerül (lásd storage.py),
a dashboard a legutolsót jeleníti meg.
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import storage  # noqa: E402

MODEL = os.environ.get("STRATEGY_AGENT_MODEL", "claude-sonnet-5")
LOG_DIR = PROJECT_ROOT / "logs"

BATTERY_CONTEXT = (
    "Portfólió: Balassagyarmat, 2 db KÁT-os napelempark, parkonként 1,2 MWh-s akkumulátorral "
    "(összesen 2,4 MWh tárolókapacitás), parkonként kb. 490 kW hálózati betáplálási korlát. "
    "A felhasználó energiapiaci szakértő (aFRR, MAVIR, KÁT, HUPX ismeretekkel)."
)

log = logging.getLogger("strategy_agent")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "strategy_agent.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def _load_wide() -> pd.DataFrame:
    conn = storage.connect()
    try:
        df = pd.read_sql_query(
            "SELECT timestamp_utc, metric, value FROM measurements",
            conn,
        )
    finally:
        conn.close()
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    return df.pivot_table(index="timestamp_utc", columns="metric", values="value", aggfunc="last").sort_index()


def _series_to_lines(series: pd.Series, unit: str, tz: str = "Europe/Budapest") -> str:
    s = series.dropna()
    if s.empty:
        return "(nincs adat)"
    local_index = s.index.tz_convert(tz)
    lines = [f"{ts.strftime('%m-%d %H:%M')}: {val:,.1f} {unit}" for ts, val in zip(local_index, s.values)]
    return "\n".join(lines)


def _latest_valid(wide: pd.DataFrame, metric: str, n: int) -> pd.Series:
    """Az utolsó N ÉRVÉNYES (nem-NaN) érték egy oszlopból - FONTOS: előbb dropna, utána tail,
    különben a ritkábban frissülő metrikák "eltűnnek" a gyakoribb metrikák időbélyegei mögött
    (mivel `wide` minden metrika időbélyegét egy közös indexbe egyesíti)."""
    if metric not in wide:
        return pd.Series(dtype=float)
    return wide[metric].dropna().tail(n)


def _future_valid(wide: pd.DataFrame, metric: str, now: pd.Timestamp, hours_forward: int) -> pd.Series:
    if metric not in wide:
        return pd.Series(dtype=float)
    s = wide[metric].dropna()
    return s[(s.index >= now) & (s.index <= now + timedelta(hours=hours_forward))]


def gather_context(hours_forward: int = 48) -> str:
    wide = _load_wide()
    now = pd.Timestamp.now(tz="UTC")

    parts = [BATTERY_CONTEXT, "", f"Jelen pillanat (UTC): {now.isoformat()}", ""]

    parts.append("## HUPX Day-Ahead ár (EUR/MWh) - a következő ismert órákra")
    parts.append(_series_to_lines(_future_valid(wide, "hupx_dam_price_eur_mwh", now, hours_forward), "EUR/MWh"))
    parts.append("")

    parts.append("## HUPX Intraday Continuous VWAP ár (EUR/MWh) - legutóbbi kereskedés")
    parts.append(_series_to_lines(_latest_valid(wide, "hupx_idc_vwap_price_eur_mwh", 48), "EUR/MWh"))
    parts.append("")

    parts.append(
        "## MAVIR kiegyenlítő energia egységár (HUF/kWh) - legutóbbi elszámolási adat "
        "(FIGYELEM: ez az adatforrás jellemzően 5-6 napos csúszással érkezik, tehát ez NEM a mai állapot)"
    )
    parts.append(_series_to_lines(_latest_valid(wide, "pozitiv_ar_huf_per_kwh", 48), "HUF/kWh"))
    parts.append("")

    parts.append("## MAVIR rendszerállapot (közel élő operatív becslés, MW) - legutóbbi állapot")
    parts.append(_series_to_lines(_latest_valid(wide, "rendszerallapot_realtime_mw", 24), "MW"))
    parts.append("")

    parts.append("## Időjárás Balassagyarmaton - a következő órákra (napsugárzás fontos a PV-termeléshez)")
    parts.append("Globálsugárzás (W/m2):")
    parts.append(_series_to_lines(_future_valid(wide, "weather_shortwave_radiation_w_m2", now, hours_forward), "W/m2"))
    parts.append("Hőmérséklet (°C):")
    parts.append(_series_to_lines(_future_valid(wide, "weather_temp_c", now, hours_forward), "°C"))
    parts.append("")

    return "\n".join(parts)


def build_prompt(context: str) -> str:
    return f"""Az alábbi adatok alapján készíts egy tömör, magyar nyelvű piaci összefoglalót és
konkrét töltés/kisütés-stratégiajavaslatot a Balassagyarmat-i akkumulátorokhoz, a
következő kb. 24-48 órára.

Formátum (Markdown):
## Piaci összefoglaló
(2-4 mondat: mi történik most a piacon, HUPX day-ahead árgörbe alakja, MAVIR
rendszerállapot, várható időjárás/PV-termelés hatása)

## Stratégiajavaslat
Egy táblázat oszlopokkal: Időablak | Javasolt művelet (töltés/kisütés/semleges) | Indoklás
(max 5-8 sor, a legfontosabb időablakokra koncentrálva, ne minden negyedórára)

## Kockázatok / bizonytalanságok
(1-2 mondat: mi lehet más, mint várt - pl. időjárás-előrejelzés pontatlansága,
MAVIR-adat késése)

Ne találj ki számokat, amik nincsenek megadva - csak a lenti adatokra
támaszkodj. Ha valamelyik adatforrás hiányzik, jelezd, és a rendelkezésre
állóra alapozd a javaslatot.

--- ADATOK ---

{context}
"""


def call_claude(prompt: str) -> str:
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Hiányzik az ANTHROPIC_API_KEY környezeti változó. Hozz létre egy API-kulcsot a "
            "console.anthropic.com-on, majd (a saját terminálodban, ne ide!) futtasd: "
            "setx ANTHROPIC_API_KEY \"...\""
        )
    client = anthropic.Anthropic()  # az ANTHROPIC_API_KEY env változóból olvas
    response = client.messages.create(
        model=MODEL,
        # a Sonnet 5 alapból "extended thinking"-et használ, ami levon a keretből -
        # legyen elég hely a gondolkodásnak ÉS a tényleges szöveges válasznak is
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "max_tokens":
        log.warning("A válasz a max_tokens korlátnál megszakadt - lehet, hogy csonka.")
    return "".join(block.text for block in response.content if block.type == "text")


def run() -> int:
    context = gather_context()
    prompt = build_prompt(context)
    log.info("Kontextus összeállítva (%d karakter), Claude (%s) hívása...", len(context), MODEL)

    content = call_claude(prompt)
    log.info("Válasz megérkezett (%d karakter)", len(content))

    now = datetime.now(timezone.utc)
    conn = storage.connect()
    try:
        insight_id = storage.save_insight(
            conn,
            generated_at=now.isoformat(),
            model=MODEL,
            content=content,
            period_start=now.isoformat(),
            period_end=(now + timedelta(hours=48)).isoformat(),
        )
        log.info("Elmentve az adatbázisba (insight id=%d)", insight_id)
    finally:
        conn.close()

    return 0


def main() -> int:
    setup_logging()
    try:
        return run()
    except Exception:
        log.exception("A stratégia-agent hibával leállt")
        return 1


if __name__ == "__main__":
    sys.exit(main())
