"""MAVIR kiegyenlitő energia egységár + rendszerirány gyűjtő.

Letölti a legfrissebb "előzetes" Poz_KE_egysegar / Neg_KE_egysegar Excel
fájlpárt a MAVIR riportok dokumentumtárából (nincs hivatalos API ehhez,
ezért a dokumentumtár HTML-oldalait kell scrapelni - lásd
mavir.hu/web/riportok/elozetes-kiegyenlito-energia-egysegarak), majd
egy közös, negyedórás bontású CSV-t ír a data/raw/ mappába:

    timestamp_utc, timestamp_local, rendszerirany_kwh, rendszerallapot_kwh,
    pozitiv_ar_huf_per_kwh, negativ_ar_huf_per_kwh, forras

Ez a "gyűjtés" fázis (1. lépés) szkriptje - az adat egyelőre CSV-be kerül,
az SQLite/DuckDB tárolás a következő fázis feladata.
"""

import io
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import storage  # noqa: E402  (a sys.path bővítés után importálható csak)
from http_utils import get_with_retry  # noqa: E402

BASE = "https://mavir.hu"
FOLDER_LISTING_URL = f"{BASE}/web/riportok/elozetes-kiegyenlito-energia-egysegarak"
USER_AGENT = "energia-agent-collector/0.1 (kontakt: vinbazsi@gmail.com)"
BUDAPEST_TZ = ZoneInfo("Europe/Budapest")

RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"
SOURCE_NAME = "MAVIR_KE_ELOZETES"

log = logging.getLogger("mavir_kiegyenlito")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "mavir_kiegyenlito.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def http_get(url: str):
    return get_with_retry(url, headers={"User-Agent": USER_AGENT}, timeout=30)


def find_current_month_folder_url(prefix: str) -> str:
    """A gyökér dokumentumtár-listából kikeresi a mai hónap mappájának 'view' URL-jét."""
    month_key = datetime.now(BUDAPEST_TZ).strftime("%Y%m")
    resp = http_get(FOLDER_LISTING_URL)
    pattern = re.compile(
        r'href="(' + re.escape(BASE) + r'/web/riportok/[^"]*?/view/(\d+)[^"]*)"[^>]*>\s*'
        + re.escape(month_key)
    )
    match = pattern.search(resp.text)
    if not match:
        raise RuntimeError(f"Nem található a(z) {month_key} hónap mappája a {FOLDER_LISTING_URL} oldalon")
    folder_url = match.group(1).replace("&amp;", "&")
    log.info("Hónap mappa (%s) megtalálva: %s", month_key, folder_url)
    return folder_url


def find_latest_file_view_url(folder_url: str, filename_prefix: str) -> tuple[str, str]:
    """A hónap-mappa listából kikeresi a legfrissebb (első találat = legújabb) fájl view_file URL-jét."""
    resp = http_get(folder_url)
    pattern = re.compile(
        r'href="(' + re.escape(BASE) + r'/web/riportok/[^"]*?/view_file/(\d+)[^"]*)"[^>]*>\s*'
        + re.escape(filename_prefix) + r'([^<]*\.xlsx)'
    )
    match = pattern.search(resp.text)
    if not match:
        raise RuntimeError(f"Nem található '{filename_prefix}*.xlsx' fájl a {folder_url} mappában")
    view_file_url = match.group(1).replace("&amp;", "&")
    filename = filename_prefix + match.group(3)
    log.info("Legfrissebb fájl megtalálva: %s", filename)
    return view_file_url, filename


def resolve_direct_download_url(view_file_url: str) -> str:
    """A fájl-előnézet oldal statikus HTML-jéből kinyeri a közvetlen /documents/... letöltési linket."""
    resp = http_get(view_file_url)
    match = re.search(r'https://mavir\.hu/documents/[^"\']+download=true', resp.text)
    if not match:
        raise RuntimeError(f"Nem található közvetlen letöltési link itt: {view_file_url}")
    return match.group(0).replace("&amp;", "&")


def download_xlsx(url: str) -> bytes:
    resp = http_get(url)
    content_type = resp.headers.get("Content-Type", "")
    if "spreadsheet" not in content_type:
        raise RuntimeError(f"Váratlan Content-Type ({content_type}) ettől: {url}")
    return resp.content


def parse_ke_file(xlsx_bytes: bytes, price_column_hint: str) -> dict[tuple[str, str], dict]:
    """Egy Poz_/Neg_ KE egységár Excel fájlt dolgoz fel.

    Visszaad egy {(dátum, intervallum): {rendszerirany_kwh, rendszerallapot_kwh, ar_huf_per_kwh}} dict-et.
    """
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb.worksheets[0]

    header_row_idx = None
    header = {}
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if row and row[0] == "Dátum":
            header_row_idx = i
            header = {str(name).strip(): idx for idx, name in enumerate(row) if name}
            break
    if header_row_idx is None:
        raise RuntimeError("Nem található a 'Dátum' fejléc a fájlban - megváltozott a formátum?")

    price_col = next((idx for name, idx in header.items() if price_column_hint in name), None)
    irany_col = header.get("Rendszer-irány (kWh)")
    allapot_col = header.get("Rendszerállapot (kWh)")
    if price_col is None or irany_col is None or allapot_col is None:
        raise RuntimeError(
            f"Hiányzó oszlop(ok) - price_col={price_col}, irany_col={irany_col}, allapot_col={allapot_col}"
        )

    results: dict[tuple[str, str], dict] = {}
    rows = list(ws.iter_rows(values_only=True))
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for row in rows[header_row_idx + 1:]:
        if not row or not row[0] or not date_pattern.match(str(row[0])):
            continue
        datum = str(row[0])
        intervallum = str(row[1])
        results[(datum, intervallum)] = {
            "rendszerirany_kwh": row[irany_col],
            "rendszerallapot_kwh": row[allapot_col],
            "ar_huf_per_kwh": row[price_col],
        }
    return results


def local_interval_start_to_utc(datum: str, intervallum: str) -> datetime:
    """'2026-09-17' + '00:00 - 00:15' -> UTC datetime az intervallum kezdetéhez."""
    start_str = intervallum.split(" - ")[0].strip()
    local_dt = datetime.strptime(f"{datum} {start_str}", "%Y-%m-%d %H:%M").replace(tzinfo=BUDAPEST_TZ)
    return local_dt.astimezone(timezone.utc)


def collect() -> Path:
    folder_url = find_current_month_folder_url("Poz_KE_egysegar_elozetes_")
    # ugyanaz a hónap-mappa szolgálja ki a Poz_ és Neg_ fájlokat is
    poz_view_url, poz_filename = find_latest_file_view_url(folder_url, "Poz_KE_egysegar_elozetes_")
    neg_view_url, neg_filename = find_latest_file_view_url(folder_url, "Neg_KE_egysegar_elozetes_")

    poz_download_url = resolve_direct_download_url(poz_view_url)
    neg_download_url = resolve_direct_download_url(neg_view_url)

    poz_bytes = download_xlsx(poz_download_url)
    neg_bytes = download_xlsx(neg_download_url)
    log.info("Letöltve: %s (%d KB), %s (%d KB)", poz_filename, len(poz_bytes) // 1024, neg_filename, len(neg_bytes) // 1024)

    poz_data = parse_ke_file(poz_bytes, "Pozitív mérlegköri")
    neg_data = parse_ke_file(neg_bytes, "Negatív mérlegköri")
    log.info("Feldolgozott sorok: pozitív=%d, negatív=%d", len(poz_data), len(neg_data))

    all_keys = sorted(set(poz_data) | set(neg_data))
    collected_at = datetime.now(timezone.utc)

    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / f"kiegyenlito_energia_{collected_at.strftime('%Y%m%dT%H%M%SZ')}.csv"

    measurement_rows = []
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(
            "timestamp_utc,timestamp_local,rendszerirany_kwh,rendszerallapot_kwh,"
            "pozitiv_ar_huf_per_kwh,negativ_ar_huf_per_kwh,forras,letoltve_utc\n"
        )
        for datum, intervallum in all_keys:
            utc_start = local_interval_start_to_utc(datum, intervallum)
            local_start = utc_start.astimezone(BUDAPEST_TZ)
            poz = poz_data.get((datum, intervallum), {})
            neg = neg_data.get((datum, intervallum), {})
            rendszerirany = poz.get("rendszerirany_kwh", neg.get("rendszerirany_kwh", ""))
            rendszerallapot = poz.get("rendszerallapot_kwh", neg.get("rendszerallapot_kwh", ""))
            pozitiv_ar = poz.get("ar_huf_per_kwh", "")
            negativ_ar = neg.get("ar_huf_per_kwh", "")
            f.write(
                f"{utc_start.isoformat()},{local_start.isoformat()},{rendszerirany},{rendszerallapot},"
                f"{pozitiv_ar},{negativ_ar},elozetes,{collected_at.isoformat()}\n"
            )

            ts = utc_start.isoformat()
            collected_iso = collected_at.isoformat()
            for metric, value, unit in (
                ("rendszerirany_kwh", rendszerirany, "kWh"),
                ("rendszerallapot_kwh", rendszerallapot, "kWh"),
                ("pozitiv_ar_huf_per_kwh", pozitiv_ar, "HUF/kWh"),
                ("negativ_ar_huf_per_kwh", negativ_ar, "HUF/kWh"),
            ):
                if value == "":
                    continue
                measurement_rows.append(
                    {
                        "timestamp_utc": ts,
                        "source": SOURCE_NAME,
                        "scope": "system",
                        "asset_id": "",
                        "metric": metric,
                        "value": value,
                        "unit": unit,
                        "collected_at": collected_iso,
                    }
                )

    log.info("Kész: %d sor -> %s", len(all_keys), out_path)

    conn = storage.connect()
    try:
        n = storage.upsert_measurements(conn, measurement_rows)
        log.info("Adatbázisba írva/frissítve: %d mérési sor -> %s", n, storage.DB_PATH)
    finally:
        conn.close()

    return out_path


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
