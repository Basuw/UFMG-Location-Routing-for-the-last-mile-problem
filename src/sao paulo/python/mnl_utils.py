"""
mnl_utils.py
============
Shared utility functions for the MNL locker location model.
Imported by 6-2_gurobi_model.py (warm start) and 6-3_heuristics.py (solver).
"""


def greedy_open(p: int, I: list, J: list, w: dict, u: dict, u0: dict) -> tuple[list, dict]:
    """
    Greedy construction: open p lockers by maximum marginal MNL gain.

    At each step, adds the candidate j* that maximises:
        Δ(j*) = Σ_i w_i * u_ij * u0_i / [(S_i + u_ij + u0_i)(S_i + u0_i)]

    Parameters
    ----------
    p   : number of lockers to open
    I   : list of zone ids
    J   : list of candidate ids
    w   : demand weights  {zone_id: float}
    u   : utility matrix  {(zone_id, candidate_id): float}
    u0  : competitor baseline {zone_id: float}

    Returns
    -------
    opened : list of p candidate ids (in order of selection)
    S      : per-zone attractiveness dict after opening all p lockers
    """
    S = {i: 0.0 for i in I}
    opened = []

    for _ in range(p):
        remaining = [j for j in J if j not in opened]
        best_j = max(
            remaining,
            key=lambda j: sum(
                w[i] * u[i, j] * u0[i]
                / ((S[i] + u[i, j] + u0[i]) * (S[i] + u0[i]))
                for i in I
            ),
        )
        opened.append(best_j)
        for i in I:
            S[i] += u[i, best_j]

    return opened, S


def mnl_objective(open_set: set, I: list, w: dict, u: dict, u0: dict) -> float:
    """
    Evaluate the MNL objective for a given open set.

        f(O) = Σ_i w_i * S_i / (S_i + u0_i)
        where S_i = Σ_{j in O} u[i, j]
    """
    total = 0.0
    for i in I:
        S_i = sum(u[i, j] for j in open_set)
        total += w[i] * S_i / (S_i + u0[i])
    return total


def build_results_df(open_set: set, I: list, w: dict, u: dict, u0: dict):
    """
    Build a per-zone results dataframe from an open set.

    Returns a DataFrame sorted by captured demand (descending) with columns:
        zone_id, demand, S_i, market_share_pct, captured
    """
    import pandas as pd

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
