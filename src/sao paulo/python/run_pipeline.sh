#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_pipeline.sh — Lance le pipeline MNL complet pour une ou plusieurs
#                   valeurs de P.
#
# Usage :
#   ./run_pipeline.sh 7          # un seul P
#   ./run_pipeline.sh 3 5 7      # plusieurs P en séquence
#   ./run_pipeline.sh            # utilise P=5 par défaut
#
# Ordre d'exécution pour chaque P :
#   1. 6-1_data_preparation.py   (recalcule Z_bar/Z_under pour ce P)
#   2. 6-2_gurobi_model.py       (MILP exact)
#   3. 6-3_heuristics.py         (Greedy + Outer Approximation)
# ---------------------------------------------------------------------------

set -euo pipefail   # stop on first error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Options
# --sensitivity : active l'analyse de sensibilité dans 6-2 (désactivée par défaut,
#                 car elle ajoute jusqu'à 10 × 60s = 10 min par valeur de P)
SENSITIVITY=0
P_VALUES=()
for arg in "$@"; do
    if [ "$arg" = "--sensitivity" ]; then
        SENSITIVITY=1
    else
        P_VALUES+=("$arg")
    fi
done

# Valeur de P par défaut si aucun argument
if [ ${#P_VALUES[@]} -eq 0 ]; then
    P_VALUES=(5)
fi

export MNL_SENSITIVITY=$SENSITIVITY

# Couleurs pour les logs
GREEN="\033[0;32m"
CYAN="\033[0;36m"
RED="\033[0;31m"
RESET="\033[0m"

total=${#P_VALUES[@]}
idx=0

for P in "${P_VALUES[@]}"; do
    idx=$((idx + 1))
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${CYAN}  Run ${idx}/${total} — P = ${P}${RESET}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

    export MNL_P=$P

    # ── Step 1 : Data preparation ─────────────────────────────────────────
    echo -e "\n${GREEN}[P=$P] Step 1/3 : Data preparation…${RESET}"
    python3 6-1_data_preparation.py
    echo -e "${GREEN}[P=$P] Step 1/3 done.${RESET}"

    # ── Step 2 : Exact MILP ───────────────────────────────────────────────
    echo -e "\n${GREEN}[P=$P] Step 2/3 : Exact MILP (Gurobi)…${RESET}"
    python3 6-2_gurobi_model.py
    echo -e "${GREEN}[P=$P] Step 2/3 done.${RESET}"

    # ── Step 3 : Heuristics (Greedy + OA) ────────────────────────────────
    echo -e "\n${GREEN}[P=$P] Step 3/3 : Heuristics (Greedy + OA)…${RESET}"
    python3 6-3_heuristics.py
    echo -e "${GREEN}[P=$P] Step 3/3 done.${RESET}"

done

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${CYAN}  Pipeline terminé pour P = ${P_VALUES[*]}${RESET}"
echo -e "${CYAN}  Lance le Streamlit avec :${RESET}"
echo -e "${CYAN}    streamlit run 6-0_streamlit_map.py${RESET}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
