#!/usr/bin/env python3
"""P-RES approximation algorithms and command-line entry point."""

from datetime import datetime
from getpass import getpass
from pathlib import Path
from collections import defaultdict

from neo4j.graph import Relationship
import argparse
import math
import random
from pulp import LpProblem, LpVariable, LpMinimize, lpSum, LpStatus, value, PULP_CBC_CMD
from Pattern_utils import (
    element_match_mapping,
    match_hitting_set as compute_match_hitting_set,
)
import time
import sys
import os

from utils import (
    close_neo4j,
    connect_to_dataset,
    get_match_elements,
    list_dataset_instances,
    normalize_dataset_name,
    run_query,
)


def solve_pattern_RES_LP(elements, matches, patterns, pattern_unit_edges, match_edges, patterns_of_edge):
    """
    Solve the pattern hitting set problem using LP.

    Variables:
    - x_edge[e]: Variable indicating if edge e is selected
    - x_pattern_unit[mo]: Variable indicating if pattern unit mo is selected

    Objective:
    - Minimize sum of x_pattern_unit[mo] (number of pattern units selected)

    Constraints:
    1. Hitting Set: Each match must have at least one selected edge
    2. Node Selection: If edge is selected, at least one of the pattern units it is a part of must be selected
    3. pattern selection: If a pattern unit is selected, all its edges must be selected

    Args:
        elements: Set of all edge elements
        matches: List of matches, where each match is a set of elements
        patterns: List of pattern units
        pattern_unit_edges: Dictionary mapping each pattern unit to its constituent edges
        match_edges: Dictionary mapping each match to its constituent edges
        patterns_of_edge: Dictionary mapping each edge to the pattern units it belongs to
    """

    # Create the problem
    prob = LpProblem("pattern_Hitting_Set", LpMinimize)

    # ============ Decision Variables ============

    # x_edge[e]: for edge selection
    x_edge = {}
    for edge in elements:
        x_edge[edge.element_id] = LpVariable(f"x_edge_{id(edge)}", lowBound=0, upBound=1, cat='Continuous')

    # x_pattern_unit[mo]: for pattern unit selection
    x_pattern_unit = {}
    for i,_ in enumerate(patterns):
        x_pattern_unit[i] = LpVariable(f"x_pattern_{i}", lowBound=0, upBound=1, cat='Continuous')

    # ============ Objective Function ============
    prob += lpSum([x_pattern_unit[i] for i,_ in enumerate(patterns)]), "Total_Pattern_units"

    # ============ Constraints ============
    # 1. Hitting Set: Each match must have at least one selected edge
    for i, match in enumerate(matches):
        prob += lpSum([x_edge[edge_id] for edge_id in match_edges[i]]) >= 1, f"Record_{i}"


    # 2. Node Selection: If an edge is selected, at least one of the pattern units it is a part of must be selected
    for edge in elements:
        prob += lpSum([x_pattern_unit[i] for i in patterns_of_edge[edge.element_id]]) >= x_edge[edge.element_id], f"Edge_pattern_{edge.element_id}"


    # 3. pattern selection: If a pattern unit is selected, all its edges must be selected
    for i,_ in enumerate(patterns):
        for edge_id in pattern_unit_edges[i]:
            prob += x_edge[edge_id] >= x_pattern_unit[i], f"pattern_Edge_{i}_{edge_id}"

    # ============ Solve ============
    prob.solve(PULP_CBC_CMD(msg=0))

    # ============ Extract Solution ============
    status = LpStatus[prob.status]
    x_edge_values = {
        edge_id: float(x_edge[edge_id].varValue or 0.0)
        for edge_id in x_edge
    }
    x_pattern_unit_values = {
        i: float(x_pattern_unit[i].varValue or 0.0)
        for i in x_pattern_unit
    }

    selected_edges = [edge for edge in elements if x_edge_values[edge.element_id] >= 0.5]
    selected_patterns = [pattern_unit for i, pattern_unit in enumerate(patterns) if x_pattern_unit_values[i] >= 0.5]

    return {
        "status": status,
        "objective_value": (
            float(value(prob.objective))
            if value(prob.objective) is not None
            else None
        ),
        "selected_elements": selected_edges,
        "selected_patterns": selected_patterns,
        "x_edge_values": x_edge_values,
        "x_pattern_unit_values": x_pattern_unit_values,
    }





def LP_rounding(
    elements,
    patterns,
    pattern_unit_edges,
    match_edges,
    patterns_of_edge,
    matches,
    match_hitting_set,
):
    """
    LP rounding for P-RES with:

    1. independent randomized rounding;
    2. filtering of redundant sampled units;
    3. coverage-aware repair;
    4. optional reverse-delete pruning.
    """

    # Solve LP
    result = solve_pattern_RES_LP(
        elements,
        matches,
        patterns,
        pattern_unit_edges,
        match_edges,
        patterns_of_edge,
    )

    if result["status"] != "Optimal":
        print("✗ LP did not find an optimal solution")
        return {
            "status": result["status"],
            "objective_value": -1,
            "solution": set(),
        }

    x_values = result["x_pattern_unit_values"]

    n = len(matches)

    if n == 0:
        return {
            "status": result["status"],
            "objective_value": 0,
            "solution": set(),
        }

    # Check feasibility and construct a sparse inverse index:
    #
    # unit_matches[u] = query matches destroyed by unit u.
    unit_matches = defaultdict(set)

    for w in range(n):
        hitters = set(match_hitting_set[w] or [])

        if not hitters:
            print(f"✗ Infeasible: match {w} has no hitting unit")
            return {
                "status": "Infeasible",
                "objective_value": -1,
                "solution": set(),
            }

        for u in hitters:
            unit_matches[u].add(w)

    # Compute alpha.
    lp_value = sum(x_values.values())

    if lp_value <= 0:
        print("✗ Infeasible or numerically invalid LP solution")
        return {
            "status": "Infeasible",
            "objective_value": -1,
            "solution": set(),
        }

    alpha = math.log(n / lp_value)

    # Use this lower bound if alpha >= 1 is required by your analysis.
    alpha = max(1.0, alpha)

    # ------------------------------------------------------------
    # Phase 1: randomized rounding
    # ------------------------------------------------------------

    sampled = set()

    for u, x_u in x_values.items():
        x_u = min(1.0, max(0.0, x_u))

        if x_u <= 0:
            continue
        elif x_u >= 1:
            p_u = 1.0
        else:
            p_u = -math.expm1(
                alpha * math.log1p(-x_u)
            )

        if random.random() < p_u:
            sampled.add(u)

    # ------------------------------------------------------------
    # Phase 2: filter sampled units
    # ------------------------------------------------------------
    #
    # Process high-coverage sampled units first. A sampled unit is
    # retained only if it covers a currently uncovered match.

    Sol = set()
    W_remaining = set(range(n))

    sampled_order = sorted(
        sampled,
        key=lambda u: (
            len(unit_matches[u]),
            x_values.get(u, 0.0),
        ),
        reverse=True,
    )

    for u in sampled_order:
        newly_covered = unit_matches[u] & W_remaining

        if newly_covered:
            Sol.add(u)
            W_remaining.difference_update(newly_covered)

    # ------------------------------------------------------------
    # Phase 3: coverage-aware repair
    # ------------------------------------------------------------

    while W_remaining:
        # Repair a constrained match first.
        w = min(
            W_remaining,
            key=lambda w: len(match_hitting_set[w] or []),
        )

        candidates = set(match_hitting_set[w] or [])

        if not candidates:
            print(f"✗ Infeasible: match {w} has no hitting unit")
            return {
                "status": "Infeasible",
                "objective_value": -1,
                "solution": set(),
            }

        # Among the units that can repair w, choose the one that
        # covers the most currently uncovered matches.
        #
        # Use the LP value only as a tie-breaker.
        u_best = max(
            candidates,
            key=lambda u: (
                len(unit_matches[u] & W_remaining),
                x_values.get(u, 0.0),
            ),
        )

        Sol.add(u_best)
        W_remaining.difference_update(unit_matches[u_best])

    # ------------------------------------------------------------
    # Phase 4: optional reverse-delete pruning
    # ------------------------------------------------------------

    coverage_count = [0] * n

    for u in Sol:
        for w in unit_matches[u]:
            coverage_count[w] += 1

    # Try low-LP-value units first.
    removal_order = sorted(
        Sol,
        key=lambda u: (
            x_values.get(u, 0.0),
            len(unit_matches[u]),
        ),
    )

    for u in removal_order:
        covered_matches = unit_matches[u]

        # Unit u is redundant if every match it destroys is also
        # destroyed by at least one other selected unit.
        if all(coverage_count[w] >= 2 for w in covered_matches):
            Sol.remove(u)

            for w in covered_matches:
                coverage_count[w] -= 1

    return {
        "status": result["status"],
        "objective_value": len(Sol),
        "solution": Sol,
    }



def LP_guided_greedy(elements, patterns, pattern_unit_edges, match_edges, patterns_of_edge, matches, match_hitting_set):
    """
    Performs the greedy algorithm, based on the output of the LP for P-RES
    """

    # If there exists a query match that doesn't overlap with any intervention unit, there is no feasible solution
    for i, _ in enumerate(matches):
        if match_hitting_set[i] is None or len(match_hitting_set[i]) == 0:
            print("✗ Infeasible Problem")
            return {
                    "status": "Infeasible",
                    "objective_value": -1,
                    "solution": set(),
                }

    # Solve the LP
    result = solve_pattern_RES_LP(elements, matches, patterns, pattern_unit_edges, match_edges, patterns_of_edge)

    if result["status"] != "Optimal":
        return {
            "status": result["status"],
            "objective_value": -1,
            "solution": set(),
        }

    # Retrieve the primary-optimal fractional unit solution.
    x_pattern_unit_values = result["x_pattern_unit_values"]

    # U+
    U_plus = set()
    for u, u_value in x_pattern_unit_values.items():
        if u_value > 0:
            U_plus.add(u)

    # Helper: does unit u share at least one edge with match w?
    def destroys(u, w):
        return bool(
            set(pattern_unit_edges.get(u, [])) &
            set(match_edges.get(w, []))
        )

    Sol = set()
    W_remaining = set(range(len(matches)))

    # Greedy phase
    while W_remaining and U_plus:
        # How many matches overlap with each intervention unit u
        coverage = {
            u: sum(1 for w in W_remaining if destroys(u, w))
            for u in U_plus
        }

        u_best = max(coverage, key=lambda u: coverage[u])

        # No LP-active unit covers any remaining match → exit greedy phase
        if coverage[u_best] == 0:
            break

        Sol.add(u_best)
        # Discard all matches destroyed by the selected unit
        W_remaining = {w for w in W_remaining if not destroys(u_best, w)}
        # Each unit may only be selected once
        U_plus.remove(u_best)


    # Repair step: for each match still uncovered, add an arbitrary hitting unit
    for w in sorted(W_remaining):
        candidates = set(match_hitting_set[w])
        if Sol & candidates:
            continue
        if not candidates:
            print(f"✗ Infeasible: no unit can destroy match w={w}")
            return {
                    "status": "Infeasible",
                    "objective_value": -1,
                    "solution": set(),
                }
        Sol.add(next(iter(candidates)))

    return {
        "status": result["status"],
        "objective_value": len(Sol),
        "solution": Sol,
    }



def LP_guided_random(elements, patterns, pattern_unit_edges, match_edges, patterns_of_edge, matches, match_hitting_set):
    """
    Performs a random selection algorithm, based on the output of the LP for P-RES
    """

    # If there exists a query match that doesn't overlap with any intervention unit, there is no feasible solution
    for i, _ in enumerate(matches):
        if match_hitting_set[i] is None or len(match_hitting_set[i]) == 0:
            print("✗ Infeasible Problem")
            return {
                    "status": "Infeasible",
                    "objective_value": -1,
                    "solution": set(),
                }

    # Solve the LP
    result = solve_pattern_RES_LP(elements, matches, patterns, pattern_unit_edges, match_edges, patterns_of_edge)

    if result["status"] != "Optimal":
        return {
            "status": result["status"],
            "objective_value": -1,
            "solution": set(),
        }

    # Retrieve the primary-optimal fractional unit solution.
    x_pattern_unit_values = result["x_pattern_unit_values"]

    Sol = set()

    for u, u_value in x_pattern_unit_values.items():
        if u_value > 0:
            if random.random() < 0.5:  # Randomly select with probability 0.5
                Sol.add(u)

    # Repair step: for each match still uncovered, add an arbitrary hitting unit
    for i, _ in enumerate(matches):
        hit_set = set(match_hitting_set[i] or [])
        if not (Sol & hit_set):
            if hit_set:
                arbitrary_u = next(iter(hit_set))
                Sol.add(arbitrary_u)
            else:
                print("✗ Infeasible Problem")
                return {
                    "status": "Infeasible",
                    "objective_value": -1,
                    "solution": set(),
                }

    return {
        "status": result["status"],
        "objective_value": len(Sol),
        "solution": Sol,
    }


class Tee:
    """Duplicates writes to both the terminal and a log file, flushing immediately."""
    def __init__(self, filepath, mode="w"):
        self.terminal = sys.stdout
        self.log = open(filepath, mode, buffering=1, encoding="utf-8")  # force UTF-8

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
    """Build the command-line interface for the multi-dataset setup."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the LP-based approximation algorithm for pattern-based "
            "query resilience on an isolated Neo4j dataset instance."
        )
    )

    parser.add_argument(
        "query",
        nargs="?",
        default=DEFAULT_QUERY,
        help="Cypher query whose resilience is computed.",
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        default=DEFAULT_PATTERN,
        help="Cypher query defining the intervention units.",
    )

    parser.add_argument(
        "--dataset",
        default=os.getenv("NEO4J_DATASET"),
        help=(
            "Neo4j dataset instance name or alias, for example ilpg, "
            "wwc2019, stackoverflow, fincen, icij, or star-wars. "
            "Required unless NEO4J_DATASET is set."
        ),
    )
    parser.add_argument(
        "--instance-root",
        default=os.getenv(
            "NEO4J_INSTANCE_ROOT",
            str(Path("~/neo4j-instances").expanduser()),
        ),
        help="Directory containing the isolated Neo4j instances.",
    )
    parser.add_argument(
        "--uri",
        default=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        help="Neo4j Bolt URI. Default: NEO4J_URI or bolt://localhost:7687.",
    )
    parser.add_argument(
        "--database",
        default=os.getenv("NEO4J_DATABASE", "neo4j"),
        help="Logical Neo4j database name. Default: neo4j.",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("NEO4J_USER", "neo4j"),
        help="Neo4j username. Default: NEO4J_USER or neo4j.",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("NEO4J_PASSWORD"),
        help=(
            "Neo4j password. Prefer setting NEO4J_PASSWORD. "
            "When omitted, it is requested securely."
        ),
    )
    parser.add_argument(
        "--no-switch",
        action="store_true",
        help=(
            "Do not start or stop Neo4j instances; only verify that the "
            "requested dataset is already active."
        ),
    )
    parser.add_argument(
        "--list-datasets",
        action="store_true",
        help="List available dataset instances and exit.",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Do not print the relationships in the selected pattern units.",
    )
    parser.add_argument(
        "-a",
        "--approximation",
        choices=["rounding", "greedy", "random", "1", "2", "3"],
        default="random",
        help=(
            "Approximation strategy: rounding (1), greedy (2), or "
            "random (3). Default: random."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed used by rounding/random baselines. Default: 42.",
    )
    parser.add_argument(
        "--log-root",
        default=os.getenv("RES_LOG_ROOT", "run_logs"),
        help=(
            "Root directory for logs. Logs are placed in a dataset-specific "
            "subdirectory. Default: RES_LOG_ROOT or ./run_logs."
        ),
    )

    return parser


def print_available_datasets(instance_root):
    """Print the Neo4j instances available under the configured root."""
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
        f"RES-RD_{dataset}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    tee = Tee(str(log_filename))
    original_stdout = sys.stdout
    sys.stdout = tee
    driver = None

    try:
        print("=" * 70)
        print("P-RES approximation experiment")
        print(f"Dataset: {dataset}")
        print(f"Instance root: {Path(args.instance_root).expanduser()}")
        print(f"URI: {args.uri}")
        print(f"Database: {args.database}")
        print(f"Approximation: {args.approximation}")
        print(f"Random seed: {args.seed}")
        print(f"Query: {args.query}")
        print(f"Pattern: {args.pattern}")
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

        matches = run_query(
            driver,
            args.query,
            database=args.database,
        )
        patterns = run_query(
            driver,
            args.pattern,
            database=args.database,
        )

        if matches is None or patterns is None:
            print("✗ Query execution failed")
            return 3

        if not matches:
            print("✗ The query has no matches; resilience is not computed")
            return 4

        if not patterns:
            print("✗ The intervention pattern has no matches")
            return 5

        print(f"Query matches: {len(matches)}")
        print(f"Intervention units: {len(patterns)}")

        _, query_elements = get_match_elements(matches)
        _, pattern_elements = get_match_elements(patterns)

        # Use element IDs to remove duplicate Relationship objects obtained
        # through different query records.
        elements = set(
            {
                edge.element_id: edge
                for edge in (query_elements or set()) | (pattern_elements or set())
            }.values()
        )

        pattern_unit_edges, match_edges, patterns_of_edge = element_match_mapping(
            elements,
            matches,
            patterns,
        )

        overlap_start = time.perf_counter()
        hitting_sets = compute_match_hitting_set(
            match_edges,
            pattern_unit_edges,
        )
        overlap_end = time.perf_counter()

        approximation_map = {
            "1": "rounding",
            "2": "greedy",
            "3": "random",
            "rounding": "rounding",
            "greedy": "greedy",
            "random": "random",
        }
        approximation = approximation_map[args.approximation]

        algorithm_start = time.perf_counter()

        if approximation == "rounding":
            result = LP_rounding(
                elements,
                patterns,
                pattern_unit_edges,
                match_edges,
                patterns_of_edge,
                matches,
                hitting_sets,
            )
        elif approximation == "greedy":
            result = LP_guided_greedy(
                elements,
                patterns,
                pattern_unit_edges,
                match_edges,
                patterns_of_edge,
                matches,
                hitting_sets,
            )
        else:
            result = LP_guided_random(
                elements,
                patterns,
                pattern_unit_edges,
                match_edges,
                patterns_of_edge,
                matches,
                hitting_sets,
            )

        algorithm_end = time.perf_counter()

        print("Status:", result["status"])
        print(
            "Objective Value (Number of Pattern Units Selected):",
            result["objective_value"],
        )
        print("Solution:", result["solution"])

        if not args.no_details:
            print("\nSelected Pattern Units:")
            for pattern_unit_index in sorted(result["solution"]):
                print(f"\n - {pattern_unit_index}")

                for edge in patterns[pattern_unit_index].values():
                    if isinstance(edge, Relationship):
                        print(f"   - edge: {edge}")

        lp_runtime = algorithm_end - algorithm_start
        overlap_runtime = overlap_end - overlap_start
        total_runtime = lp_runtime + overlap_runtime

        print("=" * 50)
        print(f"Dataset: {dataset}")
        print(f"Query resilience: {result['objective_value']}")
        print(f"Execution time: {lp_runtime:.4f} seconds")
        print(
            "+ Match hitting-set computation time: "
            f"{overlap_runtime:.4f} seconds"
        )
        print(f"Total execution time: {total_runtime:.4f} seconds")

        return 0

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
