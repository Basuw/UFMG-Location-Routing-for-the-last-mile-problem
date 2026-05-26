# Heuristics for the MNL Locker Location Problem

This document describes the two-phase heuristic algorithm used to solve the
MNL locker location problem at scale. The approach is grounded in two
academic references:

- **Camargo (2024)** — *Bilevel location-routing with MNL demand* —
  proposes Outer Approximation (OA) to solve the fractional, nonlinear
  objective to provable near-optimality.
- **Stokkink & Geroliminis (2025)** — *Optimal micro-hub locations in a
  multi-modal last-mile delivery system*, Transportation Research Part E
  203, 104344 — proposes a Continuum Approximation (CA) framework to
  pre-compute routing costs analytically, enabling an efficient MILP
  formulation for hub location.

Both approaches inform our implementation: OA for solution quality, CA for
routing cost estimation.

---

## 1. Problem Reminder

We wish to choose at most $P$ locker sites $\mathcal{O} \subseteq J$ to
maximise captured parcel demand under MNL choice:

$$
f(\mathcal{O}) = \sum_{i \in I} w_i \cdot
\frac{S_i(\mathcal{O})}{S_i(\mathcal{O}) + u_i^0},
\qquad
S_i(\mathcal{O}) = \sum_{j \in \mathcal{O}} u_{ij}
$$

The exact MILP (script `6-2_gurobi_model.py`) solves this via
Charnes-Cooper + McCormick linearisation but can take several minutes for
large instances. The heuristic (script `6-3_heuristics.py`) finds a
high-quality solution in seconds by combining a greedy phase with an
Outer Approximation phase.

---

## 2. Phase 1 — Greedy Construction

### 2.1 Principle

Open lockers one at a time, always picking the candidate that yields the
largest **marginal gain** to the MNL objective.

**State:** maintain $S_i^{(t)} = \sum_{j \in \mathcal{O}^{(t)}} u_{ij}$.
Initially $S_i^{(0)} = 0$.

**Marginal gain** of adding $j^*$ to the current open set:

$$
\Delta(j^*) = \sum_{i \in I} w_i \cdot
\frac{u_{ij^*} \cdot u_i^0}
{\bigl(S_i^{(t)} + u_{ij^*} + u_i^0\bigr)\bigl(S_i^{(t)} + u_i^0\bigr)}
$$

**Algorithm:**

```
O ← ∅,   S_i ← 0  for all i
for step = 1 … P:
    j* ← argmax_{j ∉ O}  Δ(j)
    O ← O ∪ {j*}
    S_i ← S_i + u_{ij*}   for all i
return O
```

**Complexity:** $O(P \cdot |J| \cdot |I|)$ — linear in data size.

### 2.2 Theoretical guarantee (Camargo 2024, §3.1)

The MNL objective $f$ is **submodular** in the binary vector $y$: adding
a locker is always beneficial, but less so when many lockers are already
open.  For a monotone submodular function, the greedy algorithm achieves at
least:

$$
f(\mathcal{O}_{\text{greedy}}) \;\geq\; \left(1 - \frac{1}{e}\right) f(\mathcal{O}^*) \;\approx\; 63\%
$$

In practice, on the São Paulo instance the greedy solution reaches **75–78%**
of the optimal objective — well above the worst-case bound.

---

## 3. Phase 2 — Outer Approximation (Camargo 2024)

### 3.1 Motivation

The Charnes-Cooper MILP from `6-2_gurobi_model.py` has $|I| \times |J|$
continuous variables $x_{ij}$ on top of $|J|$ binary variables $y_j$.  On
the São Paulo instance that is 132 525 continuous variables plus 57 binary
variables — solving it takes 2–5 minutes.

We can use **Outer Approximation (OA)**: instead of
reformulating the fractional objective, we iteratively linearise it around
the current solution. Each iteration only has $|J|$ binary variables $y_j$
and one continuous variable $\eta$.

### 3.2 Mathematical foundation

Since $f(y) = \sum_i w_i \cdot S_i(y)/(S_i(y) + u_i^0)$ is **concave** in
$y$ (sum of concave fractions composed with linear $S_i(y) = \sum_j u_{ij} y_j$),
it lies **below** any tangent plane. At a point $y^{(k)}$ with
$S_i^{(k)} = \sum_j u_{ij} y_j^{(k)}$:

$$
f(y) \;\leq\; f\!\left(y^{(k)}\right)
+ \sum_{j \in J} \frac{\partial f}{\partial y_j}\Bigg|_{y^{(k)}}
  \!\!\!\bigl(y_j - y_j^{(k)}\bigr)
\qquad \forall\, y \in \{0,1\}^{|J|}
$$

The gradient is:

$$
\frac{\partial f}{\partial y_j}\Bigg|_{y^{(k)}} =
\sum_{i \in I} w_i \cdot
\frac{u_{ij} \cdot u_i^0}{\bigl(S_i^{(k)} + u_i^0\bigr)^2}
$$

### 3.3 OA Master Problem

Each iteration adds a new **cut** to a small MILP called the *master problem*:

$$
\begin{aligned}
\max_{\eta,\, y} \quad & \eta \\
\text{s.t.} \quad
& \eta \;\leq\; f\!\left(y^{(k)}\right)
  + \sum_{j} g_j^{(k)} \bigl(y_j - y_j^{(k)}\bigr)
  \qquad \forall\, k = 0, \dots, t-1 \\
& \sum_j y_j \;\leq\; P \\
& y_j \;\in\; \{0,1\} \quad \forall\, j
\end{aligned}
$$

where $g_j^{(k)} = \partial f / \partial y_j|_{y^{(k)}}$ is the gradient
computed at the $k$-th iterate.

- $\eta$ is an **upper bound** on $f(y)$ for any feasible $y$; it decreases as cuts accumulate.
- The best **lower bound** is $\max_k f(y^{(k)})$.
- The **optimality gap** shrinks to zero as cuts are added.

### 3.4 OA Algorithm

```
Initialise:  y^(0) ← greedy solution
             LB ← f(y^(0)),   UB ← +∞
             cuts ← { (y^(0), f^(0), g^(0)) }

for t = 1, 2, … until (UB - LB)/LB < ε or time limit:

    1. Solve master MILP with all cuts → (y^(t), η^(t))
       UB ← min(UB, η^(t))

    2. Evaluate true objective at y^(t):
       f^(t) = Σ_i w_i S_i(y^(t)) / (S_i(y^(t)) + u0_i)

    3. Update lower bound:
       if f^(t) > LB:  LB ← f^(t),  best_y ← y^(t)

    4. Compute gradient g^(t) at y^(t) and add cut to master.

    5. If y^(t) == y^(t-1) (same binary solution): converged → stop.

Return: best_y,  LB  (= true objective at best solution)
```

**Convergence:** because $\{0,1\}^{|J|}$ is finite and each new binary
solution generates a cut that is tight at that solution, OA terminates in
finitely many iterations.  In practice on the São Paulo instance
convergence takes **5–30 iterations** (< 60 seconds).

### 3.5 Comparison with the full MILP

| Property | Exact MILP (6-2) | OA heuristic (6-3) |
|---|---|---|
| Variables per solve | $|J| + |I| + |I||J|$ | $|J| + 1$ |
| Constraints per solve | $|I|(3|J|+1) + 1$ | $t + 1$ (grows with iterations) |
| Solve time (São Paulo, P=7) | ~3–5 min | ~30–60 s |
| Optimality gap | ≤ MIP_GAP (5%) | provably ≤ ε at convergence |
| Warm start needed | Yes (greedy) | Yes (greedy, Phase 1) |

---

## 4. Routing Cost Approximation (Stokkink & Geroliminis 2025)

### 4.1 Motivation

Stokkink & Geroliminis (2025) study a multi-modal last-mile delivery system
(truck → metro → micro-mobility).  Their key methodological contribution is
a **Continuum Approximation (CA)** of routing costs that avoids solving a
vehicle routing problem explicitly.

We adapt their CA formula to estimate the **operational delivery cost** per
zone once the locker locations are fixed — a useful post-optimisation metric
independent of the demand capture objective.

### 4.2 Continuum Approximation formula

Following Daganzo (1984) and Stokkink & Geroliminis (2025, §4.3), for a
zone $i$ with:
- $d_i$ = daily parcel demand (parcels/day)
- $A_i$ = zone area (km²)
- $q^{\text{tour}}$ = courier capacity per tour (parcels)
- $k \approx 0.57$ = Beardwood-Halton-Hammersley constant

the **intra-zone routing length** (total distance traveled within zone $i$):

$$
L_i^{\text{intra}} = k \sqrt{A_i \cdot d_i}
$$

The **number of tours** per day:

$$
m_i = \left\lceil \frac{d_i}{q^{\text{tour}}} \right\rceil
$$

The **inter-zone line-haul** from the assigned locker $j^* = \arg\min_{j \in \mathcal{O}} d(i,j)$
back and forth:

$$
L_i^{\text{inter}} = 2 \cdot m_i \cdot \text{dist}(i, j^*)
$$

**Total routing length** for zone $i$:

$$
L_i^{\text{total}} = L_i^{\text{intra}} + L_i^{\text{inter}}
$$

This formula allows computing a **delivery cost score** per zone as a
post-processing step, giving insight into which zones are cheapest to serve
with a given locker placement.

### 4.3 Zone division

Stokkink & Geroliminis (2025, §4.2) assign each demand point to the
nearest metro station via a **Voronoi diagram**.  In our São Paulo
implementation, zones are already defined by the grid cell structure of the
dataset, with the centroid lat/lon computed from order locations within each
cell.

---

## 5. Script Summary

| Script | Rôle | Fichiers de sortie |
|---|---|---|
| `6-1_data_preparation.py` | Calcul des utilités et bornes Z_bar / Z_under | `zone_demand.csv`, `utility_matrix.csv` |
| `6-2_gurobi_model.py` | MILP exact (Charnes-Cooper + McCormick) | `mnl_location_results_P{P}.csv`, `mnl_location_lockers_P{P}.csv` |
| `6-3_heuristics.py` | Greedy + OA (Camargo 2024) | `mnl_greedy_results_P{P}.csv`, `mnl_oa_results_P{P}.csv`, `mnl_oa_convergence_P{P}.csv` |
| `run_pipeline.sh` | Orchestrateur — lance les 3 scripts dans l'ordre | — |
| `6-0_streamlit_map.py` | Dashboard interactif | — |

All scripts write a **runtime log** to `results/utils/solve_times.csv` so
that the Streamlit dashboard can display method comparison with wall-clock
times.

---

## 6. Lancer le pipeline (`run_pipeline.sh`)

Le script `run_pipeline.sh` orchestre les trois scripts Python dans le bon
ordre et propage la valeur de `P` à chacun via la variable d'environnement
`MNL_P`.

> **Pourquoi relancer `6-1` à chaque P ?**
> Les bornes `Z_bar` et `Z_under` dépendent de `P` (somme des P plus petites
> utilités par zone). Si `P_BOUND ≠ P`, les bornes sont incorrectes et le
> warm start greedy est rejeté par Gurobi. Le script assure que `P_BOUND = P`
> à chaque run.

### Utilisation

```bash
# Depuis le dossier src/sao paulo/python/
cd "src/sao paulo/python"

./run_pipeline.sh 7          # un seul P
./run_pipeline.sh 3 5 7      # plusieurs P en séquence
./run_pipeline.sh 1 3 5 7    # les 4 valeurs d'un coup
./run_pipeline.sh            # P=5 par défaut
```

### Puis lancer le dashboard

```bash
streamlit run 6-0_streamlit_map.py
```

### Fonctionnement interne

Pour chaque valeur de P passée en argument, le script :

1. Exporte `MNL_P=$P` dans l'environnement shell
2. Lance `python3 6-1_data_preparation.py` → recalcule `Z_bar`/`Z_under` pour ce P
3. Lance `python3 6-2_gurobi_model.py` → MILP exact, sauvegarde `mnl_location_results_P{P}.csv` et `mnl_location_lockers_P{P}.csv`
4. Lance `python3 6-3_heuristics.py` → Greedy + OA, sauvegarde `mnl_greedy_results_P{P}.csv`, `mnl_oa_results_P{P}.csv`, etc.

Chaque script Python lit `P` depuis l'environnement :
```python
import os
P = int(os.environ.get("MNL_P", 5))   # valeur par défaut si MNL_P non défini
```

---

## 7. Parameters

### 6-2_gurobi_model.py

| Paramètre | Défaut | Description |
|---|---|---|
| `P` | `$MNL_P` ou 7 | Nombre max de lockers à ouvrir |
| `MIP_GAP` | 0.05 | Tolérance d'optimalité (5 %) |
| `TIME_LIMIT` | 300 | Limite de temps Gurobi (s) |
| `SENSITIVITY_TLIMIT` | 60 | Temps par valeur de P en analyse de sensibilité (s) |

### 6-3_heuristics.py

| Paramètre | Défaut | Description |
|---|---|---|
| `P` | `$MNL_P` ou 5 | Nombre max de lockers à ouvrir |
| `OA_MAX_ITER` | 100 | Nombre max d'itérations OA |
| `OA_TOL` | 1e-4 | Seuil de convergence du gap |
| `OA_TIME_LIMIT` | 300 | Budget temps total OA (s) |
| `OA_MASTER_TLIM` | 30 | Limite de temps par MILP maître (s) |
| `TOUR_CAPACITY` | 20 | Colis par tournée (CA routing) |
| `COURIER_SPEED` | 15 | Vitesse coursier en km/h (vélo cargo) |

---

## 7. References

- Camargo, R.S. (2024). *Bilevel location-routing for last-mile delivery
  with multinomial logit demand*. Working paper, UFMG.
- Stokkink, P., Geroliminis, N. (2025). *On the optimal micro-hub locations
  in a multi-modal last-mile delivery system*. Transportation Research Part
  E, 203, 104344. https://doi.org/10.1016/j.tre.2025.104344
- Daganzo, C.F. (1984). *The length of tours in zones of different shapes*.
  Transportation Research B, 18(2), 135–145.
- Nemhauser, G.L., Wolsey, L.A., Fisher, M.L. (1978). *An analysis of
  approximations for maximizing submodular set functions*. Mathematical
  Programming, 14, 265–294.

---

*Reference document — UFMG Internship 2026 — Bastien Jacquelin*
