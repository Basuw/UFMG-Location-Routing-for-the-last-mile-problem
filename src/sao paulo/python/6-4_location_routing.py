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
  UL min  Σ_j f_j·y_j  +  Σ_h F_h·v_h  +  Σ_j a_j·q_j
        + Σ_i Σ_j c_ij·u_ij·X_ij·ω_i        [routing cost]
        + COST_UNCAPTURED · Σ_i ω_i·Z_i      [uncaptured demand]

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
  results/lr_lockers_P{P}.csv        open locker ids + coordinates
  results/lr_hubs_P{P}.csv           open small hub ids + coordinates
  results/lr_fleet_P{P}.csv          fleet sizes per locker
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
P_HUB      = int(os.environ.get("MNL_P_HUB", 3))  # small hubs to open

# MNL utility parameters
ALPHA      = 2.0    # Huff distance-decay exponent
# A_HUFF calibration (São Paulo parcel lockers, 2024):
#   u_ij = A_HUFF / d_ij²   (U0_FORM=1 fixed by CC substitution)
#   At d km, locker MNL share = (A_HUFF/d²) / (A_HUFF/d² + 1)
#   A_HUFF=5 → 83% at 1 km, 56% at 1.5 km, 20% at 3 km, 5% at 10 km
#   Aggregate share with P=7 over ~2100 km² ≈ 5–8% (realistic for nascent service)
A_HUFF     = 5.0
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
COST_PER_KM     = 0.70    # ρ: motorcycle operating cost [BRL/km] (fuel + wear)
F_LOCKER        = 150.0   # locker daily rental + electricity + maintenance [BRL/day]
F_HUB           = 800.0   # small hub: ~300 m² warehouse + 1 handler [BRL/day]
A_VEHICLE       = 200.0   # motorcycle courier: salary + fuel (fixed part) [BRL/day]
Q_CAPACITY      = 80.0    # realistic throughput for urban motorcycle courier [parcels/day]
# CAP_HUB is computed dynamically after loading demand (= total_demand / P_HUB).
# Override here only if you have real hub capacity data.
CAP_HUB_OVERRIDE = None   # set to a float to override, None = use total_demand
# COST_UNCAPTURED: opportunity cost of one parcel delivered by competitor instead of locker.
# Home delivery SP (Loggi/Flash): R$10–15/parcel → use R$20 to incentivize capture
# up to breakeven distance of ~4.5 km.  Ask professor for authoritative C0 value.
COST_UNCAPTURED = 20.0    # [BRL·√(parcel) "equivalent" per uncaptured parcel unit]

# Solver
MIP_GAP           = 0.05
TIME_LIMIT        = 7200
NO_PROGRESS_LIMIT = 3600


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

# G3 children map: h → list of G2 locker ids
g3_children = {h: [] for h in H}
for j in J:
    g3_children[g2_to_g3[j]].append(j)

print(f"  G2 locker candidates : {len(J)} cells  "
      f"(~{g2_area_arr.mean():.1f} km² per cell, factor={G2_FACTOR}×{G2_FACTOR})")
print(f"  G3 hub   candidates  : {len(H)} cells  "
      f"(~{g3_df['area_km2'].mean():.1f} km² per cell, factor={G3_FACTOR}×{G3_FACTOR})")


# ===========================================================================
# 3. Big hub capacity (fixed, from data.xlsx)
# ===========================================================================
print("\n[3] Loading big hub capacity …")
try:
    hubs_df = pd.read_excel(DATA_XLSX, sheet_name="candidates")[
        ["Nome",
         "Capacidade diária para operação de couriers (remessas)",
         "Custo diário para a operação de couriers",
         "Custo por remessa despachada por courier"]
    ].rename(columns={
        "Nome": "hub_id",
        "Capacidade diária para operação de couriers (remessas)": "capacity",
        "Custo diário para a operação de couriers":               "daily_cost",
        "Custo por remessa despachada por courier":               "cost_per_parcel",
    })
    CAP_BIG_HUB = float(hubs_df["capacity"].sum())
    print(f"  {len(hubs_df)} big hubs  |  total capacity = {CAP_BIG_HUB:,.0f} parcels/day")
except Exception as exc:
    CAP_BIG_HUB = total_demand * 10
    print(f"  WARNING: could not load big hubs ({exc}) → BCAP disabled")

# Small hub capacity: each hub handles its share of the big hub flow.
# Default = total_demand (no individual hub limit; BCAP is the binding constraint).
# Set CAP_HUB_OVERRIDE to a float to use a real hub capacity.
CAP_HUB = float(CAP_HUB_OVERRIDE) if CAP_HUB_OVERRIDE is not None else total_demand
print(f"  Small hub capacity (CAP_HUB) = {CAP_HUB:,.0f} parcels/day "
      f"({'override' if CAP_HUB_OVERRIDE else 'default = total demand'})")


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

for i in I:
    Z[i].LB = Z_under[i]
    Z[i].UB = 1.0   # see Z_bar comment above

# ── Objective ─────────────────────────────────────────────────────────────────
model.setObjective(
    gp.quicksum(F_LOCKER * y[j] for j in J)
    + gp.quicksum(F_HUB * v[h] for h in H)
    + gp.quicksum(A_VEHICLE * q[j] for j in J)
    + gp.quicksum(c[i, j] * u[i, j] * X[i, j] * w[i]
                  for i in I for j in close_J[i])
    + gp.quicksum(COST_UNCAPTURED * w[i] * Z[i] for i in I),
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

# ── (HPAR) Locker needs open parent hub ──────────────────────────────────────
for j in J:
    model.addConstr(y[j] <= v[g2_to_g3[j]], name=f"HPAR_{j}")

# ── (HCAP) Small hub capacity ─────────────────────────────────────────────────
for h in H:
    children = g3_children[h]
    if children:
        model.addConstr(
            gp.quicksum(
                w[i] * u[i, j] * X[i, j]
                for j in children
                for i in I if j in close_J[i]
            ) <= CAP_HUB * v[h],
            name=f"HCAP_{h}",
        )

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

# Restrict warm start to P_HUB hub parents so budget_hubs is satisfied.
# Pick the P_HUB most-represented hubs among greedy lockers, then collect
# P lockers from those hubs (greedy order first, then any other J in those hubs).
hub_counts = Counter(g2_to_g3[j] for j in greedy_lockers if j in g2_to_g3)
chosen_hubs = {h for h, _ in hub_counts.most_common(P_HUB)}

# Lockers in chosen hubs, greedy order first, then fill from remaining J
warm_lockers = [j for j in greedy_lockers if g2_to_g3.get(j) in chosen_hubs]
if len(warm_lockers) < P:
    extras = [j for j in J if g2_to_g3.get(j) in chosen_hubs and j not in warm_lockers]
    warm_lockers.extend(extras[:P - len(warm_lockers)])
warm_lockers = warm_lockers[:P]

print(f"  Warm-start hubs  : {chosen_hubs}")
print(f"  Warm-start lockers: {warm_lockers}")
for j in J:
    y[j].Start = 1.0 if j in warm_lockers else 0.0
for h in H:
    v[h].Start = 1.0 if h in chosen_hubs else 0.0


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
obj_val      = model.ObjVal

print("\n" + "=" * 60)
print(f"Stop        : {stop_reason}")
print(f"MIP Gap     : {mip_gap:.2%}")
print(f"Objective   : {obj_val:,.2f}")
print(f"Solve time  : {_t_elapsed:.0f}s")
print(f"Open lockers ({len(open_lockers)}) : {open_lockers}")
print(f"Open hubs   ({len(open_hubs)}) : {open_hubs}")
print(f"Fleet       : {fleet}")
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

df_results.to_csv(RESULTS_OUT / f"lr_results_P{P}.csv", index=False)

# Lockers: include coordinates for Streamlit display
locker_coords = pd.DataFrame([
    {
        "candidate_id":  j,
        "centroid_lat":  g2_meta.loc[j, "centroid_lat"],
        "centroid_lon":  g2_meta.loc[j, "centroid_lon"],
        "area_km2":      g2_meta.loc[j, "area_km2"],
    }
    for j in open_lockers
])
locker_coords.to_csv(RESULTS_OUT / f"lr_lockers_P{P}.csv", index=False)

# Hubs: include coordinates
hub_coords = pd.DataFrame([
    {
        "candidate_id":  h,
        "centroid_lat":  g3_meta.loc[h, "centroid_lat"],
        "centroid_lon":  g3_meta.loc[h, "centroid_lon"],
        "area_km2":      g3_meta.loc[h, "area_km2"],
        "n_lockers":     sum(1 for j in open_lockers if g2_to_g3.get(j) == h),
    }
    for h in open_hubs
])
hub_coords.to_csv(RESULTS_OUT / f"lr_hubs_P{P}.csv", index=False)

pd.DataFrame(
    [{"candidate_id": j, "fleet_size": fleet[j]} for j in open_lockers]
).to_csv(RESULTS_OUT / f"lr_fleet_P{P}.csv", index=False)

# Runtime log
_log = RESULTS_UTIL / "solve_times_lr.csv"
_new = pd.DataFrame([{
    "method":           "LRP-MNL",
    "P":                P,
    "P_hub":            P_HUB,
    "solve_time_s":     round(_t_elapsed, 2),
    "objective":        round(obj_val, 2),
    "market_share_pct": round(total_captured / total_demand * 100, 3),
    "detail":           f"gap={mip_gap:.2%}  [{stop_reason}]",
}])
if _log.exists():
    _ex = pd.read_csv(_log)
    _ex = _ex[~((_ex["method"] == "LRP-MNL") & (_ex["P"] == P))]
    _new = pd.concat([_ex, _new], ignore_index=True)
_new.to_csv(_log, index=False)

print(f"\nSaved → {RESULTS_OUT}  (lr_results, lr_lockers, lr_hubs, lr_fleet)")
print(f"Runtime log → {_log}")
