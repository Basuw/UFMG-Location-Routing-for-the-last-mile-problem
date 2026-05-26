"""
6-3_heuristics.py
==================
Two-phase heuristic for the MNL locker location problem.

Phase 1 – Greedy Construction (Nemhauser et al. 1978 / Camargo 2024):
    Open lockers sequentially by maximum marginal MNL gain.
    Guarantees ≥ (1 − 1/e) ≈ 63 % of optimum due to submodularity.

Phase 2 – Outer Approximation (Camargo 2024):
    Iterative MILP with tangent-plane linearisation cuts of the concave
    fractional objective.  Each iteration solves a tiny master MILP with
    only |J| binary variables (vs. the full MILP's |I|×|J| variables).
    Converges as cuts accumulate; gap tracked at every iteration.

Bonus – Continuum Approximation routing cost (Stokkink & Geroliminis 2025):
    Post-processing metric: estimates the delivery route length per zone
    given the chosen locker placement, using the BHH formula.

References:
    Camargo R.S. (2024). Bilevel location-routing with MNL demand. UFMG.
    Stokkink P., Geroliminis N. (2025). Optimal micro-hub locations in a
      multi-modal last-mile delivery system. Transportation Research Part E
      203, 104344.

Inputs (produced by 6-1_data_preparation.py):
    results/utils/zone_demand.csv      zone_id, demand, u0, Z_bar, Z_under
    results/utils/utility_matrix.csv   zone_id, candidate_id, u_ij

Outputs:
    results/mnl_greedy_results_P{P}.csv     per-zone results after Phase 1
    results/mnl_oa_results_P{P}.csv         per-zone results after Phase 2
    results/mnl_oa_convergence_P{P}.csv     OA iteration log
    results/utils/solve_times.csv           runtime log (appended)
"""

import math
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
import os
P               = int(os.environ.get("MNL_P", 5))    # overridable via: MNL_P=3 python 6-3_...

# Outer Approximation
OA_MAX_ITER     = 100     # maximum number of OA iterations
OA_TOL          = 1e-4    # convergence: stop when (UB - LB) / LB < OA_TOL
OA_TIME_LIMIT   = 300     # seconds for the full OA phase
OA_MASTER_TLIM  = 30      # seconds per master MILP solve

# Continuum Approximation routing (Stokkink & Geroliminis 2025)
TOUR_CAPACITY   = 20      # parcels per courier tour
COURIER_SPEED   = 15.0    # km/h (cargo bike assumed)
BHH_CONSTANT    = 0.57    # Beardwood-Halton-Hammersley constant

# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------
print("Loading prepared data …")

zones_df = pd.read_csv(RESULTS_UTIL / "zone_demand.csv")
util_df  = pd.read_csv(RESULTS_UTIL / "utility_matrix.csv")

I  = list(zones_df["zone_id"])
w  = dict(zip(zones_df["zone_id"], zones_df["demand"]))
u0 = dict(zip(zones_df["zone_id"], zones_df["u0"]))

J = list(util_df["candidate_id"].unique())

# Utility dict  u[i, j]
u = {
    (int(row["zone_id"]), row["candidate_id"]): row["u_ij"]
    for _, row in util_df.iterrows()
}

total_demand = sum(w.values())
print(f"  {len(I)} zones | {len(J)} candidates | P = {P}")

# Pre-compute utility matrix as numpy array for fast gradient evaluation
zone_list  = I
cand_list  = J
zone_idx   = {z: k for k, z in enumerate(zone_list)}
cand_idx   = {j: k for k, j in enumerate(cand_list)}
U_arr      = np.zeros((len(I), len(J)))    # U_arr[i_idx, j_idx] = u[zone, cand]
W_arr      = np.array([w[i] for i in zone_list])
U0_arr     = np.array([u0[i] for i in zone_list])

for (zi, cj), val in u.items():
    U_arr[zone_idx[zi], cand_idx[cj]] = val

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def mnl_objective_from_S(S_arr: np.ndarray) -> float:
    """f = Σ_i w_i * S_i / (S_i + u0_i)  given S_arr shape (|I|,)."""
    return float(np.sum(W_arr * S_arr / (S_arr + U0_arr)))


def compute_S(y_bin: np.ndarray) -> np.ndarray:
    """S_i = Σ_j u_ij * y_j.  y_bin shape (|J|,)."""
    return U_arr @ y_bin   # shape (|I|,)


def mnl_objective_from_y(y_bin: np.ndarray) -> float:
    return mnl_objective_from_S(compute_S(y_bin))


def gradient(S_arr: np.ndarray) -> np.ndarray:
    """
    ∂f / ∂y_j = Σ_i w_i * u_ij * u0_i / (S_i + u0_i)^2
    Returns shape (|J|,).
    """
    denom2 = (S_arr + U0_arr) ** 2       # shape (|I|,)
    coef   = W_arr * U0_arr / denom2     # shape (|I|,)
    return coef @ U_arr                  # shape (|J|,)  via  Σ_i coef_i * u_ij


def set_to_y(open_set) -> np.ndarray:
    """Convert a set / list of candidate ids to a binary (|J|,) array."""
    y = np.zeros(len(J))
    for j in open_set:
        y[cand_idx[j]] = 1.0
    return y


def y_to_set(y_bin: np.ndarray) -> set:
    return {cand_list[k] for k in np.where(y_bin > 0.5)[0]}


# ---------------------------------------------------------------------------
# 2. Phase 1 — Greedy Construction
# ---------------------------------------------------------------------------

def greedy_phase(p: int = P) -> tuple[set, float, float]:
    """
    Open p lockers one by one by maximum marginal MNL gain.
    Returns (open_set, objective_value, elapsed_seconds).
    """
    print("\n── Phase 1: Greedy Construction ────────────────────────────")
    t0 = time.time()

    S = np.zeros(len(I))
    open_set: set = set()

    for step in range(p):
        remaining = [j for j in J if j not in open_set]

        best_j, best_gain = None, -1.0
        for j in remaining:
            jk       = cand_idx[j]
            u_j      = U_arr[:, jk]
            denom_b  = S + U0_arr
            denom_a  = S + u_j + U0_arr
            gain     = float(np.sum(W_arr * u_j * U0_arr / (denom_a * denom_b)))
            if gain > best_gain:
                best_gain, best_j = gain, j

        open_set.add(best_j)
        S += U_arr[:, cand_idx[best_j]]
        obj = mnl_objective_from_S(S)
        print(f"  Step {step + 1:2d}: open {best_j:30s}  Δ = {best_gain:.4f}  "
              f"obj = {obj:.2f}  ({obj / total_demand:.2%})")

    elapsed = time.time() - t0
    final_obj = mnl_objective_from_S(S)
    print(f"\nGreedy objective : {final_obj:,.2f}  ({final_obj / total_demand:.2%})")
    print(f"Runtime          : {elapsed:.2f} s")
    return open_set, final_obj, elapsed


# ---------------------------------------------------------------------------
# 3. Phase 2 — Outer Approximation (Camargo 2024)
# ---------------------------------------------------------------------------

def outer_approximation(
    initial_open: set,
    p:            int   = P,
    max_iter:     int   = OA_MAX_ITER,
    tol:          float = OA_TOL,
    time_limit:   float = OA_TIME_LIMIT,
    master_tlim:  float = OA_MASTER_TLIM,
) -> tuple[set, float, list, float]:
    """
    Outer Approximation for MNL location (Camargo 2024).

    The concave MNL objective f(y) satisfies:
        f(y) ≤ f(y^k) + ∇f(y^k)^T (y − y^k)   for all y ∈ {0,1}^|J|

    Each iteration:
        1. Solve master MILP with all linearisation cuts → UB
        2. Evaluate true f at new binary y → candidate LB
        3. Add new cut
        4. Stop if (UB − LB) / LB < tol

    Returns (best_open_set, best_obj, convergence_log, elapsed_seconds).
    """
    print("\n── Phase 2: Outer Approximation ────────────────────────────")
    t0 = time.time()

    # ---------- Initialise with greedy solution ----------
    y_init = set_to_y(initial_open)
    S_init = compute_S(y_init)
    f_init = mnl_objective_from_S(S_init)
    g_init = gradient(S_init)

    # Cuts stored as list of (f_k, g_k, y_k) — all numpy arrays
    cuts    = [(f_init, g_init, y_init)]
    LB      = f_init
    UB      = float("inf")
    best_y  = y_init.copy()

    convergence = []
    print(f"  Initial (greedy): f = {f_init:,.2f}  ({f_init / total_demand:.2%})")

    for it in range(1, max_iter + 1):
        t_remaining = time_limit - (time.time() - t0)
        if t_remaining <= 0:
            print(f"  Time limit reached at iteration {it}.")
            break

        # ── Solve OA master MILP ──────────────────────────────────────────
        master = gp.Model("OA_master")
        master.Params.OutputFlag = 0
        master.Params.TimeLimit  = min(master_tlim, t_remaining)
        master.Params.MIPGap     = 1e-4

        y   = master.addVars(len(J), vtype=GRB.BINARY, name="y")
        eta = master.addVar(lb=0.0, ub=total_demand, name="eta")

        master.setObjective(eta, GRB.MAXIMIZE)
        master.addConstr(gp.quicksum(y[k] for k in range(len(J))) <= p, name="budget")

        # Add one linearisation cut per past iterate
        for (f_k, g_k, y_k) in cuts:
            rhs_const = f_k - float(g_k @ y_k)   # f_k − g_k^T y_k
            master.addConstr(
                eta <= rhs_const + gp.quicksum(float(g_k[k]) * y[k]
                                               for k in range(len(J)))
            )

        master.optimize()

        if master.SolCount == 0:
            print(f"  Master returned no solution at iter {it} — stopping.")
            break

        UB    = min(UB, master.ObjVal)
        y_new = np.array([y[k].X for k in range(len(J))])

        # ── Evaluate true objective ───────────────────────────────────────
        y_bin = (y_new > 0.5).astype(float)  # round to binary
        S_new = compute_S(y_bin)
        f_new = mnl_objective_from_S(S_new)
        g_new = gradient(S_new)

        if f_new > LB:
            LB     = f_new
            best_y = y_bin.copy()

        # ── Add new cut ───────────────────────────────────────────────────
        cuts.append((f_new, g_new, y_bin))

        gap     = (UB - LB) / LB if LB > 0 else float("inf")
        elapsed = time.time() - t0

        convergence.append({
            "iteration":      it,
            "UB":             round(UB,  2),
            "LB":             round(LB,  2),
            "gap_pct":        round(gap * 100, 4),
            "elapsed_s":      round(elapsed, 2),
            "n_cuts":         len(cuts),
        })

        print(f"  Iter {it:3d}: UB = {UB:,.2f}  LB = {LB:,.2f}  "
              f"gap = {gap:.3%}  ({elapsed:.0f}s)")

        if gap < tol:
            print(f"  ✓ Converged at iteration {it}  (gap = {gap:.4%} < {tol:.4%})")
            break

        # If the master returned the same binary solution as last time → converged
        if it > 1 and float(np.sum(np.abs(y_bin - cuts[-2][2]))) < 0.5:
            print(f"  ✓ Binary solution unchanged at iteration {it} — converged.")
            break

    elapsed_total = time.time() - t0
    open_set = y_to_set(best_y)
    final_obj = mnl_objective_from_S(compute_S(best_y))

    print(f"\nOA best objective : {final_obj:,.2f}  ({final_obj / total_demand:.2%})")
    print(f"Runtime           : {elapsed_total:.2f} s  ({len(cuts)} cuts added)")
    return open_set, final_obj, convergence, elapsed_total


# ---------------------------------------------------------------------------
# 4. Routing Cost (Stokkink & Geroliminis 2025 — Continuum Approximation)
# ---------------------------------------------------------------------------

def routing_cost_ca(open_set: set) -> pd.DataFrame:
    """
    Estimate delivery routing length per zone using the Continuum
    Approximation framework of Stokkink & Geroliminis (2025).

    For each zone i the estimated total courier distance (km/day) is:
        L_i^intra = BHH * sqrt(A_i * d_i)      (intra-zone)
        L_i^inter = 2 * m_i * dist(i, nearest locker)  (line-haul)
    where m_i = ceil(d_i / TOUR_CAPACITY).

    We do not have zone areas directly; we approximate A_i ∝ 1/zone_density
    by setting A_i = 1 / sqrt(|I|)  (unit normalisation) for illustration.
    Users should replace this with actual zone areas if available.

    Returns a DataFrame with columns:
        zone_id, demand, nearest_locker, dist_km (approx),
        tours_per_day, intra_km, inter_km, total_km
    """
    # Load zone centroids (lat/lon) from df_clients_grids.csv
    clients_path = RESULTS_UTIL / "df_clients_grids.csv"
    if not clients_path.exists():
        print("  [CA routing] df_clients_grids.csv not found — skipping.")
        return pd.DataFrame()

    df_c = pd.read_csv(clients_path)
    df_c.columns = [c.strip() for c in df_c.columns]

    grid_col = next((c for c in df_c.columns
                     if "grid" in c.lower() or "quadrado" in c.lower()), None)
    lat_col  = next((c for c in df_c.columns if c.lower() == "latitude"),  None)
    lon_col  = next((c for c in df_c.columns if c.lower() == "longitude"), None)

    if not all([grid_col, lat_col, lon_col]):
        print("  [CA routing] Could not detect grid/lat/lon columns — skipping.")
        return pd.DataFrame()

    centroids = (
        df_c.groupby(grid_col)[[lat_col, lon_col]]
        .mean().reset_index()
        .rename(columns={grid_col: "zone_id", lat_col: "lat", lon_col: "lon"})
    )
    centroids["zone_id"] = centroids["zone_id"].astype(int)
    zone_coords = centroids.set_index("zone_id")[["lat", "lon"]].to_dict("index")

    # Load candidate coordinates from data.xlsx if possible
    # Fallback: we cannot compute inter-zone distances without locker coordinates.
    # We output intra-zone only in that case.
    data_xlsx = SAO_PAULO / "data" / "data.xlsx"
    if data_xlsx.exists():
        try:
            cand_df = pd.read_excel(data_xlsx, sheet_name="candidates")[
                ["Nome", "Latitude", "Longitude"]
            ].rename(columns={"Nome": "candidate_id"})
            cand_coords = {
                row["candidate_id"]: (row["Latitude"], row["Longitude"])
                for _, row in cand_df.iterrows()
            }
        except Exception:
            cand_coords = {}
    else:
        cand_coords = {}

    # Approximate area (km²) — uniform approximation across zones
    # (replace with real area data if available)
    approx_area_km2 = 4.0    # rough urban grid cell ~2×2 km

    rows = []
    for i in I:
        d_i = w[i]
        m_i = math.ceil(d_i / max(TOUR_CAPACITY, 1))

        # Intra-zone (BHH formula)
        intra_km = BHH_CONSTANT * math.sqrt(approx_area_km2 * d_i)

        # Inter-zone: distance to nearest open locker
        nearest_j, dist_km = None, float("inf")
        if i in zone_coords and cand_coords:
            zi_lat, zi_lon = zone_coords[i]["lat"], zone_coords[i]["lon"]
            DEG_TO_KM      = 111.0
            for j in open_set:
                if j in cand_coords:
                    jlat, jlon = cand_coords[j]
                    d = math.sqrt(((zi_lat - jlat) * DEG_TO_KM) ** 2 +
                                  ((zi_lon - jlon) * DEG_TO_KM *
                                   math.cos(math.radians(zi_lat))) ** 2)
                    if d < dist_km:
                        dist_km, nearest_j = d, j

        inter_km = 2.0 * m_i * dist_km if dist_km < float("inf") else float("nan")

        rows.append({
            "zone_id":       i,
            "demand":        round(d_i, 2),
            "nearest_locker": nearest_j,
            "dist_km":       round(dist_km, 3) if dist_km < float("inf") else None,
            "tours_per_day": m_i,
            "intra_km":      round(intra_km, 3),
            "inter_km":      round(inter_km, 3) if not math.isnan(inter_km) else None,
            "total_km":      round(intra_km + inter_km, 3)
                             if not math.isnan(inter_km) else round(intra_km, 3),
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 5. Shared helpers
# ---------------------------------------------------------------------------

def build_results(open_set: set) -> pd.DataFrame:
    """Build per-zone result DataFrame compatible with the Streamlit map."""
    rows = []
    for i in I:
        S_i  = sum(u[i, j] for j in open_set)
        ms_i = S_i / (S_i + u0[i]) if (S_i + u0[i]) > 0 else 0.0
        rows.append({
            "zone_id":          i,
            "demand":           round(w[i], 2),
            "S_i":              round(S_i, 8),
            "market_share_pct": round(ms_i * 100, 3),
            "captured":         round(w[i] * ms_i, 2),
        })
    return pd.DataFrame(rows).sort_values("captured", ascending=False)


def append_solve_time(method: str, p: int, total_s: float,
                      obj: float, market_share_pct: float,
                      detail: str = "") -> None:
    """
    Append a runtime record to results/utils/solve_times.csv.
    total_s  = full wall-clock time for the method (seconds)
    detail   = optional breakdown string, e.g. "greedy=0.8s + OA=45.2s"
    """
    RESULTS_UTIL.mkdir(parents=True, exist_ok=True)
    path = RESULTS_UTIL / "solve_times.csv"

    row = pd.DataFrame([{
        "method":            method,
        "P":                 p,
        "solve_time_s":      round(total_s, 2),
        "detail":            detail,
        "objective":         round(obj, 2),
        "market_share_pct":  round(market_share_pct, 3),
    }])

    if path.exists():
        existing = pd.read_csv(path)
        existing = existing[~((existing["method"] == method) & (existing["P"] == p))]
        row = pd.concat([existing, row], ignore_index=True)

    row.to_csv(path, index=False)
    print(f"  Solve time logged → {path}")


# ---------------------------------------------------------------------------
# 6. Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    RESULTS_OUT.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: Greedy ───────────────────────────────────────────────────
    greedy_open_set, greedy_obj, greedy_time = greedy_phase(P)

    df_greedy = build_results(greedy_open_set)
    greedy_share = df_greedy["captured"].sum() / total_demand

    df_greedy.to_csv(RESULTS_OUT / f"mnl_greedy_results_P{P}.csv", index=False)
    pd.DataFrame({"candidate_id": sorted(greedy_open_set)}).to_csv(
        RESULTS_OUT / f"mnl_greedy_lockers_P{P}.csv", index=False)
    print(f"Saved: mnl_greedy_results_P{P}.csv + lockers  "
          f"(market share = {greedy_share:.2%})")

    append_solve_time(
        method="Greedy", p=P,
        total_s=greedy_time,
        obj=greedy_obj, market_share_pct=greedy_share * 100,
        detail=f"greedy={greedy_time:.2f}s",
    )

    # ── Phase 2: Outer Approximation ──────────────────────────────────────
    oa_open_set, oa_obj, oa_conv, oa_time = outer_approximation(greedy_open_set, p=P)

    df_oa   = build_results(oa_open_set)
    df_conv = pd.DataFrame(oa_conv)
    oa_share = df_oa["captured"].sum() / total_demand

    df_oa.to_csv(RESULTS_OUT / f"mnl_oa_results_P{P}.csv",           index=False)
    df_conv.to_csv(RESULTS_OUT / f"mnl_oa_convergence_P{P}.csv",     index=False)
    pd.DataFrame({"candidate_id": sorted(oa_open_set)}).to_csv(
        RESULTS_OUT / f"mnl_oa_lockers_P{P}.csv", index=False)
    print(f"Saved: mnl_oa_results_P{P}.csv + lockers  "
          f"(market share = {oa_share:.2%})")
    print(f"Saved: mnl_oa_convergence_P{P}.csv")

    append_solve_time(
        method="OA (Outer Approx.)", p=P,
        total_s=greedy_time + oa_time,
        obj=oa_obj, market_share_pct=oa_share * 100,
        detail=f"greedy={greedy_time:.2f}s + OA={oa_time:.2f}s",
    )

    # Summary
    print("\n" + "=" * 60)
    print(f"Open lockers (greedy): {sorted(greedy_open_set)}")
    print(f"Open lockers (OA)    : {sorted(oa_open_set)}")
    print(f"\nMarket share — Greedy        : {greedy_share:.2%}")
    print(f"Market share — OA            : {oa_share:.2%}")
    improvement = (oa_obj - greedy_obj) / greedy_obj * 100
    print(f"Improvement of OA over greedy: {improvement:+.2f}%")
    print("=" * 60)

    # ── Bonus: Routing Cost (Stokkink & Geroliminis 2025 CA) ─────────────
    print("\n── Bonus: Routing Cost Estimation (CA) ─────────────────────")
    df_routing = routing_cost_ca(oa_open_set)
    if not df_routing.empty:
        df_routing.to_csv(RESULTS_OUT / f"mnl_routing_ca_P{P}.csv", index=False)
        total_km = df_routing["total_km"].sum()
        print(f"Total estimated courier distance: {total_km:,.0f} km/day")
        print(f"Saved: mnl_routing_ca_P{P}.csv")
