"""
6-2_gurobi_model.py
===================
Build and solve the MNL locker location MILP with Gurobi.
Intended for the São Paulo validation instance (small/medium scale).
For large instances use 6-3_heuristics.py instead.

Mathematical formulation
------------------------
  max  Σ_i w_i Σ_j u_ij * x_ij

  s.t.
    (CC)  Σ_k u_ik * x_ik  +  u0_i * Z_i  =  1            ∀ i
    (M1)  x_ij  ≤  Z_bar_i * y_j                           ∀ i, j
    (M2)  x_ij  ≤  Z_i                                      ∀ i, j
    (M3)  x_ij  ≥  Z_i  -  Z_bar_i * (1 - y_j)             ∀ i, j
    (P)   Σ_j y_j  ≤  P

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
  results/mnl_location_results_P{P}.csv   one row per zone
  results/mnl_sensitivity_P.csv           captured demand vs. P (greedy-based)
"""

import time
import numpy as np
import pandas as pd
import gurobipy as gp
from gurobipy import GRB
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mnl_utils import greedy_open

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE        = Path(__file__).resolve().parent   # src/sao paulo/python/
SAO_PAULO    = _HERE.parent                      # src/sao paulo/
RESULTS_UTIL = SAO_PAULO / "results" / "utils"
RESULTS_OUT  = SAO_PAULO / "results"

# ---------------------------------------------------------------------------
# Parameters  — adjust here before re-running
# ---------------------------------------------------------------------------
import os
P                  = int(os.environ.get("MNL_P", 7))   # overridable via: MNL_P=3 python 6-2_...
MIP_GAP            = 0.05   # accept solution within 5 % of optimal
TIME_LIMIT         = 3600   # hard ceiling: 1 hour max
NO_PROGRESS_LIMIT  = 300    # kill if no improvement for 5 minutes
SENSITIVITY_TLIMIT = 60     # seconds per P value in sensitivity analysis

# Set RUN_SENSITIVITY=1 to enable the full sensitivity sweep (P=1..10).
# Disabled by default. Run with:  MNL_SENSITIVITY=1 python3 6-2_gurobi_model.py
RUN_SENSITIVITY = os.environ.get("MNL_SENSITIVITY", "0") == "1"

# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
print("Loading prepared data …")

zones_df = pd.read_csv(RESULTS_UTIL / "zone_demand.csv")
util_df  = pd.read_csv(RESULTS_UTIL / "utility_matrix.csv")

I       = list(zones_df["zone_id"])
w       = dict(zip(zones_df["zone_id"], zones_df["demand"]))
u0      = dict(zip(zones_df["zone_id"], zones_df["u0"]))
Z_bar   = dict(zip(zones_df["zone_id"], zones_df["Z_bar"]))    # tight upper bound on Z_i
Z_under = dict(zip(zones_df["zone_id"], zones_df["Z_under"]))  # tight lower bound on Z_i

J = list(util_df["candidate_id"].unique())

# Utility dict  u[i, j]
u = {
    (int(row["zone_id"]), row["candidate_id"]): row["u_ij"]
    for _, row in util_df.iterrows()
}

total_demand = sum(w.values())
print(f"  {len(I)} zones | {len(J)} candidates | P = {P}")
print(f"  Z_bar  range : [{min(Z_bar.values()):.2f}, {max(Z_bar.values()):.2f}]  "
      f"(old 1/u0 was ≈ {1/next(iter(u0.values())):.0f})")

# ---------------------------------------------------------------------------
# 2. Greedy warm start (from mnl_utils.py)
#    Gives Gurobi a good initial integer solution so branch-and-bound starts
#    from a realistic gap instead of ~300 %.
# ---------------------------------------------------------------------------
_t_script_start = time.time()   # total script timer (includes warm start + solve)

print("Computing greedy warm start …")
greedy_lockers, S_greedy = greedy_open(P, I, J, w, u, u0)
greedy_obj = sum(w[i] * S_greedy[i] / (S_greedy[i] + u0[i]) for i in I)
print(f"  Greedy open : {greedy_lockers}")
print(f"  Greedy obj  : {greedy_obj:,.1f}  ({greedy_obj / total_demand:.2%} market share)")

# ---------------------------------------------------------------------------
# 3. Build Gurobi model
# ---------------------------------------------------------------------------
print("Building MILP …")

model = gp.Model("MNL_Location")
model.Params.OutputFlag  = 1
model.Params.MIPGap      = MIP_GAP
model.Params.TimeLimit   = TIME_LIMIT
model.Params.MIPFocus    = 1   # prioritise finding good feasible solutions
model.Params.Cuts        = 2   # aggressive cuts
model.Params.Heuristics  = 0.3 # 30 % of time on MIP heuristics

# ── Variables ────────────────────────────────────────────────────────────────
y = model.addVars(J, vtype=GRB.BINARY, name="y")

# Z_i: Charnes-Cooper variable, bounded by tight [Z_under_i, Z_bar_i]
Z = model.addVars(I, lb=0.0, name="Z")
for i in I:
    Z[i].LB = Z_under[i]
    Z[i].UB = Z_bar[i]

# x_ij: McCormick linearisation of y_j * Z_i
x = model.addVars(I, J, lb=0.0, name="x")
for i in I:
    for j in J:
        x[i, j].UB = Z_bar[i]

# ── Warm start: inject greedy solution ───────────────────────────────────────
greedy_set = set(greedy_lockers)
for j in J:
    y[j].Start = 1.0 if j in greedy_set else 0.0

for i in I:
    Z_i_start = 1.0 / (S_greedy[i] + u0[i])
    Z[i].Start = Z_i_start
    for j in J:
        x[i, j].Start = Z_i_start if j in greedy_set else 0.0

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
    zb = Z_bar[i]   # tight upper bound for zone i
    for j in J:
        model.addConstr(x[i, j] <= zb * y[j],                  name=f"M1_{i}_{j}")
        model.addConstr(x[i, j] <= Z[i],                        name=f"M2_{i}_{j}")
        model.addConstr(x[i, j] >= Z[i] - zb * (1 - y[j]),     name=f"M3_{i}_{j}")

# ── Budget ────────────────────────────────────────────────────────────────────
model.addConstr(gp.quicksum(y[j] for j in J) <= P, name="budget")

# ---------------------------------------------------------------------------
# 4. Solve  (with no-progress callback)
# ---------------------------------------------------------------------------
# Strategy: no hard time limit on branch-and-bound progress, but if the best
# incumbent hasn't improved for NO_PROGRESS_LIMIT seconds we terminate.
# The warm-start (greedy) objective is used as the initial baseline so the
# clock starts from the moment Gurobi begins — if it never beats greedy in
# 5 min, it means B&B is stuck and we stop.

_cb_state = {
    "best_obj":    greedy_obj,   # greedy is our baseline
    "last_improve": None,        # set at solve start (see below)
    "n_incumbents": 0,
}

def _no_progress_cb(where):
    """Terminate if no improvement for NO_PROGRESS_LIMIT seconds."""
    if where == GRB.Callback.MIPSOL:
        obj = model.cbGet(GRB.Callback.MIPSOL_OBJ)
        _cb_state["n_incumbents"] += 1
        if obj > _cb_state["best_obj"] + 1e-6:
            _cb_state["best_obj"]    = obj
            _cb_state["last_improve"] = time.time()
            print(f"  ✓ Incumbent #{_cb_state['n_incumbents']}: {obj:,.2f}")

    # Check no-progress at every MIP / MIPNODE callback fire
    if where in (GRB.Callback.MIP, GRB.Callback.MIPNODE):
        elapsed = time.time() - _cb_state["last_improve"]
        if elapsed > NO_PROGRESS_LIMIT:
            print(f"\n  ⏹  No improvement for {elapsed:.0f}s "
                  f"(>{NO_PROGRESS_LIMIT}s threshold) — terminating.")
            model.terminate()

print("Solving …")
_t_solve_start = time.time()
_cb_state["last_improve"] = _t_solve_start   # start the no-progress clock
model.optimize(_no_progress_cb)
_t_solve_elapsed = time.time() - _t_solve_start

# ---------------------------------------------------------------------------
# 5. Results
# ---------------------------------------------------------------------------
# GRB.INTERRUPTED is the status when model.terminate() is called by the callback
_ACCEPTABLE = {GRB.OPTIMAL, GRB.TIME_LIMIT, GRB.INTERRUPTED}
if model.Status not in _ACCEPTABLE or model.SolCount == 0:
    # Gurobi found nothing — fall back to greedy solution
    print("\nGurobi found no integer solution — reporting greedy result.")
    open_lockers = greedy_lockers
    S_sol        = S_greedy
    obj_val      = greedy_obj
    mip_gap      = float("inf")
else:
    open_lockers = [j for j in J if y[j].X > 0.5]
    S_sol        = {i: sum(u[i, j] * y[j].X for j in J) for i in I}
    obj_val      = model.ObjVal
    mip_gap      = model.MIPGap

stop_reason = {
    GRB.OPTIMAL:     "optimal",
    GRB.TIME_LIMIT:  "time limit (1h)",
    GRB.INTERRUPTED: f"no-progress stop ({NO_PROGRESS_LIMIT}s without improvement)",
}.get(model.Status, f"status={model.Status}")

print("\n" + "=" * 60)
print(f"Stop reason : {stop_reason}")
print(f"Open lockers ({len(open_lockers)} / {P}): {open_lockers}")
print(f"MIP Gap : {mip_gap:.2%}" if mip_gap != float("inf") else "MIP Gap : N/A (greedy fallback)")
print(f"Objective (weighted captured demand): {obj_val:,.1f}")
print("=" * 60)

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

df_results = pd.DataFrame(rows).sort_values("captured", ascending=False)
total_captured = df_results["captured"].sum()

print(f"\nTotal daily demand   : {total_demand:,.0f} parcels")
print(f"Total captured       : {total_captured:,.0f} parcels")
print(f"Overall market share : {total_captured / total_demand:.2%}")
print("\nTop 10 zones by captured demand:")
print(df_results.head(10).to_string(index=False))

RESULTS_OUT.mkdir(parents=True, exist_ok=True)
df_results.to_csv(RESULTS_OUT / f"mnl_location_results_P{P}.csv", index=False)
print(f"\nSaved: {RESULTS_OUT / f'mnl_location_results_P{P}.csv'}")

# Save the list of open lockers explicitly so the Streamlit can read it directly
pd.DataFrame({"candidate_id": open_lockers}).to_csv(
    RESULTS_OUT / f"mnl_location_lockers_P{P}.csv", index=False
)
print(f"Saved: {RESULTS_OUT / f'mnl_location_lockers_P{P}.csv'}")

# ── Log solve time ────────────────────────────────────────────────────────
def _append_solve_time(method, p, total_s, obj_val, market_share, detail=""):
    path = RESULTS_UTIL / "solve_times.csv"
    row  = pd.DataFrame([{
        "method":           method,
        "P":                p,
        "solve_time_s":     round(total_s, 2),
        "detail":           detail,
        "objective":        round(obj_val, 2),
        "market_share_pct": round(market_share * 100, 3),
    }])
    if path.exists():
        ex = pd.read_csv(path)
        ex = ex[~((ex["method"] == method) & (ex["P"] == p))]
        row = pd.concat([ex, row], ignore_index=True)
    row.to_csv(path, index=False)
    print(f"Solve time logged → {path}")

_t_total_elapsed = time.time() - _t_script_start   # total time incl. greedy + solve

_append_solve_time(
    method="Exact MILP", p=P,
    total_s=_t_total_elapsed,
    obj_val=obj_val,
    market_share=total_captured / total_demand,
    detail=f"gurobi={_t_solve_elapsed:.0f}s  total={_t_total_elapsed:.0f}s  [{stop_reason}]",
)
print(f"Total script time : {_t_total_elapsed:.0f}s")

# ---------------------------------------------------------------------------
# 6. Sensitivity: effect of P  — greedy-based (fast) + optional short Gurobi
#    Disabled by default (adds up to 10 × SENSITIVITY_TLIMIT seconds).
#    Enable with:  MNL_SENSITIVITY=1 python3 6-2_gurobi_model.py
# ---------------------------------------------------------------------------
if not RUN_SENSITIVITY:
    print("\nSensitivity analysis skipped (set MNL_SENSITIVITY=1 to enable).")
else:
    print("\n" + "-" * 60)
    print("Sensitivity analysis: varying P (greedy + short Gurobi) …")

    budget_constr = model.getConstrByName("budget")
    sensitivity_rows = []

    for p_val in range(1, min(len(J) + 1, 11)):
        # Greedy value (instant)
        g_lockers, g_S = greedy_open(p_val, I, J, w, u, u0)
        g_obj = sum(w[i] * g_S[i] / (g_S[i] + u0[i]) for i in I)

        # Give Gurobi a short shot at improving the greedy
        budget_constr.RHS = p_val
        model.reset()
        model.Params.OutputFlag = 0
        model.Params.TimeLimit  = SENSITIVITY_TLIMIT

        # Inject greedy warm start for this P
        g_set = set(g_lockers)
        for j in J:
            y[j].Start = 1.0 if j in g_set else 0.0
        for i in I:
            Z_i_start = 1.0 / (g_S[i] + u0[i])
            Z[i].Start = Z_i_start
            for j in J:
                x[i, j].Start = Z_i_start if j in g_set else 0.0

        model.optimize()

        if model.Status in [GRB.OPTIMAL, GRB.TIME_LIMIT] and model.SolCount > 0:
            best_obj = model.ObjVal
            gap      = model.MIPGap
        else:
            best_obj = g_obj
            gap      = float("nan")

        share = best_obj / total_demand
        sensitivity_rows.append({
            "P":                  p_val,
            "greedy_captured":    round(g_obj, 1),
            "best_captured":      round(best_obj, 1),
            "market_share_pct":   round(share * 100, 2),
            "mip_gap_pct":        round(gap * 100, 2) if not np.isnan(gap) else "N/A",
        })
        print(f"  P={p_val:2d} | greedy={g_obj:,.0f} | best={best_obj:,.0f} "
              f"| share={share:.1%} | gap={gap:.1%}" if not np.isnan(gap)
              else f"  P={p_val:2d} | greedy={g_obj:,.0f} (Gurobi timed out)")

    df_sens = pd.DataFrame(sensitivity_rows)
    df_sens.to_csv(RESULTS_OUT / "mnl_sensitivity_P.csv", index=False)
    print(f"\nSaved: {RESULTS_OUT / 'mnl_sensitivity_P.csv'}")
