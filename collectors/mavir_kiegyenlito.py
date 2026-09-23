"""MAVIR kiegyenlitő energia egységár + rendszerirány NAPI gyűjtő.

Letölti a legfrissebb "előzetes" Poz_KE_egysegar / Neg_KE_egysegar Excel
fájlpárt a MAVIR riportok dokumentumtárából (nincs hivatalos API ehhez,
ezért a dokumentumtár HTML-oldalait kell scrapelni), majd egy közös,
negyedórás bontású CSV-t ír a data/raw/ mappába, és feltölti az
SQLite adatbázist is (lásd storage.py, collectors/mavir_ke_common.py).

A historikus visszatöltéshez lásd collectors/mavir_kiegyenlito_backfill.py
(az a "végleges" archívumból tölt vissza min. 3 hónapot).
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from mavir_ke_common import (
    BUDAPEST_TZ,
    ELOZETES_ROOT,
    PROJECT_ROOT,
    build_measurement_rows,
    fetch_and_parse_pair,
    find_month_folder_url,
    local_interval_start_to_utc,
)

sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
import storage  # noqa: E402

RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"
SOURCE_NAME = "MAVIR_KE"

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


def collect() -> Path:
    month_key = datetime.now(BUDAPEST_TZ).strftime("%Y%m")
    folder_url = find_month_folder_url(ELOZETES_ROOT, month_key, log)
    poz_data, neg_data = fetch_and_parse_pair(folder_url, "KE_egysegar_elozetes_", log)
    log.info("Feldolgozott sorok: pozitív=%d, negatív=%d", len(poz_data), len(neg_data))

    collected_at = datetime.now(timezone.utc)
    measurement_rows = build_measurement_rows(poz_data, neg_data, SOURCE_NAME, collected_at)

    all_keys = sorted(set(poz_data) | set(neg_data))
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / f"kiegyenlito_energia_{collected_at.strftime('%Y%m%dT%H%M%SZ')}.csv"
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
