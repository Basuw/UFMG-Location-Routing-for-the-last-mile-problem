# Stratégie de Résolution — MILP, Heuristiques et MLP

## 1. Ai-je besoin d'heuristiques ?

La réponse dépend de la **taille de l'instance**.

```
Taille de l'instance          Approche recommandée
─────────────────────────────────────────────────────
│J│ ≤ 30 lockers              → Gurobi exact (MILP après linéarisation)
│K│ ≤ 10 clusters             
│I│ ≤ 100 zones               

│J│ ~ 100 lockers             → Gurobi + décomposition (Benders)
│K│ ~ 20 clusters              
│I│ ~ 500 zones               

│J│ > 500 lockers             → Heuristiques (LNS, GRASP)
│I│ > 2000 zones              → + Gurobi pour les sous-problèmes
(MG complet ~ 20 000 points)  
```

> **Pour commencer :** travaille en petit (|J| ≤ 30, |I| ≤ 100). Gurobi sera suffisant et tu pourras valider ton modèle avant de t'attaquer à la vraie échelle.

---

## 2. Le vrai problème : l'objectif MNL n'est pas linéaire

L'objectif :

$$\max \sum_{i \in I} w_i \cdot \frac{S_i}{S_i + u_i^0} \quad \text{avec} \quad S_i = \sum_{j \in J} u_{ij} \cdot y_j$$

est une **somme de fractions** — Gurobi ne sait pas optimiser ça directement. Il faut le **transformer en MILP** (Mixed-Integer Linear Program) avant de le passer à Gurobi.

Il y a deux méthodes principales.

---

## 3. Méthode 1 — Transformation de Charnes-Cooper (exacte)

### Idée

Pour une seule fraction $\frac{S_i}{S_i + u_i^0}$, on pose :

$$\theta_i = \frac{1}{S_i + u_i^0}$$

Alors :

$$\frac{S_i}{S_i + u_i^0} = S_i \cdot \theta_i = \left(\sum_{j \in J} u_{ij} \cdot y_j\right) \cdot \theta_i$$

Le produit $y_j \cdot \theta_i$ est bilinéaire (binaire × continue). On introduit :

$$z_{ij} = y_j \cdot \theta_i$$

### Contraintes de linéarisation (McCormick)

Pour linéariser $z_{ij} = y_j \cdot \theta_i$ où $y_j \in \{0,1\}$ et $0 \leq \theta_i \leq \theta_i^{\max}$ :

$$z_{ij} \leq \theta_i^{\max} \cdot y_j \tag{M1}$$
$$z_{ij} \leq \theta_i \tag{M2}$$
$$z_{ij} \geq \theta_i - \theta_i^{\max}(1 - y_j) \tag{M3}$$
$$z_{ij} \geq 0 \tag{M4}$$

### Contrainte de définition de $\theta_i$

$$\theta_i \cdot \left(\sum_{k \in J} u_{ik} \cdot y_k + u_i^0\right) = 1$$

devient (en développant et en substituant $z_{ik}$) :

$$\sum_{k \in J} u_{ik} \cdot z_{ik} + u_i^0 \cdot \theta_i = 1, \quad \forall i \in I \tag{CC}$$

### Objectif linéarisé

$$\max \sum_{i \in I} w_i \sum_{j \in J} u_{ij} \cdot z_{ij}$$

### Résumé MILP résultant

```
Variables :
  y_j  ∈ {0,1}        (ouvrir locker j ?)
  θ_i  ≥ 0            (inverse du dénominateur MNL)
  z_ij ≥ 0            (produit linéarisé y_j × θ_i)

Objectif :
  max Σ_i w_i Σ_j u_ij · z_ij

Contraintes clés :
  (CC)   Σ_k u_ik · z_ik + u_i^0 · θ_i = 1     ∀ i
  (M1-4) Inégalités McCormick sur z_ij           ∀ i,j
  (P)    Σ_j y_j ≤ P                             (budget)
```

Ce MILP est **exact** et Gurobi peut le résoudre directement.

---

## 4. Méthode 2 — Approximation linéaire par morceaux (PWL)

### Idée

La fonction $f(S_i) = \frac{S_i}{S_i + u_i^0}$ est une **fonction concave croissante** de $S_i$.

```
f(S)
  1 │                    ─────────
    │               ───
    │          ──
    │      ─
    │   ─
  0 │─
    └──────────────────────────────── S_i
       0       u^0     2u^0    3u^0
```

On peut l'**approcher par des segments** (Gurobi supporte nativement les PWL) :

```python
import gurobipy as gp

# Définir les points de l'approximation (breakpoints)
breakpoints_S = [0, u0/4, u0/2, u0, 2*u0, 4*u0]
breakpoints_f = [s / (s + u0) for s in breakpoints_S]

# Gurobi crée automatiquement la PWL
model.setPWLObj(S_var, breakpoints_S, breakpoints_f)
```

### Avantages et inconvénients

| | Charnes-Cooper (exacte) | PWL (approchée) |
|---|---|---|
| Exactitude | ✅ Exacte | ⚠️ Approximation |
| Complexité | Plus de variables ($z_{ij}$) | Simple à coder |
| Taille modèle | $O(\|I\| \cdot \|J\|)$ variables en plus | $O(\|I\|)$ contraintes en plus |
| Recommandation | Petites/moyennes instances | Prototypage rapide |

---

## 5. La partie routage — Lazy Constraints (pas une heuristique)

### Le problème des sous-tours

Les contraintes MTZ (anti-sous-tours) pour le VRP sont **très nombreuses** : $O(n^2)$ contraintes dès le départ. Cela ralentit Gurobi même si la plupart ne seront jamais actives.

### Solution : Lazy Constraints (coupes à la volée)

Au lieu d'ajouter toutes les contraintes MTZ dès le début, on dit à Gurobi : *"si tu trouves une solution avec un sous-tour, je te l'interdis, sinon continue"*.

```
Boucle Gurobi :
  1. Gurobi trouve une solution entière candidate
  2. Callback : on vérifie si la solution contient des sous-tours
  3. Si OUI → on ajoute dynamiquement la contrainte qui l'interdit
  4. Gurobi reprend avec cette nouvelle contrainte
  5. Répéter jusqu'à ce qu'aucun sous-tour ne soit détecté
```

C'est **exact** (pas une heuristique) — on arrive toujours à la solution optimale, juste plus vite.

---

## 6. Pour les grandes instances — Large Neighborhood Search (LNS)

Si Gurobi ne converge pas en temps raisonnable sur les grandes instances, on utilise une **métaheuristique LNS** :

```
Algorithme LNS :
  1. Partir d'une solution initiale (heuristique gloutonne)
  2. Répéter :
     a. DESTROY  : retirer aléatoirement q lockers de la solution
     b. REPAIR   : résoudre le sous-problème avec Gurobi (petit !)
                   pour choisir les q meilleurs lockers à remettre
     c. Accepter si amélioration (ou avec probabilité → Simulated Annealing)
  3. Retourner la meilleure solution trouvée
```

### Pourquoi ça marche bien ici ?

- Le sous-problème REPAIR (q lockers à choisir parmi les candidats) est **petit** → Gurobi exact en quelques secondes
- On explore intelligemment l'espace des solutions sans tout résoudre
- Convergence rapide en pratique sur les LRP

---

## 7. MLP — Multi-Layer Perceptron

Le MLP (réseau de neurones à couches denses) intervient à deux endroits distincts dans ce problème, avec des rôles très différents.

---

### 7.1 MLP comme modèle de demande (alternative au MNL)

Le modèle MNL utilisé jusqu'ici suppose une forme fonctionnelle fixe pour l'utilité :

$$u_{ij} = \frac{A_j}{d_{ij}^\rho}$$

Cette formule est simple et interprétable, mais elle **impose une loi de puissance** et ne peut pas capturer des effets non-linéaires complexes (interactions entre attributs, effets de saturation locaux, hétérogénéité des zones...).

Un MLP peut **apprendre directement** la fonction de choix à partir de données historiques de livraison :

```
Entrées du MLP :
  features de zone i  : densité population, revenu moyen, nb de points relais existants, …
  features de locker j: capacité, type de service, distance d_ij, …

Sortie :
  P(client en zone i choisit locker j) = probabilité de choix estimée
```

**Quand utiliser le MLP comme modèle de demande ?**

| Situation | MNL | MLP |
|---|---|---|
| Pas de données historiques | ✅ Fonctionne avec paramètres calibrés | ❌ Pas entraînable |
| Données riches disponibles (livraisons passées) | ⚠️ Approximation trop simple | ✅ Capture la vraie complexité |
| Interprétabilité requise | ✅ Coefficients lisibles | ⚠️ Boîte noire |
| Intégration dans Gurobi | ✅ Linéarisable (Charnes-Cooper) | ❌ Non-linéaire, non-convexe |

**Problème clé : un MLP n'est pas linéarisable.**
Si on remplace le MNL par un MLP dans l'objectif, on ne peut plus appliquer Charnes-Cooper ni McCormick. L'optimisation exacte devient très difficile. Les solutions sont :

- **Figer le MLP, énumérer** : évaluer l'objectif MLP pour toutes les combinaisons de $P$ lockers parmi $|J|$ candidats. Faisable si $\binom{|J|}{P}$ est petit (ex. $P=3$, $|J|=20$ → 1 140 combinaisons).
- **Figer le MLP, utiliser LNS** : le MLP sert seulement à évaluer $f(\mathcal{O})$ à chaque étape du LNS — Gurobi n'est plus nécessaire si on utilise le greedy + LNS pur.
- **Approximer le MLP par une PWL** : ajuster une approximation linéaire par morceaux de la sortie du MLP pour rester dans le cadre MILP.

---

### 7.2 MLP comme accélérateur du MILP (warm start appris)

Ici, on **conserve le MNL et le MILP**, mais on utilise un MLP pour générer de meilleures solutions initiales que le greedy simple.

**Idée :** entraîner un MLP supervisé sur un ensemble de petites instances résolues exactement par Gurobi :

```
Données d'entraînement :
  Instance k : (matrice u_ij, demandes w_i, u0_i)  →  solution optimale y_j*

MLP apprend :
  (features de l'instance) → probabilité que chaque locker j soit ouvert
```

Au moment de résoudre une nouvelle grande instance :
1. Calculer les features de l'instance
2. Passer dans le MLP → scores $\hat{p}_j \in [0,1]$ pour chaque candidat $j$
3. Ouvrir les $P$ lockers avec les scores les plus élevés → **warm start MLP**
4. Donner ce warm start à Gurobi (ou au LNS) comme solution initiale

**Pourquoi c'est mieux que le greedy seul ?**
Le greedy sélectionne séquentiellement et peut se piéger dans des sous-optima locaux (effet du premier locker qui monopolise la demande centrale). Un MLP entraîné sur des solutions optimales peut capturer des patterns globaux : *"quand la demande est concentrée dans deux clusters éloignés, il faut un locker dans chaque cluster"*.

**Pipeline d'entraînement :**

```
1. Générer N instances synthétiques (petites : |I|=100, |J|=20)
       └── varier les positions des zones, les demandes, les u0
2. Résoudre chaque instance exactement avec 6-2_gurobi_model.py
       └── récupérer y_j* = solution optimale
3. Construire les features :
       pour chaque candidat j : score_greedy_j, distance_au_centroid_demande, S_j_max, ...
4. Entraîner le MLP :
       input  : features de l'instance + features de chaque candidat j
       output : y_j* (binaire, traité comme classification)
       loss   : binary cross-entropy
5. Sur une nouvelle instance :
       → prédire les scores, prendre les top-P comme warm start
```

**Comparaison des warm starts :**

| Méthode | Qualité de départ | Temps de calcul | Nécessite données |
|---|---|---|---|
| Aucun (Gurobi seul) | Mauvaise (gap 300%) | 0 s | Non |
| Greedy | Bonne (~70-80% optimal) | < 1 s | Non |
| **MLP warm start** | **Meilleure (~85-95% optimal)** | **< 0.1 s** | **Oui (instances résolues)** |

---

### 7.3 Quand intégrer le MLP ?

```
Phase 1 (São Paulo, validation) ──► MILP exact + warm start greedy
                                          Pas besoin de MLP

Phase 2 (données réelles, grande échelle)
  Si données historiques disponibles ──► Entraîner MLP comme modèle de demande
                                         et comparer avec MNL

  Si nombreuses instances à résoudre ──► Entraîner MLP comme générateur de warm start
                                         pour accélérer LNS

Phase 3 (production) ──► MLP demande + LNS guidé par MLP warm start
                          Gurobi seulement pour valider les sous-problèmes REPAIR
```

---

## 8. Stratégie recommandée pour ce stage (mise à jour)

```
Semaines 1-2 : Valider le modèle
  └── Gurobi exact sur petite instance (|J|=20, |I|=50)
  └── Linéarisation Charnes-Cooper
  └── MTZ classique (pas encore de lazy constraints)

Semaines 3-4 : Améliorer la résolution
  └── Remplacer MTZ par lazy constraints (subtour callbacks)
  └── Tester sur instances moyennes (|J|=100, |I|=500)
  └── Mesurer le gap d'optimalité

Semaines 5-6 : Passage à l'échelle
  └── Implémenter LNS si Gurobi dépasse 10 min sur grande instance
  └── Tester sur vraies données MG (agrégées par municípios)

Visualisation (en parallèle) :
  └── Streamlit dashboard au fur et à mesure
```

---

## 9. Paramètres Gurobi utiles

```python
model = gp.Model("LRP_lockers")

# Tolérance d'optimalité (accepter une solution à 1% de l'optimal)
model.Params.MIPGap = 0.01

# Temps limite (secondes)
model.Params.TimeLimit = 300

# Nombre de threads
model.Params.Threads = 4

# Activer les coupes agressives
model.Params.Cuts = 2

# Focus sur la meilleure borne (utile si on veut une bonne solution rapide)
model.Params.MIPFocus = 1
```

---

---

## 10. Lexique

| Terme | Définition |
|---|---|
| **MILP** | *Mixed-Integer Linear Program* — problème d'optimisation avec des variables entières/binaires ET continues, et des contraintes/objectif linéaires. C'est la forme que Gurobi sait résoudre. |
| **MLP** | *Multi-Layer Perceptron* — réseau de neurones entièrement connecté (couches denses + fonctions d'activation non-linéaires). Utilisé ici soit comme **modèle de demande** (apprend $P(\text{choix})$ depuis les données), soit comme **générateur de warm start** (prédit quels lockers ouvrir pour initialiser Gurobi ou le LNS). |
| **Warm start** | Solution initiale fournie à un solveur (Gurobi ou LNS) avant l'optimisation. Un bon warm start réduit drastiquement le temps de résolution car le solveur part d'une solution déjà proche de l'optimum. |
| **L2O** | *Learning to Optimize* — famille de méthodes qui utilisent le ML pour guider ou remplacer des algorithmes d'optimisation classiques. Le MLP warm start en est un exemple simple. |
| **MNL** | *Multinomial Logit Model* — modèle de choix probabiliste : la probabilité de choisir l'alternative $j$ est proportionnelle à son utilité $e^{V_j}$ divisée par la somme des utilités de toutes les alternatives. |
| **LRP** | *Location-Routing Problem* — problème combinant deux décisions : où ouvrir des installations (localisation) et comment router des véhicules (tournées). NP-difficile. |
| **VRP** | *Vehicle Routing Problem* — problème de tournées de véhicules : trouver les routes optimales pour servir un ensemble de clients depuis un dépôt. |
| **TSP** | *Travelling Salesman Problem* — cas particulier du VRP avec un seul véhicule qui doit visiter tous les nœuds une fois. NP-difficile. |
| **Branch & Bound** | Algorithme exact utilisé par Gurobi : explore un arbre de décisions en découpant le problème en sous-problèmes et en éliminant les branches non-prometteuses via des bornes. |
| **Relaxation LP** | Version continue du MILP (on ignore la contrainte d'intégrité des binaires). Fournit une borne supérieure à l'optimal. Gurobi la résout à chaque nœud du Branch & Bound. |
| **MIP Gap** | Écart relatif entre la meilleure solution entière trouvée et la borne supérieure (relaxation LP). Un gap de 1% signifie qu'on est garanti à 1% de l'optimal. |
| **Charnes-Cooper** | Transformation algébrique qui convertit une fraction $S/(S+u^0)$ en expression linéaire via le changement de variable $\theta = 1/(S+u^0)$. |
| **McCormick** | Inégalités linéaires qui approximent/linéarisent le produit de deux variables (ici binaire × continue). Transforment une contrainte bilinéaire en MILP. |
| **Lazy Constraints** | Contraintes ajoutées dynamiquement à Gurobi via un callback, seulement quand elles sont violées. Évite d'ajouter exponentiellement de contraintes dès le départ (utilisé pour les sous-tours). |
| **Callback** | Fonction Python appelée automatiquement par Gurobi à certains moments du solving (ex : quand une solution entière est trouvée). Permet d'interagir avec le solveur pendant l'optimisation. |
| **Sous-tour** | Dans un VRP, circuit qui ne passe pas par le dépôt. Solution invalide qu'on doit éliminer. Exemple : véhicule qui tourne entre A→B→C→A sans partir du dépôt. |
| **MTZ** | *Miller-Tucker-Zemlin* — contraintes classiques anti-sous-tours : ajoutent des variables de position $s_k$ pour imposer un ordre de visite. Alternative aux lazy constraints. |
| **LNS** | *Large Neighborhood Search* — métaheuristique qui améliore itérativement une solution en détruisant une partie (DESTROY) et en la reconstruisant mieux (REPAIR, souvent via un solveur exact). |
| **GRASP** | *Greedy Randomized Adaptive Search Procedure* — métaheuristique en deux phases : construction gloutonne randomisée d'une solution, puis amélioration par recherche locale. Répété de nombreuses fois. |
| **Métaheuristique** | Méthode d'optimisation approchée qui ne garantit pas l'optimalité mais trouve de bonnes solutions en temps raisonnable pour les grands problèmes. |
| **Heuristique gloutonne** | Algorithme qui construit une solution en faisant à chaque étape le choix localement optimal (ex : ajouter le locker qui améliore le plus la part de marché), sans retour en arrière. |
| **NP-difficile** | Classe de problèmes pour lesquels aucun algorithme polynomial connu n'existe. En pratique : la résolution exacte est possible sur petites instances, mais impossible sur très grandes. |
| **PWL** | *Piecewise Linear* — approximation d'une fonction non-linéaire par des segments droits. Gurobi supporte nativement les objectifs PWL. |
| **$y_j$** | Variable binaire de décision : $y_j = 1$ si le locker $j$ est ouvert. C'est LA variable que le modèle optimise. |
| **$\theta_i$** | Variable continue (Charnes-Cooper) : inverse du dénominateur MNL pour la zone $i$. $\theta_i = 1/(S_i + u_i^0)$. |
| **$z_{ij}$** | Variable de linéarisation : approxime le produit $y_j \times \theta_i$ pour rendre l'objectif linéaire. |
| **$S_i$** | Attractivité totale de nos lockers ouverts pour la zone $i$ : $S_i = \sum_j u_{ij} \cdot y_j$. |
| **$u_{ij}$** | Utilité du locker $j$ pour la zone $i$ : $u_{ij} = A_j / d_{ij}^\rho$. Décroît avec la distance. |
| **$u^0$** | Attractivité totale fixe des concurrents pour la zone $i$. Paramètre exogène (pas de variable de décision). |

---

*Document de référence — Stage UFMG 2026 — Bastien Jacquelin*
