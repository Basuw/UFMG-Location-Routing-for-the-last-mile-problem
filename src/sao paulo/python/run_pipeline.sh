#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_pipeline.sh — Lance le pipeline MNL/LRP pour une ou plusieurs valeurs de P.
#
# Usage :
#   ./run_pipeline.sh 7                      # pipeline complet, P=7
#   ./run_pipeline.sh 1 3 5 7                # pipeline complet, plusieurs P
#   ./run_pipeline.sh --milp-only 1 3 5 7    # données + MILP uniquement (pas de greedy/OA/LRP)
#   ./run_pipeline.sh --heuristics-only 1 3 5 7  # données + Greedy+OA uniquement
#   ./run_pipeline.sh --lr-only 1 3 5 7      # données + LRP-MNL uniquement (6-1 + 6-4)
#   ./run_pipeline.sh                        # pipeline complet, P=5 par défaut
#
# Ordre d'exécution (pipeline complet) pour chaque P :
#   1. 6-1_data_preparation.py   (recalcule Z_bar/Z_under — obligatoire avant 6-2 et 6-4)
#   2. 6-2_gurobi_model.py       (MILP exact MNL, B&B pur)
#   3. 6-3_heuristics.py         (Greedy + Outer Approximation)
#   4. 6-4_location_routing.py   (LRP-MNL — MILP avec couts de routage BHH)
#
# Note : 6-4 tourne independamment de 6-2/6-3.
#        Utiliser --lr-only pour ne lancer que la localisation-routage.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parse arguments ───────────────────────────────────────────────────────────
MILP_ONLY=0
HEURISTICS_ONLY=0
LR_ONLY=0
P_VALUES=()

for arg in "$@"; do
    case "$arg" in
        --milp-only)        MILP_ONLY=1 ;;
        --heuristics-only)  HEURISTICS_ONLY=1 ;;
        --lr-only)          LR_ONLY=1 ;;
        *)                  P_VALUES+=("$arg") ;;
    esac
done

if [ ${#P_VALUES[@]} -eq 0 ]; then
    P_VALUES=(5)
fi

# Vérifier les flags mutuellement exclusifs
EXCLUSIVE=$((MILP_ONLY + HEURISTICS_ONLY + LR_ONLY))
if [ $EXCLUSIVE -gt 1 ]; then
    echo "Error: --milp-only, --heuristics-only et --lr-only sont mutuellement exclusifs."
    exit 1
fi

# ── Couleurs ──────────────────────────────────────────────────────────────────
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[0;33m"
PURPLE="\033[0;35m"
RESET="\033[0m"

# ── Mode affiché ──────────────────────────────────────────────────────────────
if   [ $MILP_ONLY -eq 1 ];        then MODE="MILP only (6-1 + 6-2)"
elif [ $HEURISTICS_ONLY -eq 1 ];  then MODE="Heuristics only (6-1 + 6-3)"
elif [ $LR_ONLY -eq 1 ];          then MODE="Location-Routing only (6-1 + 6-4)"
else                                    MODE="Full pipeline (6-1 + 6-2 + 6-3 + 6-4)"
fi

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${CYAN}  Mode  : ${MODE}${RESET}"
echo -e "${CYAN}  P     : ${P_VALUES[*]}${RESET}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

total=${#P_VALUES[@]}
idx=0

for P in "${P_VALUES[@]}"; do
    idx=$((idx + 1))
    echo ""
    echo -e "${YELLOW}  ── Run ${idx}/${total} — P = ${P} ──────────────────────────────${RESET}"
    export MNL_P=$P

    # ── Step 1 : Data preparation (toujours nécessaire) ──────────────────
    echo -e "\n${GREEN}[P=$P] Step 1 — Data preparation…${RESET}"
    python3 6-1_data_preparation.py
    echo -e "${GREEN}[P=$P] Data preparation done.${RESET}"

    # ── Step 2 : Exact MILP (MNL pur) ────────────────────────────────────
    if [ $HEURISTICS_ONLY -eq 0 ] && [ $LR_ONLY -eq 0 ]; then
        echo -e "\n${GREEN}[P=$P] Step 2 — Exact MILP MNL (Gurobi)…${RESET}"
        python3 6-2_gurobi_model.py
        echo -e "${GREEN}[P=$P] Exact MILP done.${RESET}"
    fi

    # ── Step 3 : Heuristics (Greedy + OA) ────────────────────────────────
    if [ $MILP_ONLY -eq 0 ] && [ $LR_ONLY -eq 0 ]; then
        echo -e "\n${GREEN}[P=$P] Step 3 — Heuristics (Greedy + OA)…${RESET}"
        python3 6-3_heuristics.py
        echo -e "${GREEN}[P=$P] Heuristics done.${RESET}"
    fi

    # ── Step 4 : Location-Routing MNL (LRP-MNL) ─────────────────────────
    if [ $MILP_ONLY -eq 0 ] && [ $HEURISTICS_ONLY -eq 0 ]; then
        echo -e "\n${PURPLE}[P=$P] Step 4 — Location-Routing MNL (6-4)…${RESET}"
        python3 6-4_location_routing.py
        echo -e "${PURPLE}[P=$P] Location-Routing done.${RESET}"
    fi

done

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${CYAN}  Terminé — P = ${P_VALUES[*]}  (${MODE})${RESET}"
echo -e "${CYAN}  Dashboard : streamlit run 6-0_streamlit_map.py${RESET}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
