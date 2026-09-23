"""MAVIR kiegyenlítő energia egységár + rendszerirány HISTORIKUS visszatöltő.

A "végleges" (elszámolási minőségű) archívumból tölt vissza több hónapot
(mavir.hu/web/riportok/kiegyenlito-energia-egysegarak). Egy adott hónap
mappájában lévő fájlpár valójában kb. 44 napot fed le (a hónap 1-jétől a
következő hónap közepéig), ezért a hónapok között jelentős az átfedés -
ez nem probléma, mert a storage.py UPSERT-je (timestamp+forrás+metrika
szerinti kulccsal) automatikusan összefésüli/felülírja az ismétlődéseket.

Ugyanazt a SOURCE_NAME-et ("MAVIR_KE") használja, mint a napi "előzetes"
gyűjtő (mavir_kiegyenlito.py) - így a végleges adat szándékosan felülírja
a korábbi előzetes értékeket ugyanarra az időpontra.

Használat:
    python collectors/mavir_kiegyenlito_backfill.py [hónapok_száma]

Alapértelmezésben az utolsó 4 hónap mappáját dolgozza fel (~ez kb. 5-6
hónapnyi tényleges adatot ad az átfedések miatt, jóval meghaladva a
tervben kért minimum 3 hónapot).
"""

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mavir_ke_common import (
    BUDAPEST_TZ,
    PROJECT_ROOT,
    VEGLEGES_ROOT,
    build_measurement_rows,
    fetch_and_parse_pair,
    find_month_folder_url,
)

sys.path.insert(0, str(PROJECT_ROOT)) if str(PROJECT_ROOT) not in sys.path else None
import storage  # noqa: E402

LOG_DIR = PROJECT_ROOT / "logs"
SOURCE_NAME = "MAVIR_KE"
DELAY_BETWEEN_MONTHS_SEC = 5

log = logging.getLogger("mavir_kiegyenlito_backfill")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_DIR / "mavir_kiegyenlito_backfill.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)


def month_keys_back(n: int) -> list[str]:
    now = datetime.now(BUDAPEST_TZ)
    year, month = now.year, now.month
    keys = []
    for _ in range(n):
        keys.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return keys


def backfill(months_back: int = 4) -> int:
    conn = storage.connect()
    total_rows = 0
    try:
        for i, month_key in enumerate(month_keys_back(months_back)):
            if i > 0:
                time.sleep(DELAY_BETWEEN_MONTHS_SEC)
            try:
                folder_url = find_month_folder_url(VEGLEGES_ROOT, month_key, log)
            except RuntimeError as e:
                log.warning("Hónap kihagyva (%s) - mappa nem található: %s", month_key, e)
                continue

            try:
                poz_data, neg_data = fetch_and_parse_pair(folder_url, "KE_egysegar_", log)
            except RuntimeError as e:
                log.warning("Hónap kihagyva (%s) - fájl nem található/feldolgozhatatlan: %s", month_key, e)
                continue

            collected_at = datetime.now(timezone.utc)
            rows = build_measurement_rows(poz_data, neg_data, SOURCE_NAME, collected_at)
            n = storage.upsert_measurements(conn, rows)
            total_rows += n
            log.info("%s: %d mérési sor beírva/frissítve (pozitív=%d, negatív=%d intervallum)",
                      month_key, n, len(poz_data), len(neg_data))
    finally:
        conn.close()

    log.info("Visszatöltés kész: összesen %d mérési sor -> %s", total_rows, storage.DB_PATH)
    return total_rows


def main() -> int:
    setup_logging()
    months_back = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    try:
        backfill(months_back)
    except Exception:
        log.exception("A visszatöltés hibával leállt")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
