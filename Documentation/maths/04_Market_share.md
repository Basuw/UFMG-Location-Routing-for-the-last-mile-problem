# From Pipeline Data to Gurobi — MNL Location Model

This document explains how to take the data produced by scripts 1–3 and build
a solver for the locker location problem with an MNL demand model.

Two distinct implementations are planned:

- **Phase 1 — São Paulo (validation):** exact Gurobi MILP on a small instance
  to verify that the model, the linearisation, and the data pipeline are correct.
- **Phase 2 — Large network:** heuristic solver (greedy construction + LNS) that
  scales to thousands of zones and hundreds of candidates, using Gurobi only on
  small sub-problems.

Both phases solve the **location sub-problem only** (no routing). Routing is
added on top once the location model is validated (see 9).

---

## 1. What We Are Solving

$$
\max \sum_{i \in I} w_i \cdot \frac{\displaystyle\sum_{j \in J} u_{ij} \cdot y_j}
{\displaystyle\sum_{j \in J} u_{ij} \cdot y_j + u_i^0}
\quad \text{s.t.} \quad \sum_{j \in J} y_j \leq P, \quad y_j \in \{0,1\}
$$

| Symbol | Meaning | Source in pipeline |
|---|---|---|
| $i \in I$ | Demand zones (grid cells) | `results/utils/df_square_demand.csv` |
| $j \in J$ | Candidate locker sites | `data/data.xlsx` → sheet `candidates` |
| $w_i$ | Average daily demand in zone $i$ (parcels/day) | `results/utils/df_square_demand.csv` |
| $u_{ij}$ | Attractiveness of site $j$ for zone $i$ | Computed from `results/utils/df_dist_dcs.csv` |
| $u_i^0$ | Fixed competitor attractiveness in zone $i$ | Parameter (see 4) |
| $P$ | Maximum number of lockers to open | Business constraint |
| $y_j$ | 1 if locker $j$ is opened, 0 otherwise | **Decision variable** |

The objective is non-linear (sum of fractions). For the exact phase it is
linearised using **Charnes-Cooper + McCormick** (5). For the heuristic phase
it is evaluated directly at each step (7).

---

## 2. Resolution Strategy by Instance Size

```
Instance size             |I|      |J|      Approach
──────────────────────────────────────────────────────────────────
São Paulo (validation)    ≤ 1 000  ≤  50    Gurobi exact MILP
Medium network            ≤ 5 000  ≤ 200    Gurobi + greedy warm-start
Large network (target)    > 5 000  > 200    Greedy + LNS with Gurobi sub-problems
```

**Why does Gurobi become slow on large instances?**
The MILP has $|I| \times |J|$ variables $z_{ij}$ and $3 \times |I| \times |J|$
McCormick constraints. At $|I| = 5\,000$ and $|J| = 200$ that is one million
variables and three million constraints. Gurobi's branch-and-bound tree explodes.

The heuristic approach keeps Gurobi's sub-problems small by restricting the
search to a neighbourhood of the current solution at each iteration.

---

## 3. Data Pipeline

The script `6-1_data_preparation.py` reads the following files:

| File | Produced by | Content |
|---|---|---|
| `results/utils/df_square_demand.csv` | 2-2 | Daily demand per grid cell per simulated instance |
| `results/utils/df_dist_dcs.csv` | 3-1 | Distances (km) from each grid cell to each candidate site |
| `results/utils/df_clients_grids.csv` | 1-1 | Grid cell coordinates (lat/lon bounds) |
| `data/data.xlsx` sheet `candidates` | Manual | Candidate locker locations (name, lat, lon) |

It computes $w_i$ by averaging demand across all instances and dates for each
grid cell, then builds the utility matrix and competitor baseline.

---

## 4. Computing Utilities $u_{ij}$ and $u_i^0$

### 4.1 Huff Gravity Model

$$
u_{ij} = \frac{A_j}{d_{ij}^{\,\rho}}
$$

| Parameter | Meaning | Value in `6-1_data_preparation.py` |
|---|---|---|
| $A_j$ | Intrinsic attractiveness of site $j$ | `A_J = 1.0` — uniform (all candidates treated equally) |
| $d_{ij}$ | Distance in km from zone $i$ to site $j$ | From `df_dist_dcs.csv` |
| $\rho$ | Distance decay exponent | `RHO = 2.0` — quadratic decay |
| `EPS` | Small constant to avoid division by zero | `1e-6` added to $d_{ij}$ |

Higher $\rho$ means customers are more sensitive to distance. At $\rho = 2$ a
locker twice as far loses four times the attractiveness.

> **Limitations of current values:** `A_J = 1.0` uniform means we ignore
> differences in locker capacity, visibility, or brand. `RHO = 2.0` is a
> reasonable default for last-mile delivery but has not been calibrated on
> observed São Paulo choice data.

### 4.2 Competitor Attractiveness $u_i^0$

$u_i^0$ is the total attractiveness of all existing alternatives (competitors,
home delivery, etc.) for zone $i$. It acts as a **baseline**: a high $u_i^0$
means customers are already well-served and our lockers must be very close to
capture significant demand.

**Option A — Uniform (default, implemented in `6-1`):**
Set $u_i^0 = u_0$ constant for all zones. Calibrate $u_0$ so that one locker
placed at the median distance captures a target share $\tau$ of demand:

$$
u_0 = u_{\text{med}} \cdot \frac{1 - \tau}{\tau}
$$

In `6-1_data_preparation.py`: `TARGET_SHARE = 0.30`, giving:
- $u_{\text{med}} \approx 1.40 \times 10^{-3}$ (median utility across all zone–candidate pairs)
- $u_0 \approx 4.75 \times 10^{-3}$ (≈ 3.4× the median utility)
- $\theta_i^{\max} \approx 210.5$ for all zones

This means a single median-distance locker captures exactly 30 % of demand in
its zone. All zones share the same competitor strength — this is a simplification.

**Option B — Distance-based (not yet implemented):**
If competitor locations are known, compute $u_i^0$ per zone using the same gravity model:

$$
u_i^0 = \sum_{c \in C} \frac{A_c}{d_{ic}^{\,\rho}}
$$

Each zone would then have a different $u_i^0$ reflecting how well it is already
served by existing alternatives (e.g. dense downtown areas would have a much
larger $u_i^0$ than peripheral zones).

### 4.3 Upper Bound on $\theta_i$

Used only in the exact MILP (5–6). The Charnes-Cooper variable
$\theta_i = 1 / (S_i + u_i^0)$ is largest when no locker is open ($S_i = 0$):

$$
\theta_i^{\max} = \frac{1}{u_i^0}
$$

With the current uniform $u_0 \approx 4.75 \times 10^{-3}$, this gives
$\theta_i^{\max} \approx 210.5$ — the same for every zone.

---

## 5. Linearisation: Charnes-Cooper + McCormick

*(Used by the exact Gurobi MILP — Phase 1 / São Paulo only.)*

The MNL objective is a sum of fractions and cannot be passed to Gurobi directly.
Two successive transformations convert it into a MILP.

### Why the objective is non-linear

The raw objective is:

$$
\max \sum_{i \in I} w_i \cdot \frac{\overbrace{\sum_{j \in J} u_{ij} \cdot y_j}^{S_i}}{\underbrace{\sum_{j \in J} u_{ij} \cdot y_j + u_i^0}_{S_i + u_i^0}}
$$

$S_i$ is a linear function of the binary variables $y_j$, so the objective is a
**ratio of two linear functions** — a fractional program. Gurobi requires a
purely linear (or quadratic) objective, so two successive transformations are
applied.

---

### Step 1 — Charnes-Cooper substitution: introducing $Z_i$

**The idea:** instead of dividing by $(S_i + u_i^0)$, introduce a new variable
$Z_i$ equal to that inverse:

$$
\boxed{Z_i = \frac{1}{S_i + u_i^0}}
$$

$Z_i$ is a **continuous variable** (one per zone $i$), strictly positive.
It is the inverse of the MNL denominator: large when few/bad lockers are open,
small when many/good lockers are open.

**Effect on the objective:** the market share of zone $i$ becomes
$S_i \cdot Z_i$, and expanding $S_i = \sum_j u_{ij} y_j$:

$$
\max \sum_{i \in I} w_i \sum_{j \in J} u_{ij} \cdot \underbrace{y_j \cdot Z_i}_{\text{bilinear}}
$$

**The Charnes-Cooper equality** encodes the definition $Z_i(S_i + u_i^0) = 1$.
Writing $z_i = u_i^0 Z_i$ (the competitor portion, linear in $Z_i$):

$$
\sum_{j \in J} u_{ij}\, x_{ij} + z_i = 1, \quad \forall i \in I \tag{CC}
$$

where $x_{ij}$ is the McCormick variable defined next, and $z_i = u_i^0 Z_i$
is substituted directly (no new variable needed).

---

### Step 2 — McCormick linearisation: introducing $x_{ij}$

The product $y_j \cdot Z_i$ (binary × continuous) is bilinear. Introduce:

$$
\boxed{x_{ij} = y_j \cdot Z_i} \quad \forall i \in I,\ j \in J
$$

There is one $x_{ij}$ per (zone, candidate) pair — $|I| \times |J| = 132\,525$
variables for São Paulo.

**Physical meaning:**
- $y_j = 0$ → $x_{ij} = 0$ (locker closed, no contribution to zone $i$)
- $y_j = 1$ → $x_{ij} = Z_i$ (locker open, zone $i$ receives the full CC value)

**McCormick envelope** with bounds $\underline{Z}_i \leq Z_i \leq \overline{Z}_i$:

| Constraint | Formula | Forces |
|---|---|---|
| **(M1)** | $x_{ij} \leq \overline{Z}_i \cdot y_j$ | $x_{ij} = 0$ when $y_j = 0$ |
| **(M2)** | $x_{ij} \leq Z_i$ | $x_{ij}$ cannot exceed $Z_i$ |
| **(M3)** | $x_{ij} \geq Z_i - \overline{Z}_i\,(1 - y_j)$ | $x_{ij} = Z_i$ when $y_j = 1$ |
| **(M4)** | $x_{ij} \geq 0$ | non-negativity |

Proof: when $y_j=0$, M1+M4 force $x_{ij}=0$; when $y_j=1$, M2+M3 force $x_{ij}=Z_i$. ✓

---

### Step 3 — Tighter bounds on $Z_i$

The quality of the McCormick relaxation depends directly on how tight
$[\underline{Z}_i,\, \overline{Z}_i]$ is. The previous formulation used
$\overline{Z}_i = 1/u_i^0$ (assuming $S_i = 0$, i.e. no lockers open), which
is very loose since we know exactly $P$ lockers will be open.

**Improved bounds** exploit the fact that $|S| = P$ always:

$$
\overline{Z}_i = \frac{1}{\displaystyle\min_{S \subset J,\,|S|=P}\sum_{j \in S} u_{ij} + u_i^0}
\qquad\qquad
\underline{Z}_i = \frac{1}{\displaystyle\max_{S \subset J,\,|S|=P}\sum_{j \in S} u_{ij} + u_i^0}
$$

Since $u_{ij} \geq 0$, both extrema are found by sorting:
- $\overline{Z}_i$: sum the **P smallest** $u_{ij}$ values for zone $i$ (worst-case open set)
- $\underline{Z}_i$: sum the **P largest** $u_{ij}$ values for zone $i$ (best-case open set)

**Why this matters:** a tighter $\overline{Z}_i$ shrinks the McCormick envelope,
bringing the LP relaxation closer to the true integer optimum. Gurobi's
branch-and-bound tree is smaller and the solve is faster.

> **Example (São Paulo, $P=5$):** the old bound was
> $\overline{Z}_i \approx 210$ for all zones. With the improved bounds,
> $\overline{Z}_i$ is typically 5–20× smaller because the sum of even the
> 5 weakest utilities is much larger than zero.

---

### Resulting MILP

| Variable | Type | Bounds | Meaning |
|---|---|---|---|
| $y_j$ | Binary $\{0,1\}$ | — | Open locker $j$? |
| $Z_i$ | Continuous | $[\underline{Z}_i,\, \overline{Z}_i]$ | Inverse of MNL denominator for zone $i$ |
| $x_{ij}$ | Continuous | $[0,\, \overline{Z}_i]$ | Linearised product $y_j \cdot Z_i$ |

| Element | Expression |
|---|---|
| **Objective** | $\max \displaystyle\sum_{i} w_i \sum_{j} u_{ij}\, x_{ij}$ |
| **(CC)** | $\displaystyle\sum_j u_{ij}\, x_{ij} + u_i^0\, Z_i = 1 \quad \forall i$ |
| **(M1–M4)** | McCormick bounds on every $x_{ij}$ using $\overline{Z}_i$ |
| **(P)** | $\displaystyle\sum_j y_j \leq P$ |

**Constraint count for São Paulo** ($|I|=2\,325$, $|J|=57$):