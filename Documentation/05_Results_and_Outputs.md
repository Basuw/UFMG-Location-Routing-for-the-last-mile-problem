# Results and Outputs — MNL Locker Location (São Paulo)

This document contains the actual results of the São Paulo validation run,
the description of all output files, and the sensitivity analysis.

For the mathematical model and algorithm details, see `04_Gurobi_Implementation.md`.

---

## 1. Instance Summary

| Quantity | Value |
|---|---|
| Demand zones $\|I\|$ | **2 325** |
| Candidate locker sites $\|J\|$ | **57** |
| Total daily demand | **10 637.5 parcels/day** |
| Competitor baseline $u_0$ | $1.154 \times 10^{-2}$ (`TARGET_SHARE = 0.15`) |
| Distance decay $\rho$ | 2.0 |
| Old upper bound $1/u_0$ | 86.7 (same for all zones) |
| New $\overline{Z}_i$ range | **[49.5, 74.0]** — avg **1.4× tighter** than $1/u_0$ |
| New $\underline{Z}_i$ range | [32.1, 50.0] |

The tight bounds reduce the McCormick envelope by ~30% on average, which accelerates Gurobi's branch-and-bound.

---

## 2. Main Results — P = 7

Run: `6-2_gurobi_model.py` with `P = 7`, `TIME_LIMIT = 300 s`, `MIP_GAP = 0.05`.

| Metric | Value |
|---|---|
| **Open lockers** | 7 / 57 candidates |
| **Total captured demand** | **8 431 parcels/day** |
| **Overall market share** | **79.25 %** |
| Total daily demand | 10 638 parcels/day |
| Zones with share > 80 % | 946 / 2 325 (41 %) |
| Zones with share > 50 % | 1 971 / 2 325 (85 %) |
| Zones with share < 10 % | 0 |

### Market share distribution across zones (P = 7)

| Stat | Value |
|---|---|
| Minimum | 31.5 % |
| Median | 74.3 % |
| Mean | 71.4 % |
| Maximum | 100.0 % |

No zone is left without coverage (min > 30 %). The distribution reflects the
MNL model: zones close to an open locker get very high shares, distant zones
still capture demand through the gravity decay.

### Top 10 zones by captured demand (P = 7)

| Zone | Demand | $S_i$ | Share % | Captured |
|---|---|---|---|---|
| 1827 | 21.41 | 0.203 | 94.6 % | 20.27 |
| 1907 | 16.74 | 7.349 | 99.8 % | 16.71 |
| 1428 | 15.95 | 0.431 | 97.4 % | 15.53 |
| 1670 | 17.15 | 0.093 | 89.0 % | 15.25 |
| 1824 | 16.78 | 0.088 | 88.4 % | 14.83 |
| 2375 | 17.36 | 0.066 | 85.1 % | 14.78 |
| 1904 | 15.44 | 0.141 | 92.4 % | 14.27 |
| 1747 | 16.02 | 0.094 | 89.0 % | 14.26 |
| 2140 | 15.29 | 0.132 | 92.0 % | 14.07 |
| 1982 | 14.68 | 0.132 | 92.0 % | 13.50 |

> **Reading $S_i$:** zone 1907 has a very high $S_i = 7.35$ meaning it sits very
> close to one of the 7 open lockers (high utility). Zone 1670 has $S_i = 0.09$
> but still captures 89 % because its $u_0$ is the same for all zones — even a
> moderately close locker dominates the competition at `TARGET_SHARE = 0.15`.

---

## 3. Sensitivity Analysis — P from 1 to 10

Run automatically at the end of `6-2_gurobi_model.py` (60 s per P value).
Greedy solution used as fallback when Gurobi finds no integer solution within the time limit.

| P | Captured (parcels/day) | Market share | MIP gap | Note |
|---|---|---|---|---|
| 1 | 3 611 | 33.95 % | — | Greedy only (Gurobi: no sol. in 60 s) |
| 2 | 5 290 | 49.73 % | — | Greedy only |
| 3 | 6 438 | 60.52 % | — | Greedy only |
| 4 | 7 103 | 66.77 % | 31.45 % | Gurobi improved |
| **5** | **7 658** | **71.99 %** | **25.87 %** | |
| 6 | 8 062 | 75.79 % | 20.83 % | |
| **7** | **8 431** | **79.25 %** | **16.11 %** | Main run |
| 8 | 8 675 | 81.55 % | 13.24 % | |
| 9 | 8 883 | 83.51 % | 10.88 % | |
| 10 | 9 046 | 85.04 % | 9.14 % | |

**Total daily demand: 10 638 parcels/day.**

### Observations

**Diminishing returns:** the marginal gain per additional locker decreases steadily:

| Step | Marginal gain | Gain (parcels/day) |
|---|---|---|
| 1 → 2 | +15.8 pts | +1 679 |
| 2 → 3 | +10.8 pts | +1 148 |
| 3 → 4 | +6.3 pts | +665 |
| 4 → 5 | +5.2 pts | +555 |
| 5 → 6 | +3.8 pts | +404 |
| 6 → 7 | +3.5 pts | +369 |
| 7 → 8 | +2.3 pts | +244 |
| 8 → 9 | +2.0 pts | +209 |
| 9 → 10 | +1.5 pts | +163 |

The steepest drops are between P=1→2 and P=2→3. Beyond P=7 the gain per
locker falls below 250 parcels/day — the point at which the fixed daily opening
cost likely exceeds the marginal revenue.

**MIP gaps (P ≥ 4):** the remaining gaps (9–31 %) mean the true optimal could
still be higher. To tighten them, increase `SENSITIVITY_TLIMIT` from 60 s or
run the main solver with a longer `TIME_LIMIT`.

---

## 4. Output File Formats

### 4.1 Exact MILP — `6-2_gurobi_model.py`

**`results/mnl_location_results_P{P}.csv`** — one row per demand zone:

| Column | Description |
|---|---|
| `zone_id` | Grid cell identifier |
| `demand` | Average daily demand $w_i$ (parcels/day) |
| `S_i` | Total attractiveness of open lockers $= \sum_{j \in \mathcal{O}} u_{ij}$ |
| `market_share_pct` | Fraction of demand captured: $S_i / (S_i + u_i^0) \times 100$ |
| `captured` | Estimated daily parcels captured $= w_i \times \text{share}_i$ |

**`results/mnl_sensitivity_P.csv`** — one row per P tested (1 to 10):

| Column | Description |
|---|---|
| `P` | Number of open lockers |
| `greedy_captured` | Captured demand from greedy (instant baseline) |
| `best_captured` | Best result found (greedy + short Gurobi) |
| `market_share_pct` | Overall market share % |
| `mip_gap_pct` | Remaining optimality gap, or `N/A` if Gurobi timed out |

### 4.2 Heuristics — `6-3_heuristics.py`

**`results/mnl_greedy_results_P{P}.csv`** — per-zone results after greedy construction (same columns as above).

**`results/mnl_lns_results_P{P}.csv`** — per-zone results for the best LNS solution.

**`results/mnl_lns_convergence_P{P}.csv`** — LNS convergence trace:

| Column | Description |
|---|---|
| `iteration` | LNS iteration number |
| `objective` | Current solution objective |
| `best_objective` | Best objective seen so far |
| `temperature` | Current Simulated Annealing temperature |
| `elapsed_s` | Wall-clock time since LNS start |
| `accepted` | Whether the candidate solution was accepted |

### 4.3 Intermediate pipeline — `6-1_data_preparation.py`

**`results/utils/zone_demand.csv`**:

| Column | Description |
|---|---|
| `zone_id` | Grid cell identifier |
| `demand` | Average daily demand $w_i$ |
| `u0` | Competitor baseline $u_i^0$ (uniform, from `TARGET_SHARE`) |
| `Z_bar` | Tight upper bound: $1/(\text{sum of P smallest } u_{ij} + u_i^0)$ |
| `Z_under` | Tight lower bound: $1/(\text{sum of P largest } u_{ij} + u_i^0)$ |

**`results/utils/utility_matrix.csv`**:

| Column | Description |
|---|---|
| `zone_id` | Grid cell identifier |
| `candidate_id` | Candidate locker name |
| `u_ij` | Huff gravity utility $= A_j / d_{ij}^\rho$ |

---

## 5. File Map

```
data/
└── data.xlsx
    ├── sheet "candidates"     ← candidate locker sites (name, lat, lon, capacity, cost)
    ├── sheet "vehicles"       ← vehicle types (moto, car) with costs and constraints
    ├── sheet "hub"            ← depot location and transfer cost
    └── sheet "zips"           ← zip code metadata (risk level, failure rate)

results/utils/
├── df_square_demand.csv       ← demand per grid cell (from script 2-2)
├── df_dist_dcs.csv            ← distances d_ij in km (from script 3-1)
├── df_clients_grids.csv       ← grid cell centroids with lat/lon (from script 1-1)
├── zone_demand.csv            ← output of 6-1: w_i, u0_i, Z_bar_i, Z_under_i
└── utility_matrix.csv         ← output of 6-1: u_ij for all (i, j)

results/
├── mnl_location_results_P{P}.csv   ← exact MILP solution per P (6-2)
├── mnl_sensitivity_P.csv           ← captured demand vs. P, P=1..10 (6-2)
├── mnl_greedy_results_P{P}.csv     ← greedy solution per P (6-3)
├── mnl_lns_results_P{P}.csv        ← LNS best solution per P (6-3)
└── mnl_lns_convergence_P{P}.csv    ← LNS convergence curve per P (6-3)

src/sao paulo/python/
├── 6-1_data_preparation.py         ← builds utility matrix and Z bounds
├── 6-2_gurobi_model.py             ← exact MILP (São Paulo validation)
├── 6-3_heuristics.py               ← greedy + LNS (large instances)
└── 6-4_streamlit_map.py            ← interactive map dashboard
```

**Run order:** `6-1` → `6-2` (exact) or `6-3` (heuristic) → `6-4` (visualisation).
Re-run `6-1` only when raw data or parameters (`TARGET_SHARE`, `RHO`, `P_BOUND`) change.

---

## 6. How to Interpret the Results

### Market share caveats

`market_share_pct` is $S_i / (S_i + u_i^0) \times 100$ — the MNL model's
estimate of what fraction of zone $i$'s total parcel demand our lockers capture.

Two important limitations:
- $w_i$ is **total demand** (all delivery modes). The model implicitly assumes
  all customers are convertible to locker users. Real addressable demand is lower.
- High shares (> 80 %) are mathematically valid but indicate zones where our
  lockers strongly dominate the competition **under the model's assumptions**.
  They should be read as upper bounds on actual capture.

**Relative comparisons are reliable.** Zone A at 90 % is genuinely better
served than zone B at 40 %. But the absolute values depend on `TARGET_SHARE`.

### Parameter effects

| Parameter | Current value | Effect of increasing |
|---|---|---|
| `TARGET_SHARE` | 0.15 | Lower → stronger competition → lower market shares |
| `RHO` | 2.0 | Higher → customers more distance-sensitive → solution concentrates near dense zones |
| `P` | 7 | More lockers → more coverage, diminishing returns beyond P=7 |

---

*Reference document — UFMG Internship 2026 — Bastien Jacquelin*
