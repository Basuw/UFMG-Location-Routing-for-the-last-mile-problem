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

| Constraint block | Count |
|---|---|
| CC equalities | 2 325 |
| McCormick M1 | 132 525 |
| McCormick M2 | 132 525 |
| McCormick M3 | 132 525 |
| Budget | 1 |
| **Total** | **~400 000** |

This MILP is **exact**. The improved bounds on $Z_i$ make the LP relaxation
tighter, so branch-and-bound converges faster than with the previous
$\overline{Z}_i = 1/u_i^0$ bound.

## 6. Exact MILP — São Paulo Validation

### 6.1 Model Size

**Actual São Paulo instance** (`6-1_data_preparation.py` outputs):

| Quantity | Count |
|---|---|
| Demand zones $|I|$ | **2 325** |
| Candidate sites $|J|$ | **57** |
| Binary variables $y_j$ | 57 |
| Continuous variables $Z_i$ | 2 325 |
| Continuous variables $x_{ij}$ | 132 525 |
| CC constraints | 2 325 |
| McCormick constraints | 397 575 |
| Budget constraint | 1 |
| **Total variables** | **134 907** |
| **Total constraints** | **~400 000** |

Gurobi solves this in under 5 minutes with the greedy warm start.

### 6.2 Solver Parameters (from `6-2_gurobi_model.py`)

| Parameter | Value | Role |
|---|---|---|
| `P` | **1,3,5,7** | Maximum number of lockers to open |
| `MIP_GAP` | **0.05** | Accept a solution within 5 % of optimal |
| `TIME_LIMIT` | **300 s** | Hard stop for the main solve |
| `SENSITIVITY_TLIMIT` | **60 s** | Time per P value in sensitivity loop |
| `MIPFocus` | **1** | Prioritise finding good feasible solutions |
| `Cuts` | **2** | Aggressive cut generation |
| `Heuristics` | **0.3** | 30 % of time on MIP heuristics |

The greedy warm start (computed before the solve) injects a realistic initial
integer solution via `y[j].Start`, `Z[i].Start`, and `x[i,j].Start`.
Without it, Gurobi starts from an empty solution and the initial gap can exceed
300 %, leading to very slow convergence.

### 6.2 Scripts

| Script | Role |
|---|---|
| `6-1_data_preparation.py` | Build $u_{ij}$, $u_i^0$, $\overline{Z}_i$, $\underline{Z}_i$ and export intermediate CSVs |
| `6-2_gurobi_model.py` | Build and solve the MILP, export results and sensitivity curve |

Run `6-1` once (or whenever the raw data changes), then iterate on `6-2`
to test different values of $P$, $\rho$, or $\tau$ without reloading the
raw data.

### 6.3 Outputs

Output file formats are described in `05_Results_and_Outputs.md §1`.

---

## 7. Heuristics for Large Instances

### 7.1 Why Gurobi Alone Is Not Enough

At large scale ($|I| \sim 10\,000$, $|J| \sim 500$), the full MILP has
5 million variables and 15 million constraints. Even with a 1-hour time
limit, Gurobi may not find a solution within a few percent of optimal.

The strategy is:
1. Build a **good feasible solution quickly** with a greedy construction
   heuristic.
2. **Improve it iteratively** with LNS, calling Gurobi only on small,
   restricted sub-problems at each step.

This keeps Gurobi's role tractable (sub-problems of tens of variables)
while exploring the solution space intelligently.

---

### 7.2 Greedy Construction Heuristic

**Idea:** open lockers one by one, always picking the candidate that
provides the largest marginal gain to the MNL objective.

**State:** at each step maintain $S_i^{(t)} = \sum_{j \in \mathcal{O}^{(t)}} u_{ij}$,
the current total attractiveness for zone $i$. Initially $S_i^{(0)} = 0$
and $\mathcal{O}^{(0)} = \emptyset$.

**Marginal gain** of adding locker $j^*$ to the current open set:

$$
\Delta(j^*) = \sum_{i \in I} w_i \left[
  \frac{S_i + u_{ij^*}}{S_i + u_{ij^*} + u_i^0}
  - \frac{S_i}{S_i + u_i^0}
\right]
$$

Which simplifies to:

$$
\Delta(j^*) = \sum_{i \in I} w_i \cdot
\frac{u_{ij^*} \cdot u_i^0}{\left(S_i + u_{ij^*} + u_i^0\right)\left(S_i + u_i^0\right)}
$$

**Algorithm:**

```
Initialise:  O = ∅,  S_i = 0  for all i
For p = 1 … P:
    For each candidate j ∉ O:
        compute Δ(j)
    j* = argmax_j Δ(j)
    O ← O ∪ {j*}
    S_i ← S_i + u_{ij*}   for all i
Return O
```

**Complexity:** $O(P \times |J| \times |I|)$ — linear in the data size,
fast even for $|I| = 10\,000$ and $|J| = 500$.

**Quality:** greedy is not guaranteed to be optimal, but for concave
submodular objectives like the MNL it typically achieves 90–95% of the
optimal value and provides an excellent warm start for LNS.

> **Submodularity note:** the MNL objective is submodular in $y$ (adding a
> locker is always useful, but less so when many lockers are already open).
> This guarantees that greedy achieves at least $(1 - 1/e) \approx 63\%$ of
> the optimum in the worst case — a well-known theoretical result.

---

### 7.3 Large Neighbourhood Search (LNS)

LNS alternates between two operations — DESTROY and REPAIR — to escape
local optima that the greedy solution may have reached.

**Notation:**
- $\mathcal{O}$ — current open set ($|\mathcal{O}| = P$)
- $q$ — destroy size (number of lockers removed per iteration, e.g. $q = \lfloor P / 3 \rfloor$)
- $K$ — neighbourhood size for REPAIR (e.g. $K = 3q$)

#### DESTROY

Randomly remove $q$ lockers from $\mathcal{O}$ to get the *destroyed* set
$\mathcal{R} \subset \mathcal{O}$, leaving $\mathcal{O}' = \mathcal{O} \setminus \mathcal{R}$
with $P - q$ open lockers.

Several destroy strategies can be used:
- **Random destroy:** remove $q$ lockers uniformly at random.
- **Worst destroy:** remove the $q$ lockers with the smallest individual
  contribution to the objective (the least useful open lockers).
- **Zone-based destroy:** remove the $q$ lockers covering the zones with
  the lowest current market share (trying to improve the weakest areas).

#### REPAIR (Gurobi sub-problem)

Goal: choose the best $q$ lockers to re-open from a small neighbourhood
$\mathcal{C} \subset J \setminus \mathcal{O}'$.

**Building $\mathcal{C}$:**
Select the $K$ candidates not already in $\mathcal{O}'$ with the highest
greedy score given the current state $S_i^{\mathcal{O}'}$. This ensures
the neighbourhood is promising without being exhaustive.

**Sub-problem structure:**
The lockers in $\mathcal{O}'$ are fixed. Their contribution to each zone is:

$$
S_i^{\text{fixed}} = \sum_{j \in \mathcal{O}'} u_{ij}
$$

The REPAIR problem optimises only over the $|\mathcal{C}|$ candidates in
the neighbourhood. Introduce binary variables $y_j$ for $j \in \mathcal{C}$
and the Charnes-Cooper / McCormick variables for those candidates only.
The CC constraint becomes:

$$
\sum_{k \in \mathcal{C}} u_{ik} \, z_{ik}
+ u_i^0 \, \theta_i
= \frac{1}{1 + S_i^{\text{fixed}} \, \theta_i}
$$

In practice, $S_i^{\text{fixed}}$ is a known constant, so the constraint
remains linear. The sub-problem has:
- $|\mathcal{C}|$ binary variables ($\ll |J|$)
- $|I| \times |\mathcal{C}|$ continuous variables $z_{ij}$
- Budget: $\sum_{j \in \mathcal{C}} y_j \leq q$

With $K = 3q$ and $q \approx P/3$, this sub-problem is tiny compared to
the full MILP and Gurobi solves it in seconds.

#### Acceptance Criterion

Two options:

**Pure improvement (simple):** accept the new solution only if it strictly
improves the objective. Easy to implement, but may get stuck.

**Simulated Annealing (recommended):** accept a worse solution with
probability $e^{-\delta / T}$ where $\delta$ is the loss and $T$ is a
temperature that decreases over iterations. Allows escaping local optima.
The temperature schedule:
- Start: $T_0$ chosen so that a 1% loss is accepted with probability 0.5.
- Cooling: $T \leftarrow \alpha \cdot T$ after each iteration ($\alpha \approx 0.995$).
- Stop: when $T$ is small (all worsening moves rejected) or time limit reached.

#### Full LNS Algorithm

```
Initialise with greedy solution  O_best = O_0
T ← T_0

Repeat until time limit:
    1. DESTROY: choose R ⊂ O  (size q)
                O' ← O \ R

    2. REPAIR:  build neighbourhood C ⊂ J \ O' (top-K greedy candidates)
                solve Gurobi sub-problem on C with budget q
                R_new ← the q lockers selected by Gurobi
                O_candidate ← O' ∪ R_new

    3. EVALUATE: Δ ← f(O_candidate) - f(O)
       if Δ > 0:
           O ← O_candidate          (always accept improvement)
       elif exp(Δ / T) > random():
           O ← O_candidate          (SA: sometimes accept worsening)

    4. UPDATE BEST: if f(O) > f(O_best): O_best ← O

    5. COOL: T ← α · T

Return O_best
```

**Why this works well for the MNL problem:**
- The MNL objective is smooth and concave — small changes in the open set
  cause small changes in objective value, so neighbourhoods are informative.
- The REPAIR sub-problem is a self-contained MILP with the same structure
  as the full problem, so the Charnes-Cooper formulation applies directly.
- In practice, LNS on location problems converges within 50–200 iterations,
  each taking a few seconds → total runtime of a few minutes.

### 7.4 Scripts

| Script | Role |
|---|---|
| `6-3_heuristics.py` | Greedy + LNS solver; writes per-zone results and convergence curve |
| `6-4_streamlit_map.py` | Interactive map dashboard — run with `streamlit run 6-4_streamlit_map.py` |

Output file formats: see `05_Results_and_Outputs.md §1`.
Sensitivity analysis and parameter effects: see `05_Results_and_Outputs.md §4–5`.

---

## 8. Coupling with Routing (Next Steps)

The location model above is the foundation. The full LRP adds routing on top:

```
Step 1  ──► Solve location (6-2 or 6-3) → which P lockers to open?
                  │
                  ▼
Step 2  ──► Fix open lockers → solve VRP (truck routes between clusters)
                  │           using vrpy (already in 4-1-routing.ipynb)
                  ▼
Step 3  ──► Couple: subtract routing cost from MNL objective
                  │
                  ▼
Step 4  ──► For very large instances: Benders decomposition
```

When routing cost $C_{\text{routing}}$ is known for a given set of open
lockers, the combined objective becomes:

$$
Z = \underbrace{\text{MNL captured demand} \times r}_{\text{revenue}}
  - \underbrace{C_{\text{routing}}}_{\text{vrpy / routing sub-problem}}
  - \underbrace{\sum_j f_j \cdot y_j}_{\text{fixed opening costs}}
$$

In the LNS framework, routing cost can be evaluated inside the REPAIR
step: after Gurobi selects the $q$ lockers to re-open, call vrpy to
compute the routing cost for $\mathcal{O}_{\text{candidate}}$, then
compute the net objective $Z$. This adds a few seconds per iteration but
gives a much more realistic objective.

---

## 10. Key Parameters

### Data preparation (`6-1_data_preparation.py`)

| Parameter | Symbol | Value | How to calibrate |
|---|---|---|---|
| Distance decay exponent | $\rho$ | **2.0** | Fit to observed delivery choice data (try 1.0 / 3.0) |
| Target market share | $\tau$ | **0.30** | Reasonable for a new entrant; adjust to observed data |
| Intrinsic attractiveness | $A_j$ | **1.0** | Set proportional to locker capacity when data is available |

### Exact MILP (`6-2_gurobi_model.py`)

| Parameter | Value | Role |
|---|---|---|
| `P` | **5** | Maximum open lockers — main business lever |
| `MIP_GAP` | **0.05** | Stop at 5 % from optimal (faster); lower for tighter bound |
| `TIME_LIMIT` | **300 s** | Hard stop on main solve |
| `SENSITIVITY_TLIMIT` | **60 s** | Time budget per P value in sensitivity loop |

### Heuristics (`6-3_heuristics.py`)

| Parameter | Symbol | Value | Role |
|---|---|---|---|
| `P` | $P$ | **5** | Maximum open lockers |
| `DESTROY_SIZE` | $q$ | **`max(1, P//3)` = 1** | Lockers removed per LNS iteration |
| `NEIGHBOURHOOD` | $K$ | **`3*q` = 3** | Candidate pool for REPAIR sub-problem |
| `LNS_TIME_LIMIT` | — | **300 s** | Total LNS budget |
| `REPAIR_TIMELIM` | — | **30 s** | Gurobi budget per REPAIR sub-problem |
| `SA_ALPHA` | $\alpha$ | **0.995** | SA cooling rate (smaller → faster cooling) |
| `RANDOM_SEED` | — | **42** | Reproducibility |
| `SA_INITIAL_T` | $T_0$ | **auto** | Auto-calibrated: $T_0 = 0.01 \cdot f_0 / \ln 2$ so a 1 % loss is accepted at 50 % |

---

---

*Reference document — UFMG Internship 2026 — Bastien Jacquelin*
*File map and output descriptions: see `05_Results_and_Outputs.md`.*
