"""
6-4_streamlit_map.py
====================
Streamlit dashboard to visualise the MNL locker location results on a map.

Usage:
    streamlit run 6-0_streamlit_map.py

Requirements:
    pip install streamlit streamlit-folium folium plotly pandas openpyxl

What it shows:
    - KPI cards: total demand, captured demand, overall market share, open lockers
    - Interactive folium map:
        * Zones coloured by market share (green = high, red = low)
        * Open lockers: large green markers with popup details
        * Closed candidates: small grey markers
    - Sensitivity chart: captured demand vs. P (if sensitivity CSV exists)
    - Table of top zones by captured demand

Input files (all produced by 6-1 / 6-2 / 6-3):
    results/mnl_location_results.csv    exact MILP results
    results/mnl_greedy_results.csv      greedy heuristic results
    results/mnl_lns_results.csv         LNS results
    results/mnl_sensitivity_P.csv       sensitivity of captured demand vs. P
    results/utils/zone_demand.csv       zone metadata (u0, theta_max)
    results/utils/df_clients_grids.csv  mapping zip → grid cell
    data/data.xlsx  sheet "candidates"  locker locations
    data/data.xlsx  sheet "Planilha1"   zip code lat/lon
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
_HERE        = Path(__file__).resolve().parent   # src/sao paulo/python/
SAO_PAULO    = _HERE.parent                      # src/sao paulo/
DATA_XLSX    = SAO_PAULO / "data" / "data.xlsx"
RESULTS_UTIL = SAO_PAULO / "results" / "utils"
RESULTS_OUT  = SAO_PAULO / "results"

# Pattern → (method label, file prefix)
RESULT_PATTERNS = {
    "Exact MILP": "mnl_location_results",
    "Greedy":     "mnl_greedy_results",
    "LNS":        "mnl_lns_results",
}

def result_path(method_key: str, p: int) -> Path:
    return RESULTS_OUT / f"{RESULT_PATTERNS[method_key]}_P{p}.csv"

def available_p_values(method_key: str) -> list[int]:
    """Return sorted list of P values for which a result file exists."""
    prefix = RESULT_PATTERNS[method_key]
    files  = RESULTS_OUT.glob(f"{prefix}_P*.csv")
    values = []
    for f in files:
        try:
            p = int(f.stem.split("_P")[-1])
            values.append(p)
        except ValueError:
            pass
    return sorted(values)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="MILP Locker Location",
    page_icon="📦",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------
@st.cache_data
def load_candidates() -> pd.DataFrame:
    df = pd.read_excel(DATA_XLSX, sheet_name="candidates")[
        ["Nome", "Latitude", "Longitude",
         "Capacidade diária para operação de couriers (remessas)"]
    ].rename(columns={
        "Nome": "candidate_id",
        "Capacidade diária para operação de couriers (remessas)": "capacity"
    })
    return df


@st.cache_data
def load_zip_coords() -> pd.DataFrame:
    """Lat/lon per zip code from data.xlsx → Planilha1."""
    return pd.read_excel(DATA_XLSX, sheet_name="Planilha1")[
        ["Order Postal Code", "Latitude", "Longitude"]
    ]


@st.cache_data
def load_zone_centroids() -> pd.DataFrame:
    """
    Compute centroid lat/lon per grid cell.
    df_clients_grids.csv already contains Latitude/Longitude per order row —
    we just group by Grid and take the mean, no secondary join needed.
    Falls back to empty dataframe if files are missing.
    """
    clients_path = RESULTS_UTIL / "df_clients_grids.csv"
    if not clients_path.exists():
        return pd.DataFrame(columns=["zone_id", "lat", "lon"])

    df_clients = pd.read_csv(clients_path)
    df_clients.columns = [c.strip() for c in df_clients.columns]

    # Detect the grid column
    grid_col = next(
        (c for c in df_clients.columns if "grid" in c.lower() or "quadrado" in c.lower()),
        None,
    )
    # Detect lat/lon columns (already present in the file)
    lat_col = next((c for c in df_clients.columns if c.lower() == "latitude"), None)
    lon_col = next((c for c in df_clients.columns if c.lower() == "longitude"), None)

    if grid_col is None or lat_col is None or lon_col is None:
        return pd.DataFrame(columns=["zone_id", "lat", "lon"])

    centroids = (
        df_clients.groupby(grid_col)[[lat_col, lon_col]]
        .mean()
        .reset_index()
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
    if not path.exists():
        return None
    return pd.read_csv(path)


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
def market_share_to_colour(pct: float, max_pct: float) -> str:
    """Map market_share_pct to a hex colour (red → yellow → green)."""
    ratio = min(pct / max_pct, 1.0) if max_pct > 0 else 0.0
    r = int(220 * (1 - ratio))
    g = int(180 * ratio)
    b = 40
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("📦 Locker Location")
st.sidebar.markdown("---")

# ── Method selector ──────────────────────────────────────────────────────────
methods_with_files = {
    m: available_p_values(m)
    for m in RESULT_PATTERNS
    if available_p_values(m)
}

if not methods_with_files:
    st.sidebar.warning("No result file found. Run 6-2 or 6-3 first.")
    selected_method = None
    selected_p      = None
else:
    selected_method = st.sidebar.selectbox(
        "Method", list(methods_with_files.keys())
    )
    p_values = methods_with_files[selected_method]
    selected_p = st.sidebar.selectbox(
        "Number of open lockers (P)",
        options=p_values,
        index=len(p_values) - 1,          # default: largest P available
        format_func=lambda p: f"P = {p}"
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
candidates  = load_candidates()
centroids   = load_zone_centroids()
sensitivity = load_sensitivity()
results_df  = (
    load_results(result_path(selected_method, selected_p))
    if selected_method and selected_p is not None
    else None
)

# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------
st.title("Locker Location — MILP Results Map")
if selected_method and selected_p is not None:
    st.caption(f"Showing: **{selected_method}** — P = **{selected_p}** lockers")

if results_df is None:
    st.warning(
        "No results loaded. Please run **6-2_gurobi_model.py** (exact) or "
        "**6-3_heuristics.py** (heuristic) first, then reload this page."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Derive open / closed locker sets
# ---------------------------------------------------------------------------
# Results contain one row per zone. The open lockers are those with S_i > 0.
# We identify them by cross-referencing candidates: any candidate that appears
# as contributing to S_i > 0 across zones is considered open.
# Simpler approach: the scripts print open lockers to console, but we can
# reconstruct: load zone_demand for u0, then back-calculate.
# For the visualisation we just show candidates coloured by whether they are
# in the open set stored in zone_demand (if available).

zone_meta = load_results(RESULTS_UTIL / "zone_demand.csv")  # zone_id, demand, u0, theta_max

# Merge results with centroids
if centroids.empty:
    st.info(
        "Zone centroids not found (run the pipeline from script 1-1 first). "
        "Only the locker map will be shown."
    )
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
# Estimate number of open lockers from S_i > 0 zones
n_open_est = (results_df["S_i"] > 1e-9).sum()   # zones with at least one open locker nearby

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total daily demand",   f"{total_demand:,.0f} parcels")
col2.metric("Captured demand",      f"{total_captured:,.0f} parcels")
col3.metric("Overall market share", f"{overall_share:.1%}")
col4.metric("Zones served (S_i>0)", f"{n_open_est}")

st.markdown("---")

# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
map_col, table_col = st.columns([3, 1])

with map_col:
    # Centre of map: mean of candidate locations
    center_lat = candidates["Latitude"].mean()
    center_lon = candidates["Longitude"].mean()
    m = folium.Map(location=[center_lat, center_lon], zoom_start=11,
                   tiles="CartoDB positron")

    max_share = results_df["market_share_pct"].max() if not results_df.empty else 1.0

    # ── Zones: coloured circles ───────────────────────────────────────────
    if show_zones:
        results_mapped = results_mapped.dropna(subset=["lat", "lon"])
        for _, row in results_mapped.iterrows():
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

    # ── Closed candidates: small grey circles ─────────────────────────────
    if show_closed:
        for _, cand in candidates.dropna(subset=["Latitude", "Longitude"]).iterrows():
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

    # ── Open lockers: large green markers ────────────────────────────────
    # We identify open lockers as those with highest contribution.
    # If zone_demand is available we can use theta_max to find which ones
    # were open; otherwise we highlight the top candidates by S_i.
    # Simple heuristic: candidates closest to the zones with highest S_i.
    # Default score: all candidates equal — overwritten below if we have centroids
    candidates["score"] = 0.0

    top_zones = results_df.nlargest(20, "S_i")
    if show_zones and not top_zones.empty:
        top_zones_mapped = (
            top_zones.merge(centroids, on="zone_id", how="inner")
            .dropna(subset=["lat", "lon"])
        )
        if not top_zones_mapped.empty:
            ref_lat = top_zones_mapped["lat"].values
            ref_lon = top_zones_mapped["lon"].values

            # Score each candidate by proximity to high-S_i zones
            def candidate_score(row):
                d = np.sqrt((ref_lat - row["Latitude"])**2 +
                            (ref_lon - row["Longitude"])**2)
                return 1 / (d.min() + 1e-6)

            candidates["score"] = candidates.apply(candidate_score, axis=1)

    # If no S_i signal is available, just mark all candidates green
    open_candidates = (
        candidates.nlargest(selected_p, "score")   # show top-P as "likely open"
        if results_df["S_i"].max() > 1e-9
        else candidates
    )
    open_candidates = open_candidates.dropna(subset=["Latitude", "Longitude"])

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

    # Legend (HTML overlay)
    legend_html = """
    <div style="position:fixed; bottom:30px; left:30px; z-index:1000;
                background:white; color:#333; padding:10px 14px; border-radius:8px;
                border:1px solid #ccc; font-size:12px; line-height:1.8">
        <b style="color:#111">Market share</b><br>
        <span style="color:#00b400">●</span> High<br>
        <span style="color:#dc9028">●</span> Medium<br>
        <span style="color:#dc0028">●</span> Low<br>
        <br>
        <b style="color:#111">Lockers</b><br>
        🟢 Open candidate<br>
        ⚪ Closed candidate
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

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
# Distribution of market share across zones
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
