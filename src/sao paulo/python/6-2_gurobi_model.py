"""
6-2_gurobi_model.py
===================
Build and solve the MNL locker location MILP with Gurobi.
Uses a greedy warm start (MIP Start) to seed B&B with a good initial
incumbent; Gurobi then explores and closes the gap via branch-and-bound.
Use 6-3_heuristics.py for the standalone Greedy + OA comparison.

Mathematical formulation
------------------------
  max  Σ_i w_i Σ_j u_ij * x_ij

  s.t.
    (CC)  Σ_k u_ik * x_ik  +  u0_i * Z_i  =  1            ∀ i
    (M1)  x_ij  ≤  Z_bar_i * y_j                           ∀ i, j
    (M2)  x_ij  ≤  Z_i                                      ∀ i, j
    (M3)  x_ij  ≥  Z_i  -  Z_bar_i * (1 - y_j)             ∀ i, j
    (P)   Σ_j y_j  =  P

  Variables:
    y_j      ∈ {0, 1}   open locker j?
    Z_i      ≥ 0        Charnes-Cooper variable  =  1 / (S_i + u0_i)
                        bounded by [Z_under_i, Z_bar_i]  (tight bounds from 6-1)
    x_ij     ≥ 0        linearisation of y_j * Z_i  (McCormick)

  Tight bounds (computed in 6-1, stored in zone_demand.csv):
    Z_bar_i   = 1 / (sum of P smallest u_ij  +  u0_i)   [upper bound on Z_i]
    Z_under_i = 1 / (sum of P largest  u_ij  +  u0_i)   [lower bound on Z_i]

Inputs  (produced by 6-1_data_preparation.py):
  results/utils/zone_demand.csv      columns: zone_id, demand, u0, Z_bar, Z_under
  results/utils/utility_matrix.csv   columns: zone_id, candidate_id, u_ij

Outputs:
  results/mnl_location_results_P{P}.csv    one row per zone
  results/mnl_location_lockers_P{P}.csv    list of open locker ids
  results/utils/solve_times.csv            runtime log (appended)
"""

import os
import time
import numpy as np
import pandas as pd
import gurobipy as gp
from gurobipy import GRB
from pathlib import Path
from mnl_utils import greedy_open

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE        = Path(__file__).resolve().parent   # src/sao paulo/python/
SAO_PAULO    = _HERE.parent                      # src/sao paulo/
RESULTS_UTIL = SAO_PAULO / "results" / "utils"
RESULTS_OUT  = SAO_PAULO / "results"

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
P                 = int(os.environ.get("MNL_P", 7))   # MNL_P=3 python 6-2_...
MIP_GAP           = 0.05    # stop when within 5 % of optimal
TIME_LIMIT        = 7200    # hard ceiling: 1 hour
NO_PROGRESS_LIMIT = 3600     # kill if no new incumbent for 15 minutes

# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
print("Loading prepared data …")

zones_df = pd.read_csv(RESULTS_UTIL / "zone_demand.csv")
util_df  = pd.read_csv(RESULTS_UTIL / "utility_matrix.csv")

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
print(f"  Z_bar range : [{min(Z_bar.values()):.4f}, {max(Z_bar.values()):.4f}]")

# ---------------------------------------------------------------------------
# 2. Build Gurobi model
# ---------------------------------------------------------------------------
print("Building MILP …")

model = gp.Model("MNL_Location")
model.Params.OutputFlag = 1
model.Params.MIPGap     = MIP_GAP
model.Params.TimeLimit  = TIME_LIMIT
model.Params.MIPFocus   = 1    # prioritise finding feasible solutions early
model.Params.Cuts       = 2    # aggressive cuts
model.Params.Heuristics = 0.3  # 30 % of time on MIP heuristics

# ── Variables ────────────────────────────────────────────────────────────────
y = model.addVars(J, vtype=GRB.BINARY, name="y")

Z = model.addVars(I, lb=0.0, name="Z")
for i in I:
    Z[i].LB = Z_under[i]
    Z[i].UB = Z_bar[i]

x = model.addVars(I, J, lb=0.0, name="x")
for i in I:
    for j in J:
        x[i, j].UB = Z_bar[i]

# ── Objective ─────────────────────────────────────────────────────────────────
model.setObjective(
    gp.quicksum(w[i] * u[i, j] * x[i, j] for i in I for j in J),
    GRB.MAXIMIZE,
)

# ── Charnes-Cooper (CC) ───────────────────────────────────────────────────────
for i in I:
    model.addConstr(
        gp.quicksum(u[i, k] * x[i, k] for k in J) + u0[i] * Z[i] == 1,
        name=f"CC_{i}",
    )

# ── McCormick (M1–M3) ─────────────────────────────────────────────────────────
for i in I:
    zb = Z_bar[i]
    for j in J:
        model.addConstr(x[i, j] <= zb * y[j],              name=f"M1_{i}_{j}")
        model.addConstr(x[i, j] <= Z[i],                    name=f"M2_{i}_{j}")
        model.addConstr(x[i, j] >= Z[i] - zb * (1 - y[j]), name=f"M3_{i}_{j}")

# ── Budget ────────────────────────────────────────────────────────────────────
# == P (not <=P): opening more lockers is always weakly better for the MNL
# objective, so the optimum always uses exactly P.  Using == P forces Gurobi
# to search in the right sub-space from the start.
model.addConstr(gp.quicksum(y[j] for j in J) == P, name="budget")

# ---------------------------------------------------------------------------
# 3. Greedy warm start  (hint for B&B — not the final solution)
# ---------------------------------------------------------------------------
# Providing a good initial feasible point dramatically reduces the time
# Gurobi needs to find its first incumbent and close the MIP gap.
# This is standard MILP practice: the B&B still proves optimality (or the
# gap), it just starts from a better place than Gurobi's generic heuristics.
print("Computing greedy warm start …")
_t_ws = time.time()
greedy_lockers, _ = greedy_open(P, I, J, w, u, u0)
print(f"  Greedy solution in {time.time()-_t_ws:.1f}s → {greedy_lockers}")

# Inject into Gurobi as a MIP start (Start attribute)
for j in J:
    y[j].Start = 1.0 if j in greedy_lockers else 0.0

# ---------------------------------------------------------------------------
# 4. Solve  (with no-progress callback)
# ---------------------------------------------------------------------------
# Terminate if no new incumbent has been found for NO_PROGRESS_LIMIT seconds.
# The clock starts when Gurobi begins.  Any new feasible solution resets it.
# This catches both "fails to find any solution" and "B&B is stuck".

_cb_state = {
    "best_obj":        -float("inf"),
    "last_incumbent_t": None,   # set just before optimize()
    "n_incumbents":     0,
}

def _no_progress_cb(cb_model, where):
    if where == GRB.Callback.MIPSOL:
        obj = cb_model.cbGet(GRB.Callback.MIPSOL_OBJ)
        _cb_state["n_incumbents"]      += 1
        _cb_state["last_incumbent_t"]   = time.time()
        if obj > _cb_state["best_obj"]:
            _cb_state["best_obj"] = obj
            print(f"  ✓ Incumbent #{_cb_state['n_incumbents']}: {obj:,.2f}  "
                  f"({obj / total_demand:.2%})")

    if where in (GRB.Callback.MIP, GRB.Callback.MIPNODE):
        elapsed = time.time() - _cb_state["last_incumbent_t"]
        if elapsed > NO_PROGRESS_LIMIT:
            print(f"\n  ⏹  No new incumbent for {elapsed:.0f}s "
                  f"(>{NO_PROGRESS_LIMIT}s) — terminating.")
            cb_model.terminate()

print(f"Solving (time limit {TIME_LIMIT}s, no-progress limit {NO_PROGRESS_LIMIT}s) …")
_t_solve_start = time.time()
_cb_state["last_incumbent_t"] = _t_solve_start
model.optimize(_no_progress_cb)
_t_solve_elapsed = time.time() - _t_solve_start

# ---------------------------------------------------------------------------
# 5. Results
# ---------------------------------------------------------------------------
_ACCEPTABLE = {GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.INTERRUPTED}

stop_reason = {
    GRB.OPTIMAL:     "optimal",
    GRB.TIME_LIMIT:  f"time limit ({TIME_LIMIT}s)",
    GRB.INTERRUPTED: f"no-progress stop ({NO_PROGRESS_LIMIT}s without new incumbent)",
}.get(model.Status, f"status={model.Status}")

if model.Status not in _ACCEPTABLE or model.SolCount == 0:
    print(f"\nGurobi found no feasible solution (stop: {stop_reason}).")
    print("No output files written — run again with a longer time limit.")
    # Log the failed attempt
    RESULTS_UTIL.mkdir(parents=True, exist_ok=True)
    _fail_row = pd.DataFrame([{
        "method": "Exact MILP", "P": P,
        "solve_time_s": round(_t_solve_elapsed, 2),
        "detail": f"NO SOLUTION — {stop_reason}",
        "objective": None, "market_share_pct": None,
    }])
    _path = RESULTS_UTIL / "solve_times.csv"
    if _path.exists():
        _ex = pd.read_csv(_path)
        _ex = _ex[~((_ex["method"] == "Exact MILP") & (_ex["P"] == P))]
        _fail_row = pd.concat([_ex, _fail_row], ignore_index=True)
    _fail_row.to_csv(_path, index=False)
    raise SystemExit(1)

open_lockers = [j for j in J if y[j].X > 0.5]
S_sol        = {i: sum(u[i, j] * y[j].X for j in J) for i in I}
obj_val      = model.ObjVal
mip_gap      = model.MIPGap

print("\n" + "=" * 60)
print(f"Stop reason  : {stop_reason}")
print(f"Open lockers ({len(open_lockers)}/{P}): {open_lockers}")
print(f"MIP Gap      : {mip_gap:.2%}")
print(f"Objective    : {obj_val:,.1f}")
print(f"Solve time   : {_t_solve_elapsed:.0f}s")
print("=" * 60)

# ── Per-zone results ──────────────────────────────────────────────────────────
rows = []
for i in I:
    S_i  = S_sol[i]
    ms_i = S_i / (S_i + u0[i]) if (S_i + u0[i]) > 0 else 0.0
    rows.append({
        "zone_id":          i,
        "demand":           round(w[i], 2),
        "S_i":              round(S_i, 8),
        "market_share_pct": round(ms_i * 100, 3),
        "captured":         round(w[i] * ms_i, 2),
    })

df_results     = pd.DataFrame(rows).sort_values("captured", ascending=False)
total_captured = df_results["captured"].sum()

print(f"\nTotal daily demand   : {total_demand:,.0f} parcels")
print(f"Total captured       : {total_captured:,.0f} parcels")
print(f"Overall market share : {total_captured / total_demand:.2%}")
print("\nTop 10 zones by captured demand:")
print(df_results.head(10).to_string(index=False))

RESULTS_OUT.mkdir(parents=True, exist_ok=True)
df_results.to_csv(RESULTS_OUT / f"mnl_location_results_P{P}.csv", index=False)
print(f"\nSaved: mnl_location_results_P{P}.csv")

pd.DataFrame({"candidate_id": open_lockers}).to_csv(
    RESULTS_OUT / f"mnl_location_lockers_P{P}.csv", index=False
)
print(f"Saved: mnl_location_lockers_P{P}.csv")

# ── Runtime log ───────────────────────────────────────────────────────────────
RESULTS_UTIL.mkdir(parents=True, exist_ok=True)
_path = RESULTS_UTIL / "solve_times.csv"
_row = pd.DataFrame([{
    "method":           "Exact MILP",
    "P":                P,
    "solve_time_s":     round(_t_solve_elapsed, 2),
    "detail":           f"gap={mip_gap:.2%}  [{stop_reason}]",
    "objective":        round(obj_val, 2),
    "market_share_pct": round(total_captured / total_demand * 100, 3),
}])
if _path.exists():
    _ex = pd.read_csv(_path)
    _ex = _ex[~((_ex["method"] == "Exact MILP") & (_ex["P"] == P))]
    _row = pd.concat([_ex, _row], ignore_index=True)
_row.to_csv(_path, index=False)
print(f"Runtime logged → {_path}  ({_t_solve_elapsed:.0f}s)")
