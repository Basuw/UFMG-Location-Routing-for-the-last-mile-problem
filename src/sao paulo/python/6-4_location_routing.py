"""
6-4_location_routing.py
=======================
Bilevel Location-Routing Problem with MNL demand capture.
Implements the formulation from Documentation/maths/06_Location_Routing.md.

Mathematical formulation
------------------------
The bilevel problem collapses to a single-level program because the LL
optimum (MNL customer choice) is an explicit function of the UL decision y_j:

    x_ij  =  u_ij * y_j / (Σ_k u_ik * y_k  +  1)

After the Charnes-Cooper substitution  Z_i = 1 / (Σ_k u_ik * y_k + 1)
and McCormick linearization  X_ij = y_j * Z_i, the UL becomes:

  min   Σ_j f_j * y_j                               (fixed facility cost)
      + Σ_j a_j * q_j                               (fleet cost)
      + Σ_i Σ_j c_ij * u_ij * X_ij * ω_i           (routing cost)
      + Σ_i ω_i * Z_i                               (uncaptured demand cost)

  s.t.
    (CC)  Σ_k u_ik * X_ik  +  Z_i  =  1            ∀ i    [CC normalisation]
    (M1)  X_ij  ≤  Z_bar_i * y_j                   ∀ i,j  [McCormick 1]
    (M2)  X_ij  ≤  Z_i                              ∀ i,j  [McCormick 2]
    (M3)  X_ij  ≥  Z_i - Z_bar_i*(1-y_j)           ∀ i,j  [McCormick 3]
    (CAP) Σ_i ω_i * u_ij * X_ij  ≤  Q * q_j        ∀ j    [vehicle capacity]
    (LNK) q_j  ≤  M_big * y_j                       ∀ j    [fleet-facility link]
    (BDG) Σ_j y_j  =  P                                    [budget]

  Variables:
    y_j   ∈ {0,1}     open facility j?
    q_j   ∈ Z+        number of vehicles assigned to facility j
    Z_i   ≥ 0         Charnes-Cooper variable, bounded [Z_under_i, Z_bar_i]
    X_ij  ≥ 0         McCormick linearisation of y_j * Z_i

Routing cost approximation (Beardwood-Halton-Hammersley):
    c_ij  ≈  l_ij * sqrt(A_j * η_i)

where:
    l_ij  = distance from zone i to facility j        [km, from data]
    A_j   = service area of facility j                [km² — ⚠ PLACEHOLDER]
    η_i   = delivery density in zone i = ω_i / area_i [parcels/km², computable]

⚠ PARAMETERS REQUIRING PROFESSOR CLARIFICATION — see bottom of file.

Inputs  (produced by 6-1_data_preparation.py):
  results/utils/zone_demand.csv
  results/utils/utility_matrix.csv
  results/utils/df_dist_dcs.csv
  results/utils/df_grids.csv

Outputs:
  results/lr_results_P{P}.csv          one row per zone
  results/lr_lockers_P{P}.csv          open facility ids
  results/lr_fleet_P{P}.csv            fleet sizes per facility
  results/utils/solve_times_lr.csv     runtime log
"""

import os
import time
import math
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

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
P          = int(os.environ.get("MNL_P", 7))
MIP_GAP    = 0.05
TIME_LIMIT = 7200
NO_PROGRESS_LIMIT = 3600

# ── Cost parameters  ⚠ PLACEHOLDER — ask professor for real values ──────────
#
# f_j : fixed cost to open facility j (€ or arbitrary unit)
#   Assumed uniform across all j.  Replace with a per-facility vector if
#   different sites have different costs.
F_FIXED = 1_000.0          # ⚠ PLACEHOLDER

# a_j : cost per vehicle assigned to facility j (€/vehicle/day)
#   Assumed uniform.
A_VEHICLE = 500.0          # ⚠ PLACEHOLDER

# Q : vehicle capacity (demand units = parcels, or kg, or whatever ω_i is in)
Q_CAPACITY = 50.0          # ⚠ PLACEHOLDER

# cost_per_unit_distance : scaling factor for routing cost (€/km)
#   Multiplied into c_ij before the optimisation.
COST_PER_KM = 1.0          # ⚠ PLACEHOLDER  (set to 1 → c_ij in km)

# A_FACILITY : service area of each facility [km²]
#   In BHH the route covers the Voronoi cell of the facility.  We use a
#   uniform placeholder; ideally compute Voronoi areas from candidate coords.
#   ⚠ CLARIFICATION NEEDED: is A_j the Voronoi cell area of facility j,
#   or the attractiveness parameter from the Huff model (same letter in .tex)?
A_FACILITY = 50.0          # ⚠ PLACEHOLDER (km² per facility)

# BHH constant k: empirical ~0.57 for uniform random points in a square
BHH_K = 0.57               # well-known constant, should be OK

# cost_uncaptured : unit cost multiplier for uncaptured demand
#   The last term Σ_i ω_i * Z_i represents demand that goes elsewhere
#   (home delivery?).  Set to 0 to ignore it in the objective.
#   ⚠ CLARIFICATION NEEDED: is there a unit cost for unmet demand?
COST_UNCAPTURED = 0.0      # ⚠ PLACEHOLDER (0 = ignored for now)

# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
print("Loading data …")

zones_df = pd.read_csv(RESULTS_UTIL / "zone_demand.csv")
util_df  = pd.read_csv(RESULTS_UTIL / "utility_matrix.csv")
dist_df  = pd.read_csv(RESULTS_UTIL / "df_dist_dcs.csv", index_col=0)
grids_df = pd.read_csv(RESULTS_UTIL / "df_grids.csv", index_col=0)

I       = list(zones_df["zone_id"])
w       = dict(zip(zones_df["zone_id"], zones_df["demand"]))
u0      = dict(zip(zones_df["zone_id"], zones_df["u0"]))
Z_bar   = dict(zip(zones_df["zone_id"], zones_df["Z_bar"]))
Z_under = dict(zip(zones_df["zone_id"], zones_df["Z_under"]))

J = list(util_df["candidate_id"].unique())

u = {
    (int(row["zone_id"]), row["candidate_id"]): row["u_ij"]
    for _, row in util_df.iterrows()
}

total_demand = sum(w.values())
print(f"  {len(I)} zones | {len(J)} candidates | P = {P}")

# ---------------------------------------------------------------------------
# 2. Compute zone areas and delivery density η_i
#
# Grid cells are uniform rectangles in geographic coordinates.
# Area in km²: Δlat_km × Δlon_km
#   Δlat_km  ≈  (maxLat - minLat) × 111
#   Δlon_km  ≈  (maxLon - minLon) × 111 × cos(lat_centre)
# ---------------------------------------------------------------------------
print("Computing zone areas and delivery density η_i …")

LAT_KM = 111.0   # km per degree latitude

grids_df["lat_centre"] = (grids_df["maxLat"] + grids_df["minLat"]) / 2
grids_df["lon_scale"]  = LAT_KM * np.cos(np.radians(grids_df["lat_centre"]))
grids_df["area_km2"]   = (
    (grids_df["maxLat"] - grids_df["minLat"]).abs() * LAT_KM *
    (grids_df["maxLong"] - grids_df["minLong"]).abs() * grids_df["lon_scale"]
)
grids_df.index = grids_df.index  # keep numeric index from "id" col if exists

# Map zone_id → area_km2 (zone_id == grid id)
area = {}
for _, row in grids_df.iterrows():
    gid = int(row.get("id", row.name))
    area[gid] = max(row["area_km2"], 1e-6)   # avoid division by zero

# η_i = demand / area  [parcels / km²]
eta = {i: w[i] / area.get(i, 1.0) for i in I}

mean_area = np.mean(list(area.values()))
print(f"  Zone area range : [{min(area.values()):.3f}, {max(area.values()):.3f}] km²")
print(f"  Mean zone area  : {mean_area:.3f} km²")
print(f"  η_i range       : [{min(eta.values()):.3f}, {max(eta.values()):.3f}] parcels/km²")

# ---------------------------------------------------------------------------
# 3. Compute routing cost c_ij ≈ l_ij * sqrt(A_j * η_i)
#
# l_ij = distance from zone i to candidate j  [km]
#        taken from df_dist_dcs.csv
#
# ⚠ A_j = A_FACILITY (uniform placeholder — see parameter section)
# ⚠ CLARIFICATION NEEDED: is A_j the Voronoi area of facility j or the
#   Huff attractiveness parameter?  The two uses share the same letter in
#   the formulation files.
# ---------------------------------------------------------------------------
print("Computing routing costs c_ij …")

# Build a distance lookup: {(zone_id, candidate_id): dist_km}
dist_df["grid"] = dist_df["grid"].astype(int)
dist_pivot = dist_df.pivot(index="grid", columns="CD", values="Distance")
# Reindex to our I × J matrices (fill missing with a large distance)
max_dist  = dist_pivot.values.max() * 10
dist_arr  = dist_pivot.reindex(index=I, columns=J).fillna(max_dist).values  # |I|×|J|

# BHH routing cost: c_ij = l_ij * sqrt(A_j * η_i)
# Using A_j = A_FACILITY (uniform placeholder)
eta_arr = np.array([eta[i] for i in I])                     # shape (|I|,)
bhh_arr = np.sqrt(A_FACILITY * eta_arr)[:, np.newaxis]      # shape (|I|, 1)
c_arr   = COST_PER_KM * dist_arr * bhh_arr                   # shape (|I|, |J|)

# Store as dict for Gurobi
c = {
    (I[ii], J[jj]): c_arr[ii, jj]
    for ii in range(len(I))
    for jj in range(len(J))
}

print(f"  c_ij range : [{c_arr.min():.2f}, {c_arr.max():.2f}]")

# ---------------------------------------------------------------------------
# 4. Big-M for the fleet-capacity linking constraint q_j ≤ M_big * y_j
#    M_big must be ≥ max possible q_j = ceil(total_demand / Q)
# ---------------------------------------------------------------------------
M_big = math.ceil(total_demand / Q_CAPACITY) + 1
print(f"  Big-M (fleet link) : {M_big}")

# ---------------------------------------------------------------------------
# 5. Build Gurobi model
# ---------------------------------------------------------------------------
print("Building MILP …")

model = gp.Model("LRP_MNL")
model.Params.OutputFlag  = 1
model.Params.MIPGap      = MIP_GAP
model.Params.TimeLimit   = TIME_LIMIT
model.Params.MIPFocus    = 1
model.Params.Cuts        = 2
model.Params.Heuristics  = 0.3

# ── Variables ────────────────────────────────────────────────────────────────
y = model.addVars(J, vtype=GRB.BINARY, name="y")

# q_j : fleet size — integer, ≥ 0, upper-bounded by M_big
q = model.addVars(J, vtype=GRB.INTEGER, lb=0.0, ub=M_big, name="q")

# Z_i : Charnes-Cooper variable, bounded [Z_under_i, Z_bar_i]
Z = model.addVars(I, lb=0.0, name="Z")
for i in I:
    Z[i].LB = Z_under[i]
    Z[i].UB = Z_bar[i]

# X_ij : McCormick linearisation of y_j * Z_i
X = model.addVars(I, J, lb=0.0, name="X")
for i in I:
    for j in J:
        X[i, j].UB = Z_bar[i]

# ── Objective ─────────────────────────────────────────────────────────────────
#   min  Σ_j f_j*y_j  +  Σ_j a_j*q_j
#      + Σ_i Σ_j c_ij * u_ij * X_ij * ω_i
#      + COST_UNCAPTURED * Σ_i ω_i * Z_i
#
# ⚠ NOTE on the third term: the formulation writes c_ij * x_ij * ω_{ij}.
#   We interpret ω_{ij} as ω_i (typo — the variable list only defines ω_i).
#   After substitution x_ij = u_ij * y_j * Z_i = u_ij * X_ij:
#     routing term = Σ_i Σ_j c_ij * u_ij * X_ij * ω_i

model.setObjective(
    gp.quicksum(F_FIXED * y[j] for j in J)
    + gp.quicksum(A_VEHICLE * q[j] for j in J)
    + gp.quicksum(c[i, j] * u[i, j] * X[i, j] * w[i] for i in I for j in J)
    + gp.quicksum(COST_UNCAPTURED * w[i] * Z[i] for i in I),
    GRB.MINIMIZE,
)

# ── Charnes-Cooper (CC) ───────────────────────────────────────────────────────
# Σ_k u_ik * X_ik + Z_i = 1   ∀ i
# (outside-option utility = 1 in this formulation)
for i in I:
    model.addConstr(
        gp.quicksum(u[i, k] * X[i, k] for k in J) + Z[i] == 1,
        name=f"CC_{i}",
    )

# ── McCormick (M1–M3) ─────────────────────────────────────────────────────────
for i in I:
    zb = Z_bar[i]
    for j in J:
        model.addConstr(X[i, j] <= zb * y[j],              name=f"M1_{i}_{j}")
        model.addConstr(X[i, j] <= Z[i],                   name=f"M2_{i}_{j}")
        model.addConstr(X[i, j] >= Z[i] - zb*(1 - y[j]),  name=f"M3_{i}_{j}")

# ── Capacity (CAP) ────────────────────────────────────────────────────────────
# Σ_i ω_i * u_ij * X_ij ≤ Q * q_j   ∀ j
# Left side = demand allocated to facility j = Σ_i ω_i * x_ij
for j in J:
    model.addConstr(
        gp.quicksum(w[i] * u[i, j] * X[i, j] for i in I) <= Q_CAPACITY * q[j],
        name=f"CAP_{j}",
    )

# ── Fleet–facility link (LNK) ─────────────────────────────────────────────────
# q_j ≤ M_big * y_j   ∀ j  (cannot assign vehicles to a closed facility)
for j in J:
    model.addConstr(q[j] <= M_big * y[j], name=f"LNK_{j}")

# ── Budget ────────────────────────────────────────────────────────────────────
model.addConstr(gp.quicksum(y[j] for j in J) == P, name="budget")

# ---------------------------------------------------------------------------
# 6. Greedy warm start (seeds B&B with a good initial solution)
# ---------------------------------------------------------------------------
print("Computing greedy warm start …")
_t_ws = time.time()
greedy_lockers, _ = greedy_open(P, I, J, w, u, u0)
print(f"  Greedy lockers in {time.time()-_t_ws:.1f}s → {greedy_lockers}")

for j in J:
    y[j].Start = 1.0 if j in greedy_lockers else 0.0

# ---------------------------------------------------------------------------
# 7. Solve with no-progress callback
# ---------------------------------------------------------------------------
_cb_state = {
    "best_obj":         float("inf"),   # minimisation → start at +∞
    "last_incumbent_t": None,
    "n_incumbents":     0,
}

def _cb(cb_model, where):
    if where == GRB.Callback.MIPSOL:
        obj = cb_model.cbGet(GRB.Callback.MIPSOL_OBJ)
        _cb_state["n_incumbents"]     += 1
        _cb_state["last_incumbent_t"]  = time.time()
        if obj < _cb_state["best_obj"]:
            _cb_state["best_obj"] = obj
            print(f"  ✓ Incumbent #{_cb_state['n_incumbents']}: {obj:,.2f}")
    if where in (GRB.Callback.MIP, GRB.Callback.MIPNODE):
        elapsed = time.time() - _cb_state["last_incumbent_t"]
        if elapsed > NO_PROGRESS_LIMIT:
            print(f"\n  ⏹  No new incumbent for {elapsed:.0f}s — terminating.")
            cb_model.terminate()

print(f"Solving (time limit {TIME_LIMIT}s, no-progress {NO_PROGRESS_LIMIT}s) …")
_t0 = time.time()
_cb_state["last_incumbent_t"] = _t0
model.optimize(_cb)
_t_elapsed = time.time() - _t0

# ---------------------------------------------------------------------------
# 8. Results
# ---------------------------------------------------------------------------
_ACCEPTABLE = {GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.INTERRUPTED}

stop_reason = {
    GRB.OPTIMAL:     "optimal",
    GRB.TIME_LIMIT:  f"time limit ({TIME_LIMIT}s)",
    GRB.INTERRUPTED: f"no-progress stop ({NO_PROGRESS_LIMIT}s)",
}.get(model.Status, f"status={model.Status}")

if model.Status not in _ACCEPTABLE or model.SolCount == 0:
    print(f"\nNo feasible solution found ({stop_reason}).")
    raise SystemExit(1)

open_facilities = [j for j in J if y[j].X > 0.5]
fleet           = {j: int(round(q[j].X)) for j in J if y[j].X > 0.5}
mip_gap         = model.MIPGap
obj_val         = model.ObjVal

print("\n" + "=" * 60)
print(f"Stop reason      : {stop_reason}")
print(f"Open facilities  : {open_facilities}")
print(f"Fleet sizes      : {fleet}")
print(f"MIP Gap          : {mip_gap:.2%}")
print(f"Objective (cost) : {obj_val:,.2f}")
print(f"Solve time       : {_t_elapsed:.0f}s")
print("=" * 60)

# ── Compute MNL market share from the solution ────────────────────────────────
S_sol = {i: sum(u[i, j] * y[j].X for j in J) for i in I}
rows  = []
for i in I:
    S_i   = S_sol[i]
    ms_i  = S_i / (S_i + 1.0) if (S_i + 1.0) > 0 else 0.0   # u0 = 1 here
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

RESULTS_OUT.mkdir(parents=True, exist_ok=True)
df_results.to_csv(RESULTS_OUT / f"lr_results_P{P}.csv", index=False)

pd.DataFrame({"candidate_id": open_facilities}).to_csv(
    RESULTS_OUT / f"lr_lockers_P{P}.csv", index=False
)

pd.DataFrame(
    [{"candidate_id": j, "fleet_size": fleet[j]} for j in open_facilities]
).to_csv(RESULTS_OUT / f"lr_fleet_P{P}.csv", index=False)

print(f"\nSaved: lr_results_P{P}.csv, lr_lockers_P{P}.csv, lr_fleet_P{P}.csv")

# ── Runtime log ───────────────────────────────────────────────────────────────
RESULTS_UTIL.mkdir(parents=True, exist_ok=True)
_log_path = RESULTS_UTIL / "solve_times_lr.csv"
_row = pd.DataFrame([{
    "method":           "LRP-MNL",
    "P":                P,
    "solve_time_s":     round(_t_elapsed, 2),
    "objective":        round(obj_val, 2),
    "market_share_pct": round(total_captured / total_demand * 100, 3),
    "detail":           f"gap={mip_gap:.2%}  [{stop_reason}]",
}])
if _log_path.exists():
    _ex = pd.read_csv(_log_path)
    _ex = _ex[~((_ex["method"] == "LRP-MNL") & (_ex["P"] == P))]
    _row = pd.concat([_ex, _row], ignore_index=True)
_row.to_csv(_log_path, index=False)
print(f"Runtime logged → {_log_path}  ({_t_elapsed:.0f}s)")
