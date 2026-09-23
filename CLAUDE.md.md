# Energiakereskedési elemző rendszer – MVP

## Cél
Adatgyűjtő és elemző rendszer két KÁT-os napelemparkhoz kapcsolt akkumulátorokkal
(Balassagyarmat, 2 × 1,2 MWh, parkonként ~490 kW betáplálási korlát).
Hosszabb távon: menetrend-, ENTSO-E (PICASSO/MARI), MAVIR- és akkumulátor-adatok
gyűjtése, erre épülő előrejelzés és LLM-alapú stratégiajavaslat.

## Alapelvek
- Minden adat eszközazonosítóval (asset_id) és portfóliószinten is tárolva,
  hogy új eszköz felvétele csak konfiguráció legyen.
- Az adatgyűjtők egyszerű Python-szkriptek, nem LLM-hívások.
- Minden időbélyeg UTC-ben, negyedórás felbontásban, ahol lehet.
- Az akkumulátor-rendszerhez kizárólag olvasási hozzáférés.
- Hibák naplózása; egy gyűjtő leállása ne állítsa le a többit.

## Jelenlegi fázis: 2 hetes próba
1. MAVIR rendszerirány és kiegyenlítő energia árak gyűjtése
2. Tárolás SQLite/DuckDB-ben
3. Időzített futtatás
4. Streamlit dashboard
5. Historikus visszatöltés (min. 3 hónap)

## A felhasználóról
Energiapiaci szakértő (aFRR, MAVIR, KÁT, HUPX), nem fejlesztő.
Magyarázd el röviden, mit csinál a kód, és kérdezz, mielőtt nagy döntést hozol
(új könyvtár, adatbázis-szerkezet).
