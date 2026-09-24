"""Energiakereskedési elemző - egyszerű Streamlit dashboard.

Az összegyűjtött MAVIR adatokat jeleníti meg a storage.py-beli SQLite
adatbázisból (data/energia.db). Indítás:

    .venv\\Scripts\\streamlit.exe run dashboard.py
"""

from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

from collectors.hupx_dam import get_live_prices as get_live_hupx_dam_prices
from collectors.hupx_ida import get_live_prices as get_live_hupx_ida_prices
from collectors.hupx_idc import get_live_prices as get_live_hupx_idc_prices
from collectors.mavir_frekvencia import get_live_frequency
from collectors.mavir_rendszerallapot_realtime import get_live_rendszerallapot
from storage import DB_PATH, connect, get_latest_insight

st.set_page_config(page_title="Energiakereskedési elemző", layout="wide")

st.title("Energiakereskedési elemző")
st.caption(f"Adatforrás: {DB_PATH}")

st.subheader("📋 Legutóbbi stratégiajavaslat (LLM-összefoglaló)")
try:
    conn = connect()
    try:
        insight = get_latest_insight(conn)
    finally:
        conn.close()
    if insight:
        st.caption(f"Készült: {insight['generated_at']} · modell: {insight['model']}")
        with st.container(border=True):
            st.markdown(insight["content"])
    else:
        st.info("Még nincs elmentett stratégiajavaslat. Futtasd le: `python strategy_agent.py`")
except Exception as e:
    st.error(f"Nem sikerült betölteni a stratégiajavaslatot: {e}")

st.divider()


def rendszer_badge(value: float | None, convention: str) -> str:
    """convention: 'rendszerirany' (pozitív=többletes rendszer) vagy 'rendszerallapot'
    (pozitív=hiányos rendszer - ellentétes előjelű a rendszeriránnyal, lásd MAVIR módszertan)."""
    if value is None or pd.isna(value):
        return ":gray[Nincs adat]"
    if abs(value) < 1e-6:
        return ":blue[⚖️ Kiegyensúlyozott rendszer]"
    is_surplus = (value > 0) if convention == "rendszerirany" else (value < 0)
    if is_surplus:
        return ":green[🟢 **RENDSZERTÖBBLET** → leszabályozás (LE)]"
    return ":red[🔴 **RENDSZERHIÁNY** → felszabályozás (FEL)]"


@st.cache_data(ttl=30)
def load_live_frequency() -> pd.DataFrame:
    rows = get_live_frequency(hours_back=1)
    freq_df = pd.DataFrame(rows, columns=["timestamp_utc", "frekvencia_hz"])
    freq_df["timestamp_utc"] = pd.to_datetime(freq_df["timestamp_utc"], utc=True)
    return freq_df.set_index("timestamp_utc")


@st.cache_data(ttl=60)
def load_live_rendszerallapot() -> pd.DataFrame:
    rows = get_live_rendszerallapot(hours_back=6)
    ra_df = pd.DataFrame(rows, columns=["timestamp_utc", "rendszerallapot_mw"])
    ra_df["timestamp_utc"] = pd.to_datetime(ra_df["timestamp_utc"], utc=True)
    return ra_df.set_index("timestamp_utc")


@st.cache_data(ttl=300)
def load_live_hupx() -> pd.DataFrame:
    """A három fő HUPX piaci ár (DAM, IDC VWAP, IDA1) egy hosszú formátumú táblában."""
    frames = []
    for label, rows in (
        ("Day-Ahead (DAM)", get_live_hupx_dam_prices(days_back=1, days_forward=1)),
        ("Intraday Continuous (IDC VWAP)", get_live_hupx_idc_prices(days_back=1, days_forward=1)),
        ("Intraday Auction (IDA1)", get_live_hupx_ida_prices(round_no=1, days_back=1, days_forward=1)),
    ):
        if rows:
            df = pd.DataFrame(rows, columns=["timestamp_utc", "ár"])
            df["piac"] = label
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["timestamp_utc", "ár", "piac"])
    combined = pd.concat(frames, ignore_index=True)
    combined["timestamp_utc"] = pd.to_datetime(combined["timestamp_utc"], utc=True)
    return combined.sort_values("timestamp_utc")


st.subheader("HUPX piaci árak (EUR/MWh)")
st.caption(
    "Közvetlenül a HUPX Labs API-ból, három piaci szegmens összehasonlítva: **Day-Ahead (DAM)** - az "
    "egyetlen előretekintő adatforrás a rendszerben (a napi aukció kb. dél körül, CET, lezár, utána a "
    "HOLNAPI árak is ismertek); **Intraday Continuous (IDC)** - a szállításhoz közeli folyamatos "
    "kereskedés forgalommal súlyozott átlagára; **Intraday Auction (IDA1)** - az első napon belüli "
    "aukciós kör ára. Az eltérésük mutatja, mennyire tért el a piac a day-ahead várakozástól."
)
try:
    live_hupx = load_live_hupx()
    if not live_hupx.empty:
        now_utc = pd.Timestamp.now(tz="UTC")
        dam_future = live_hupx[(live_hupx["piac"] == "Day-Ahead (DAM)") & (live_hupx["timestamp_utc"] >= now_utc)]
        if not dam_future.empty:
            st.metric(
                "Következő negyedóra DAM ára (EUR/MWh)",
                f"{dam_future['ár'].iloc[0]:,.2f}",
                help=str(dam_future["timestamp_utc"].iloc[0]),
            )
        hupx_chart = (
            alt.Chart(live_hupx)
            .mark_line(point=True)
            .encode(
                x=alt.X("timestamp_utc:T", title="Időpont"),
                y=alt.Y("ár:Q", title="EUR/MWh"),
                color=alt.Color("piac:N", legend=alt.Legend(title=None)),
            )
            .properties(height=300)
        )
        st.altair_chart(hupx_chart, use_container_width=True)

        local_now = pd.Timestamp.now(tz="Europe/Budapest")
        dam_rows = live_hupx[live_hupx["piac"] == "Day-Ahead (DAM)"]
        has_tomorrow = (dam_rows["timestamp_utc"].dt.tz_convert("Europe/Budapest").dt.date > local_now.date()).any()
        if not has_tomorrow:
            st.info("A holnapi DAM aukció még nem zárult le (kb. dél körül, CET) - egyelőre csak a mai/korábbi árak láthatók.")
    else:
        st.info("Nincs élő HUPX adat.")
except Exception as e:
    st.error(f"Nem sikerült lekérni az élő HUPX árat: {e}")

st.divider()

st.subheader("Jelenlegi rendszerállapot")
st.caption(
    "Élőben lekérve a MAVIR-tól (rtdwweb, chart 1000727), nem az adatbázisból - ez a legfrissebb "
    "elérhető jelzés arra, hogy a rendszer épp hiányos (felszabályozás) vagy többletes (leszabályozás)."
)
try:
    live_ra = load_live_rendszerallapot()
    if not live_ra.empty:
        latest_value = live_ra["rendszerallapot_mw"].iloc[-1]
        latest_ts = live_ra.index[-1]
        st.markdown(f"**{latest_ts}** (MW: {latest_value:,.1f}): {rendszer_badge(latest_value, 'rendszerallapot')}")
    else:
        st.info("Nincs élő rendszerállapot adat.")
except Exception as e:
    st.error(f"Nem sikerült lekérni az élő rendszerállapotot: {e}")

st.divider()

st.subheader("Élő hálózati frekvencia (Hz)")
st.caption("Közvetlenül a MAVIR-tól lekérve, adatbázis nélkül - kb. 30 másodpercenként frissül újratöltéskor.")
try:
    live_freq = load_live_frequency()
    if not live_freq.empty:
        latest_value = live_freq["frekvencia_hz"].iloc[-1]
        latest_ts = live_freq.index[-1]
        st.metric("Jelenlegi frekvencia (Hz)", f"{latest_value:,.3f}", help=str(latest_ts))
        y_min = min(49.8, live_freq["frekvencia_hz"].min() - 0.02)
        y_max = max(50.2, live_freq["frekvencia_hz"].max() + 0.02)
        freq_chart = (
            alt.Chart(live_freq.reset_index())
            .mark_line()
            .encode(
                x=alt.X("timestamp_utc:T", title="Időpont"),
                y=alt.Y("frekvencia_hz:Q", title="Hz", scale=alt.Scale(domain=[y_min, y_max])),
            )
            .properties(height=250)
        )
        st.altair_chart(freq_chart, use_container_width=True)
    else:
        st.info("Nincs élő frekvencia adat.")
except Exception as e:
    st.error(f"Nem sikerült lekérni az élő frekvenciát: {e}")

st.divider()


@st.cache_data(ttl=60)
def load_data() -> pd.DataFrame:
    conn = connect()
    try:
        df = pd.read_sql_query(
            "SELECT timestamp_utc, source, scope, asset_id, metric, value, unit FROM measurements",
            conn,
        )
    finally:
        conn.close()
    if df.empty:
        return df
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    return df


df = load_data()

if df.empty:
    st.warning("Még nincs adat az adatbázisban. Futtasd le a gyűjtőt: collectors/mavir_kiegyenlito.py")
    st.stop()

# Hosszú -> széles formátum, hogy egy sorban legyen minden metrika egy adott időpontra
wide = df.pivot_table(index="timestamp_utc", columns="metric", values="value", aggfunc="last").sort_index()

min_ts = wide.index.min().to_pydatetime()
max_ts = wide.index.max().to_pydatetime()

st.sidebar.header("Szűrés")
# az alapértelmezett tartomány a "mára" végződjön, ne az időjárás-előrejelzés jövőjéig -
# aki meg akarja nézni az előrejelzést, kézzel kitolhatja a végdátumot max_ts-ig
now_ts = min(pd.Timestamp.now(tz="UTC").to_pydatetime(), max_ts)
default_start = max(min_ts, now_ts - timedelta(days=8))  # a kiegyenlítő ár ~5-6 napos csúszása is beleférjen
date_range = st.sidebar.date_input(
    "Időszak (helyi dátum)",
    value=(default_start.date(), now_ts.date()),
    min_value=min_ts.date(),
    max_value=max_ts.date(),
)
st.sidebar.caption("A végdátumot kitolhatod a jövőbe is, hogy lásd a HUPX/időjárás-előrejelzést.")

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = date_range

start_ts = pd.Timestamp(start_date, tz="UTC")
end_ts = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
filtered = wide[(wide.index >= start_ts) & (wide.index < end_ts)]

def last_valid(col: str) -> tuple[str, str, float | None]:
    if col not in wide:
        return "-", "-", None
    series = wide[col].dropna()
    if series.empty:
        return "-", "-", None
    return f"{series.iloc[-1]:,.2f}", str(series.index[-1]), series.iloc[-1]


col1, col2, col3, col4 = st.columns(4)
v, t, _ = last_valid("rendszerirany_kwh")
col1.metric("Utolsó rendszerirány (kWh)", v, help=f"Elszámolási adat, {t}")
v, t, _ = last_valid("pozitiv_ar_huf_per_kwh")
col2.metric("Utolsó pozitív egységár (HUF/kWh)", v, help=f"Elszámolási adat, {t}")
v, t, _ = last_valid("negativ_ar_huf_per_kwh")
col3.metric("Utolsó negatív egységár (HUF/kWh)", v, help=f"Elszámolási adat, {t}")
v, t, _ = last_valid("rendszerallapot_realtime_mw")
col4.metric("Rendszerállapot - operatív becslés (MW)", v, help=f"Közel valós idejű, {t}")

st.subheader("Rendszerirány (kWh)")
st.caption("Ez múltbeli, elszámolási minőségű adat (~5-6 napos csúszással) - a JELENLEGI állapotot fent találod.")
_, t, raw = last_valid("rendszerirany_kwh")
st.markdown(f"Állapot ekkor ({t}): {rendszer_badge(raw, 'rendszerirany')}")
if "rendszerirany_kwh" in filtered:
    st.line_chart(filtered["rendszerirany_kwh"])
else:
    st.info("Nincs rendszerirány adat ebben az időszakban.")

st.subheader("Napi összesítő: felszabályozás vs leszabályozás")
st.caption(
    "A rendszerirány (kWh) előjele alapján naponta összesítve, helyi idő (Europe/Budapest) szerinti "
    "naptári napokra: pozitív = leszabályozási igény (rendszertöbblet), negatív = felszabályozási "
    "igény (rendszerhiány). Elszámolási minőségű, ~5-6 napos csúszású adat."
)
if "rendszerirany_kwh" in filtered:
    ri = filtered["rendszerirany_kwh"].dropna()
    if not ri.empty:
        local_dates = ri.index.tz_convert("Europe/Budapest").date
        daily_src = pd.DataFrame({"value": ri.values, "date": local_dates})
        daily = daily_src.groupby("date")["value"].agg(
            leszabalyozas_kwh=lambda s: s[s > 0].sum(),
            felszabalyozas_kwh=lambda s: -s[s < 0].sum(),
        )
        daily["netto_kwh"] = daily["leszabalyozas_kwh"] - daily["felszabalyozas_kwh"]
        daily = daily.reset_index()

        plot_df = pd.concat(
            [
                pd.DataFrame(
                    {"date": daily["date"], "típus": "Leszabályozás (többlet)", "kWh": daily["leszabalyozas_kwh"]}
                ),
                pd.DataFrame(
                    {"date": daily["date"], "típus": "Felszabályozás (hiány)", "kWh": -daily["felszabalyozas_kwh"]}
                ),
            ]
        )
        daily_chart = (
            alt.Chart(plot_df)
            .mark_bar()
            .encode(
                x=alt.X("date:O", title="Nap"),
                y=alt.Y("kWh:Q", title="kWh"),
                color=alt.Color(
                    "típus:N",
                    scale=alt.Scale(
                        domain=["Leszabályozás (többlet)", "Felszabályozás (hiány)"],
                        range=["#2ecc71", "#e74c3c"],
                    ),
                    legend=alt.Legend(title=None),
                ),
            )
            .properties(height=300)
        )
        st.altair_chart(daily_chart, use_container_width=True)
        st.dataframe(
            daily.rename(
                columns={
                    "date": "Nap",
                    "leszabalyozas_kwh": "Leszabályozás (kWh)",
                    "felszabalyozas_kwh": "Felszabályozás (kWh)",
                    "netto_kwh": "Nettó (kWh, +=többlet)",
                }
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Nincs rendszerirány adat a napi összesítéshez.")
else:
    st.info("Nincs rendszerirány adat a napi összesítéshez.")

st.subheader("Rendszerállapot - történeti trend (MW)")
st.caption(
    "Ugyanannak a legfelül látható élő jelzésnek a története - a napi gyűjtő menti el az adatbázisba, "
    "ezért ez a görbe csak a napi futásokig frissül, nem élőben. A JELENLEGI állapotot fent találod."
)
if "rendszerallapot_realtime_mw" in filtered:
    st.line_chart(filtered["rendszerallapot_realtime_mw"])
else:
    st.info("Nincs közel valós idejű rendszerállapot adat ebben az időszakban.")

st.subheader("Kiegyenlítő energia egységár (HUF/kWh)")
price_cols = [c for c in ("pozitiv_ar_huf_per_kwh", "negativ_ar_huf_per_kwh") if c in filtered]
if price_cols:
    st.line_chart(filtered[price_cols])
else:
    st.info("Nincs árszint adat ebben az időszakban.")

st.subheader("MAVIR vs. ENTSO-E imbalance ár (HUF/kWh) - keresztellenőrzés")
st.caption(
    "A MAVIR pozitív egységár és az ENTSO-E Transparency Platform-ról lekért imbalance ár "
    "összevetése - eltérés esetén valamelyik adatforrás/módszertan hibás vagy máshogy definiált."
)
entsoe_cols = [c for c in ("pozitiv_ar_huf_per_kwh", "imbalance_imbalance_price_amount") if c in filtered]
if len(entsoe_cols) >= 1 and "imbalance_imbalance_price_amount" in filtered:
    st.line_chart(filtered[entsoe_cols])
else:
    st.info("Nincs ENTSO-E imbalance ár adat ebben az időszakban.")

st.subheader("Időjárás - Balassagyarmat (napsugárzás, hőmérséklet)")
st.caption(
    "Open-Meteo (ingyenes, API-kulcs nélküli) előrejelzés/mért adat a napelempark-portfólió "
    "helyszínére. A szűrt időszak végét kitolva a jövőbe kb. 7 napos előrejelzés is látszik - "
    "ez segíthet a várható PV-termelés és így a töltési stratégia tervezésében."
)
weather_cols = [c for c in ("weather_shortwave_radiation_w_m2", "weather_direct_radiation_w_m2") if c in filtered]
if weather_cols:
    radiation_chart = (
        alt.Chart(filtered[weather_cols].reset_index().melt(id_vars="timestamp_utc", var_name="típus", value_name="W/m2"))
        .mark_line()
        .encode(
            x=alt.X("timestamp_utc:T", title="Időpont"),
            y=alt.Y("W/m2:Q", title="Napsugárzás (W/m2)"),
            color=alt.Color(
                "típus:N",
                legend=alt.Legend(title=None),
                scale=alt.Scale(
                    domain=["weather_shortwave_radiation_w_m2", "weather_direct_radiation_w_m2"],
                    range=["#f39c12", "#e67e22"],
                ),
            ),
        )
        .properties(height=250)
    )
    st.altair_chart(radiation_chart, use_container_width=True)
if "weather_temp_c" in filtered:
    st.line_chart(filtered["weather_temp_c"])
if not weather_cols and "weather_temp_c" not in filtered:
    st.info("Nincs időjárás-adat ebben az időszakban.")

st.subheader("Órás mintázat (átlag a kiválasztott időszakra)")
st.caption(
    "A rendszerirány és az egységár átlaga óránkénti bontásban (helyi idő), a szűrt időszak minden "
    "napjára összesítve - segít azonosítani, mikor jellemző a hiány/többlet és a magasabb/alacsonyabb "
    "ár a nap folyamán."
)
if "rendszerirany_kwh" in filtered:
    ri = filtered["rendszerirany_kwh"].dropna()
    if not ri.empty:
        hours = ri.index.tz_convert("Europe/Budapest").hour
        hourly_ri = (
            pd.DataFrame({"óra": hours, "kWh": ri.values})
            .groupby("óra")["kWh"]
            .mean()
            .reset_index()
        )
        hourly_ri_chart = (
            alt.Chart(hourly_ri)
            .mark_bar()
            .encode(
                x=alt.X("óra:O", title="Óra (helyi idő)"),
                y=alt.Y("kWh:Q", title="Átlagos rendszerirány (kWh)"),
                color=alt.condition(alt.datum.kWh > 0, alt.value("#2ecc71"), alt.value("#e74c3c")),
            )
            .properties(height=250, title="Rendszerirány óránkénti átlaga")
        )
        st.altair_chart(hourly_ri_chart, use_container_width=True)

    price_hourly_frames = []
    for col, label in (("pozitiv_ar_huf_per_kwh", "Pozitív"), ("negativ_ar_huf_per_kwh", "Negatív")):
        if col in filtered:
            series = filtered[col].dropna()
            if not series.empty:
                hours = series.index.tz_convert("Europe/Budapest").hour
                grouped = pd.DataFrame({"óra": hours, "HUF/kWh": series.values}).groupby("óra")["HUF/kWh"].mean()
                price_hourly_frames.append(pd.DataFrame({"óra": grouped.index, "HUF/kWh": grouped.values, "típus": label}))
    if price_hourly_frames:
        price_hourly = pd.concat(price_hourly_frames)
        price_hourly_chart = (
            alt.Chart(price_hourly)
            .mark_line(point=True)
            .encode(
                x=alt.X("óra:O", title="Óra (helyi idő)"),
                y=alt.Y("HUF/kWh:Q", title="Átlagos egységár (HUF/kWh)"),
                color=alt.Color("típus:N", legend=alt.Legend(title=None)),
            )
            .properties(height=250, title="Egységár óránkénti átlaga")
        )
        st.altair_chart(price_hourly_chart, use_container_width=True)
else:
    st.info("Nincs adat az órás mintázathoz.")

st.subheader("Árszint eloszlás")
st.caption(
    "Hisztogram: milyen gyakran fordul elő az adott árszint a kiválasztott időszakban - "
    "segíthet az arbitrázs küszöbök meghatározásában."
)
if price_cols:
    price_long = (
        filtered[price_cols]
        .reset_index()
        .melt(id_vars="timestamp_utc", value_vars=price_cols, var_name="típus", value_name="ár")
        .dropna()
    )
    price_long["típus"] = price_long["típus"].map(
        {"pozitiv_ar_huf_per_kwh": "Pozitív", "negativ_ar_huf_per_kwh": "Negatív"}
    )
    hist_chart = (
        alt.Chart(price_long)
        .mark_bar(opacity=0.6)
        .encode(
            x=alt.X("ár:Q", bin=alt.Bin(maxbins=40), title="HUF/kWh"),
            y=alt.Y("count():Q", title="Előfordulások száma (15 perces intervallum)", stack=None),
            color=alt.Color("típus:N", legend=alt.Legend(title=None)),
        )
        .properties(height=280)
    )
    st.altair_chart(hist_chart, use_container_width=True)
    st.dataframe(
        filtered[price_cols].describe().T.round(2).rename(
            columns={"count": "db", "mean": "átlag", "std": "szórás", "min": "min", "max": "max"}
        ),
        use_container_width=True,
    )
else:
    st.info("Nincs árszint adat az eloszláshoz.")

st.subheader("Nyers adattábla")
st.dataframe(filtered, use_container_width=True)

st.sidebar.divider()
st.sidebar.caption(f"Oldal frissítve: {datetime.now(timezone.utc).isoformat()} UTC")
if st.sidebar.button("Adatok frissítése"):
    load_data.clear()
    st.rerun()
