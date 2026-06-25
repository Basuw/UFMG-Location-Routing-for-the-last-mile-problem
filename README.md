# 📦 Location-Routing for the Last-Mile Problem

> Where should we open **parcel lockers** and **micro-hubs** in a city so that the largest share of
> demand is served at the lowest delivery cost?

A bilevel **Location-Routing Problem (LRP)** coupled with a **Multinomial-Logit (MNL) / Huff** customer
choice model, applied to **São Paulo**. Customers choose a locker according to a gravity model; we
decide *where* to open lockers and hubs, and *how* to route the deliveries — jointly.

*UFMG Internship 2026 — Bastien Jacquelin.*
🔗 [github.com/Basuw/UFMG-Location-Routing-for-the-last-mile-problem](https://github.com/Basuw/UFMG-Location-Routing-for-the-last-mile-problem)

---

## 🎯 What the model does

We place facilities on a **3-level grid hierarchy** and optimise a two-echelon delivery network:

```
Big hub (external, fixed) ──truck──▶ Small hubs ──van/bike (ρ₂)──▶ Lockers ──courier (ρ)──▶ Demand zones
        BIG                            G3 cells                      G2 cells                  G1 cells
```

| Layer | Grid | Role | Decision |
|---|---|---|---|
| **Big hub** | fixed, external | upstream source (flux ≥ total demand) | — |
| **Small hubs** | G3 (~25 km²) | dispatch parcels to lockers | open `v_h` (cost-driven count) |
| **Lockers** | G2 (~9 km²) | customer pick-up points | open `y_j` (exactly `P`) |
| **Demand zones** | G1 (~1 km²) | customers (MNL choice) | capture share `x_ij` |

The objective (minimise, in BRL/day):

```
  Σ opening + daily cost of lockers & hubs            (CapEx amortised + OpEx)
+ Σ fleet cost                                        (couriers)
+ Σ last-mile routing      ρ · l_ij · √(A_j·η_i)       (locker → customers, BHH approx.)
+ Σ hub→locker tours       ρ₂ · l_hj · a_hj           (2nd echelon, flexible assignment)
+ Σ uncaptured-demand cost L · ω_i · Z_i              (L = cost of one lost parcel)
```

solved as a MILP with **Gurobi** (Charnes–Cooper linearisation of the MNL share).

---

## 📚 Documentation

The model is built **step by step** — each note explains one piece. Start at the top; the **current
work** is `06`.

| Doc | Content |
|---|---|
| [`0_Basics`](Documentation/maths/0_Basics.md) | mathematical basics of the last-mile problem |
| [`01_Problem_Description`](Documentation/maths/01_Problem_Description.md) | problem statement (smart lockers) |
| [`02_Mathematical_Formulation`](Documentation/maths/02_Mathematical_Formulation.md) | LRP + MNL formulation |
| [`03_Resolution_Strategy`](Documentation/maths/03_Resolution_Strategy.md) | MILP, heuristics & MLP strategy |
| [`04_Market_share`](Documentation/maths/04_Market_share.md) | from pipeline data to the Gurobi model |
| [`04-5_Results_and_Outputs`](Documentation/maths/04-5_Results_and_Outputs.md) | results & output files |
| [`05_Heuristics`](Documentation/maths/05_Heuristics.md) | greedy + outer-approximation heuristics |
| ⭐ [**`06_Location_Routing`**](Documentation/maths/06_Location_Routing.md) | **current step — LRP-MNL + 2-echelon hubs** |

Reference material lives in [`Documentation/routing/`](Documentation/routing/) (Stokkink & Geroliminis
2025 — the BHH routing approximation + the LaTeX formulation) and [`Research/`](Research/).

---

## 🚧 Current step — `06_Location_Routing.md`

The location-routing extension is being built incrementally. See the **`## Results`** section of the
doc for the full story (and the maps of each fix).

- ✅ **Step 1** — *cost of lost market share, coherent capture radius, real 2nd echelon*
  - added a per-parcel cost `L` on uncaptured demand so the model is pushed to capture share;
  - widened the MNL capture radius (`A_HUFF`) to a coherent middle vs the exact MILP;
  - **fixed the "too many hubs" issue**: replaced the rigid locker→hub nesting by a **flexible
    assignment** `a_hj` with a hub→locker tour cost `ρ₂·l_hj` → one hub now covers several
    lockers, and `ρ₂` tunes the hub count.
- ⬜ **Step 2** — locker / hub **capacity** (test at 30 %, 40 %, … of total demand).
- ⬜ **Step 3** — **congestion** cost `φ(f) = c·f / (τ − f)` (clients complain at 70–80 % load).

---

## 🗂 Repository structure

```
.
├── Documentation/
│   ├── maths/         # step-by-step model write-ups (0 → 06)  ← start here
│   ├── routing/       # reference paper (Stokkink & Geroliminis) + LaTeX
│   └── img/           # figures used in the docs
├── Research/          # background papers (locker location, last-mile networks)
├── src/
│   ├── sao paulo/
│   │   ├── data/      # data.xlsx — candidate sites & capacities
│   │   ├── python/    # the pipeline (6-0 … 6-4) + run_pipeline.sh
│   │   ├── scripts/   # upstream prep (clustering, demand sim, allocation, routing)
│   │   └── results/   # CSV outputs, one set per method × P
│   └── mock/          # standalone Gurobi / LNS prototypes
├── tests/
├── Sujet.pdf          # internship subject
└── LICENSE
```

### Pipeline scripts (`src/sao paulo/python/`)

| Script | Role |
|---|---|
| `6-1_data_preparation.py` | build demand zones, utility matrix, Charnes–Cooper bounds |
| `6-2_gurobi_model.py` | **exact** MNL locker-location MILP |
| `6-3_heuristics.py` | **Greedy** + **Outer-Approximation** heuristics |
| ⭐ `6-4_location_routing.py` | **LRP-MNL** — lockers + small hubs + 2-echelon routing |
| `6-0_streamlit_map.py` | interactive **dashboard** (map, KPIs, cost breakdown) |
| `run_pipeline.sh` | orchestrates the pipeline for given `P` values |

---

## 🚀 Getting started

**Requirements:** Python 3.10+, a working **Gurobi** license, and:

```bash
pip install gurobipy numpy pandas streamlit streamlit-folium folium plotly openpyxl
```

**Run the pipeline** (location-routing only, for several locker budgets `P`):

```bash
cd "src/sao paulo/python"
./run_pipeline.sh --lr-only 1 3 5 7      # or the full pipeline: ./run_pipeline.sh 1 3 5 7
```

**Launch the dashboard:**

```bash
streamlit run 6-0_streamlit_map.py
```

Then pick a **Method** (the LRP variants tell the story: *routing double-count → coherent costs →
two-echelon hubs*) and a **P**; the map shows the lockers, the small hubs, the hub→locker tours and
the per-zone market share, with a full daily **cost breakdown**.

### Tuning the model (`6-4_location_routing.py`)

Key parameters are env-overridable (defaults in the doc's *Constants used* table):

```bash
MNL_P=7 MNL_L=20 MNL_COST_PER_KM_HUB=100 MNL_A_HUFF=12 python3 6-4_location_routing.py
```

| Env var | Meaning |
|---|---|
| `MNL_P` / `MNL_P_HUB` | number of lockers / max small hubs |
| `MNL_L` | cost of one uncaptured parcel (drives capture) |
| `MNL_COST_PER_KM` / `MNL_COST_PER_KM_HUB` | last-mile (ρ) / hub→locker (ρ₂) cost per km |
| `MNL_A_HUFF` / `MNL_ALPHA` | MNL attractiveness / distance-decay (capture radius) |

---

## 📈 Results (final two-echelon model, São Paulo)

Market share grows monotonically with the locker budget `P`, and the hub count stays realistic:

| P (lockers) | Small hubs | Market share |
|:---:|:---:|:---:|
| 1 | 1 | **8.0 %** |
| 3 | 1 | **19.2 %** |
| 5 | 2 | **28.6 %** |
| 7 | 2 | **36.5 %** |

---

## 📖 References

- **Stokkink & Geroliminis (2025)** — bilevel location-routing with BHH continuum routing approximation
  *(the routing cost `c_ij ≈ l_ij·√(A_j·η_i)`)* — [`Documentation/routing/`](Documentation/routing/).
- **Winkenbach et al. (2016)** — designing multi-tier, multi-modal last-mile distribution networks — [`Research/`](Research/).
- *Optimal locker location under a multinomial-logit choice model* — [`Research/`](Research/).

---

## 👤 Author & license

**Bastien Jacquelin** — UFMG Internship 2026. Licensed under the terms in [`LICENSE`](LICENSE).
