"""
6-0_streamlit_map.py
====================
Streamlit dashboard to visualise the MNL locker location results on a map.

Usage:
    streamlit run 6-0_streamlit_map.py

Requirements:
    pip install streamlit streamlit-folium folium plotly pandas openpyxl

What it shows:
    - Method selector: Exact MILP / Greedy / OA (Outer Approx.)
    - KPI cards: total demand, captured demand, overall market share
    - Runtime comparison table (methods × P) with bar chart
    - Interactive folium map:
        * Zones coloured by market share (green = high, red = low)
        * Open lockers: large green markers (read from *_lockers_P{P}.csv)
        * Closed candidates: small grey markers
    - Sensitivity chart: captured demand vs. P (if sensitivity CSV exists)
    - Distribution histogram of market share across zones
    - Table of top zones by captured demand

Input files (all produced by 6-1 / 6-2 / 6-3):
    results/mnl_location_results_P{P}.csv    exact MILP results
    results/mnl_location_lockers_P{P}.csv    exact MILP open lockers
    results/mnl_greedy_results_P{P}.csv      greedy heuristic results
    results/mnl_greedy_lockers_P{P}.csv      greedy open lockers
    results/mnl_oa_results_P{P}.csv          OA heuristic results
    results/mnl_oa_lockers_P{P}.csv          OA open lockers
    results/mnl_sensitivity_P.csv            sensitivity vs. P
    results/utils/zone_demand.csv            zone metadata
    results/utils/solve_times.csv            runtime log
    results/utils/df_clients_grids.csv       zone centroids
    data/data.xlsx  sheet "candidates"       locker locations
"""

from pathlib import Path
import numpy as np
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium
import plotly.express as px
import plotly.graph_objects as go

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE        = Path(__file__).resolve().parent
SAO_PAULO    = _HERE.parent
DATA_XLSX    = SAO_PAULO / "data" / "data.xlsx"
RESULTS_UTIL = SAO_PAULO / "results" / "utils"
RESULTS_OUT  = SAO_PAULO / "results"

# method → (results prefix, lockers prefix)
RESULT_PATTERNS = {
    "Exact MILP":         ("mnl_location_results",  "mnl_location_lockers"),
    "Greedy":             ("mnl_greedy_results",    "mnl_greedy_lockers"),
    "OA (Outer Approx.)": ("mnl_oa_results",        "mnl_oa_lockers"),
}

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def result_path(method_key: str, p: int) -> Path:
    prefix = RESULT_PATTERNS[method_key][0]
    return RESULTS_OUT / f"{prefix}_P{p}.csv"

def lockers_path(method_key: str, p: int) -> Path:
    prefix = RESULT_PATTERNS[method_key][1]
    return RESULTS_OUT / f"{prefix}_P{p}.csv"

def available_p_values(method_key: str) -> list[int]:
    prefix = RESULT_PATTERNS[method_key][0]
    values = []
    for f in RESULTS_OUT.glob(f"{prefix}_P*.csv"):
        try:
            values.append(int(f.stem.split("_P")[-1]))
        except ValueError:
            pass
    return sorted(values)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Locker Location",
    page_icon="📦",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------
@st.cache_data
def load_candidates() -> pd.DataFrame:
    return pd.read_excel(DATA_XLSX, sheet_name="candidates")[
        ["Nome", "Latitude", "Longitude",
         "Capacidade diária para operação de couriers (remessas)"]
    ].rename(columns={
        "Nome": "candidate_id",
        "Capacidade diária para operação de couriers (remessas)": "capacity",
    })


@st.cache_data
def load_zone_centroids() -> pd.DataFrame:
    clients_path = RESULTS_UTIL / "df_clients_grids.csv"
    if not clients_path.exists():
        return pd.DataFrame(columns=["zone_id", "lat", "lon"])
    df = pd.read_csv(clients_path)
    df.columns = [c.strip() for c in df.columns]
    grid_col = next((c for c in df.columns if "grid" in c.lower() or "quadrado" in c.lower()), None)
    lat_col  = next((c for c in df.columns if c.lower() == "latitude"), None)
    lon_col  = next((c for c in df.columns if c.lower() == "longitude"), None)
    if not all([grid_col, lat_col, lon_col]):
        return pd.DataFrame(columns=["zone_id", "lat", "lon"])
    centroids = (
        df.groupby(grid_col)[[lat_col, lon_col]]
        .mean().reset_index()
        .rename(columns={grid_col: "zone_id", lat_col: "lat", lon_col: "lon"})
    )
    centroids["zone_id"] = centroids["zone_id"].astype(int)
    return centroids


@st.cache_data
def load_results(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_csv(path)


@st.cache_data
def load_sensitivity() -> pd.DataFrame | None:
    path = RESULTS_OUT / "mnl_sensitivity_P.csv"
    return pd.read_csv(path) if path.exists() else None


@st.cache_data
def load_solve_times() -> pd.DataFrame | None:
    path = RESULTS_UTIL / "solve_times.csv"
    return pd.read_csv(path) if path.exists() else None


@st.cache_data
def load_open_lockers(method_key: str, p: int) -> list[str] | None:
    path = lockers_path(method_key, p)
    if not path.exists():
        return None
    return pd.read_csv(path)["candidate_id"].tolist()


# ---------------------------------------------------------------------------
# Colour helper
# ---------------------------------------------------------------------------
def market_share_to_colour(pct: float, max_pct: float) -> str:
    ratio = min(pct / max_pct, 1.0) if max_pct > 0 else 0.0
    r = int(220 * (1 - ratio))
    g = int(180 * ratio)
    return f"#{r:02x}{g:02x}28"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("📦 Locker Location")
st.sidebar.markdown("---")

methods_with_files = {
    m: available_p_values(m)
    for m in RESULT_PATTERNS
    if available_p_values(m)
}

if not methods_with_files:
    st.sidebar.warning("No result file found. Run `./run_pipeline.sh <P>` first.")
    selected_method = None
    selected_p      = None
else:
    selected_method = st.sidebar.selectbox("Method", list(methods_with_files.keys()))
    p_values = methods_with_files[selected_method]
    selected_p = st.sidebar.selectbox(
        "Number of open lockers (P)",
        options=p_values,
        index=len(p_values) - 1,
        format_func=lambda p: f"P = {p}",
    )

st.sidebar.markdown("---")
st.sidebar.markdown("**Map options**")
show_closed  = st.sidebar.checkbox("Show closed candidates", value=True)
circle_scale = st.sidebar.slider("Zone circle size", 50, 500, 150, step=25)

st.sidebar.markdown("---")
st.sidebar.markdown("**Top-N zones in table**")
top_n = st.sidebar.slider("N", 5, 50, 15)

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
candidates      = load_candidates()
centroids       = load_zone_centroids()
sensitivity     = load_sensitivity()
solve_times     = load_solve_times()
results_df      = (
    load_results(result_path(selected_method, selected_p))
    if selected_method and selected_p is not None else None
)
open_locker_ids = (
    load_open_lockers(selected_method, selected_p)
    if selected_method and selected_p is not None else None
)

# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------
st.title("📦 Locker Location — Results Map")
if selected_method and selected_p is not None:
    st.caption(f"Showing: **{selected_method}** — P = **{selected_p}** lockers")

if results_df is None:
    st.warning(
        "No results loaded. Run **`./run_pipeline.sh <P>`** first, then reload."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Merge with centroids
# ---------------------------------------------------------------------------
if centroids.empty:
    st.info("Zone centroids not found — only the locker map will be shown.")
    show_zones = False
else:
    results_mapped = results_df.merge(centroids, on="zone_id", how="inner")
    show_zones = not results_mapped.empty

# ---------------------------------------------------------------------------
# KPI cards
# ---------------------------------------------------------------------------
total_demand   = results_df["demand"].sum()
total_captured = results_df["captured"].sum()
overall_share  = total_captured / total_demand if total_demand > 0 else 0.0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total daily demand",   f"{total_demand:,.0f} parcels")
col2.metric("Captured demand",      f"{total_captured:,.0f} parcels")
col3.metric("Overall market share", f"{overall_share:.1%}")
col4.metric("Open lockers",         str(len(open_locker_ids)) if open_locker_ids else f"P={selected_p}")

st.markdown("---")

# ---------------------------------------------------------------------------
# Runtime comparison
# ---------------------------------------------------------------------------
if solve_times is not None and not solve_times.empty:
    st.subheader("⏱ Method Comparison — Runtime & Quality")

    # Use consistent column name: prefer solve_time_s (new format),
    # fall back to total_time_s (old format)
    st_all = solve_times.copy()
    if "solve_time_s" not in st_all.columns and "total_time_s" in st_all.columns:
        st_all["solve_time_s"] = st_all["total_time_s"]

    # ── Left: table for the selected P ──────────────────────────────────
    tab_left, tab_right = st.columns([1, 2])

    with tab_left:
        st.markdown(f"**For P = {selected_p}**")
        st_p = st_all[st_all["P"] == selected_p] if selected_p is not None else st_all
        if st_p.empty:
            st_p = st_all

        display_cols = {
            "method":           "Method",
            "solve_time_s":     "Runtime (s)",
            "market_share_pct": "Share (%)",
            "detail":           "Breakdown",
        }
        show_df = (
            st_p[[c for c in display_cols if c in st_p.columns]]
            .rename(columns=display_cols)
            .sort_values("Runtime (s)")
            .reset_index(drop=True)
        )
        show_df.index += 1

        def _highlight(row):
            if "Method" in row.index and row["Method"] == selected_method:
                return ["background-color: #2e7d32; color: white; font-weight: bold"] * len(row)
            return [""] * len(row)

        st.dataframe(
            show_df.style.apply(_highlight, axis=1),
            use_container_width=True,
            height=min(100 + 38 * len(show_df), 260),
        )

    # ── Right: runtime vs P line chart (all methods, all P) ──────────────
    with tab_right:
        st.markdown("**Runtime vs P — all methods**")
        if "solve_time_s" in st_all.columns:
            methods_order = ["Greedy", "OA (Outer Approx.)", "Exact MILP"]
            colours = {
                "Exact MILP":         "#c0392b",
                "Greedy":             "#27ae60",
                "OA (Outer Approx.)": "#2980b9",
            }
            fig_trend = go.Figure()
            for meth in methods_order:
                df_m = (
                    st_all[st_all["method"] == meth]
                    .sort_values("P")
                    .dropna(subset=["solve_time_s"])
                )
                if df_m.empty:
                    continue
                # Highlight the selected P with a larger marker
                marker_sizes = [
                    14 if p == selected_p else 8
                    for p in df_m["P"]
                ]
                fig_trend.add_trace(go.Scatter(
                    x=df_m["P"],
                    y=df_m["solve_time_s"],
                    mode="lines+markers",
                    name=meth,
                    line=dict(color=colours.get(meth, "#888"), width=2),
                    marker=dict(size=marker_sizes,
                                color=colours.get(meth, "#888")),
                    hovertemplate=(
                        f"<b>{meth}</b><br>"
                        "P = %{x}<br>"
                        "Runtime: %{y:.1f}s<extra></extra>"
                    ),
                ))

            fig_trend.update_layout(
                xaxis_title="P (number of open lockers)",
                yaxis_title="Runtime (s)",
                height=240,
                margin=dict(l=40, r=20, t=10, b=40),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                hovermode="x unified",
            )
            # Mark Gurobi time limit as a dashed line if MILP hits it
            milp_df = st_all[st_all["method"] == "Exact MILP"]
            if not milp_df.empty and "solve_time_s" in milp_df.columns:
                max_milp = milp_df["solve_time_s"].max()
                # If most MILP times cluster near max_milp → likely timeout
                near_max = (milp_df["solve_time_s"] >= max_milp * 0.9).sum()
                if near_max >= 2:
                    fig_trend.add_hline(
                        y=max_milp,
                        line_dash="dash",
                        line_color="#c0392b",
                        opacity=0.5,
                        annotation_text=f"⏱ MILP timeout ≈ {max_milp:.0f}s",
                        annotation_position="bottom right",
                    )
            st.plotly_chart(fig_trend, use_container_width=True)

st.markdown("---")

# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
map_col, table_col = st.columns([3, 1])

with map_col:
    center_lat = candidates["Latitude"].mean()
    center_lon = candidates["Longitude"].mean()
    m = folium.Map(location=[center_lat, center_lon], zoom_start=11,
                   tiles="CartoDB positron")

    max_share = results_df["market_share_pct"].max() if not results_df.empty else 1.0

    # ── Zones: coloured circles ───────────────────────────────────────────
    if show_zones:
        for _, row in results_mapped.dropna(subset=["lat", "lon"]).iterrows():
            colour = market_share_to_colour(row["market_share_pct"], max_share)
            folium.CircleMarker(
                location=[row["lat"], row["lon"]],
                radius=circle_scale / 50,
                color=colour,
                fill=True,
                fill_color=colour,
                fill_opacity=0.6,
                weight=0,
                popup=folium.Popup(
                    f"<b>Zone {row['zone_id']}</b><br>"
                    f"Demand: {row['demand']:,.1f}<br>"
                    f"Captured: {row['captured']:,.1f}<br>"
                    f"Market share: {row['market_share_pct']:.1f}%",
                    max_width=200,
                ),
            ).add_to(m)

    # ── Identify open vs closed candidates ───────────────────────────────
    if open_locker_ids is not None:
        # Read directly from the lockers file saved by 6-2 / 6-3
        open_candidates = candidates[
            candidates["candidate_id"].isin(open_locker_ids)
        ].dropna(subset=["Latitude", "Longitude"])
        if open_candidates.empty:
            st.warning("Locker IDs don't match candidates table — check data.xlsx.")
    else:
        # Fallback: proximity heuristic when lockers file is missing
        candidates["score"] = 0.0
        top_zones = results_df.nlargest(20, "S_i")
        if show_zones and not top_zones.empty:
            top_mapped = top_zones.merge(centroids, on="zone_id", how="inner").dropna(subset=["lat", "lon"])
            if not top_mapped.empty:
                ref_lat = top_mapped["lat"].values
                ref_lon = top_mapped["lon"].values
                candidates["score"] = candidates.apply(
                    lambda r: 1 / (np.sqrt((ref_lat - r["Latitude"])**2 +
                                           (ref_lon - r["Longitude"])**2).min() + 1e-6),
                    axis=1,
                )
        open_candidates = (
            candidates.nlargest(selected_p, "score")
            if results_df["S_i"].max() > 1e-9 else candidates
        ).dropna(subset=["Latitude", "Longitude"])
        st.caption("⚠️ Lockers file not found — showing approximate open lockers.")

    open_ids_set = set(open_candidates["candidate_id"].tolist())

    # ── Open lockers: green markers ───────────────────────────────────────
    for _, cand in open_candidates.iterrows():
        folium.Marker(
            location=[cand["Latitude"], cand["Longitude"]],
            icon=folium.Icon(color="green", icon="archive", prefix="fa"),
            popup=folium.Popup(
                f"<b>✅ {cand['candidate_id']}</b><br>"
                f"Capacity: {cand['capacity']} parcels/day<br>"
                f"Lat: {cand['Latitude']:.4f}, Lon: {cand['Longitude']:.4f}",
                max_width=220,
            ),
            tooltip=cand["candidate_id"],
        ).add_to(m)

    # ── Closed candidates: grey circles ──────────────────────────────────
    if show_closed:
        for _, cand in candidates[
            ~candidates["candidate_id"].isin(open_ids_set)
        ].dropna(subset=["Latitude", "Longitude"]).iterrows():
            folium.CircleMarker(
                location=[cand["Latitude"], cand["Longitude"]],
                radius=6,
                color="#888888",
                fill=True,
                fill_color="#cccccc",
                fill_opacity=0.5,
                weight=1.5,
                popup=folium.Popup(
                    f"<b>{cand['candidate_id']}</b><br>"
                    f"Capacity: {cand['capacity']}<br>"
                    f"<i>Closed</i>",
                    max_width=180,
                ),
            ).add_to(m)

    # ── Legend ────────────────────────────────────────────────────────────
    m.get_root().html.add_child(folium.Element("""
    <div style="position:fixed; bottom:30px; left:30px; z-index:1000;
                background:white; color:#333; padding:10px 14px; border-radius:8px;
                border:1px solid #ccc; font-size:12px; line-height:1.8">
        <b style="color:#111">Market share</b><br>
        <span style="color:#00b428">●</span> High<br>
        <span style="color:#b49028">●</span> Medium<br>
        <span style="color:#dc0028">●</span> Low<br>
        <br>
        <b style="color:#111">Lockers</b><br>
        🟢 Open&nbsp;&nbsp;⚪ Closed
    </div>
    """))

    st_folium(m, width=None, height=520, returned_objects=[])

# ---------------------------------------------------------------------------
# Top-N zones table
# ---------------------------------------------------------------------------
with table_col:
    st.subheader(f"Top {top_n} zones")
    top_df = (
        results_df[["zone_id", "demand", "market_share_pct", "captured"]]
        .nlargest(top_n, "captured")
        .reset_index(drop=True)
    )
    top_df.index += 1
    st.dataframe(
        top_df.rename(columns={
            "zone_id":          "Zone",
            "demand":           "Demand",
            "market_share_pct": "Share %",
            "captured":         "Captured",
        }),
        height=500,
        use_container_width=True,
    )

# ---------------------------------------------------------------------------
# Sensitivity chart
# ---------------------------------------------------------------------------
if sensitivity is not None:
    st.markdown("---")
    st.subheader("Sensitivity: captured demand vs. number of open lockers (P)")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=sensitivity["P"],
        y=sensitivity["market_share_pct"],
        mode="lines+markers",
        marker=dict(size=8, color="#2e7d32"),
        line=dict(width=2),
        name="Market share (%)",
        hovertemplate="P=%{x}<br>Market share: %{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        xaxis_title="P (number of open lockers)",
        yaxis_title="Overall market share (%)",
        height=320,
        margin=dict(l=40, r=20, t=20, b=40),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------------------
# Distribution histogram
# ---------------------------------------------------------------------------
st.markdown("---")
st.subheader("Distribution of market share across zones")
fig2 = px.histogram(
    results_df,
    x="market_share_pct",
    nbins=40,
    color_discrete_sequence=["#2e7d32"],
    labels={"market_share_pct": "Market share per zone (%)"},
)
fig2.update_layout(height=280, margin=dict(l=40, r=20, t=10, b=40))
st.plotly_chart(fig2, use_container_width=True)

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
st.markdown("---")
st.caption("UFMG Internship 2026 — Bastien Jacquelin")
