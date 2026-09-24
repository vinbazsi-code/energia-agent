"""Energiakereskedési elemző - egyszerű Streamlit dashboard.

Az összegyűjtött MAVIR adatokat jeleníti meg a storage.py-beli SQLite
adatbázisból (data/energia.db). Indítás:

    .venv\\Scripts\\streamlit.exe run dashboard.py
"""

from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

from collectors.entsoe_balancing import get_live_imbalance_price
from collectors.hupx_dam import get_live_prices as get_live_hupx_dam_prices
from collectors.hupx_ida import get_live_prices as get_live_hupx_ida_prices
from collectors.hupx_idc import get_live_prices as get_live_hupx_idc_prices
from collectors.mavir_aktivalas import get_live_activations
from collectors.mavir_frekvencia import get_live_frequency
from collectors.mavir_pv_termeles import get_live_pv
from collectors.mavir_rendszerallapot_realtime import get_live_rendszerallapot
from collectors.mavir_szabalyozasi_tartalekok import get_live_tartalekok
from collectors.mavir_tarolok import get_live_tarolok
from storage import DB_PATH, connect, get_latest_insight

st.set_page_config(page_title="Energiakereskedési elemző", layout="wide")

st.title("Energiakereskedési elemző")
st.caption(f"Adatforrás: {DB_PATH}")


@st.cache_data(ttl=60)
def load_latest_metric(metric: str) -> tuple[float, str] | None:
    """Egy adott metrika legfrissebb (bármikori) ismert értéke + időbélyege - a stálé MAVIR-adathoz kell,
    ahol nem élő lekérdezés van, hanem a tárolt legutolsó ismert érték."""
    conn = connect()
    try:
        cur = conn.execute(
            "SELECT value, timestamp_utc FROM measurements WHERE metric = ? "
            "ORDER BY timestamp_utc DESC LIMIT 1",
            (metric,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    return (row[0], row[1]) if row else None


@st.cache_data(ttl=30)
def load_live_imbalance() -> list[tuple]:
    return get_live_imbalance_price(days_back=1)


st.header("💰 Aktuális árak")
st.caption(
    "Egy pillantásra: melyik piaci ár mennyire friss. Az ENTSO-E és a HUPX élőben lekérdezve "
    "(gyakorlatilag valós idejű), a MAVIR kiegyenlítő ár mindig ~5-6 napos csúszású - ez utóbbi "
    "az utolsó ISMERT elszámolási érték, nem a mai állapot."
)
row1a, row1b = st.columns(2)
row2a, row2b = st.columns(2)

with row1a:
    try:
        imb = load_live_imbalance()
        if imb:
            ts, val = imb[-1]
            row1a.metric("ENTSO-E imbalance ár (élő)", f"{val:,.1f} Ft/kWh", help=f"{ts.isoformat()} (UTC)")
        else:
            row1a.metric("ENTSO-E imbalance ár (élő)", "–")
    except Exception as e:
        row1a.metric("ENTSO-E imbalance ár (élő)", "hiba")
        row1a.caption(str(e))

with row1b:
    try:
        dam = get_live_hupx_dam_prices(days_back=0, days_forward=1)
        now_utc = datetime.now(timezone.utc)
        future_dam = [(t, v) for t, v in dam if t >= now_utc]
        if future_dam:
            ts, val = future_dam[0]
            row1b.metric("HUPX Day-Ahead, köv. negyedóra (élő)", f"{val:,.1f} €/MWh", help=f"{ts.isoformat()} (UTC)")
        else:
            row1b.metric("HUPX Day-Ahead, köv. negyedóra (élő)", "–")
    except Exception as e:
        row1b.metric("HUPX Day-Ahead, köv. negyedóra (élő)", "hiba")
        row1b.caption(str(e))

with row2a:
    try:
        idc = get_live_hupx_idc_prices(days_back=1, days_forward=0)
        if idc:
            ts, val = idc[-1]
            row2a.metric("HUPX Intraday, legutóbbi kereskedés (élő)", f"{val:,.1f} €/MWh", help=f"{ts.isoformat()} (UTC)")
        else:
            row2a.metric("HUPX Intraday, legutóbbi kereskedés (élő)", "–")
    except Exception as e:
        row2a.metric("HUPX Intraday, legutóbbi kereskedés (élő)", "hiba")
        row2a.caption(str(e))

with row2b:
    latest = load_latest_metric("pozitiv_ar_huf_per_kwh")
    if latest:
        val, ts = latest
        row2b.metric("MAVIR kiegyenlítő ár (utolsó ISMERT)", f"{val:,.1f} Ft/kWh", help=f"{ts} - NEM a mai állapot, ~5-6 napos csúszás!")
    else:
        row2b.metric("MAVIR kiegyenlítő ár (utolsó ISMERT)", "–")

st.divider()

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


def render_multiseries_chart(
    df: pd.DataFrame,
    label_map: dict,
    *,
    key: str,
    value_name: str = "MW",
    chart_type: str = "line",
    height: int = 300,
    title: str | None = None,
    default_labels: list[str] | None = None,
) -> None:
    """Több-sorozatos vonal/oszlopdiagram, 'pipálós' (multiselect) sorozatválasztóval -
    sok sorozatnál (5+) e nélkül átláthatatlanná válik a chart, ezért a felhasználó
    maga választhatja ki, mit lásson egyszerre."""
    cols = [c for c in label_map if c in df]
    if not cols:
        st.info("Nincs megjeleníthető sorozat.")
        return
    options = [label_map[c] for c in cols]
    default = default_labels if default_labels is not None else options
    selected = st.multiselect("Megjelenítendő sorozatok", options, default=default, key=key)
    if not selected:
        st.info("Válassz legalább egy sorozatot a megjelenítéshez.")
        return
    long_df = df[cols].reset_index().melt(id_vars="timestamp_utc", var_name="col", value_name=value_name)
    long_df["típus"] = long_df["col"].map(label_map)
    long_df = long_df[long_df["típus"].isin(selected)]
    base = alt.Chart(long_df).mark_line() if chart_type == "line" else alt.Chart(long_df).mark_bar()
    props = {"height": height}
    if title:
        props["title"] = title
    chart = base.encode(
        x=alt.X("timestamp_utc:T", title="Időpont"),
        y=alt.Y(f"{value_name}:Q", title=value_name, stack="zero" if chart_type == "bar" else None),
        color=alt.Color("típus:N", legend=alt.Legend(title=None)),
    ).properties(**props)
    st.altair_chart(chart, use_container_width=True)


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


@st.cache_data(ttl=60)
def load_live_activations() -> pd.DataFrame:
    rows = get_live_activations(hours_back=6)
    act_df = pd.DataFrame([{"timestamp_utc": ts, **values} for ts, values in rows])
    if act_df.empty:
        return act_df
    act_df["timestamp_utc"] = pd.to_datetime(act_df["timestamp_utc"], utc=True)
    return act_df.set_index("timestamp_utc").sort_index()


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
        markets = sorted(live_hupx["piac"].unique())
        selected_markets = st.multiselect("Megjelenítendő piaci szegmensek", markets, default=markets, key="hupx_markets")
        if selected_markets:
            hupx_chart = (
                alt.Chart(live_hupx[live_hupx["piac"].isin(selected_markets)])
                .mark_line(point=True)
                .encode(
                    x=alt.X("timestamp_utc:T", title="Időpont"),
                    y=alt.Y("ár:Q", title="EUR/MWh"),
                    color=alt.Color("piac:N", legend=alt.Legend(title=None)),
                )
                .properties(height=300)
            )
            st.altair_chart(hupx_chart, use_container_width=True)
        else:
            st.info("Válassz legalább egy piaci szegmenst.")

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

st.subheader("Élő aktiválás - kiegyenlítő szabályozás forrásonként (MW)")
st.caption(
    "Élőben lekérve a MAVIR-tól (rtdwweb, chart 11326), nem az adatbázisból - forrásonkénti bontásban "
    "mutatja, mi hajtja az aktuális fel-/leszabályozást: automatikus aFRR, hazai aFRR, IGCC "
    "(nemzetközi csereszabályozás), és a nem automatikus (balancing) aktiválás."
)
ACT_LABELS = {
    "afrr_automatikus_fel_mw": "aFRR automatikus FEL",
    "afrr_automatikus_le_mw": "aFRR automatikus LE",
    "hazai_afrr_automatikus_fel_mw": "Hazai aFRR FEL",
    "hazai_afrr_automatikus_le_mw": "Hazai aFRR LE",
    "igcc_fel_mw": "IGCC FEL",
    "igcc_le_mw": "IGCC LE",
    "nem_automatikus_fel_mw": "Nem automatikus FEL",
    "nem_automatikus_le_mw": "Nem automatikus LE",
}
try:
    live_act = load_live_activations()
    if not live_act.empty:
        render_multiseries_chart(
            live_act, ACT_LABELS, key="act_series", value_name="MW", chart_type="bar", height=300
        )
        st.caption(f"Legfrissebb adatpont: {live_act.index[-1]}")
    else:
        st.info("Nincs élő aktiválási adat.")
except Exception as e:
    st.error(f"Nem sikerült lekérni az élő aktiválási adatot: {e}")

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


@st.cache_data(ttl=300)
def load_live_pv() -> pd.DataFrame:
    rows = get_live_pv(hours_back=48)
    pv_df = pd.DataFrame([{"timestamp_utc": ts, **values} for ts, values in rows])
    if pv_df.empty:
        return pv_df
    pv_df["timestamp_utc"] = pd.to_datetime(pv_df["timestamp_utc"], utc=True)
    return pv_df.set_index("timestamp_utc").sort_index()


st.subheader("☀️ Ipari PV-termelés (élő, országos)")
st.caption(
    "Élőben lekérve a MAVIR-tól (rtdwweb, chart 11838) - az ÖSSZES hazai ipari naperőmű becsült "
    "(dayahead/intraday/aktuális) és tényleges termelése, nem csak a Balassagyarmat-i parkoké. "
    "Csak irányadó jelzés arra, hogy a piaci PV-termelés \"jól fest-e\" - nem helyettesíti a helyi "
    "időjárás-alapú becslést."
)
try:
    live_pv = load_live_pv()
    if not live_pv.empty:
        pv_labels = {
            "pv_becsult_dayahead_mw": "Becsült (dayahead)",
            "pv_becsult_intraday_mw": "Becsült (intraday)",
            "pv_becsult_aktualis_mw": "Becsült (aktuális)",
            "pv_teny_netto_kereskedelmi_mw": "Tény (nettó kereskedelmi)",
            "pv_teny_netto_uzemiranyitasi_mw": "Tény (nettó üzemirányítási)",
        }
        render_multiseries_chart(live_pv, pv_labels, key="pv_series", value_name="MW", chart_type="line", height=300)
    else:
        st.info("Nincs élő PV-termelési adat.")
except Exception as e:
    st.error(f"Nem sikerült lekérni az élő PV-termelést: {e}")

st.divider()


@st.cache_data(ttl=300)
def load_live_tarolok() -> pd.DataFrame:
    rows = get_live_tarolok(hours_back=48)
    tar_df = pd.DataFrame([{"timestamp_utc": ts, **values} for ts, values in rows])
    if tar_df.empty:
        return tar_df
    tar_df["timestamp_utc"] = pd.to_datetime(tar_df["timestamp_utc"], utc=True)
    return tar_df.set_index("timestamp_utc").sort_index()


st.subheader("🔋 MAVIR-nak megfigyelhető tárolók energiaforgalmazása (élő, országos)")
st.caption(
    "Élőben lekérve a MAVIR-tól (rtdwweb, chart 22361) - az ÖSSZES MAVIR-nak látható energiatároló "
    "aggregált töltés/kisütés teljesítménye, NEM a saját Balassagyarmat-i akkumulátoroké. "
    "Referencia/benchmark: mutatja, mikor tölt/süt ki a piac többi tárolója - segíthet megítélni, "
    "hogy a saját KÁT-stratégia összhangban van-e a piaci mintázattal."
)
try:
    live_tarolok = load_live_tarolok()
    if not live_tarolok.empty:
        tar_labels = {
            "tarolo_teny_kitarolas_mw": "Tény kitárolás",
            "tarolo_teny_betarolas_mw": "Tény betárolás",
        }
        tar_cols = [c for c in tar_labels if c in live_tarolok]
        if tar_cols:
            tar_long = live_tarolok[tar_cols].reset_index().melt(
                id_vars="timestamp_utc", var_name="típus", value_name="MW"
            )
            tar_long["típus"] = tar_long["típus"].map(tar_labels)
            tar_chart = (
                alt.Chart(tar_long)
                .mark_area(opacity=0.6)
                .encode(
                    x=alt.X("timestamp_utc:T", title="Időpont"),
                    y=alt.Y("MW:Q", title="MW", stack=None),
                    color=alt.Color(
                        "típus:N",
                        scale=alt.Scale(
                            domain=["Tény kitárolás", "Tény betárolás"],
                            range=["#2ecc71", "#e74c3c"],
                        ),
                        legend=alt.Legend(title=None),
                    ),
                )
                .properties(height=300)
            )
            st.altair_chart(tar_chart, use_container_width=True)
        else:
            st.info("Nincs élő tény kitárolás/betárolás adat.")
    else:
        st.info("Nincs élő tárolói adat.")
except Exception as e:
    st.error(f"Nem sikerült lekérni az élő tárolói adatot: {e}")

st.divider()


@st.cache_data(ttl=300)
def load_live_tartalekok() -> pd.DataFrame:
    rows = get_live_tartalekok(hours_back=48)
    tt_df = pd.DataFrame([{"timestamp_utc": ts, **values} for ts, values in rows])
    if tt_df.empty:
        return tt_df
    tt_df["timestamp_utc"] = pd.to_datetime(tt_df["timestamp_utc"], utc=True)
    return tt_df.set_index("timestamp_utc").sort_index()


st.subheader("⚖️ Kiegyenlítő és nem kiegyenlítő célú szabályozási tartalékok (élő, országos)")
st.caption(
    "Élőben lekérve a MAVIR-tól (rtdwweb, chart 1000726) - a hazai rendszer (VER) teljes "
    "szabályozási igénye (burkológörbe FEL/LE + nettó), illetve hogy ezt milyen forrásból "
    "(belföldi aFRR, mFRR SA/DA, RIR) fedezik kiegyenlítő céllal. Országos aggregátum."
)
try:
    live_tt = load_live_tartalekok()
    if not live_tt.empty:
        ver_labels = {
            "ver_igeny_fel_burkologorbe_mw": "VER igény FEL (burkológörbe)",
            "ver_igeny_le_burkologorbe_mw": "VER igény LE (burkológörbe)",
            "ver_igeny_netto_mw": "VER igény - nettó",
        }
        st.caption("Hazai rendszer szabályozási igénye")
        render_multiseries_chart(live_tt, ver_labels, key="ver_series", value_name="MW", chart_type="line", height=280)

        ke_labels = {
            "ke_belfoldi_afrr_fel_mw": "aFRR FEL",
            "ke_belfoldi_afrr_le_mw": "aFRR LE",
            "ke_belfoldi_mfrr_sa_fel_mw": "mFRR SA FEL",
            "ke_belfoldi_mfrr_sa_le_mw": "mFRR SA LE",
            "ke_belfoldi_mfrr_da_fel_mw": "mFRR DA FEL",
            "ke_belfoldi_mfrr_da_le_mw": "mFRR DA LE",
            "ke_rir_osszesen_fel_mw": "RIR FEL",
            "ke_rir_osszesen_le_mw": "RIR LE",
        }
        st.caption("Kiegyenlítő célú belföldi szabályozás forrásonként (FEL/LE)")
        render_multiseries_chart(live_tt, ke_labels, key="ke_series", value_name="MW", chart_type="bar", height=280)
    else:
        st.info("Nincs élő szabályozási tartalék adat.")
except Exception as e:
    st.error(f"Nem sikerült lekérni az élő szabályozási tartalék adatot: {e}")

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

st.subheader("Negyedóránkénti mintázat (elmúlt 14 nap)")
st.caption(
    "Naptári negyedóránként (helyi idő, pl. mindig a 14:00-14:15-ös szeletet nézve az elmúlt 14 napban): "
    "milyen gyakran volt jellemzően hiány/többlet ebben az időpontban, illetve átlagosan melyik piac "
    "(MAVIR kiegyenlítő vagy HUPX day-ahead) volt kedvezőbb. FONTOS: a MAVIR kiegyenlítő ár ~5-6 napos "
    "csúszású, ezért az ő 14 napos ablaka valójában a 20-6 nappal ezelőtti időszakot fedi le, nem a "
    "legutóbbi napokat."
)

LOOKBACK_DAYS = 14
_qh_window_start = pd.Timestamp.now(tz="UTC") - timedelta(days=LOOKBACK_DAYS)


def _qh_label_series(idx: pd.DatetimeIndex) -> pd.Series:
    local_idx = idx.tz_convert("Europe/Budapest")
    return pd.Series(local_idx.strftime("%H:%M"), index=idx)


if "rendszerirany_kwh" in wide:
    ri_window = wide.loc[wide.index >= _qh_window_start, "rendszerirany_kwh"].dropna()
else:
    ri_window = pd.Series(dtype=float)

if not ri_window.empty:
    qh_labels = _qh_label_series(ri_window.index)

    def _classify(v: float) -> str:
        if v > 0:
            return "Többlet (leszab)"
        if v < 0:
            return "Hiány (felszab)"
        return "Kiegyensúlyozott"

    ri_tmp = pd.DataFrame({"qh": qh_labels.values, "állapot": [_classify(v) for v in ri_window.values]})
    freq_chart = (
        alt.Chart(ri_tmp)
        .mark_bar()
        .encode(
            x=alt.X("qh:O", title="Negyedóra (helyi idő)", sort=sorted(ri_tmp["qh"].unique())),
            y=alt.Y("count():Q", stack="normalize", title="Arány", axis=alt.Axis(format="%")),
            color=alt.Color(
                "állapot:N",
                scale=alt.Scale(
                    domain=["Hiány (felszab)", "Kiegyensúlyozott", "Többlet (leszab)"],
                    range=["#e74c3c", "#95a5a6", "#2ecc71"],
                ),
                legend=alt.Legend(title=None),
            ),
        )
        .properties(height=300, title="Rendszerirány gyakorisága negyedóránként")
    )
    st.altair_chart(freq_chart, use_container_width=True)
else:
    st.info("Nincs elég rendszerirány adat a negyedóránkénti gyakorisághoz.")

ke_window = (
    wide.loc[wide.index >= _qh_window_start, "pozitiv_ar_huf_per_kwh"].dropna()
    if "pozitiv_ar_huf_per_kwh" in wide
    else pd.Series(dtype=float)
)
hupx_window = (
    wide.loc[wide.index >= _qh_window_start, "hupx_dam_price_eur_mwh"].dropna()
    if "hupx_dam_price_eur_mwh" in wide
    else pd.Series(dtype=float)
)

if not ke_window.empty or not hupx_window.empty:
    layers = []
    if not ke_window.empty:
        ke_avg = (
            pd.DataFrame({"qh": _qh_label_series(ke_window.index).values, "átlag": ke_window.values})
            .groupby("qh")["átlag"].mean().reset_index()
        )
        ke_layer = (
            alt.Chart(ke_avg)
            .mark_line(color="#3498db", point=True)
            .encode(
                x=alt.X("qh:O", title="Negyedóra (helyi idő)", sort=sorted(ke_avg["qh"].unique())),
                y=alt.Y("átlag:Q", title="MAVIR KE átlagár (HUF/kWh)", axis=alt.Axis(titleColor="#3498db")),
            )
        )
        layers.append(ke_layer)
        cheapest = ke_avg.loc[ke_avg["átlag"].idxmin()]
        priciest = ke_avg.loc[ke_avg["átlag"].idxmax()]
        st.caption(
            f"MAVIR KE: legolcsóbb negyedóra átlagosan **{cheapest['qh']}** ({cheapest['átlag']:,.1f} HUF/kWh), "
            f"legdrágább **{priciest['qh']}** ({priciest['átlag']:,.1f} HUF/kWh)."
        )
    if not hupx_window.empty:
        hupx_avg = (
            pd.DataFrame({"qh": _qh_label_series(hupx_window.index).values, "átlag": hupx_window.values})
            .groupby("qh")["átlag"].mean().reset_index()
        )
        hupx_layer = (
            alt.Chart(hupx_avg)
            .mark_line(color="#e67e22", point=True)
            .encode(
                x=alt.X("qh:O", title="Negyedóra (helyi idő)", sort=sorted(hupx_avg["qh"].unique())),
                y=alt.Y("átlag:Q", title="HUPX DAM átlagár (EUR/MWh)", axis=alt.Axis(titleColor="#e67e22")),
            )
        )
        layers.append(hupx_layer)
        cheapest = hupx_avg.loc[hupx_avg["átlag"].idxmin()]
        priciest = hupx_avg.loc[hupx_avg["átlag"].idxmax()]
        st.caption(
            f"HUPX DAM: legolcsóbb negyedóra átlagosan **{cheapest['qh']}** ({cheapest['átlag']:,.1f} EUR/MWh), "
            f"legdrágább **{priciest['qh']}** ({priciest['átlag']:,.1f} EUR/MWh)."
        )
    combo_chart = alt.layer(*layers).resolve_scale(y="independent").properties(
        height=300, title="MAVIR KE vs. HUPX DAM átlagár negyedóránként (eltérő tengelyek, más pénznem/mértékegység!)"
    )
    st.altair_chart(combo_chart, use_container_width=True)
else:
    st.info("Nincs elég ár adat a negyedóránkénti KE vs. HUPX összevetéshez.")

st.subheader("Nyers adattábla")
st.dataframe(filtered, use_container_width=True)

st.sidebar.divider()
st.sidebar.caption(f"Oldal frissítve: {datetime.now(timezone.utc).isoformat()} UTC")
if st.sidebar.button("Adatok frissítése"):
    load_data.clear()
    st.rerun()
