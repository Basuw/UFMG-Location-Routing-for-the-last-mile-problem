"""
6-4_location_routing.py
=======================
Multi-layer Bilevel LRP-MNL for last-mile delivery in São Paulo.

Grid hierarchy (per 06_Location_Routing.md — "New considerations")
-------------------------------------------------------------------
  G1  ~1 km² cells   : demand zones (from df_grids.csv)
  G2  ~9 km² cells   : locker candidate sites  (G2_FACTOR × G2_FACTOR G1 merge)
  G3  ~25 km² cells  : small hub candidate sites (G3_FACTOR × G3_FACTOR G1 merge)
  BIG : fixed bases (data.xlsx "candidates") — big hubs, known capacity

Why G2/G3 instead of data.xlsx candidates for lockers?
  The 57 "Bases" in data.xlsx are logistics distribution centres (big hubs).
  Using them as locker candidates places lockers at depots, far from demand.
  G2 centroids cover the full demand grid → lockers placed inside the city.

Mathematical formulation (06_Location_Routing.md)
-------------------------------------------------
  UL min  Σ_j f_j·y_j  +  Σ_h F_h·v_h                    [daily OpEx]
        + Σ_j (o_j/T)·y_j  +  Σ_h (O_h/T)·v_h            [opening CapEx, amortised over T days]
        + Σ_j a_j·q_j
        + Σ_i Σ_j c_ij·u_ij·X_ij             [routing cost — BHH total tour, not ×ω_i]
        + L · COST_UNCAPTURED · Σ_i ω_i·Z_i  [uncaptured demand, weighted by L]

  s.t.
  (CC)    Σ_k u_ik·X_ik + Z_i = 1            ∀ i
          (Charnes-Cooper, u0 = μ_i = 1 per .md normalisation)
  (M1)    X_ij ≤ Z̄_i · y_j                  ∀ i,j   [McCormick ub 1]
  (M2)    X_ij ≤ Z_i                         ∀ i,j   [McCormick ub 2]
  (M3)    X_ij ≥ Z_i − Z̄_i·(1−y_j)         ∀ i,j   [McCormick lb]
  (CAP)   Σ_i ω_i·u_ij·X_ij ≤ Q·q_j         ∀ j     [courier capacity]
  (LNK)   q_j ≤ M·y_j                        ∀ j     [fleet-facility link]
  (HPAR)  y_j ≤ v_{h(j)}                     ∀ j     [locker needs open parent hub]
  (HCAP)  Σ_{j∈h} Σ_i ω_i·u_ij·X_ij ≤ C_h·v_h  ∀ h [small hub capacity]
  (BCAP)  Σ_i ω_i·(1−Z_i) ≤ CAP_BIG           [big hub total capacity]
  (BDG_L) Σ_j y_j = P                         [exactly P lockers]
  (BDG_H) Σ_h v_h ≤ P_HUB                    [at most P_HUB small hubs]

  LL (substituted):
    x_ij = u_ij·y_j / (Σ_k u_ik·y_k + 1)    MNL with μ_i = 1

  Routing cost (BHH — A_j = area of G2 cell, confirmed by professor):
    c_ij = l_ij · sqrt(A_j · η_i)
    where l_ij = Euclidean distance [km], A_j = G2 cell area [km²],
          η_i  = ω_i / area_i [parcels/km²]

Inputs (produced by 6-1_data_preparation.py):
  results/utils/zone_demand.csv
  results/utils/df_grids.csv
  data/data.xlsx  sheet "candidates"  (big hubs)

Outputs:
  results/lr_results_P{P}.csv        per-zone market share
  results/lr_lockers_P{P}.csv        open locker ids + coordinates + parent_hub
  results/lr_hubs_P{P}.csv           open small hub ids + coordinates
  results/lr_fleet_P{P}.csv          fleet sizes per locker
  results/lr_big_hub.csv             single external big hub location
  results/lr_grid_g2.csv             G2 locker candidate grid (cell rectangles)
  results/lr_grid_g3.csv             G3 small-hub candidate grid (cell rectangles)
  results/utils/solve_times_lr.csv   runtime log
"""

import os
import time
import math
from collections import Counter
import numpy as np
import pandas as pd
import gurobipy as gp
from gurobipy import GRB
from pathlib import Path
from mnl_utils import greedy_open

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE        = Path(__file__).resolve().parent
SAO_PAULO    = _HERE.parent
RESULTS_UTIL = SAO_PAULO / "results" / "utils"
RESULTS_OUT  = SAO_PAULO / "results"
DATA_XLSX    = SAO_PAULO / "data" / "data.xlsx"

LAT_KM = 111.0   # km per degree latitude

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
G2_FACTOR  = 3     # G1 cells per side for locker grid  → ~9 km² per cell
G3_FACTOR  = 5     # G1 cells per side for small hub grid → ~25 km² per cell

P          = int(os.environ.get("MNL_P",     7))   # lockers to open
# Small-hub budget: default = P ⇒ up to one hub per locker ⇒ the cap never binds,
# so the SOLVER chooses how many hubs to open, driven purely by the hub cost.
P_HUB      = int(os.environ.get("MNL_P_HUB", P))  # max small hubs (default: free)

# Output naming.  The default run is the final two-echelon model; the dashboard
# also keeps two earlier variants as a trace, regenerated with:
#   step 1 (bug)  → MNL_ROUTING_BY_DEMAND=1 LR_OUT_PREFIX=lr_old \
#                   LR_METHOD_LABEL="LRP-MNL — 1. routing double-count (clusters)"
#   step 2 (7 hubs) → MNL_COST_PER_KM_HUB=1e9 LR_OUT_PREFIX=lr_bal \
#                   LR_METHOD_LABEL="LRP-MNL — 2. coherent costs (7 hubs)"
#   LR_OUT_PREFIX    : filename prefix for results/lockers/hubs/fleet
#   LR_METHOD_LABEL  : label written to the runtime log
OUT_PREFIX   = os.environ.get("LR_OUT_PREFIX",   "lr_2ech")
METHOD_LABEL = os.environ.get(
    "LR_METHOD_LABEL", "LRP-MNL — 3. two-echelon hubs (final)"
)

# MNL utility parameters
ALPHA      = float(os.environ.get("MNL_ALPHA", 2.0))   # Huff distance-decay exponent
# A_HUFF calibration (São Paulo parcel lockers).  U0_FORM=1 fixed by CC.
#   u_ij = A_HUFF / d_ij^ALPHA ;  locker MNL share = u/(u+1).
#   Bigger A_HUFF ⇒ wider capture radius (more share at distance).
#   A_HUFF=5  → 83% @1km, 56% @2km, 36% @3km, 17% @5km  (narrow — old)
#   A_HUFF=12 → 92% @1km, 75% @2km, 57% @3km, 32% @5km  (mid — between MILP & old LRP)
# The Exact-MILP model uses a much smaller outside option, so it captures ~71% of
# *every* zone (radius "too wide"); A_HUFF here sets a coherent middle ground.
A_HUFF     = float(os.environ.get("MNL_A_HUFF", 12.0))
U0_FORM    = 1.0    # outside-option utility — must stay 1.0 for CC formulation validity

# Distance cutoff: only model X_ij for zone-locker pairs within this range.
# Beyond MAX_DIST_KM, u_ij ~ 1/d^2 < 1% of nearest candidate → negligible.
MAX_DIST_KM = 12.0   # ignore zone-locker pairs beyond this distance
MIN_DIST_KM = 0.15  # clamp to avoid u_ij → ∞ when zone centroid ≈ G2 centroid

# ---------------------------------------------------------------------------
# Cost parameters — calibrated for São Paulo last-mile market (2024-2025)
# Reference currency: BRL (Brazilian Real).  1 EUR ≈ 5.5 BRL.
#
# COST_PER_KM (ρ) multiplies c_ij = l_ij·√(A_j·η_i) in the objective.
# c_ij units are km·√(parcel), so ρ·c_ij is BRL·√(parcel) — a BHH approximation
# artefact. All costs below are in BRL/day so their *relative* scale is correct.
# Exact unit reconciliation requires the professor's full BHH formula.
#
# Breakeven capture distance (ρ·c_ij = COST_UNCAPTURED):
#   d* = COST_UNCAPTURED / (ρ · √(A_j·η_i))
#   With A_j=8.2, η=5: d* ≈ 20/(0.7·6.4) ≈ 4.5 km
#   → model captures demand from zones within ~4.5 km; ignores zones farther away.
# ---------------------------------------------------------------------------
COST_PER_KM     = float(os.environ.get("MNL_COST_PER_KM", 0.30))  # ρ [BRL/km]; set 0 → no routing cost
# Second-echelon transport: daily van/bike tour cost from a small hub to each
# locker it supplies, ≈ ρ2 · distance(hub, locker).  This makes "open another hub"
# vs "deliver farther from an existing hub" an endogenous trade-off, so one hub
# can cover several lockers.  MAX_DIST_HUB_KM caps how far a hub may supply a locker.
COST_PER_KM_HUB = float(os.environ.get("MNL_COST_PER_KM_HUB", 100.0))  # ρ2 [BRL/km/day] (≈100 → 2 hubs)
MAX_DIST_HUB_KM = float(os.environ.get("MNL_MAX_DIST_HUB_KM", 30.0))   # hub→locker reach [km]
F_LOCKER        = 150.0   # locker daily rental + electricity + maintenance [BRL/day]
# A small hub is a staffed micro-depot (≈300 m² rent + 2 handlers + handling
# equipment), MUCH more expensive to open than an automated parcel locker.
F_HUB           = float(os.environ.get("MNL_F_HUB", 2000.0))  # small hub daily cost [BRL/day]
A_VEHICLE       = 200.0   # motorcycle courier: salary + fuel (fixed part) [BRL/day]

# ---------------------------------------------------------------------------
# One-time OPENING cost (CapEx) — in addition to the daily OpEx above.
# F_LOCKER / F_HUB are recurring daily costs; OPEN_* are paid ONCE when a site
# is opened (hardware purchase + installation / fit-out).  Because the whole
# objective is expressed in BRL/day, the CapEx is amortised over AMORT_DAYS
# (asset lifetime) and the resulting daily-equivalent is added to the objective:
#       daily opening charge = OPEN_* / AMORT_DAYS .
# Shorten AMORT_DAYS to make opening cost weigh more per day.
# ---------------------------------------------------------------------------
OPEN_LOCKER     = 20000.0   # one-time locker unit purchase + install [BRL]
OPEN_HUB        = float(os.environ.get("MNL_OPEN_HUB", 150000.0))  # one-time small-hub fit-out [BRL]
AMORT_DAYS      = 730.0     # amortisation horizon [days] (≈2 operational years)
OPEN_LOCKER_DAY = OPEN_LOCKER / AMORT_DAYS   # daily-equivalent CapEx, locker
OPEN_HUB_DAY    = OPEN_HUB    / AMORT_DAYS   # daily-equivalent CapEx, small hub
Q_CAPACITY      = 80.0    # realistic throughput for urban motorcycle courier [parcels/day]
# Single external big hub: its inbound flux is a multiple of total demand.
# Factor > 1 ⇒ the big hub can always supply the whole city ⇒ BCAP never binds.
BIG_HUB_FLUX_FACTOR = 1.5
# Small hub throughput: a 300 m² micro-fulfilment depot dispatching to lockers by
# bike/car can handle ~3000 parcels/day.  With ≤ P_HUB=3 hubs this caps total
# capacity at 9000 < total demand → HCAP is a *real* constraint that also nudges
# lockers to spread across hubs instead of cramming a single dense block.
CAP_HUB_OVERRIDE = 3000.0  # coherent micro-hub throughput [parcels/day]; None = total_demand
# COST_UNCAPTURED: opportunity cost of one parcel delivered by competitor instead of locker.
# Home delivery SP (Loggi/Flash): R$10–15/parcel → use R$20 to incentivize capture
# up to breakeven distance of ~4.5 km.  Ask professor for authoritative C0 value.
COST_UNCAPTURED = 20.0    # [BRL·√(parcel) "equivalent" per uncaptured parcel unit]

# L: extra weight on the UNCAPTURED-demand term (md "step 1").
# The uncaptured penalty becomes  L · COST_UNCAPTURED · Σ ω_i Z_i.
# Raising L gives lost demand more importance → the P lockers spread to cover more
# distinct demand instead of clustering.  L = 1 reproduces the previous behaviour.
#   variant run → MNL_L=3 LR_OUT_PREFIX=lr_L \
#                 LR_METHOD_LABEL="LRP-MNL (with cost on lost market share)"
L = float(os.environ.get("MNL_L", 1.0))   # weight on uncaptured demand (≥ 1)

# ---------------------------------------------------------------------------
# Routing weighting — HYPOTHESIS / FIX (lockers clustering at the periphery)
# ---------------------------------------------------------------------------
# The BHH cost  c_ij = ρ·l_ij·√(A_j·η_i)  is ALREADY a *total* tour cost for the
# zone, because √(A_j·η_i) ≈ √(number of parcels in the cell).  Multiplying it a
# second time by ω_i in the objective double-counts demand: the routing penalty
# to fully serve a zone then scales as ω_i^1.5 while the capture reward
# (COST_UNCAPTURED·ω_i) is only linear in ω_i.  Dense central zones therefore look
# "too expensive to serve", so the optimiser parks the P lockers in low-density
# cells at the city edge (observed: all lockers at the northern extremity, far
# from the demand-weighted centroid) and market share even DROPS as P grows.
#
# Correct BHH reading: weight the routing term by the captured *share* only
# (c_ij·x_ij), NOT by ω_i.  Then routing ∝ √ω_i and reward ∝ ω_i, so dense demand
# becomes attractive and lockers move onto the demand.  Set MNL_ROUTING_BY_DEMAND=1
# to restore the old (double-counted) behaviour that clustered lockers at the edge.
ROUTING_WEIGHT_BY_DEMAND = os.environ.get("MNL_ROUTING_BY_DEMAND", "0") == "1"

# Solver
MIP_GAP           = float(os.environ.get("MNL_MIP_GAP",     0.05))
TIME_LIMIT        = int(os.environ.get("MNL_TIME_LIMIT",    7200))
NO_PROGRESS_LIMIT = int(os.environ.get("MNL_NO_PROGRESS",   3600))


# ===========================================================================
# 1. Load G1 demand data
# ===========================================================================
print("=" * 60)
print(f"LRP-MNL  |  P = {P} lockers  |  P_HUB = {P_HUB} small hubs")
print("=" * 60)
print("\n[1] Loading G1 demand data …")

grids_raw = pd.read_csv(RESULTS_UTIL / "df_grids.csv")
# Normalise: unnamed index column → "id"
if "id" not in grids_raw.columns:
    grids_raw = grids_raw.rename(columns={grids_raw.columns[0]: "id"})
grids_raw["id"] = grids_raw["id"].astype(int)

zones_df = pd.read_csv(RESULTS_UTIL / "zone_demand.csv")
zones_df["zone_id"] = zones_df["zone_id"].astype(int)

I  = sorted(zones_df["zone_id"].tolist())
w  = dict(zip(zones_df["zone_id"], zones_df["demand"]))
total_demand = sum(w.values())

# G1 cell size
g1_dlat = (grids_raw["maxLat"]  - grids_raw["minLat"]).abs().median()
g1_dlon = (grids_raw["maxLong"] - grids_raw["minLong"]).abs().median()

# G1 cell centroids (all cells, not just demand zones)
g1_centroids = {
    int(r["id"]): (
        (r["minLat"] + r["maxLat"]) / 2,
        (r["minLong"] + r["maxLong"]) / 2,
    )
    for _, r in grids_raw.iterrows()
}

# Arrays for demand zones
zone_lat = np.array([g1_centroids[i][0] for i in I])
zone_lon = np.array([g1_centroids[i][1] for i in I])

lat_mean = zone_lat.mean()
lon_km   = LAT_KM * math.cos(math.radians(lat_mean))

g1_cell_area = abs(g1_dlat) * LAT_KM * abs(g1_dlon) * lon_km   # km²
eta_arr = np.array([w[i] / g1_cell_area for i in I])            # parcels/km²

print(f"  {len(I)} demand zones  |  G1 cell ≈ {g1_cell_area:.3f} km²  |  "
      f"total demand = {total_demand:,.1f}")


# ===========================================================================
# 2. Build G2 (locker) and G3 (small hub) grids
# ===========================================================================
print("\n[2] Building G2 and G3 grids …")

def build_grid(grids_df, factor, dlat, dlon, demand_ids):
    """
    Merge G1 cells into factor×factor coarse cells.
    Returns:
      cells_df     : DataFrame with coarse cell geometry, centroids, area_km2
      g1_to_coarse : dict {g1_id (int) → coarse_id (str)}
    """
    demand_set = set(demand_ids)
    lat_origin = grids_df["minLat"].min()
    lon_origin = grids_df["minLong"].min()
    step_lat = factor * dlat
    step_lon = factor * dlon

    df = grids_df.copy()
    eps = 1e-9
    df["ci"] = np.floor((df["minLat"] - lat_origin + eps) / step_lat).astype(int)
    df["cj"] = np.floor((df["minLong"] - lon_origin + eps) / step_lon).astype(int)
    df["coarse_id"] = "g" + df["ci"].astype(str) + "_" + df["cj"].astype(str)

    # G1-to-coarse mapping
    g1_to_coarse = dict(zip(df["id"].astype(int), df["coarse_id"]))

    # Aggregate
    coarse = (
        df.groupby("coarse_id", sort=False)
        .agg(
            minLat=("minLat", "min"),  maxLat=("maxLat", "max"),
            minLong=("minLong", "min"), maxLong=("maxLong", "max"),
            g1_ids=("id", list),
        )
        .reset_index()
    )
    # Keep only cells with at least one demand zone
    coarse["has_demand"] = coarse["g1_ids"].apply(
        lambda ids: any(int(i) in demand_set for i in ids)
    )
    coarse = coarse[coarse["has_demand"]].copy().reset_index(drop=True)

    lat_c = (coarse["minLat"] + coarse["maxLat"]) / 2
    lon_c = (coarse["minLong"] + coarse["maxLong"]) / 2
    coarse["centroid_lat"] = lat_c
    coarse["centroid_lon"] = lon_c
    coarse["area_km2"] = (
        (coarse["maxLat"]  - coarse["minLat"]).abs()  * LAT_KM *
        (coarse["maxLong"] - coarse["minLong"]).abs() *
        LAT_KM * np.cos(np.radians(lat_c))
    )

    # Remove g1_ids from final df (not needed further)
    cells_df = coarse.drop(columns=["has_demand", "g1_ids"]).reset_index(drop=True)
    return cells_df, g1_to_coarse


g2_df, g1_to_g2 = build_grid(grids_raw, G2_FACTOR, g1_dlat, g1_dlon, I)
g3_df, g1_to_g3 = build_grid(grids_raw, G3_FACTOR, g1_dlat, g1_dlon, I)

# Build G2→G3 parent mapping via G1
g2_to_g3 = {}
for i in I:
    g2_id = g1_to_g2.get(i)
    g3_id = g1_to_g3.get(i)
    if g2_id and g3_id:
        g2_to_g3[g2_id] = g3_id   # deterministic (G3 ⊃ G2 ⊃ G1)

J = [j for j in g2_df["coarse_id"] if j in g2_to_g3]
H_set = set(g2_to_g3[j] for j in J)
H = [h for h in g3_df["coarse_id"] if h in H_set]

# Index lookups
g2_meta = g2_df.set_index("coarse_id")
g3_meta = g3_df.set_index("coarse_id")

g2_lat_arr  = np.array([g2_meta.loc[j, "centroid_lat"] for j in J])
g2_lon_arr  = np.array([g2_meta.loc[j, "centroid_lon"] for j in J])
g2_area_arr = np.array([g2_meta.loc[j, "area_km2"]     for j in J])

# G3 children map: h → list of G2 locker ids (kept for warm start / reference)
g3_children = {h: [] for h in H}
for j in J:
    g3_children[g2_to_g3[j]].append(j)

# ── Flexible hub→locker candidate pairs (2nd echelon) ───────────────────────
# A locker may be supplied by any open hub within MAX_DIST_HUB_KM (its own G3
# parent is always allowed).  hub_dist[(h, j)] = van-tour distance [km].
g3_lat = {h: float(g3_meta.loc[h, "centroid_lat"]) for h in H}
g3_lon = {h: float(g3_meta.loc[h, "centroid_lon"]) for h in H}
g2_lat = dict(zip(J, g2_lat_arr))
g2_lon = dict(zip(J, g2_lon_arr))

hub_dist  = {}                 # (h, j) → km
cand_hubs = {j: [] for j in J}  # j → list of candidate hubs
for j in J:
    for h in H:
        dkm = math.hypot((g3_lat[h] - g2_lat[j]) * LAT_KM,
                         (g3_lon[h] - g2_lon[j]) * lon_km)
        if dkm <= MAX_DIST_HUB_KM or h == g2_to_g3[j]:
            hub_dist[h, j] = dkm
            cand_hubs[j].append(h)

print(f"  G2 locker candidates : {len(J)} cells  "
      f"(~{g2_area_arr.mean():.1f} km² per cell, factor={G2_FACTOR}×{G2_FACTOR})")
print(f"  G3 hub   candidates  : {len(H)} cells  "
      f"(~{g3_df['area_km2'].mean():.1f} km² per cell, factor={G3_FACTOR}×{G3_FACTOR})")
print(f"  Hub→locker pairs (≤{MAX_DIST_HUB_KM:g} km) : {len(hub_dist)}  "
      f"(ρ2={COST_PER_KM_HUB:g} BRL/km/day)")


# ===========================================================================
# 3. Big hub (single, external, fixed)
# ===========================================================================
# We model ONE big hub placed arbitrarily outside the demand area.  It is
# assumed to receive a known inbound flux that exceeds the city's total
# demand, so the big hub never limits how much we can capture (BCAP is
# non-binding by construction).  We only optimise the location of the small
# hubs and lockers inside the city; the big hub is a fixed upstream source.
print("\n[3] Placing single external big hub …")

CAP_BIG_HUB = total_demand * BIG_HUB_FLUX_FACTOR   # flux > total demand ⇒ BCAP slack

# Arbitrary location: just north of the demand bounding box, centred E–W.
_bb_min_lat = float(grids_raw["minLat"].min())
_bb_max_lat = float(grids_raw["maxLat"].max())
_bb_min_lon = float(grids_raw["minLong"].min())
_bb_max_lon = float(grids_raw["maxLong"].max())
_margin_lat = 0.10 * (_bb_max_lat - _bb_min_lat)
big_hub = {
    "hub_id":   "BIG_HUB",
    "lat":      _bb_max_lat + _margin_lat,        # outside (north of) the zone
    "lon":      (_bb_min_lon + _bb_max_lon) / 2,  # centred east–west
    "capacity": CAP_BIG_HUB,
}
print(f"  1 big hub @ ({big_hub['lat']:.4f}, {big_hub['lon']:.4f}) — external")
print(f"  flux = {CAP_BIG_HUB:,.0f} parcels/day "
      f"(= {BIG_HUB_FLUX_FACTOR:g}× total demand → BCAP non-binding)")

# Small hub capacity: each hub handles its share of the big hub flow.
# Default = total_demand (no individual hub limit; BCAP is the binding constraint).
# Set CAP_HUB_OVERRIDE to a float to use a real hub capacity.
CAP_HUB = float(CAP_HUB_OVERRIDE) if CAP_HUB_OVERRIDE is not None else total_demand
print(f"  Small hub capacity (CAP_HUB) = {CAP_HUB:,.0f} parcels/day "
      f"({'override' if CAP_HUB_OVERRIDE else 'default = total demand'})")

print(f"  Costs — locker: {F_LOCKER:.0f} BRL/day OpEx + {OPEN_LOCKER:,.0f} BRL CapEx "
      f"(→ {OPEN_LOCKER_DAY:.1f}/day over {AMORT_DAYS:.0f}d)")
print(f"  Costs — hub   : {F_HUB:.0f} BRL/day OpEx + {OPEN_HUB:,.0f} BRL CapEx "
      f"(→ {OPEN_HUB_DAY:.1f}/day over {AMORT_DAYS:.0f}d)")
print(f"  Uncaptured weight L = {L:g}  → effective penalty "
      f"{L * COST_UNCAPTURED:g} BRL per uncaptured parcel")


# ===========================================================================
# 4. Distances (G1→G2), MNL utilities, BHH routing costs
# ===========================================================================
print("\n[4] Computing distances, utilities and routing costs …")

dlat_mat = np.subtract.outer(zone_lat, g2_lat_arr) * LAT_KM   # (|I|, |J|)
dlon_mat = np.subtract.outer(zone_lon, g2_lon_arr) * lon_km
dist_mat = np.sqrt(dlat_mat**2 + dlon_mat**2)                  # km
# Clamp to MIN_DIST_KM: G1 centroids can be very close to G2 centroids
# (G1 cell inside G2 cell → centroid dist < half G2 side ≈ 1.5 km).
# Without this, u_ij → 1/d² → ∞, causing numerical instability.
dist_mat = np.clip(dist_mat, MIN_DIST_KM, None)

# MNL utility: u_ij = A_HUFF / d_ij^ALPHA   (π_ij in .tex notation with μ_i=1)
util_mat = A_HUFF / dist_mat**ALPHA                            # (|I|, |J|)

# BHH routing cost: c_ij = l_ij * sqrt(A_j * η_i)
# A_j = G2 cell area [km²]  (confirmed by professor: "Its the area")
bhh_mat = np.sqrt(g2_area_arr[np.newaxis, :] * eta_arr[:, np.newaxis])
c_mat   = COST_PER_KM * dist_mat * bhh_mat                     # (|I|, |J|)

print(f"  Distance range : [{dist_mat.min():.2f}, {dist_mat.max():.2f}] km")
print(f"  u_ij range     : [{util_mat.min():.2e},  {util_mat.max():.2e}]")
print(f"  c_ij range     : [{c_mat.min():.2f},   {c_mat.max():.2f}]")

# Distance cutoff: only model X_ij for pairs within MAX_DIST_KM
close_mask = dist_mat <= MAX_DIST_KM   # (|I|, |J|) bool
n_pairs = int(close_mask.sum())
print(f"  Close pairs (≤{MAX_DIST_KM} km) : {n_pairs}  "
      f"({n_pairs / (len(I)*len(J)):.1%} of {len(I)*len(J)})")

# Build sparse dictionaries for Gurobi
I_idx = {i: ii for ii, i in enumerate(I)}
J_idx = {j: jj for jj, j in enumerate(J)}

u   = {}   # (zone_id, locker_id) → utility
c   = {}   # (zone_id, locker_id) → routing cost
close_J = {i: [] for i in I}   # i → list of close locker ids

for ii, i in enumerate(I):
    for jj, j in enumerate(J):
        if close_mask[ii, jj]:
            u[i, j] = float(util_mat[ii, jj])
            c[i, j] = float(c_mat[ii, jj])
            close_J[i].append(j)


# ===========================================================================
# 5. Charnes-Cooper bounds (Z_bar, Z_under) with u0 = U0_FORM = 1
# ===========================================================================
print("\n[5] Computing CC bounds (u0 = 1) …")

p_cap = min(P, len(J))
util_sorted_desc = np.sort(util_mat, axis=1)[:, ::-1]
Z_under_arr = 1.0 / (util_sorted_desc[:, :p_cap].sum(axis=1) + U0_FORM)
Z_under = dict(zip(I, Z_under_arr))

# Z_bar must be 1.0 because the distance cutoff (MAX_DIST_KM) excludes far
# pairs from the CC constraint.  When no close locker is open for zone i the
# CC forces Z_i = 1; any Z_bar < 1 would make that infeasible.
Z_bar = {i: 1.0 for i in I}

n_empty = sum(1 for i in I if not close_J[i])
print(f"  Z_bar   = 1.0 (fixed — distance cutoff; {n_empty} zones have no close pair)")
print(f"  Z_under ∈ [{Z_under_arr.min():.6f}, {Z_under_arr.max():.4f}]")

M_big = math.ceil(total_demand / Q_CAPACITY) + 1
print(f"  Big-M (fleet) : {M_big}")


# ===========================================================================
# 6. Build Gurobi model
# ===========================================================================
print("\n[6] Building MILP …")

model = gp.Model("LRP_MNL")
model.Params.OutputFlag  = 1
model.Params.MIPGap      = MIP_GAP
model.Params.TimeLimit   = TIME_LIMIT
model.Params.MIPFocus    = 1
model.Params.Cuts        = 2
model.Params.Heuristics  = 0.3

# ── Variables ────────────────────────────────────────────────────────────────
y = model.addVars(J, vtype=GRB.BINARY,  name="y")           # open locker
q = model.addVars(J, vtype=GRB.INTEGER, lb=0, ub=M_big, name="q")   # fleet
v = model.addVars(H, vtype=GRB.BINARY,  name="v")           # open small hub
Z = model.addVars(I, lb=0.0, name="Z")                      # CC variable

# X[i,j] only for close pairs (distance cutoff)
X = model.addVars(
    [(i, j) for i in I for j in close_J[i]],
    lb=0.0, name="X"
)

# a[h,j] ∈ [0,1] : share of locker j supplied by hub h (continuous → no extra
# binaries; transport minimisation drives it to the nearest open hub).
a = model.addVars(list(hub_dist.keys()), lb=0.0, ub=1.0, name="a")

for i in I:
    Z[i].LB = Z_under[i]
    Z[i].UB = 1.0   # see Z_bar comment above

# ── Objective ─────────────────────────────────────────────────────────────────
model.setObjective(
    # Daily OpEx (recurring) ────────────────────────────────────────────────
    gp.quicksum(F_LOCKER * y[j] for j in J)
    + gp.quicksum(F_HUB * v[h] for h in H)
    # One-time opening CapEx, amortised to a daily charge ───────────────────
    + gp.quicksum(OPEN_LOCKER_DAY * y[j] for j in J)
    + gp.quicksum(OPEN_HUB_DAY * v[h] for h in H)
    + gp.quicksum(A_VEHICLE * q[j] for j in J)
    # Routing: weight by captured share x_ij = u_ij·X_ij only (correct BHH).
    # The extra ·w[i] (ROUTING_WEIGHT_BY_DEMAND=True) double-counts demand — see
    # the "Routing weighting" hypothesis block above.
    + gp.quicksum(c[i, j] * u[i, j] * X[i, j] * (w[i] if ROUTING_WEIGHT_BY_DEMAND else 1.0)
                  for i in I for j in close_J[i])
    # 2nd-echelon hub→locker tour cost: ρ2 · distance · a[h,j]
    + gp.quicksum(COST_PER_KM_HUB * hub_dist[h, j] * a[h, j] for (h, j) in hub_dist)
    # Uncaptured-demand penalty, weighted by L (md "step 1"): L·C0·Σ ω_i Z_i
    + gp.quicksum(L * COST_UNCAPTURED * w[i] * Z[i] for i in I),
    GRB.MINIMIZE,
)

# ── (CC) Charnes-Cooper ───────────────────────────────────────────────────────
# Z_i = 1/(S_i + 1)  →  Σ_k u_ik·X_ik + Z_i = 1
# Only close pairs contribute; far pairs have X_ij = 0 (not modelled)
for i in I:
    model.addConstr(
        gp.quicksum(u[i, j] * X[i, j] for j in close_J[i]) + Z[i] == 1,
        name=f"CC_{i}",
    )

# ── (M1–M3) McCormick ────────────────────────────────────────────────────────
for i in I:
    for j in close_J[i]:
        model.addConstr(X[i, j] <= y[j],              name=f"M1_{i}_{j}")
        model.addConstr(X[i, j] <= Z[i],              name=f"M2_{i}_{j}")
        model.addConstr(X[i, j] >= Z[i] - (1-y[j]),  name=f"M3_{i}_{j}")

# ── (CAP) Courier capacity ────────────────────────────────────────────────────
# Demand allocated to locker j = Σ_i ω_i · u_ij · X_ij  ≤  Q · q_j
for j in J:
    close_I_j = [i for i in I if j in close_J[i]]
    if close_I_j:
        model.addConstr(
            gp.quicksum(w[i] * u[i, j] * X[i, j] for i in close_I_j)
            <= Q_CAPACITY * q[j],
            name=f"CAP_{j}",
        )
    else:
        # No close demand zone → force locker closed
        model.addConstr(y[j] == 0, name=f"NODEM_{j}")

# ── (LNK) Fleet-facility link ─────────────────────────────────────────────────
for j in J:
    model.addConstr(q[j] <= M_big * y[j], name=f"LNK_{j}")

# ── (ASSIGN) Flexible 2nd echelon: each open locker → exactly one open hub ─────
# Replaces the old fixed parent nesting (HPAR).  A locker may attach to any open
# hub within MAX_DIST_HUB_KM; the van-tour cost ρ2·dist is paid in the objective,
# so one hub can cover several lockers.
for j in J:
    model.addConstr(
        gp.quicksum(a[h, j] for h in cand_hubs[j]) == y[j], name=f"ASSIGN_{j}"
    )
    for h in cand_hubs[j]:
        model.addConstr(a[h, j] <= v[h], name=f"ALINK_{h}_{j}")

# (HCAP small-hub flow capacity dropped: with flexible assignment it would be
#  bilinear a·X.  Total throughput is still bounded by the big hub, BCAP below.)

# ── (BCAP) Big hub total capacity ─────────────────────────────────────────────
# Total captured = Σ_i ω_i · (1 - Z_i) ≤ CAP_BIG_HUB
model.addConstr(
    gp.quicksum(w[i] * (1 - Z[i]) for i in I) <= CAP_BIG_HUB,
    name="BCAP",
)

# ── (BDG) Budget constraints ──────────────────────────────────────────────────
model.addConstr(gp.quicksum(y[j] for j in J) == P,       name="budget_lockers")
model.addConstr(gp.quicksum(v[h] for h in H) <= P_HUB,   name="budget_hubs")

model.update()   # flush lazy additions before querying counts
print(f"  Variables   : {model.NumVars:,}")
print(f"  Constraints : {model.NumConstrs:,}")


# ===========================================================================
# 7. Greedy warm start
# ===========================================================================
print("\n[7] Computing greedy warm start …")
_t_ws = time.time()
u0_g = {i: U0_FORM for i in I}
# Only pass close-pair utilities to greedy (consistent with model)
u_sparse = {(i, j): u[i, j] for i in I for j in close_J[i]}
# Also include far pairs with tiny utility so greedy can evaluate all J
for i in I:
    for j in J:
        if (i, j) not in u_sparse:
            u_sparse[i, j] = 1e-9

greedy_lockers, _ = greedy_open(P, I, J, w, u_sparse, u0_g)
print(f"  Greedy done in {time.time()-_t_ws:.1f}s → {greedy_lockers[:5]}…")

# Warm start = the P greedy lockers, supplied by a hub set chosen by a greedy
# facility-location heuristic.  Gurobi does NOT change the hub count of the warm
# start within the time limit (it neither closes nor opens hubs), so the warm
# start must already use the ~right number of hubs for the current ρ2.
warm_lockers = greedy_lockers[:P]

cand_set = sorted({h for j in warm_lockers for h in cand_hubs[j]})

def _assign_cost(open_set):
    """Each warm locker → nearest open hub; return (assignment, transport cost)."""
    assign, tr = {}, 0.0
    for j in warm_lockers:
        h = min((h for h in open_set if h in cand_hubs[j]),
                key=lambda h: hub_dist[h, j])
        assign[j] = h
        tr += hub_dist[h, j]
    return assign, COST_PER_KM_HUB * tr

# Base: minimal set cover so every warm locker is reachable
hub_to_warm = {}
for j in warm_lockers:
    for h in cand_hubs[j]:
        hub_to_warm.setdefault(h, set()).add(j)
uncovered, chosen_hubs = set(warm_lockers), set()
while uncovered:
    best_h = max(hub_to_warm, key=lambda h: len(hub_to_warm[h] & uncovered))
    chosen_hubs.add(best_h)
    uncovered -= hub_to_warm[best_h]

# Greedily OPEN hubs while it lowers (hub cost + transport) — matches the ρ2 regime
_HUB_DAY = F_HUB + OPEN_HUB_DAY
_cur = len(chosen_hubs) * _HUB_DAY + _assign_cost(chosen_hubs)[1]
while True:
    best_h, best_cost = None, _cur
    for h in cand_set:
        if h in chosen_hubs:
            continue
        c2 = (len(chosen_hubs) + 1) * _HUB_DAY + _assign_cost(chosen_hubs | {h})[1]
        if c2 < best_cost - 1e-6:
            best_cost, best_h = c2, h
    if best_h is None:
        break
    chosen_hubs.add(best_h)
    _cur = best_cost

warm_assign, _ = _assign_cost(chosen_hubs)

print(f"  Warm-start hubs  ({len(chosen_hubs)}): {chosen_hubs}")
print(f"  Warm-start lockers: {warm_lockers}")
for j in J:
    y[j].Start = 1.0 if j in warm_lockers else 0.0
for h in H:
    v[h].Start = 1.0 if h in chosen_hubs else 0.0
for (h, j) in hub_dist:
    a[h, j].Start = 1.0 if (j in warm_assign and warm_assign[j] == h) else 0.0


# ===========================================================================
# 8. Solve with no-progress callback
# ===========================================================================
_cb_state = {
    "best_obj":         float("inf"),
    "last_incumbent_t": None,
    "n_incumbents":     0,
}

def _cb(cb_model, where):
    if where == GRB.Callback.MIPSOL:
        obj = cb_model.cbGet(GRB.Callback.MIPSOL_OBJ)
        _cb_state["n_incumbents"]    += 1
        _cb_state["last_incumbent_t"] = time.time()
        if obj < _cb_state["best_obj"]:
            _cb_state["best_obj"] = obj
            print(f"  ✓ Incumbent #{_cb_state['n_incumbents']}: {obj:,.2f}")
    if where in (GRB.Callback.MIP, GRB.Callback.MIPNODE):
        if (_cb_state["last_incumbent_t"] and
                time.time() - _cb_state["last_incumbent_t"] > NO_PROGRESS_LIMIT):
            print(f"\n  ⏹  No new incumbent for {NO_PROGRESS_LIMIT}s → stopping.")
            cb_model.terminate()

print(f"\n[8] Solving …")
_t0 = time.time()
_cb_state["last_incumbent_t"] = _t0
model.optimize(_cb)
_t_elapsed = time.time() - _t0


# ===========================================================================
# 9. Extract results
# ===========================================================================
_ACCEPTABLE = {GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.INTERRUPTED}
stop_reason = {
    GRB.OPTIMAL:     "optimal",
    GRB.TIME_LIMIT:  f"time limit ({TIME_LIMIT}s)",
    GRB.INTERRUPTED: f"no-progress stop ({NO_PROGRESS_LIMIT}s)",
}.get(model.Status, f"status={model.Status}")

if model.Status not in _ACCEPTABLE or model.SolCount == 0:
    print(f"\nNo feasible solution ({stop_reason}). Exiting.")
    raise SystemExit(1)

open_lockers = [j for j in J if y[j].X > 0.5]
open_hubs    = [h for h in H if v[h].X > 0.5]
fleet        = {j: int(round(q[j].X)) for j in open_lockers}
mip_gap      = model.MIPGap

# Resolve the flexible hub→locker assignment: each open locker → its dominant hub.
assigned_hub = {}
for j in open_lockers:
    best_h, best_share = g2_to_g3.get(j), -1.0
    for h in cand_hubs[j]:
        sh = a[h, j].X
        if sh > best_share:
            best_share, best_h = sh, h
    assigned_hub[j] = best_h
# Total 2nd-echelon distance (van tours) of the chosen solution
hub_tour_km = sum(hub_dist[assigned_hub[j], j] for j in open_lockers)
obj_val      = model.ObjVal

# Cost breakdown of the chosen solution
capex_total = len(open_lockers) * OPEN_LOCKER + len(open_hubs) * OPEN_HUB
opex_daily  = len(open_lockers) * F_LOCKER + len(open_hubs) * F_HUB
open_daily  = len(open_lockers) * OPEN_LOCKER_DAY + len(open_hubs) * OPEN_HUB_DAY

print("\n" + "=" * 60)
print(f"Stop        : {stop_reason}")
print(f"MIP Gap     : {mip_gap:.2%}")
print(f"Objective   : {obj_val:,.2f}")
print(f"Solve time  : {_t_elapsed:.0f}s")
print(f"Open lockers ({len(open_lockers)}) : {open_lockers}")
print(f"Open hubs   ({len(open_hubs)}) : {open_hubs}")
print(f"Hub→locker  : {len(open_lockers)} lockers from {len(open_hubs)} hubs  "
      f"(total van-tour {hub_tour_km:.1f} km, cost {COST_PER_KM_HUB*hub_tour_km:,.0f} BRL/day)")
print(f"Fleet       : {fleet}")
print(f"Opening CapEx (one-time) : {capex_total:,.0f} BRL "
      f"({len(open_lockers)}×{OPEN_LOCKER:,.0f} + {len(open_hubs)}×{OPEN_HUB:,.0f})")
print(f"Fixed daily : {opex_daily:,.0f} OpEx + {open_daily:,.0f} amortised CapEx "
      f"= {opex_daily + open_daily:,.0f} BRL/day")
print("=" * 60)

# ── Market share (MNL with u0 = 1) ────────────────────────────────────────────
rows = []
for i in I:
    S_i  = sum(u.get((i, j), 0.0) * y[j].X for j in open_lockers)
    denom = S_i + U0_FORM
    ms_i  = S_i / denom if denom > 0 else 0.0
    rows.append({
        "zone_id":          i,
        "demand":           round(w[i], 2),
        "S_i":              round(S_i, 8),
        "market_share_pct": round(ms_i * 100, 3),
        "captured":         round(w[i] * ms_i, 2),
    })

df_results     = pd.DataFrame(rows).sort_values("captured", ascending=False)
total_captured = df_results["captured"].sum()
print(f"\nTotal demand   : {total_demand:,.1f}")
print(f"Total captured : {total_captured:,.1f}")
print(f"Market share   : {total_captured / total_demand:.2%}")
print("\nTop 10 zones:")
print(df_results.head(10).to_string(index=False))


# ===========================================================================
# 10. Save results
# ===========================================================================
RESULTS_OUT.mkdir(parents=True, exist_ok=True)

df_results.to_csv(RESULTS_OUT / f"{OUT_PREFIX}_results_P{P}.csv", index=False)

# Lockers: include coordinates + parent hub (for routing display in Streamlit)
locker_coords = pd.DataFrame([
    {
        "candidate_id":  j,
        "centroid_lat":  g2_meta.loc[j, "centroid_lat"],
        "centroid_lon":  g2_meta.loc[j, "centroid_lon"],
        "area_km2":      g2_meta.loc[j, "area_km2"],
        "parent_hub":    assigned_hub.get(j),                 # flexible assignment
        "hub_dist_km":   round(hub_dist[assigned_hub[j], j], 3),
    }
    for j in open_lockers
])
locker_coords.to_csv(RESULTS_OUT / f"{OUT_PREFIX}_lockers_P{P}.csv", index=False)

# Hubs: include coordinates
hub_coords = pd.DataFrame([
    {
        "candidate_id":  h,
        "centroid_lat":  g3_meta.loc[h, "centroid_lat"],
        "centroid_lon":  g3_meta.loc[h, "centroid_lon"],
        "area_km2":      g3_meta.loc[h, "area_km2"],
        "n_lockers":     sum(1 for j in open_lockers if assigned_hub.get(j) == h),
    }
    for h in open_hubs
])
hub_coords.to_csv(RESULTS_OUT / f"{OUT_PREFIX}_hubs_P{P}.csv", index=False)

pd.DataFrame(
    [{"candidate_id": j, "fleet_size": fleet[j]} for j in open_lockers]
).to_csv(RESULTS_OUT / f"{OUT_PREFIX}_fleet_P{P}.csv", index=False)

# Single external big hub location (for the map) — does not depend on P.
pd.DataFrame([{
    "hub_id":   big_hub["hub_id"],
    "lat":      big_hub["lat"],
    "lon":      big_hub["lon"],
    "capacity": big_hub["capacity"],
}]).to_csv(RESULTS_OUT / "lr_big_hub.csv", index=False)

# Candidate grids (cell rectangles) for the map overlay — independent of P.
#   G2 = locker candidate grid, G3 = small-hub candidate grid.
g2_df.to_csv(RESULTS_OUT / "lr_grid_g2.csv", index=False)
g3_df.to_csv(RESULTS_OUT / "lr_grid_g3.csv", index=False)

# Cost parameters — single source of truth for the Streamlit cost panel.
pd.DataFrame([{
    "F_LOCKER":        F_LOCKER,
    "F_HUB":           F_HUB,
    "A_VEHICLE":       A_VEHICLE,
    "OPEN_LOCKER":     OPEN_LOCKER,
    "OPEN_HUB":        OPEN_HUB,
    "AMORT_DAYS":      AMORT_DAYS,
    "OPEN_LOCKER_DAY": OPEN_LOCKER_DAY,
    "OPEN_HUB_DAY":    OPEN_HUB_DAY,
    "COST_UNCAPTURED": COST_UNCAPTURED,
    "L":               L,
    "COST_PER_KM":     COST_PER_KM,
    "COST_PER_KM_HUB": COST_PER_KM_HUB,
    "Q_CAPACITY":      Q_CAPACITY,
    "CAP_HUB":         CAP_HUB,
}]).to_csv(RESULTS_OUT / f"{OUT_PREFIX}_cost_params.csv", index=False)

# Runtime log
_log = RESULTS_UTIL / "solve_times_lr.csv"
_new = pd.DataFrame([{
    "method":           METHOD_LABEL,
    "P":                P,
    "P_hub":            P_HUB,
    "L":                L,
    "solve_time_s":     round(_t_elapsed, 2),
    "objective":        round(obj_val, 2),
    "market_share_pct": round(total_captured / total_demand * 100, 3),
    "capex_brl":        round(capex_total, 0),
    "detail":           f"gap={mip_gap:.2%}  [{stop_reason}]  CapEx={capex_total:,.0f}",
}])
if _log.exists():
    _ex = pd.read_csv(_log)
    if "L" not in _ex.columns:
        _ex["L"] = 1.0   # rows logged before L existed used L = 1
    # Replace the row for this exact (method, P, L) combination
    _ex = _ex[~((_ex["method"] == METHOD_LABEL) & (_ex["P"] == P) & (_ex["L"] == L))]
    _new = pd.concat([_ex, _new], ignore_index=True)
_new.to_csv(_log, index=False)

print(f"\nSaved → {RESULTS_OUT}  (lr_results, lr_lockers, lr_hubs, lr_fleet, "
      f"lr_big_hub, lr_grid_g2/g3)")
print(f"Runtime log → {_log}")
