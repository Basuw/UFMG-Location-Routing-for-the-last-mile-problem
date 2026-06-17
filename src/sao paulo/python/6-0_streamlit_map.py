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
    results/utils/solve_times.csv            runtime log (MNL methods)
    results/utils/solve_times_lr.csv         runtime log (LRP-MNL)
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
    "LRP-MNL (without cost on lost market share)": ("lr_results",     "lr_lockers"),
    "LRP-MNL (with cost on lost market share)":    ("lr_old_results", "lr_old_lockers"),
}

# Colour per method (used in chart + highlight)
METHOD_COLOURS = {
    "Exact MILP":         "#c0392b",   # red
    "Greedy":             "#27ae60",   # green
    "OA (Outer Approx.)": "#2980b9",   # blue
    "LRP-MNL (without cost on lost market share)": "#8e44ad",   # purple (corrected)
    "LRP-MNL (with cost on lost market share)":    "#b5651d",   # brown (routing×ω, clusters)
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
    """Merge MNL solve times (6-2/6-3) + LRP solve times (6-4) into one table."""
    frames = []
    for fname in ("solve_times.csv", "solve_times_lr.csv"):
        p = RESULTS_UTIL / fname
        if p.exists():
            frames.append(pd.read_csv(p))
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


@st.cache_data
def load_open_lockers(method_key: str, p: int) -> list[str] | None:
    path = lockers_path(method_key, p)
    if not path.exists():
        return None
    return pd.read_csv(path)["candidate_id"].tolist()


@st.cache_data
def load_lr_big_hub() -> pd.DataFrame | None:
    """Single external big hub (LRP-MNL) — produced by 6-4_location_routing.py."""
    path = RESULTS_OUT / "lr_big_hub.csv"
    return pd.read_csv(path) if path.exists() else None


@st.cache_data
def load_cost_params() -> dict | None:
    """LRP-MNL cost constants exported by 6-4 (single source of truth)."""
    path = RESULTS_OUT / "lr_cost_params.csv"
    if not path.exists():
        return None
    return pd.read_csv(path).iloc[0].to_dict()


@st.cache_data
def load_lr_grid(name: str) -> pd.DataFrame | None:
    """G2 (locker) / G3 (small hub) candidate grid rectangles."""
    path = RESULTS_OUT / f"lr_grid_{name}.csv"
    return pd.read_csv(path) if path.exists() else None


@st.cache_data
def load_g1_grid() -> pd.DataFrame:
    """G1 demand grid (all cells) from df_grids.csv."""
    path = RESULTS_UTIL / "df_grids.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "id" not in df.columns:
        df = df.rename(columns={df.columns[0]: "id"})
    return df


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
show_closed    = st.sidebar.checkbox("Show closed candidates", value=True)
show_big_hubs  = st.sidebar.checkbox("Show big hubs (57 fixed bases)", value=False)
circle_scale   = st.sidebar.slider("Zone circle size", 50, 500, 150, step=25)

st.sidebar.markdown("**Grids overlay**")
show_g1   = st.sidebar.checkbox("G1 demand grid (~1 km²)", value=False)
show_g2   = st.sidebar.checkbox("G2 locker grid (~9 km²)", value=False)
show_g3   = st.sidebar.checkbox("G3 small-hub grid (~25 km²)", value=False)

st.sidebar.markdown("**Location routing** *(LRP-MNL)*")
show_routing  = st.sidebar.checkbox("Show routing network", value=True)
show_big_hub  = st.sidebar.checkbox("Show external big hub", value=True)

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
big_hub_df      = load_lr_big_hub()
g1_grid         = load_g1_grid()
g2_grid         = load_lr_grid("g2")
g3_grid         = load_lr_grid("g3")
cost_params     = load_cost_params()

# LRP family helpers: any method whose key starts with "LRP" is an LRP variant.
# lr_base is the filename prefix for its hubs/fleet files (e.g. "lr" or "lr_old").
is_lrp = bool(selected_method) and selected_method.startswith("LRP")
lr_base = (
    RESULT_PATTERNS[selected_method][1].rsplit("_lockers", 1)[0]
    if is_lrp else "lr"
)
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

# ── Cost panel (LRP-MNL only) ──────────────────────────────────────────────────
# Breakdown is reconstructed from the saved solution + the cost constants
# exported by 6-4 (lr_cost_params.csv).  Routing is the residual of the daily
# objective so the four daily terms always sum to the optimiser's objective.
if is_lrp and cost_params is not None and selected_p is not None:
    cp = cost_params
    n_lockers = len(open_locker_ids) if open_locker_ids else 0

    _hub_p   = RESULTS_OUT / f"{lr_base}_hubs_P{selected_p}.csv"
    _fleet_p = RESULTS_OUT / f"{lr_base}_fleet_P{selected_p}.csv"
    n_hubs      = len(pd.read_csv(_hub_p))   if _hub_p.exists()   else 0
    total_fleet = int(pd.read_csv(_fleet_p)["fleet_size"].sum()) if _fleet_p.exists() else 0

    # One-time opening CapEx
    capex_total = n_lockers * cp["OPEN_LOCKER"] + n_hubs * cp["OPEN_HUB"]
    # Daily terms
    opex_daily    = n_lockers * cp["F_LOCKER"]        + n_hubs * cp["F_HUB"]
    capex_daily   = n_lockers * cp["OPEN_LOCKER_DAY"] + n_hubs * cp["OPEN_HUB_DAY"]
    vehicle_daily = cp["A_VEHICLE"] * total_fleet
    uncaptured_daily = cp["COST_UNCAPTURED"] * (total_demand - total_captured)

    # Daily objective from the runtime log (sum of all daily terms)
    _obj = None
    if solve_times is not None:
        _row = solve_times[(solve_times["method"] == selected_method) & (solve_times["P"] == selected_p)]
        if not _row.empty and "objective" in _row.columns:
            _obj = float(_row["objective"].values[0])
    routing_daily = (
        _obj - opex_daily - capex_daily - vehicle_daily - uncaptured_daily
        if _obj is not None else None
    )
    daily_total = _obj if _obj is not None else (
        opex_daily + capex_daily + vehicle_daily + uncaptured_daily
    )

    st.markdown("##### 💰 Costs")
    cc1, cc2, cc3, cc4 = st.columns(4)
    cc1.metric("Opening CapEx (one-time)", f"{capex_total:,.0f} BRL",
               help=f"{n_lockers} lockers × {cp['OPEN_LOCKER']:,.0f} + "
                    f"{n_hubs} hubs × {cp['OPEN_HUB']:,.0f}")
    cc2.metric("Fixed cost / day", f"{opex_daily + capex_daily:,.0f} BRL",
               help=f"OpEx {opex_daily:,.0f} + amortised CapEx {capex_daily:,.0f} "
                    f"(over {cp['AMORT_DAYS']:.0f} days)")
    cc3.metric("Fleet cost / day", f"{vehicle_daily:,.0f} BRL",
               help=f"{total_fleet} vehicles × {cp['A_VEHICLE']:,.0f} BRL/day")
    cc4.metric("Total daily cost", f"{daily_total:,.0f} BRL",
               help="Optimiser objective (fixed + fleet + routing + uncaptured penalty)")

    if routing_daily is not None:
        st.caption(
            f"Daily breakdown — "
            f"fixed: **{opex_daily + capex_daily:,.0f}**  ·  "
            f"fleet: **{vehicle_daily:,.0f}**  ·  "
            f"routing: **{max(routing_daily, 0):,.0f}**  ·  "
            f"uncaptured penalty: **{uncaptured_daily:,.0f}**  =  "
            f"**{daily_total:,.0f} BRL/day**  "
            f"(lockers {cp['F_LOCKER']:.0f}/day + {cp['OPEN_LOCKER']:,.0f} CapEx; "
            f"hubs {cp['F_HUB']:.0f}/day + {cp['OPEN_HUB']:,.0f} CapEx)"
        )

# ── Method info bar ───────────────────────────────────────────────────────────
# Show MIP gap for Exact MILP, and whether this method's lockers differ
_info_parts = []
if solve_times is not None and selected_method and selected_p is not None:
    _st_row = solve_times[
        (solve_times["method"] == selected_method) & (solve_times["P"] == selected_p)
    ]
    if not _st_row.empty:
        _detail = str(_st_row["detail"].values[0])
        if "gap=" in _detail:
            _gap_str = _detail.split("gap=")[1].split()[0].rstrip(",")
            _info_parts.append(f"**MIP gap:** {_gap_str}")
        if "[" in _detail and "]" in _detail:
            _stop = _detail.split("[")[1].split("]")[0]
            _info_parts.append(f"**Stop:** {_stop}")
        # Show total daily objective for LRP variants (breakdown is in the cost panel)
        if is_lrp and "objective" in _st_row.columns:
            _obj = _st_row["objective"].values[0]
            if pd.notna(_obj):
                _info_parts.append(f"**Total daily cost:** {_obj:,.0f} BRL")

# Compare lockers with the other two methods
if open_locker_ids and selected_p is not None:
    _other_methods = [m for m in ["Exact MILP", "Greedy", "OA (Outer Approx.)", "LRP-MNL (without cost on lost market share)", "LRP-MNL (with cost on lost market share)"] if m != selected_method]
    _cur_set = set(open_locker_ids)
    _diff_flags = []
    for _om in _other_methods:
        _op = lockers_path(_om, selected_p)
        if _op.exists():
            _other_set = set(pd.read_csv(_op)["candidate_id"].tolist())
            if _cur_set == _other_set:
                _diff_flags.append(f"= {_om}")
            else:
                _n_diff = len(_cur_set.symmetric_difference(_other_set))
                _diff_flags.append(f"≠ {_om} ({_n_diff} lockers differ)")
    if _diff_flags:
        _info_parts.append("**vs others:** " + "  |  ".join(_diff_flags))

if _info_parts:
    st.info("  ·  ".join(_info_parts))

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
                return ["background-color: #2e7d32; color: #ffffff; font-weight: bold"] * len(row)
            return ["color: #111111; background-color: #f9f9f9"] * len(row)

        st.dataframe(
            show_df.style.apply(_highlight, axis=1),
            use_container_width=True,
            height=min(100 + 38 * len(show_df), 260),
        )

    # ── Right: runtime vs P line chart (log scale) ───────────────────────
    with tab_right:
        st.markdown("**Runtime vs P — all methods** *(log scale)*")
        if "solve_time_s" in st_all.columns:
            methods_order = ["Greedy", "OA (Outer Approx.)", "Exact MILP", "LRP-MNL (without cost on lost market share)", "LRP-MNL (with cost on lost market share)"]
            fig_trend = go.Figure()
            for meth in methods_order:
                df_m = (
                    st_all[st_all["method"] == meth]
                    .sort_values("P")
                    .dropna(subset=["solve_time_s"])
                )
                if df_m.empty:
                    continue
                # Floor at 0.01s so log scale stays valid even for near-zero times
                y_vals = df_m["solve_time_s"].clip(lower=0.01)
                # Highlight the selected P with a larger marker
                marker_sizes = [14 if p == selected_p else 8 for p in df_m["P"]]
                fig_trend.add_trace(go.Scatter(
                    x=df_m["P"],
                    y=y_vals,
                    mode="lines+markers",
                    name=meth,
                    line=dict(color=METHOD_COLOURS.get(meth, "#888"), width=2),
                    marker=dict(size=marker_sizes, color=METHOD_COLOURS.get(meth, "#888")),
                    customdata=df_m["solve_time_s"].values,
                    hovertemplate=(
                        f"<b>{meth}</b><br>"
                        "P = %{x}<br>"
                        "Runtime: %{customdata:.2f}s<extra></extra>"
                    ),
                ))

            fig_trend.update_layout(
                xaxis_title="P (number of open lockers)",
                yaxis_title="Runtime (s)",
                yaxis_type="log",
                height=260,
                margin=dict(l=40, r=20, t=10, b=40),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                hovermode="x unified",
            )
            # Mark Gurobi time limit as a dashed line if MILP hits it
            milp_df = st_all[st_all["method"] == "Exact MILP"]
            if not milp_df.empty and "solve_time_s" in milp_df.columns:
                max_milp = milp_df["solve_time_s"].max()
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

    # ── Locker comparison across methods for selected P ───────────────────
    st.markdown(f"**Locker sets for P = {selected_p}** — which lockers each method opens")
    _locker_rows = []
    for _m in ["Exact MILP", "Greedy", "OA (Outer Approx.)", "LRP-MNL (without cost on lost market share)", "LRP-MNL (with cost on lost market share)"]:
        _lpath = lockers_path(_m, selected_p)
        if _lpath.exists():
            _ids = sorted(pd.read_csv(_lpath)["candidate_id"].tolist())
            _locker_rows.append({"Method": _m, "Open lockers": "  ·  ".join(_ids)})
    if _locker_rows:
        _ldf = pd.DataFrame(_locker_rows)
        # Find reference set (first method available)
        _ref_set = set(_locker_rows[0]["Open lockers"].split("  ·  "))
        def _hl_locker(row):
            cur_set = set(row["Open lockers"].split("  ·  "))
            if row["Method"] == selected_method:
                # Selected method: green highlight
                return ["background-color: #2e7d32; color: #ffffff; font-weight: bold"] * len(row)
            if cur_set == _ref_set:
                # Same lockers as the first method: amber tint, dark text
                return ["background-color: #fff3cd; color: #111111"] * len(row)
            # Different lockers: neutral dark text, explicit so dark-mode doesn't invert it
            return ["color: #111111; background-color: #f0f0f0"] * len(row)
        st.dataframe(
            _ldf.style.apply(_hl_locker, axis=1),
            use_container_width=True,
            hide_index=True,
            height=min(80 + 35 * len(_ldf), 200),
        )

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

    # ── Grid overlays (drawn first → underneath markers) ──────────────────
    def _draw_grid(df: pd.DataFrame, colour: str, weight: float, ids=None):
        if df is None or df.empty:
            return
        rows = df if ids is None else df[df["id"].isin(ids)]
        for _, r in rows.iterrows():
            folium.Rectangle(
                bounds=[[r["minLat"], r["minLong"]], [r["maxLat"], r["maxLong"]]],
                color=colour, weight=weight, fill=False, opacity=0.55,
            ).add_to(m)

    if show_g1 and not g1_grid.empty:
        # Limit to demand zones to keep the map responsive
        _demand_ids = set(results_df["zone_id"].tolist())
        _draw_grid(g1_grid, "#9aa0a6", 0.4, ids=_demand_ids)
    if show_g2:
        _draw_grid(g2_grid, "#8e44ad", 0.8)
    if show_g3:
        _draw_grid(g3_grid, "#d35400", 1.2)

    # ── Location-routing network (LRP-MNL) ────────────────────────────────
    # 2-echelon flow:  big hub → small hubs (truck)  →  lockers (bike/car).
    # Drawn before markers so the lines sit underneath the icons.
    _routing_drawn = False
    if is_lrp and show_routing and selected_p is not None:
        _hub_path    = RESULTS_OUT / f"{lr_base}_hubs_P{selected_p}.csv"
        _locker_path = RESULTS_OUT / f"{lr_base}_lockers_P{selected_p}.csv"
        if _hub_path.exists() and _locker_path.exists():
            _hubs_r    = pd.read_csv(_hub_path)
            _lockers_r = pd.read_csv(_locker_path)
            _hub_xy = {
                r["candidate_id"]: (r["centroid_lat"], r["centroid_lon"])
                for _, r in _hubs_r.iterrows()
            }
            # First echelon: external big hub → each small hub (truck legs)
            if big_hub_df is not None and not big_hub_df.empty:
                _bh = big_hub_df.iloc[0]
                for _, h in _hubs_r.iterrows():
                    folium.PolyLine(
                        [[_bh["lat"], _bh["lon"]],
                         [h["centroid_lat"], h["centroid_lon"]]],
                        color="#1a1a1a", weight=3, opacity=0.65,
                        dash_array="10",
                        tooltip=f"Truck: BIG_HUB → {h['candidate_id']}",
                    ).add_to(m)
            # Second echelon: small hub → its lockers (last-mile legs)
            if "parent_hub" in _lockers_r.columns:
                for _, l in _lockers_r.iterrows():
                    parent = l["parent_hub"]
                    if parent in _hub_xy:
                        folium.PolyLine(
                            [[_hub_xy[parent][0], _hub_xy[parent][1]],
                             [l["centroid_lat"], l["centroid_lon"]]],
                            color="#8e44ad", weight=2, opacity=0.7,
                            tooltip=f"{parent} → {l['candidate_id']}",
                        ).add_to(m)
            _routing_drawn = True

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

    # ── Identify open lockers ─────────────────────────────────────────────
    # LRP-MNL lockers come from the G2 grid (coords stored in the CSV).
    # MILP / Greedy / OA lockers use data.xlsx candidate coordinates.
    _lrp_locker_path = lockers_path(selected_method, selected_p)
    _lrp_coords = None
    if is_lrp and _lrp_locker_path.exists():
        _lrp_df = pd.read_csv(_lrp_locker_path)
        if "centroid_lat" in _lrp_df.columns:
            _lrp_coords = _lrp_df

    if _lrp_coords is not None:
        # G2 grid lockers: use coordinates stored in the lockers CSV
        open_candidates = _lrp_coords.rename(columns={
            "candidate_id": "candidate_id",
            "centroid_lat": "Latitude",
            "centroid_lon": "Longitude",
        })
        open_candidates["capacity"] = "G2 cell"
        open_ids_set = set(open_candidates["candidate_id"].tolist())
    elif open_locker_ids is not None:
        # MILP / Greedy / OA: look up coordinates in data.xlsx
        open_candidates = candidates[
            candidates["candidate_id"].isin(open_locker_ids)
        ].dropna(subset=["Latitude", "Longitude"])
        open_ids_set = set(open_candidates["candidate_id"].tolist())
        if open_candidates.empty:
            st.warning("Locker IDs don't match candidates table — check data.xlsx.")
    else:
        open_candidates = pd.DataFrame(columns=["candidate_id", "Latitude", "Longitude", "capacity"])
        open_ids_set = set()
        st.caption("⚠️ Lockers file not found.")

    # ── Open lockers: green markers ───────────────────────────────────────
    for _, cand in open_candidates.iterrows():
        folium.Marker(
            location=[cand["Latitude"], cand["Longitude"]],
            icon=folium.Icon(color="green", icon="archive", prefix="fa"),
            popup=folium.Popup(
                f"<b>✅ {cand['candidate_id']}</b><br>"
                f"Capacity: {cand['capacity']}<br>"
                f"Lat: {cand['Latitude']:.4f}, Lon: {cand['Longitude']:.4f}",
                max_width=220,
            ),
            tooltip=str(cand["candidate_id"]),
        ).add_to(m)

    # ── LRP-MNL: small hubs (orange markers) ─────────────────────────────
    if is_lrp and selected_p is not None:
        _hub_path = RESULTS_OUT / f"{lr_base}_hubs_P{selected_p}.csv"
        if _hub_path.exists():
            _hub_df = pd.read_csv(_hub_path)
            for _, h in _hub_df.iterrows():
                folium.Marker(
                    location=[h["centroid_lat"], h["centroid_lon"]],
                    icon=folium.Icon(color="orange", icon="home", prefix="fa"),
                    popup=folium.Popup(
                        f"<b>🏪 Small hub {h['candidate_id']}</b><br>"
                        f"Lockers served: {int(h.get('n_lockers', 0))}<br>"
                        f"Area: {h['area_km2']:.1f} km²",
                        max_width=220,
                    ),
                    tooltip=f"Hub {h['candidate_id']}",
                ).add_to(m)

    # ── LRP-MNL: single external big hub (blue truck marker) ─────────────
    if (is_lrp and show_big_hub
            and big_hub_df is not None and not big_hub_df.empty):
        _bh = big_hub_df.iloc[0]
        folium.Marker(
            location=[_bh["lat"], _bh["lon"]],
            icon=folium.Icon(color="blue", icon="industry", prefix="fa"),
            popup=folium.Popup(
                f"<b>🔵 {_bh['hub_id']} (external)</b><br>"
                f"Inbound flux: {_bh['capacity']:,.0f} parcels/day<br>"
                f"Lat: {_bh['lat']:.4f}, Lon: {_bh['lon']:.4f}<br>"
                f"<i>Fixed upstream source — placed outside the zone</i>",
                max_width=240,
            ),
            tooltip=f"{_bh['hub_id']} (external big hub)",
        ).add_to(m)

    # ── Big hubs: blue markers (optional, OFF by default) ───────────────
    if show_big_hubs:
        for _, hub in candidates.iterrows():
            folium.CircleMarker(
                location=[hub["Latitude"], hub["Longitude"]],
                radius=6,
                color="#1a6eb5",
                fill=True,
                fill_color="#2980b9",
                fill_opacity=0.5,
                weight=1,
                popup=folium.Popup(
                    f"<b>Big hub: {hub['candidate_id']}</b><br>"
                    f"Capacity: {hub['capacity']} parcels/day",
                    max_width=200,
                ),
                tooltip=hub["candidate_id"],
            ).add_to(m)

    # ── Closed candidates: grey circles (non-LRP methods only) ───────────
    if show_closed and not is_lrp:
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
    _legend_lrp = (
        "<br><b style='color:#111'>LRP-MNL only</b><br>"
        "🟠 Small hub<br>🔵 External big hub<br>"
        "<span style='color:#1a1a1a'>┄┄</span> Truck (hub→hub)<br>"
        "<span style='color:#8e44ad'>──</span> Last-mile (hub→locker)"
        if is_lrp else ""
    )
    _legend_grids = ""
    if show_g1 or show_g2 or show_g3:
        _parts = []
        if show_g1:
            _parts.append("<span style='color:#9aa0a6'>▢</span> G1 demand")
        if show_g2:
            _parts.append("<span style='color:#8e44ad'>▢</span> G2 locker")
        if show_g3:
            _parts.append("<span style='color:#d35400'>▢</span> G3 hub")
        _legend_grids = "<br><b style='color:#111'>Grids</b><br>" + "<br>".join(_parts)
    _legend_lrp += _legend_grids
    m.get_root().html.add_child(folium.Element(f"""
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
        {_legend_lrp}
    </div>
    """))

    # ── Fit bounds so the external big hub (north of the zone) stays visible ──
    if (is_lrp and show_big_hub
            and big_hub_df is not None and not big_hub_df.empty and show_zones):
        _bh = big_hub_df.iloc[0]
        _lats = results_mapped["lat"].dropna()
        _lons = results_mapped["lon"].dropna()
        if not _lats.empty:
            m.fit_bounds([
                [min(_lats.min(), _bh["lat"]), min(_lons.min(), _bh["lon"])],
                [max(_lats.max(), _bh["lat"]), max(_lons.max(), _bh["lon"])],
            ])

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
# Fleet size (LRP-MNL only)
# ---------------------------------------------------------------------------
if is_lrp and selected_p is not None:
    fleet_path = RESULTS_OUT / f"{lr_base}_fleet_P{selected_p}.csv"
    if fleet_path.exists():
        st.markdown("---")
        st.subheader(f"🚚 Fleet sizes — {selected_method}")
        fleet_df = pd.read_csv(fleet_path)
        # Merge with capacity from candidates table for comparison
        fleet_disp = fleet_df.merge(
            candidates[["candidate_id", "capacity"]],
            on="candidate_id", how="left"
        ).rename(columns={
            "candidate_id": "Locker",
            "fleet_size":   "Vehicles assigned",
            "capacity":     "Daily capacity (parcels)",
        })
        st.dataframe(fleet_disp, use_container_width=True, hide_index=True)
        st.caption(
            "fleet_size = number of couriers/vehicles assigned to this locker "
            f"(Q = {80} parcels/vehicle — adjust Q_CAPACITY in 6-4_location_routing.py)"
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
