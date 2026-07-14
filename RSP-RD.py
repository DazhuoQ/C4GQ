#!/usr/bin/env python3
"""P-RSP approximation algorithms and command-line entry point."""

from datetime import datetime
from getpass import getpass
from pathlib import Path

from neo4j.graph import Relationship
import argparse
import math
import random
from pulp import LpProblem, LpVariable, LpMinimize, lpSum, LpStatus, value, PULP_CBC_CMD
from Pattern_utils import element_match_mapping
import time
import sys
import os

from utils import (
    choose_target_from_pattern_units,
    close_neo4j,
    connect_to_dataset,
    find_pattern_unit_from_edges_ids,
    get_match_elements,
    list_dataset_instances,
    normalize_dataset_name,
    run_query,
)

def solve_survivor_conditioned_LP(
    survivor_index,
    eligible_units,
    W_zero,
    pattern_unit_edges,
    match_edges,
    patterns_of_edge,
):
    """Solve the edge-based LP obtained by fixing one surviving match.

    The LP contains no survivor variables and no match--unit hitting-set
    constraints.  Only intervention units that do not overlap with the fixed
    survivor are included.
    """
    problem = LpProblem(
        f"P_RSP_survivor_{survivor_index}", LpMinimize
    )

    x_unit = {
        u: LpVariable(f"x_unit_{survivor_index}_{u}", 0, 1, cat="Continuous")
        for u in eligible_units
    }

    # Duplicate query records can induce identical edge constraints.  Keep one
    # representative because duplicates do not change LP feasibility.
    unique_W_zero = []
    seen_match_edges = set()
    for w in W_zero:
        key = frozenset(match_edges.get(w, []))
        if key in seen_match_edges:
            continue
        seen_match_edges.add(key)
        unique_W_zero.append(w)

    # Include every edge used by a remaining query match or an eligible unit.
    edge_ids = set()
    for w in unique_W_zero:
        edge_ids.update(match_edges.get(w, []))
    for u in eligible_units:
        edge_ids.update(pattern_unit_edges.get(u, []))

    d_edge = {
        e: LpVariable(f"d_edge_{survivor_index}_{e}", 0, 1, cat="Continuous")
        for e in edge_ids
    }

    primary_objective = lpSum(x_unit.values())
    problem += primary_objective, "Contingency_Size"

    # d_e <= sum_{u: e in E_u} x_u.
    for e in edge_ids:
        covering_units = [
            x_unit[u]
            for u in patterns_of_edge.get(e, [])
            if u in x_unit
        ]
        problem += (
            d_edge[e] <= lpSum(covering_units),
            f"Edge_Upper_{survivor_index}_{e}",
        )

    # d_e >= x_u for every e in E_u.
    for u in eligible_units:
        for e in pattern_unit_edges.get(u, []):
            problem += (
                d_edge[e] >= x_unit[u],
                f"Edge_Lower_{survivor_index}_{u}_{e}",
            )

    # Matches unaffected by the target must be destroyed by the contingency.
    for w in unique_W_zero:
        problem += (
            lpSum(d_edge[e] for e in match_edges.get(w, []) if e in d_edge) >= 1,
            f"Destroy_Match_{survivor_index}_{w}",
        )

    problem.solve(PULP_CBC_CMD(msg=0))
    status = LpStatus[problem.status]
    if status != "Optimal":
        return {
            "status": status,
            "objective_value": None,
            "x_unit_values": {},
        }

    primary_value = float(value(primary_objective) or 0.0)
    primary_x_values = {
        u: float(x_unit[u].varValue or 0.0) for u in x_unit
    }

    # Among primary-optimal LP solutions, minimize the largest x_u.  This
    # lexicographic tie-break spreads mass across interchangeable units while
    # preserving the original LP optimum and its approximation guarantee.
    tolerance = max(1e-7, 1e-7 * max(1.0, primary_value))
    problem += (
        primary_objective <= primary_value + tolerance,
        f"Preserve_Primary_Optimum_{survivor_index}",
    )
    max_x = LpVariable(
        f"max_x_{survivor_index}", 0, 1, cat="Continuous"
    )
    for u in eligible_units:
        problem += (
            x_unit[u] <= max_x,
            f"Spread_Unit_{survivor_index}_{u}",
        )
    problem.setObjective(max_x)
    problem.solve(PULP_CBC_CMD(msg=0))
    spread_status = LpStatus[problem.status]
    if spread_status == "Optimal":
        final_x_values = {
            u: float(x_unit[u].varValue or 0.0) for u in x_unit
        }
        spread_max_x = float(max_x.varValue or 0.0)
    else:
        final_x_values = primary_x_values
        spread_max_x = max(primary_x_values.values(), default=0.0)

    return {
        "status": status,
        "objective_value": primary_value,
        "x_unit_values": final_x_values,
        "spread_status": spread_status,
        "spread_max_x": spread_max_x,
    }


def _rounding_probability(x_value, amplification):
    """Compute 1 - (1 - x)^amplification stably."""
    x_value = min(1.0, max(0.0, float(x_value)))
    if x_value <= 0.0:
        return 0.0
    if x_value >= 1.0:
        return 1.0
    return min(
        1.0,
        -math.expm1(amplification * math.log1p(-x_value)),
    )


def LP_rounding(
    matches,
    pattern_unit_edges,
    match_edges,
    patterns_of_edge,
    target_index,
):
    """Survivor-conditioned LP rounding for P-RSP.

    Enumerate every query match hit by the target unit.  For each candidate
    survivor r, solve the edge-based LP restricted to units that avoid r,
    round its fractional unit variables, repair target-unaffected matches,
    and return the smallest feasible contingency set.
    """
    target_edges = set(pattern_unit_edges.get(target_index, []))
    if not target_edges:
        print("✗ The target intervention unit contains no relationship edges.")
        return {
            "status": "Infeasible",
            "objective_value": -1,
            "lp_objective_value": -1,
            "solution": set(),
            "survivor_match_index": None,
            "survivor_match_edges": [],
        }

    W_plus = [
        w for w in match_edges
        if target_edges & set(match_edges.get(w, []))
    ]
    W_zero = [
        w for w in match_edges
        if not (target_edges & set(match_edges.get(w, [])))
    ]

    print(f"Candidate surviving matches: {len(W_plus)}")
    print(f"Matches requiring contingency deletion: {len(W_zero)}")

    if not W_plus:
        return {
            "status": "Infeasible",
            "objective_value": -1,
            "lp_objective_value": -1,
            "solution": set(),
            "survivor_match_index": None,
            "survivor_match_edges": [],
        }

    # If every match is hit by the target, the empty contingency is optimal.
    if not W_zero:
        survivor = W_plus[0]
        return {
            "status": "Optimal",
            "objective_value": 0,
            "lp_objective_value": 0.0,
            "selected_survivor_lp_value": 0.0,
            "solution": set(),
            "survivor_match_index": survivor,
            "survivor_match_edges": [
                edge for edge in matches[survivor].values()
                if isinstance(edge, Relationship)
            ],
            "candidate_lp_values": {survivor: 0.0},
            "lp_count": 0,
        }

    q = max(1, max(len(match_edges.get(w, [])) for w in W_zero))
    all_contingency_units = {
        u for u in pattern_unit_edges if u != target_index
    }
    best = None
    candidate_lp_values = {}

    for survivor in W_plus:
        survivor_edges = set(match_edges.get(survivor, []))
        eligible_units = {
            u for u in all_contingency_units
            if not (set(pattern_unit_edges.get(u, [])) & survivor_edges)
        }

        lp_result = solve_survivor_conditioned_LP(
            survivor,
            eligible_units,
            W_zero,
            pattern_unit_edges,
            match_edges,
            patterns_of_edge,
        )
        if lp_result["status"] != "Optimal":
            print(f"  survivor {survivor}: {lp_result['status']}")
            continue

        lp_value = lp_result["objective_value"]
        if lp_value <= 0.0:
            # W_zero is nonempty, so a zero-valued feasible branch would
            # indicate numerical trouble rather than a valid fractional cover.
            print(f"  survivor {survivor}: invalid zero LP value; skipped")
            continue
        candidate_lp_values[survivor] = lp_value

        amplification = max(
            1.0,
            q * math.log(len(W_zero) / lp_value),
        )
        selected = set()
        x_values = lp_result["x_unit_values"]

        for u in eligible_units:
            probability = _rounding_probability(
                x_values.get(u, 0.0), amplification
            )
            if random.random() < probability:
                selected.add(u)

        selected = _repair_conditioned_solution(
            selected,
            eligible_units,
            W_zero,
            pattern_unit_edges,
            match_edges,
            patterns_of_edge,
            x_values,
        )
        if selected is None:
            print(f"  survivor {survivor}: no compatible repair; skipped")
            continue

        selected = _prune_conditioned_solution(
            selected,
            W_zero,
            pattern_unit_edges,
            match_edges,
            x_values,
        )

        print(
            f"  survivor {survivor}: LP={lp_value:.6f}, "
            f"lambda={amplification:.4f}, contingency={len(selected)}"
        )
        candidate = {
            "solution": selected,
            "survivor": survivor,
            "lp_value": lp_value,
        }
        if best is None or (
            len(candidate["solution"]), candidate["lp_value"]
        ) < (
            len(best["solution"]), best["lp_value"]
        ):
            best = candidate

    if best is None:
        return {
            "status": "Infeasible",
            "objective_value": -1,
            "lp_objective_value": -1,
            "solution": set(),
            "survivor_match_index": None,
            "survivor_match_edges": [],
            "candidate_lp_values": candidate_lp_values,
            "lp_count": len(candidate_lp_values),
        }

    survivor = best["survivor"]
    return {
        "status": "Optimal",
        "objective_value": len(best["solution"]),
        # The minimum over all feasible conditioned LPs is the global lower
        # bound from the analysis.  Also retain the LP value of the survivor
        # whose rounded solution was selected.
        "lp_objective_value": min(candidate_lp_values.values()),
        "selected_survivor_lp_value": best["lp_value"],
        "solution": best["solution"],
        "survivor_match_index": survivor,
        "survivor_match_edges": [
            edge for edge in matches[survivor].values()
            if isinstance(edge, Relationship)
        ],
        "candidate_lp_values": candidate_lp_values,
        "lp_count": len(candidate_lp_values),
    }







def _conditioned_rsp_data(match_edges, pattern_unit_edges, target_index):
    """Partition matches and return the non-target intervention units."""
    target_edges = set(pattern_unit_edges.get(target_index, []))
    W_plus = [
        w for w in match_edges
        if target_edges & set(match_edges.get(w, []))
    ]
    W_zero = [
        w for w in match_edges
        if not (target_edges & set(match_edges.get(w, [])))
    ]
    contingency_units = {
        u for u in pattern_unit_edges if u != target_index
    }
    return target_edges, W_plus, W_zero, contingency_units


def _trivial_conditioned_result(matches, survivor):
    """Return the responsibility-one solution for an empty contingency."""
    return {
        "status": "Optimal",
        "objective_value": 0,
        "lp_objective_value": 0.0,
        "selected_survivor_lp_value": 0.0,
        "solution": set(),
        "survivor_match_index": survivor,
        "survivor_match_edges": [
            edge for edge in matches[survivor].values()
            if isinstance(edge, Relationship)
        ],
        "candidate_lp_values": {survivor: 0.0},
        "lp_count": 0,
    }


def _repair_conditioned_solution(
    selected,
    eligible_units,
    W_zero,
    pattern_unit_edges,
    match_edges,
    patterns_of_edge,
    x_values,
):
    """Repair uncovered matches using local maximum marginal coverage."""
    selected = set(selected)
    matches_of_edge = {}
    for w in W_zero:
        for edge_id in match_edges.get(w, []):
            matches_of_edge.setdefault(edge_id, set()).add(w)

    def covered_by_unit(unit):
        covered = set()
        for edge_id in pattern_unit_edges.get(unit, []):
            covered.update(matches_of_edge.get(edge_id, ()))
        return covered

    remaining = set(W_zero)
    for u in selected:
        remaining.difference_update(covered_by_unit(u))

    while remaining:
        w = min(remaining)
        match_edge_set = set(match_edges.get(w, []))
        repair_candidates = set()
        for edge_id in match_edge_set:
            repair_candidates.update(patterns_of_edge.get(edge_id, []))
        repair_candidates &= eligible_units

        if not repair_candidates:
            return None

        repair_unit = max(
            repair_candidates,
            key=lambda u: (
                len(covered_by_unit(u) & remaining),
                x_values.get(u, 0.0),
                -u,
            ),
        )
        selected.add(repair_unit)
        remaining.difference_update(covered_by_unit(repair_unit))

    return selected


def _prune_conditioned_solution(
    selected,
    W_zero,
    pattern_unit_edges,
    match_edges,
    x_values,
):
    """Reverse-delete redundant selected units while preserving coverage."""
    selected = set(selected)
    matches_of_edge = {}
    for w in W_zero:
        for edge_id in match_edges.get(w, []):
            matches_of_edge.setdefault(edge_id, set()).add(w)

    unit_matches = {}
    cover_count = {w: 0 for w in W_zero}
    for u in selected:
        covered = set()
        for edge_id in pattern_unit_edges.get(u, []):
            covered.update(matches_of_edge.get(edge_id, ()))
        unit_matches[u] = covered
        for w in covered:
            cover_count[w] += 1

    for u in sorted(
        selected,
        key=lambda item: (x_values.get(item, 0.0), item),
    ):
        covered = unit_matches[u]
        if covered and all(cover_count[w] >= 2 for w in covered):
            selected.remove(u)
            for w in covered:
                cover_count[w] -= 1

    return selected


def _conditioned_result(matches, best, candidate_lp_values):
    """Build the common result returned by the three conditioned methods."""
    if best is None:
        return {
            "status": "Infeasible",
            "objective_value": -1,
            "lp_objective_value": -1,
            "solution": set(),
            "survivor_match_index": None,
            "survivor_match_edges": [],
            "candidate_lp_values": candidate_lp_values,
            "lp_count": len(candidate_lp_values),
        }

    survivor = best["survivor"]
    return {
        "status": "Optimal",
        "objective_value": len(best["solution"]),
        "lp_objective_value": min(candidate_lp_values.values()),
        "selected_survivor_lp_value": best["lp_value"],
        "solution": best["solution"],
        "survivor_match_index": survivor,
        "survivor_match_edges": [
            edge for edge in matches[survivor].values()
            if isinstance(edge, Relationship)
        ],
        "candidate_lp_values": candidate_lp_values,
        "lp_count": len(candidate_lp_values),
    }


def LP_guided_greedy(
    matches,
    pattern_unit_edges,
    match_edges,
    patterns_of_edge,
    target_index,
):
    """Greedy baseline over the same survivor-conditioned LP branches."""
    target_edges, W_plus, W_zero, contingency_units = _conditioned_rsp_data(
        match_edges, pattern_unit_edges, target_index
    )

    print(f"Candidate surviving matches: {len(W_plus)}")
    print(f"Matches requiring contingency deletion: {len(W_zero)}")

    if not target_edges or not W_plus:
        return _conditioned_result(matches, None, {})
    if not W_zero:
        return _trivial_conditioned_result(matches, W_plus[0])

    best = None
    candidate_lp_values = {}

    for survivor in W_plus:
        survivor_edges = set(match_edges.get(survivor, []))
        eligible_units = {
            u for u in contingency_units
            if not (set(pattern_unit_edges.get(u, [])) & survivor_edges)
        }
        lp_result = solve_survivor_conditioned_LP(
            survivor,
            eligible_units,
            W_zero,
            pattern_unit_edges,
            match_edges,
            patterns_of_edge,
        )
        if lp_result["status"] != "Optimal":
            print(f"  survivor {survivor}: {lp_result['status']}")
            continue

        lp_value = lp_result["objective_value"]
        if lp_value <= 0.0:
            print(f"  survivor {survivor}: invalid zero LP value; skipped")
            continue

        candidate_lp_values[survivor] = lp_value
        x_values = lp_result["x_unit_values"]
        active_units = {
            u for u in eligible_units if x_values.get(u, 0.0) > 1e-9
        }
        remaining = set(W_zero)
        selected = set()

        while remaining and active_units:
            coverage = {
                u: sum(
                    bool(
                        set(pattern_unit_edges.get(u, []))
                        & set(match_edges.get(w, []))
                    )
                    for w in remaining
                )
                for u in active_units
            }
            best_unit = max(
                active_units,
                key=lambda u: (coverage[u], x_values.get(u, 0.0), -u),
            )
            if coverage[best_unit] == 0:
                break

            selected.add(best_unit)
            unit_edges = set(pattern_unit_edges.get(best_unit, []))
            remaining = {
                w for w in remaining
                if not (unit_edges & set(match_edges.get(w, [])))
            }
            active_units.remove(best_unit)

        selected = _repair_conditioned_solution(
            selected,
            eligible_units,
            W_zero,
            pattern_unit_edges,
            match_edges,
            patterns_of_edge,
            x_values,
        )
        if selected is None:
            print(f"  survivor {survivor}: no compatible repair; skipped")
            continue

        print(
            f"  survivor {survivor}: LP={lp_value:.6f}, "
            f"contingency={len(selected)}"
        )
        candidate = {
            "solution": selected,
            "survivor": survivor,
            "lp_value": lp_value,
        }
        if best is None or (
            len(candidate["solution"]), candidate["lp_value"]
        ) < (
            len(best["solution"]), best["lp_value"]
        ):
            best = candidate

    return _conditioned_result(matches, best, candidate_lp_values)


def LP_guided_random(
    matches,
    pattern_unit_edges,
    match_edges,
    patterns_of_edge,
    target_index,
):
    """Random baseline over the same survivor-conditioned LP branches."""
    target_edges, W_plus, W_zero, contingency_units = _conditioned_rsp_data(
        match_edges, pattern_unit_edges, target_index
    )

    print(f"Candidate surviving matches: {len(W_plus)}")
    print(f"Matches requiring contingency deletion: {len(W_zero)}")

    if not target_edges or not W_plus:
        return _conditioned_result(matches, None, {})
    if not W_zero:
        return _trivial_conditioned_result(matches, W_plus[0])

    best = None
    candidate_lp_values = {}

    for survivor in W_plus:
        survivor_edges = set(match_edges.get(survivor, []))
        eligible_units = {
            u for u in contingency_units
            if not (set(pattern_unit_edges.get(u, [])) & survivor_edges)
        }
        lp_result = solve_survivor_conditioned_LP(
            survivor,
            eligible_units,
            W_zero,
            pattern_unit_edges,
            match_edges,
            patterns_of_edge,
        )
        if lp_result["status"] != "Optimal":
            print(f"  survivor {survivor}: {lp_result['status']}")
            continue

        lp_value = lp_result["objective_value"]
        if lp_value <= 0.0:
            print(f"  survivor {survivor}: invalid zero LP value; skipped")
            continue

        candidate_lp_values[survivor] = lp_value
        x_values = lp_result["x_unit_values"]
        selected = {
            u for u in eligible_units
            if x_values.get(u, 0.0) > 1e-9 and random.random() < 0.5
        }
        selected = _repair_conditioned_solution(
            selected,
            eligible_units,
            W_zero,
            pattern_unit_edges,
            match_edges,
            patterns_of_edge,
            x_values,
        )
        if selected is None:
            print(f"  survivor {survivor}: no compatible repair; skipped")
            continue

        print(
            f"  survivor {survivor}: LP={lp_value:.6f}, "
            f"contingency={len(selected)}"
        )
        candidate = {
            "solution": selected,
            "survivor": survivor,
            "lp_value": lp_value,
        }
        if best is None or (
            len(candidate["solution"]), candidate["lp_value"]
        ) < (
            len(best["solution"]), best["lp_value"]
        ):
            best = candidate

    return _conditioned_result(matches, best, candidate_lp_values)


def _direct_conditioned_result(matches, best):
    """Build a result for a conditioned method that does not solve an LP."""
    if best is None:
        return {
            "status": "Infeasible",
            "objective_value": -1,
            "lp_objective_value": -1,
            "solution": set(),
            "survivor_match_index": None,
            "survivor_match_edges": [],
            "lp_count": 0,
        }

    survivor = best["survivor"]
    return {
        "status": "Optimal",
        "objective_value": len(best["solution"]),
        "lp_objective_value": -1,
        "solution": best["solution"],
        "survivor_match_index": survivor,
        "survivor_match_edges": [
            edge for edge in matches[survivor].values()
            if isinstance(edge, Relationship)
        ],
        "lp_count": 0,
    }


def direct_greedy(
    matches,
    pattern_unit_edges,
    match_edges,
    patterns_of_edge,
    target_index,
):
    """Greedy conditioned set cover without solving an LP."""
    target_edges, W_plus, W_zero, contingency_units = _conditioned_rsp_data(
        match_edges, pattern_unit_edges, target_index
    )
    print(f"Candidate surviving matches: {len(W_plus)}")
    print(f"Matches requiring contingency deletion: {len(W_zero)}")

    if not target_edges or not W_plus:
        return _direct_conditioned_result(matches, None)
    if not W_zero:
        result = _trivial_conditioned_result(matches, W_plus[0])
        result["lp_objective_value"] = -1
        result.pop("selected_survivor_lp_value", None)
        result["lp_count"] = 0
        return result

    match_edge_sets = {
        w: set(match_edges.get(w, [])) for w in W_zero
    }
    best = None

    for survivor in W_plus:
        survivor_edges = set(match_edges.get(survivor, []))
        available = {
            u for u in contingency_units
            if not (set(pattern_unit_edges.get(u, [])) & survivor_edges)
        }
        remaining = set(W_zero)
        selected = set()

        while remaining and available:
            coverage = {
                u: sum(
                    bool(set(pattern_unit_edges.get(u, [])) & match_edge_sets[w])
                    for w in remaining
                )
                for u in available
            }
            best_unit = max(available, key=lambda u: (coverage[u], -u))
            if coverage[best_unit] == 0:
                break

            selected.add(best_unit)
            unit_edges = set(pattern_unit_edges.get(best_unit, []))
            remaining = {
                w for w in remaining if not (unit_edges & match_edge_sets[w])
            }
            available.remove(best_unit)

        if remaining:
            print(f"  survivor {survivor}: no direct-greedy cover; skipped")
            continue

        print(
            f"  survivor {survivor}: direct-greedy "
            f"contingency={len(selected)}"
        )
        candidate = {"solution": selected, "survivor": survivor}
        if best is None or len(candidate["solution"]) < len(best["solution"]):
            best = candidate

    return _direct_conditioned_result(matches, best)


def direct_random(
    matches,
    pattern_unit_edges,
    match_edges,
    patterns_of_edge,
    target_index,
):
    """Random-order conditioned set cover without solving an LP."""
    target_edges, W_plus, W_zero, contingency_units = _conditioned_rsp_data(
        match_edges, pattern_unit_edges, target_index
    )
    print(f"Candidate surviving matches: {len(W_plus)}")
    print(f"Matches requiring contingency deletion: {len(W_zero)}")

    if not target_edges or not W_plus:
        return _direct_conditioned_result(matches, None)
    if not W_zero:
        result = _trivial_conditioned_result(matches, W_plus[0])
        result["lp_objective_value"] = -1
        result.pop("selected_survivor_lp_value", None)
        result["lp_count"] = 0
        return result

    match_edge_sets = {
        w: set(match_edges.get(w, [])) for w in W_zero
    }
    best = None

    for survivor in W_plus:
        survivor_edges = set(match_edges.get(survivor, []))
        eligible_units = [
            u for u in contingency_units
            if not (set(pattern_unit_edges.get(u, [])) & survivor_edges)
        ]
        random.shuffle(eligible_units)
        remaining = set(W_zero)
        selected = set()

        for unit in eligible_units:
            unit_edges = set(pattern_unit_edges.get(unit, []))
            covered = {
                w for w in remaining if unit_edges & match_edge_sets[w]
            }
            if not covered:
                continue
            selected.add(unit)
            remaining.difference_update(covered)
            if not remaining:
                break

        if remaining:
            print(f"  survivor {survivor}: no direct-random cover; skipped")
            continue

        print(
            f"  survivor {survivor}: direct-random "
            f"contingency={len(selected)}"
        )
        candidate = {"solution": selected, "survivor": survivor}
        if best is None or len(candidate["solution"]) < len(best["solution"]):
            best = candidate

    return _direct_conditioned_result(matches, best)






class Tee:
    """Duplicate output to the terminal and a UTF-8 log file."""

    def __init__(self, filepath, mode="w"):
        self.terminal = sys.stdout
        self.log = open(filepath, mode, buffering=1, encoding="utf-8")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.terminal.flush()
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()


DEFAULT_QUERY = (
    "MATCH (n1:Person)-[r1]->(n2)-[r2]->(n3:Person)-[r3]-(n1) "
    "RETURN n1, n2, n3, r1, r2, r3"
)

DEFAULT_PATTERN = (
    "MATCH (n1:Person)-[r1]->(n2)-[r2]->(n3:Person) "
    "RETURN n1, n2, n3, r1, r2"
)


def build_argument_parser():
    """Build a CLI compatible with the current multi-dataset experiments."""
    parser = argparse.ArgumentParser(
        description=(
            "Run an LP/MILP-based approximation algorithm for pattern-based "
            "query responsibility on an isolated Neo4j dataset instance."
        )
    )
    parser.add_argument(
        "query", nargs="?", default=DEFAULT_QUERY,
        help="Cypher query whose responsibility is computed.",
    )
    parser.add_argument(
        "pattern", nargs="?", default=DEFAULT_PATTERN,
        help="Cypher query defining the intervention units.",
    )
    parser.add_argument(
        "--target", default=None,
        help=(
            "Comma-separated relationship element IDs that exactly identify "
            "the target intervention unit. The first unit is used if omitted."
        ),
    )
    parser.add_argument(
        "--dataset", default=os.getenv("NEO4J_DATASET"),
        help="Neo4j dataset instance name or alias.",
    )
    parser.add_argument(
        "--instance-root",
        default=os.getenv(
            "NEO4J_INSTANCE_ROOT", str(Path("~/neo4j-instances").expanduser())
        ),
        help="Directory containing the isolated Neo4j instances.",
    )
    parser.add_argument(
        "--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        help="Neo4j Bolt URI.",
    )
    parser.add_argument(
        "--database", default=os.getenv("NEO4J_DATABASE", "neo4j"),
        help="Logical Neo4j database name.",
    )
    parser.add_argument(
        "--user", default=os.getenv("NEO4J_USER", "neo4j"),
        help="Neo4j username.",
    )
    parser.add_argument(
        "--password", default=os.getenv("NEO4J_PASSWORD"),
        help="Neo4j password; prefer NEO4J_PASSWORD.",
    )
    parser.add_argument(
        "--no-switch", action="store_true",
        help="Use the already-active dataset instead of switching instances.",
    )
    parser.add_argument(
        "--list-datasets", action="store_true",
        help="List available dataset instances and exit.",
    )
    parser.add_argument(
        "--no-details", action="store_true",
        help="Do not print target and selected relationship details.",
    )
    parser.add_argument(
        "-a", "--approximation",
        choices=[
            "rounding", "greedy", "random",
            "direct-greedy", "direct-random",
            "1", "2", "3", "4", "5",
        ],
        default="rounding",
        help=(
            "Strategy: LP rounding (1), LP-guided greedy (2), LP-guided "
            "random (3), direct greedy (4), or direct random (5)."
        ),
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed used by rounding/random baselines.",
    )
    parser.add_argument(
        "--log-root", default=os.getenv("RSP_LOG_ROOT", "run_logs"),
        help="Root directory for dataset-specific logs.",
    )
    return parser


def print_available_datasets(instance_root):
    instances = list_dataset_instances(instance_root)
    if not instances:
        print(f"No Neo4j instances found under {Path(instance_root).expanduser()}")
        return
    print(f"Neo4j datasets under {Path(instance_root).expanduser()}:")
    for name in instances:
        print(f"  - {name}")


def main():
    parser = build_argument_parser()
    args = parser.parse_args()

    if args.list_datasets:
        print_available_datasets(args.instance_root)
        return 0
    if not args.dataset:
        parser.error("--dataset is required unless NEO4J_DATASET is set")

    dataset = normalize_dataset_name(args.dataset)
    password = args.password or getpass("Neo4j password: ")
    random.seed(args.seed)

    log_directory = Path(args.log_root).expanduser() / dataset
    log_directory.mkdir(parents=True, exist_ok=True)
    log_filename = log_directory / (
        f"RSP-RD_{dataset}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    tee = Tee(str(log_filename))
    original_stdout = sys.stdout
    sys.stdout = tee
    driver = None

    try:
        print("=" * 70)
        print("P-RSP approximation experiment")
        print(f"Dataset: {dataset}")
        print(f"Instance root: {Path(args.instance_root).expanduser()}")
        print(f"URI: {args.uri}")
        print(f"Database: {args.database}")
        print(f"Approximation: {args.approximation}")
        print(f"Random seed: {args.seed}")
        print(f"Query: {args.query}")
        print(f"Pattern: {args.pattern}")
        print(f"Requested target: {args.target or 'first intervention unit'}")
        print(f"Log file: {log_filename}")
        print("=" * 70)

        driver = connect_to_dataset(
            dataset=dataset,
            user=args.user,
            password=password,
            uri=args.uri,
            database=args.database,
            instance_root=args.instance_root,
            auto_switch=not args.no_switch,
        )
        if driver is None:
            print("✗ Could not activate or connect to the requested dataset")
            return 2

        matches = run_query(driver, args.query, database=args.database)
        pattern_units = run_query(driver, args.pattern, database=args.database)
        if matches is None or pattern_units is None:
            print("✗ Query execution failed")
            return 3
        if not matches:
            print("✗ The query has no matches; responsibility is not computed")
            return 4
        if not pattern_units:
            print("✗ The intervention pattern has no matches")
            return 5

        print(f"Query matches: {len(matches)}")
        print(f"Intervention units: {len(pattern_units)}")

        if args.target:
            target_ids = [item.strip() for item in args.target.split(",") if item.strip()]
            target_result = find_pattern_unit_from_edges_ids(pattern_units, target_ids)
        else:
            target_result = choose_target_from_pattern_units(pattern_units)

        if not target_result:
            print("✗ No intervention unit exactly matches --target")
            return 6
        target_pattern_unit, target_index = target_result
        print(f"Target intervention unit: {target_index}")

        _, query_elements = get_match_elements(matches)
        _, pattern_elements = get_match_elements(pattern_units)
        elements = set(
            {
                edge.element_id: edge
                for edge in (query_elements or set()) | (pattern_elements or set())
            }.values()
        )
        pattern_unit_edges, match_edges, patterns_of_edge = element_match_mapping(
            elements, matches, pattern_units
        )

        approximation = {
            "1": "rounding", "2": "greedy", "3": "random",
            "4": "direct-greedy", "5": "direct-random",
            "rounding": "rounding", "greedy": "greedy", "random": "random",
            "direct-greedy": "direct-greedy",
            "direct-random": "direct-random",
        }[args.approximation]

        # All survivor-conditioned methods use only the sparse
        # edge--unit mapping; no full match--unit incidence matrix is built.
        overlap_start = time.perf_counter()
        overlap_end = time.perf_counter()

        algorithm_start = time.perf_counter()
        if approximation == "rounding":
            result = LP_rounding(
                matches,
                pattern_unit_edges,
                match_edges,
                patterns_of_edge,
                target_index,
            )
        elif approximation == "greedy":
            result = LP_guided_greedy(
                matches,
                pattern_unit_edges,
                match_edges,
                patterns_of_edge,
                target_index,
            )
        elif approximation == "random":
            result = LP_guided_random(
                matches,
                pattern_unit_edges,
                match_edges,
                patterns_of_edge,
                target_index,
            )
        elif approximation == "direct-greedy":
            result = direct_greedy(
                matches,
                pattern_unit_edges,
                match_edges,
                patterns_of_edge,
                target_index,
            )
        else:
            result = direct_random(
                matches,
                pattern_unit_edges,
                match_edges,
                patterns_of_edge,
                target_index,
            )
        algorithm_end = time.perf_counter()

        if result is None:
            print("✗ No feasible survivor-compatible repair exists")
            return 7

        print("Status:", result["status"])
        print("Objective Value (Contingency Set Size):", result["objective_value"])
        print("LP Objective Value:", result.get("lp_objective_value", -1))
        if "selected_survivor_lp_value" in result:
            print(
                "Selected Survivor LP Value:",
                result["selected_survivor_lp_value"],
            )
        if "lp_count" in result:
            print("Feasible survivor-conditioned LPs:", result["lp_count"])
        print("Solution:", result["solution"])
        print("Surviving match:", result["survivor_match_index"])

        if not args.no_details:
            print("\nTarget Pattern Unit:")
            print(f"Index: {target_index}")
            for edge in target_pattern_unit.values():
                if isinstance(edge, Relationship):
                    print(f" - edge: {edge}")

            print("\nSelected Pattern Units:")
            for pattern_unit_index in sorted(result["solution"]):
                print(f"\n - {pattern_unit_index}")
                for edge in pattern_units[pattern_unit_index].values():
                    if isinstance(edge, Relationship):
                        print(f"   - edge: {edge}")

        contingency_size = result["objective_value"]
        responsibility = 1.0 / (1.0 + contingency_size) if contingency_size >= 0 else 0.0
        algorithm_runtime = algorithm_end - algorithm_start
        overlap_runtime = overlap_end - overlap_start
        total_runtime = algorithm_runtime + overlap_runtime

        print("=" * 50)
        print(f"Dataset: {dataset}")
        print(f"Contingency size: {contingency_size}")
        print(f"Query responsibility: {responsibility:.10f}")
        print(f"Execution time: {algorithm_runtime:.4f} seconds")
        print("+ Match hitting-set computation time: 0.0000 seconds")
        print("+ Match hitting-set computation: not materialized")
        print(f"Total execution time: {total_runtime:.4f} seconds")
        return 0 if result["status"] == "Optimal" else 8

    except KeyboardInterrupt:
        print("\n✗ Interrupted by user")
        return 130
    except Exception as exc:
        print(f"✗ Experiment failed: {exc}")
        return 1
    finally:
        if driver is not None:
            close_neo4j(driver)
            print("✓ Driver closed")
        sys.stdout = original_stdout
        tee.close()
        print(f"Log saved to: {log_filename}")


if __name__ == "__main__":
    raise SystemExit(main())
