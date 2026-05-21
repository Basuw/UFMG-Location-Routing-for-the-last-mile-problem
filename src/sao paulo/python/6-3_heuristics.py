"""
6-3_heuristics.py
=================
Greedy construction + Large Neighbourhood Search (LNS) for the MNL locker
location problem.  Designed for large instances where 6-2_gurobi_model.py
is too slow.

Algorithm overview
------------------
1. Greedy construction:
     Open lockers one by one, always picking the candidate with the
     largest marginal gain on the MNL objective.

2. LNS improvement:
     Repeat until time limit:
       DESTROY : remove q lockers from the current solution
       REPAIR  : build a small neighbourhood C, solve a restricted
                 Gurobi sub-problem to choose the best q replacements
       ACCEPT  : always accept improvements; optionally accept
                 worse solutions via Simulated Annealing

Inputs (produced by 6-1_data_preparation.py):
  results/utils/zone_demand.csv      columns: zone_id, demand, u0, theta_max
  results/utils/utility_matrix.csv   columns: zone_id, candidate_id, u_ij

Outputs:
  results/mnl_greedy_results.csv     per-zone results after greedy
  results/mnl_lns_results.csv        per-zone results after LNS
  results/mnl_lns_convergence.csv    objective value per LNS iteration
"""

import math
import random
import time
import numpy as np
import pandas as pd
import gurobipy as gp
from gurobipy import GRB
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mnl_utils import greedy_open as _greedy_open, build_results_df as _build_results_df

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
P              = 5       # maximum number of lockers to open
DESTROY_SIZE   = max(1, P // 3)    # q: lockers removed per LNS iteration
NEIGHBOURHOOD  = 3 * DESTROY_SIZE  # K: candidate pool for REPAIR
LNS_TIME_LIMIT = 300    # seconds for the full LNS loop
SA_INITIAL_T   = None   # None = auto-calibrate from first few iterations
SA_ALPHA       = 0.995  # cooling rate
REPAIR_TIMELIM = 30     # seconds per Gurobi REPAIR sub-problem
RANDOM_SEED    = 42

random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)

# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
print("Loading prepared data …")

zones_df = pd.read_csv(RESULTS_UTIL / "zone_demand.csv")
util_df  = pd.read_csv(RESULTS_UTIL / "utility_matrix.csv")

I         = list(zones_df["zone_id"])
w         = dict(zip(zones_df["zone_id"], zones_df["demand"]))
u0        = dict(zip(zones_df["zone_id"], zones_df["u0"]))
theta_max = dict(zip(zones_df["zone_id"], zones_df["theta_max"]))

J = list(util_df["candidate_id"].unique())

# Utility dict  u[i, j]
u = {
    (int(row["zone_id"]), row["candidate_id"]): row["u_ij"]
    for _, row in util_df.iterrows()
}

print(f"  {len(I)} zones | {len(J)} candidates | P = {P}")

# ---------------------------------------------------------------------------
# Helper: evaluate the MNL objective for a given open set O
# ---------------------------------------------------------------------------
def mnl_objective(open_set: set) -> float:
    """
    Compute  Σ_i  w_i * S_i / (S_i + u0_i)
    where    S_i = Σ_{j in open_set}  u[i, j]
    """
    total = 0.0
    for i in I:
        S_i = sum(u[i, j] for j in open_set)
        total += w[i] * S_i / (S_i + u0[i])
    return total


def compute_S(open_set: set) -> dict:
    """Return the per-zone attractiveness dict for a given open set."""
    return {i: sum(u[i, j] for j in open_set) for i in I}


def marginal_gain(j_new: str, S: dict) -> float:
    """
    Marginal gain of adding locker j_new given current attractiveness S_i.

    Δ(j*) = Σ_i w_i * [u_ij * u0_i] / [(S_i + u_ij + u0_i)(S_i + u0_i)]
    """
    gain = 0.0
    for i in I:
        u_ij = u[i, j_new]
        denom_before = S[i] + u0[i]
        denom_after  = S[i] + u_ij + u0[i]
        gain += w[i] * u_ij * u0[i] / (denom_after * denom_before)
    return gain

# ---------------------------------------------------------------------------
# 2. Greedy Construction  (delegates to mnl_utils.greedy_open)
# ---------------------------------------------------------------------------
def greedy_construction(p: int = P) -> tuple[set, float]:
    """
    Open p lockers one by one by maximum marginal MNL gain.
    Wraps mnl_utils.greedy_open with verbose step-by-step output.

    Returns (open_set, objective_value).
    """
    print("\n── Greedy construction ──────────────────────────────────")
    # Build step-by-step so we can print progress
    S = {i: 0.0 for i in I}
    open_set = set()
    for step in range(p):
        opened_so_far = list(open_set)
        remaining = [j for j in J if j not in open_set]
        best_j = max(remaining, key=lambda j: marginal_gain(j, S))
        delta  = marginal_gain(best_j, S)
        open_set.add(best_j)
        for i in I:
            S[i] += u[i, best_j]
        obj = mnl_objective(open_set)
        print(f"  Step {step + 1:2d}: open {best_j:30s}  Δ = {delta:.4f}  obj = {obj:.4f}")

    final_obj = mnl_objective(open_set)
    print(f"\nGreedy objective : {final_obj:.6f}")
    return open_set, final_obj


# ---------------------------------------------------------------------------
# 3. LNS — REPAIR sub-problem (Gurobi)
# ---------------------------------------------------------------------------
def repair_subproblem(
    fixed_open: set,
    neighbourhood: list,
    q: int,
) -> set:
    """
    Solve a restricted Gurobi MILP:
      - Lockers in `fixed_open` are already open (constants, not variables).
      - Choose q lockers from `neighbourhood` to re-open.

    Returns the set of q lockers selected by Gurobi.
    """
    # Pre-compute fixed attractiveness contribution
    S_fixed = {i: sum(u[i, j] for j in fixed_open) for i in I}

    C = neighbourhood  # candidate pool

    model = gp.Model("LNS_REPAIR")
    model.Params.OutputFlag = 0
    model.Params.TimeLimit  = REPAIR_TIMELIM
    model.Params.MIPGap     = 1e-3

    # Variables
    y     = model.addVars(C, vtype=GRB.BINARY, name="y")
    theta = model.addVars(I, lb=0.0, name="theta")
    for i in I:
        # theta_max must account for S_fixed: theta = 1/(S_fixed + S_new + u0)
        # maximum when S_new = 0: theta_max_repair = 1 / (S_fixed_i + u0_i)
        theta[i].UB = 1.0 / (S_fixed[i] + u0[i]) if (S_fixed[i] + u0[i]) > 0 else theta_max[i]
    z = model.addVars(I, C, lb=0.0, name="z")

    # Objective: max Σ_i w_i * [ S_fixed_i * theta_i + Σ_{j in C} u_ij * z_ij ]
    # The S_fixed * theta_i term is linear in theta_i (S_fixed is a constant).
    model.setObjective(
        gp.quicksum(
            w[i] * (S_fixed[i] * theta[i] + gp.quicksum(u[i, j] * z[i, j] for j in C))
            for i in I
        ),
        GRB.MAXIMIZE
    )

    # Charnes-Cooper: (S_fixed_i + Σ_k u_ik z_ik) + u0_i * theta_i = 1
    # where S_fixed_i * theta_i appears inside the sum once we expand:
    # θ_i * (S_fixed_i + S_new_i + u0_i) = 1
    # ⟹  S_fixed_i * θ_i + Σ_k u_ik z_ik + u0_i θ_i = 1
    for i in I:
        model.addConstr(
            S_fixed[i] * theta[i]
            + gp.quicksum(u[i, k] * z[i, k] for k in C)
            + u0[i] * theta[i]
            == 1,
            name=f"CC_{i}"
        )

    # McCormick on z[i,j] = y[j] * theta[i]
    for i in I:
        tm = theta[i].UB
        for j in C:
            model.addConstr(z[i, j] <= tm * y[j],                    name=f"M1_{i}_{j}")
            model.addConstr(z[i, j] <= theta[i],                     name=f"M2_{i}_{j}")
            model.addConstr(z[i, j] >= theta[i] - tm * (1 - y[j]),   name=f"M3_{i}_{j}")

    # Budget on neighbourhood
    model.addConstr(gp.quicksum(y[j] for j in C) <= q, name="budget")

    model.optimize()

    if model.Status not in [GRB.OPTIMAL, GRB.TIME_LIMIT] or model.SolCount == 0:
        # Fallback: greedy within the neighbourhood
        S = S_fixed.copy()
        selected = set()
        remaining = list(C)
        for _ in range(q):
            best = max(remaining, key=lambda j: marginal_gain(j, S))
            selected.add(best)
            remaining.remove(best)
            for i in I:
                S[i] += u[i, best]
        return selected

    return {j for j in C if y[j].X > 0.5}


# ---------------------------------------------------------------------------
# 4. LNS Main Loop
# ---------------------------------------------------------------------------
def lns(
    initial_open: set,
    time_limit: float = LNS_TIME_LIMIT,
    destroy_size: int = DESTROY_SIZE,
    neighbourhood_size: int = NEIGHBOURHOOD,
) -> tuple[set, float, list]:
    """
    Large Neighbourhood Search with Simulated Annealing acceptance.

    Returns (best_open_set, best_objective, convergence_log).
    convergence_log is a list of dicts {iteration, objective, best_objective, temperature}.
    """
    print("\n── LNS ──────────────────────────────────────────────────")

    current  = set(initial_open)
    best     = set(initial_open)
    obj_cur  = mnl_objective(current)
    obj_best = obj_cur

    # Auto-calibrate SA temperature: T0 such that a 1% loss is accepted ~50%
    # exp(-delta / T0) = 0.5  with delta = 0.01 * obj_cur
    # => T0 = 0.01 * obj_cur / ln(2)
    T = SA_INITIAL_T if SA_INITIAL_T is not None else 0.01 * obj_cur / math.log(2)
    print(f"  Initial objective : {obj_cur:.6f}  |  T0 = {T:.4e}")

    convergence = []
    iteration   = 0
    t_start     = time.time()

    while time.time() - t_start < time_limit:
        iteration += 1

        # ── DESTROY ──────────────────────────────────────────────────────
        R_removed = set(random.sample(list(current), destroy_size))
        remaining = current - R_removed   # fixed open lockers

        # ── BUILD NEIGHBOURHOOD ──────────────────────────────────────────
        # Score all candidates not in `remaining` by greedy gain
        S_fixed = compute_S(remaining)
        pool    = [j for j in J if j not in remaining]
        pool.sort(key=lambda j: marginal_gain(j, S_fixed), reverse=True)
        neighbourhood = pool[:neighbourhood_size]

        # ── REPAIR ───────────────────────────────────────────────────────
        new_lockers = repair_subproblem(remaining, neighbourhood, destroy_size)
        candidate   = remaining | new_lockers

        # ── EVALUATE & ACCEPT ────────────────────────────────────────────
        obj_cand = mnl_objective(candidate)
        delta    = obj_cand - obj_cur

        accepted = False
        if delta > 0 or (T > 1e-10 and random.random() < math.exp(delta / T)):
            current = candidate
            obj_cur = obj_cand
            accepted = True

        if obj_cur > obj_best:
            best     = set(current)
            obj_best = obj_cur

        # ── COOL ─────────────────────────────────────────────────────────
        T *= SA_ALPHA

        elapsed = time.time() - t_start
        convergence.append({
            "iteration":      iteration,
            "objective":      round(obj_cur, 6),
            "best_objective": round(obj_best, 6),
            "temperature":    round(T, 8),
            "elapsed_s":      round(elapsed, 2),
            "accepted":       accepted,
        })

        if iteration % 10 == 0:
            print(f"  Iter {iteration:4d} | obj = {obj_cur:.4f} | best = {obj_best:.4f} "
                  f"| T = {T:.3e} | {elapsed:.0f}s")

    print(f"\nLNS finished after {iteration} iterations ({time.time() - t_start:.0f}s)")
    print(f"Best objective : {obj_best:.6f}  (greedy was {mnl_objective(initial_open):.6f})")
    return best, obj_best, convergence


# ---------------------------------------------------------------------------
# 5. Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    total_demand = sum(w.values())

    # ── Phase A: Greedy ───────────────────────────────────────────────────
    greedy_open, greedy_obj = greedy_construction(P)

    df_greedy = _build_results_df(greedy_open, I, w, u, u0)
    print(f"\nGreedy market share : {df_greedy['captured'].sum() / total_demand:.2%}")

    RESULTS_OUT.mkdir(parents=True, exist_ok=True)
    df_greedy.to_csv(RESULTS_OUT / f"mnl_greedy_results_P{P}.csv", index=False)
    print(f"Saved: {RESULTS_OUT / 'mnl_greedy_results.csv'}")

    # ── Phase B: LNS ─────────────────────────────────────────────────────
    lns_open, lns_obj, convergence = lns(greedy_open)

    df_lns  = _build_results_df(lns_open, I, w, u, u0)
    df_conv = pd.DataFrame(convergence)

    print(f"\nLNS market share    : {df_lns['captured'].sum() / total_demand:.2%}")
    print(f"Improvement over greedy: "
          f"{(lns_obj - greedy_obj) / greedy_obj * 100:+.2f}%")
    print(f"\nOpen lockers (LNS): {sorted(lns_open)}")

    df_lns.to_csv(RESULTS_OUT / f"mnl_lns_results_P{P}.csv", index=False)
    df_conv.to_csv(RESULTS_OUT / f"mnl_lns_convergence_P{P}.csv", index=False)
    print(f"Saved: {RESULTS_OUT / 'mnl_lns_results.csv'}")
    print(f"Saved: {RESULTS_OUT / 'mnl_lns_convergence.csv'}")
