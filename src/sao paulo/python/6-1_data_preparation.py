"""
6-1_data_preparation.py
=======================
Load pipeline outputs (scripts 1-3) and build the data structures needed
by the Gurobi MNL location model:

  - zones        : demand weight w_i for each grid cell i
  - candidates   : list of candidate locker sites j
  - u[i, j]      : utility matrix (Huff gravity model)
  - u0[i]        : competitor baseline attractiveness per zone
  - Z_bar[i]     : tight upper bound on Charnes-Cooper variable Z_i
                   = 1 / (sum of P smallest u_ij  +  u0_i)
  - Z_under[i]   : tight lower bound on Z_i
                   = 1 / (sum of P largest  u_ij  +  u0_i)

The bounds Z_bar / Z_under are tighter than the old theta_max = 1/u0_i
because they account for the fact that exactly P lockers will be open.
Tighter bounds → tighter McCormick relaxation → faster Gurobi solve.

Outputs:
  results/utils/utility_matrix.csv  (columns: zone_id, candidate_id, u_ij)
  results/utils/zone_demand.csv     (columns: zone_id, demand, u0, Z_bar, Z_under)

Run this script before 6-2_gurobi_model.py.
"""

import numpy as np
import pandas as pd
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths  (relative to this script's location)
# ---------------------------------------------------------------------------
_HERE        = Path(__file__).resolve().parent   # src/sao paulo/python/
SAO_PAULO    = _HERE.parent                      # src/sao paulo/
DATA_DIR     = SAO_PAULO / "data"
RESULTS_UTIL = SAO_PAULO / "results" / "utils"
RESULTS_OUT  = SAO_PAULO / "results"

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
RHO          = 2.0    # distance decay exponent  (try 1.0 / 2.0 / 3.0)
A_J          = 1.0    # intrinsic attractiveness  (uniform; adjust if capacity data available)
EPS          = 1e-6   # avoid division by zero for co-located points
TARGET_SHARE = 0.15   # target market share for one locker at median distance
                      # used to derive uniform competitor baseline u0
import os
P_BOUND = int(os.environ.get("MNL_P", 7))   # overridable via: MNL_P=3 python 6-1_...
                      # should match the P value in 6-2_gurobi_model.py

# ---------------------------------------------------------------------------
# 1. Load demand zones
# ---------------------------------------------------------------------------
print("Loading demand data …")

df_demand = pd.read_csv(RESULTS_UTIL / "df_square_demand.csv")

# Average demand across all simulated instances and all dates
instance_cols = [c for c in df_demand.columns if c.startswith("instance_")]
df_demand["w"] = df_demand[instance_cols].mean(axis=1)

zones = (
    df_demand.groupby("Grid")["w"]
    .mean()
    .reset_index()
    .rename(columns={"Grid": "zone_id", "w": "demand"})
)
zones = zones[zones["demand"] > 0].reset_index(drop=True)

print(f"  {len(zones)} zones with positive demand")

# ---------------------------------------------------------------------------
# 2. Load candidate locker sites
# ---------------------------------------------------------------------------
print("Loading candidate sites …")

candidates = pd.read_excel(DATA_DIR / "data.xlsx", sheet_name="candidates")[
    ["Nome", "Latitude", "Longitude"]
].rename(columns={"Nome": "candidate_id"})

print(f"  {len(candidates)} candidate sites")

# ---------------------------------------------------------------------------
# 3. Load distances and build the utility matrix u[i, j]
# ---------------------------------------------------------------------------
print("Building utility matrix …")

distances = pd.read_csv(RESULTS_UTIL / "df_dist_dcs.csv").drop(
    columns=["Unnamed: 0"], errors="ignore"
)
distances["grid"] = distances["grid"].astype(int)

I = list(zones["zone_id"])
J = list(candidates["candidate_id"])

# Pivot to a (zone × candidate) distance matrix
# Missing pairs get a very large distance → near-zero utility
max_dist = distances["Distance"].max()
dist_pivot = (
    distances.pivot(index="grid", columns="CD", values="Distance")
    .reindex(index=I, columns=J)
    .fillna(max_dist * 10)
)

# Huff gravity: u_ij = A_j / d_ij^RHO
utility_pivot = A_J / (dist_pivot.values + EPS) ** RHO   # shape (|I|, |J|)

# Store as a tidy dataframe for inspection / export
utility_long = (
    pd.DataFrame(utility_pivot, index=I, columns=J)
    .reset_index()
    .rename(columns={"index": "zone_id"})
    .melt(id_vars="zone_id", var_name="candidate_id", value_name="u_ij")
)

print(f"  Utility matrix: {len(utility_long)} entries")
print(f"  u_ij range: [{utility_long['u_ij'].min():.2e}, {utility_long['u_ij'].max():.2e}]")

# ---------------------------------------------------------------------------
# 4. Competitor baseline u0[i]
#
# Option A (default): uniform u0 derived from TARGET_SHARE.
#   market_share = u_avg / (u_avg + u0) = TARGET_SHARE
#   => u0 = u_avg * (1 - TARGET_SHARE) / TARGET_SHARE
#
# Option B: replace this block with a gravity sum over real competitor locations.
# ---------------------------------------------------------------------------
print("Computing competitor baseline u0 …")

median_u = float(np.median(utility_long["u_ij"]))
u0_uniform = median_u * (1 - TARGET_SHARE) / TARGET_SHARE

print(f"  Median utility : {median_u:.4e}")
print(f"  u0 (uniform)   : {u0_uniform:.4e}")
print(f"  => one median locker captures {median_u / (median_u + u0_uniform):.1%} of demand in a zone")

zones["u0"] = u0_uniform

# ---------------------------------------------------------------------------
# 5. Compute tight bounds Z_bar[i] and Z_under[i]
#
# Z_i = 1 / (S_i + u0_i)  where S_i = sum of utilities of open lockers.
# Since exactly P_BOUND lockers are open:
#   Z_bar[i]   = 1 / (sum of P_BOUND SMALLEST u_ij  +  u0_i)   [Z_i upper bound]
#   Z_under[i] = 1 / (sum of P_BOUND LARGEST  u_ij  +  u0_i)   [Z_i lower bound]
#
# These are tighter than the previous theta_max = 1/u0_i which assumed S_i=0.
# Tighter bounds → smaller McCormick envelope → faster Gurobi solve.
# ---------------------------------------------------------------------------
print(f"Computing tight Z bounds for P = {P_BOUND} …")

# utility_pivot shape: (|I|, |J|), rows indexed by zone_id (order matches I)
util_arr = utility_pivot   # numpy array (|I| x |J|)

# Sort each row: ascending for min-P-sum, descending for max-P-sum
util_sorted_asc  = np.sort(util_arr, axis=1)          # smallest first
util_sorted_desc = np.sort(util_arr, axis=1)[:, ::-1]  # largest first

p = min(P_BOUND, util_arr.shape[1])
P_min_sum = util_sorted_asc[:, :p].sum(axis=1)   # sum of P smallest per zone
P_max_sum = util_sorted_desc[:, :p].sum(axis=1)  # sum of P largest  per zone

Z_bar_arr   = 1.0 / (P_min_sum + u0_uniform)
Z_under_arr = 1.0 / (P_max_sum + u0_uniform)

zones["Z_bar"]   = Z_bar_arr
zones["Z_under"] = Z_under_arr

print(f"  Old upper bound (1/u0):   {1.0/u0_uniform:.2f} (same for all zones)")
print(f"  New Z_bar  range : [{Z_bar_arr.min():.2f},  {Z_bar_arr.max():.2f}]")
print(f"  New Z_under range: [{Z_under_arr.min():.4f}, {Z_under_arr.max():.4f}]")
print(f"  Avg tightening factor: {(1.0/u0_uniform) / Z_bar_arr.mean():.1f}×")

# ---------------------------------------------------------------------------
# 6. Save outputs
# ---------------------------------------------------------------------------
RESULTS_UTIL.mkdir(parents=True, exist_ok=True)

zones[["zone_id", "demand", "u0", "Z_bar", "Z_under"]].to_csv(
    RESULTS_UTIL / "zone_demand.csv", index=False
)
utility_long.to_csv(RESULTS_UTIL / "utility_matrix.csv", index=False)

print("\nSaved:")
print(f"  {RESULTS_UTIL / 'zone_demand.csv'}  (zone_id, demand, u0, Z_bar, Z_under)")
print(f"  {RESULTS_UTIL / 'utility_matrix.csv'}")
print("\nData preparation complete. Run 6-2_gurobi_model.py next.")
