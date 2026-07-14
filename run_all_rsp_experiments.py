#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from getpass import getpass
from pathlib import Path
from typing import Any

from neo4j import Query
from pulp import (
    LpMinimize,
    LpProblem,
    LpStatus,
    LpVariable,
    PULP_CBC_CMD,
    lpSum,
    value,
)

from utils import (
    close_neo4j,
    connect_to_dataset,
    get_match_elements,
    list_dataset_instances,
    normalize_dataset_name,
)


METRIC_PATTERNS = {
    "query_matches": re.compile(r"^Query matches:\s*(\d+)\s*$", re.MULTILINE),
    "intervention_units": re.compile(r"^Intervention units:\s*(\d+)\s*$", re.MULTILINE),
    "target_intervention_unit": re.compile(
        r"^Target intervention unit:\s*(\d+)\s*$", re.MULTILINE
    ),
    "candidate_surviving_matches": re.compile(
        r"^Candidate surviving matches:\s*(\d+)\s*$", re.MULTILINE
    ),
    "matches_requiring_contingency": re.compile(
        r"^Matches requiring contingency deletion:\s*(\d+)\s*$",
        re.MULTILINE,
    ),
    "survivor_match": re.compile(
        r"^Surviving match:\s*(\d+)\s*$", re.MULTILINE
    ),
    "status": re.compile(r"^Status:\s*(.+?)\s*$", re.MULTILINE),
    "contingency_size": re.compile(
        r"^Contingency size:\s*(-?\d+)\s*$", re.MULTILINE
    ),
    "responsibility": re.compile(
        r"^Query responsibility:\s*(-?\d+(?:\.\d+)?)\s*$", re.MULTILINE
    ),
    "lp_objective": re.compile(
        r"^LP Objective Value:\s*(-?\d+(?:\.\d+)?)\s*$", re.MULTILINE
    ),
    "selected_survivor_lp_value": re.compile(
        r"^Selected Survivor LP Value:\s*(-?\d+(?:\.\d+)?)\s*$",
        re.MULTILINE,
    ),
    "feasible_survivor_lps": re.compile(
        r"^Feasible survivor-conditioned LPs:\s*(\d+)\s*$", re.MULTILINE
    ),
    "algorithm_time": re.compile(r"^Execution time:\s*([\d.]+)\s+seconds\s*$", re.MULTILINE),
    "hitting_set_time": re.compile(
        r"^\+ Match hitting-set computation time:\s*([\d.]+)\s+seconds\s*$",
        re.MULTILINE,
    ),
    "reported_total_time": re.compile(
        r"^Total execution time:\s*([\d.]+)\s+seconds\s*$",
        re.MULTILINE,
    ),
}


def parse_csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def candidates_for_dataset(
    config: dict[str, Any],
    dataset: str,
) -> list[dict[str, Any]]:
    """Prepend a schema-specific runtime/quality candidate for a dataset."""
    dataset_config = config["datasets"][dataset]
    edge_match = dataset_config.get("runtime_quality_edge_match")
    local = [
        candidate for candidate in dataset_config.get("candidates", [])
        if candidate["name"] != "anchored_balanced_edge_pair_cover"
    ]
    if not edge_match:
        return local

    prefix = (
        "CALL { " + edge_match + " WITH a, b, r "
        "ORDER BY elementId(r) "
        "LIMIT toInteger(ceil((1.0 + sqrt(1.0 + 4.0 * (__N__ - 1))) "
        "/ 2.0)) + 2 "
        "WITH collect({source: a, target: b, rel: r}) AS edges "
        "RETURN edges } "
    )
    pattern = prefix + (
        "CALL { WITH edges "
        "RETURN edges[0].source AS a1, edges[0].target AS b1, "
        "edges[1].source AS a2, edges[1].target AS b2, "
        "edges[0].rel AS r1, edges[1].rel AS r2, 0 AS branch "
        "UNION ALL WITH edges WITH edges[2..] AS core "
        "UNWIND range(0, size(core) - 2) AS i "
        "UNWIND range(i + 1, size(core) - 1) AS j "
        "WITH core, i, j, CASE WHEN "
        "(j = i + 1 AND i % 2 = 0) OR "
        "(size(core) % 2 = 1 AND i = 0 AND j = size(core) - 1) "
        "THEN 0 ELSE 1 END AS priority, j - i AS gap "
        "ORDER BY priority, gap, i, j LIMIT toInteger(__M__ - 1) "
        "RETURN core[i].source AS a1, core[i].target AS b1, "
        "core[j].source AS a2, core[j].target AS b2, "
        "core[i].rel AS r1, core[j].rel AS r2, 1 AS branch } "
        "WITH a1, b1, a2, b2, r1, r2, branch "
        "ORDER BY branch, elementId(r1), elementId(r2) LIMIT __M__ "
        "RETURN a1, b1, a2, b2, r1, r2"
    )
    query = prefix + (
        "CALL { WITH edges "
        "RETURN edges[0].source AS a1, edges[0].target AS b1, "
        "edges[1].source AS a2, edges[1].target AS b2, "
        "edges[0].rel AS r1, edges[1].rel AS r2, 0 AS branch "
        "UNION ALL WITH edges WITH edges[2..] AS core "
        "UNWIND range(0, size(core) - 1) AS i "
        "UNWIND range(0, size(core) - 1) AS j "
        "WITH core, i, j WHERE i <> j ORDER BY i, j "
        "LIMIT toInteger(__N__ - 1) "
        "RETURN core[i].source AS a1, core[i].target AS b1, "
        "core[j].source AS a2, core[j].target AS b2, "
        "core[i].rel AS r1, core[j].rel AS r2, 1 AS branch } "
        "WITH a1, b1, a2, b2, r1, r2, branch "
        "ORDER BY branch, elementId(r1), elementId(r2) LIMIT __N__ "
        "RETURN a1, b1, a2, b2, r1, r2"
    )
    generated = {
        "name": dataset.replace("-", "_") + "_runtime_quality_pair_cover",
        "description": (
            "Dataset-specific controlled pair-cover candidate built from "
            + dataset_config.get("runtime_quality_edge_description", edge_match)
            + "; screened for near-greedy size and lower rounding work."
        ),
        "pattern": pattern,
        "query": query,
        "rounding_favorable": True,
    }
    return [generated, *local]


def render(template: str, *, n: int, m: int) -> str:
    return (
        template.replace("__N__", str(n))
        .replace("__M__", str(m))
        .strip()
        .rstrip(";")
    )


def load_reused_preflight(
    roots: list[Path],
    *,
    dataset: str,
    setting: str,
    target_n: int,
    target_m: int,
    allow_smaller: bool,
    requested_target: str | None,
) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    """Load and validate one previously resolved dataset/setting artifact."""
    last_reason = None
    for root in roots:
        query_dir = root / "resolved_queries" / dataset / setting
        query_file = query_dir / "query.cypher"
        pattern_file = query_dir / "pattern.cypher"
        metadata_file = query_dir / "metadata.json"
        if not all(path.is_file() for path in (
            query_file, pattern_file, metadata_file
        )):
            continue
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            query = query_file.read_text(encoding="utf-8").strip()
            pattern = pattern_file.read_text(encoding="utf-8").strip()
            q_count = int(metadata["query_matches"])
            p_count = int(metadata["intervention_units"])
            saved_target = str(metadata["target"])
        except Exception as exc:
            last_reason = f"invalid saved metadata: {type(exc).__name__}: {exc}"
            continue

        if metadata.get("dataset") != dataset or metadata.get("setting") != setting:
            last_reason = "saved dataset/setting identity does not match"
            continue
        exact = q_count == target_n and p_count == target_m
        if not exact and not (
            allow_smaller and 0 < q_count <= target_n and 0 < p_count <= target_m
        ):
            last_reason = (
                f"saved counts n={q_count},m={p_count} do not match "
                f"requested n={target_n},m={target_m}"
            )
            continue
        if requested_target and saved_target != requested_target:
            last_reason = "saved target differs from --target"
            continue
        if not query or not pattern or not saved_target:
            last_reason = "saved query, pattern, or target is empty"
            continue

        resolved = {
            **metadata,
            "name": metadata.get("candidate", "reused_candidate"),
            "query": query,
            "pattern": pattern,
            "query_matches": q_count,
            "intervention_units": p_count,
            "target": saved_target,
            "target_index": int(metadata.get("target_index", -1)),
        }
        return resolved, root, None
    return None, None, last_reason


def count_query(cypher: str) -> str:
    return f"CALL {{ {cypher.strip().rstrip(';')} }} RETURN count(*) AS count"


def run_count(session, cypher: str, timeout_seconds: int) -> int:
    result = session.run(Query(count_query(cypher), timeout=timeout_seconds))
    record = result.single()
    return 0 if record is None else int(record["count"])


def resolve_target_ids(
    session,
    pattern: str,
    query: str,
    timeout_seconds: int,
) -> str | None:
    """Return one intervention unit overlapping a returned query match."""
    query_result = session.run(Query(query, timeout=timeout_seconds))
    query_record = next(iter(query_result), None)
    if query_record is None:
        return None

    _, query_relationships = get_match_elements([dict(query_record)])
    query_edge_ids = {edge.element_id for edge in query_relationships}
    if not query_edge_ids:
        return None

    pattern_result = session.run(Query(pattern, timeout=timeout_seconds))
    for record in pattern_result:
        _, unit_relationships = get_match_elements([dict(record)])
        unit_edge_ids = sorted(edge.element_id for edge in unit_relationships)
        if unit_edge_ids and query_edge_ids.intersection(unit_edge_ids):
            return ",".join(unit_edge_ids)

    return None


def load_edge_sets(session, cypher: str, timeout_seconds: int) -> list[set[str]]:
    """Execute a bounded query and return relationship-ID sets per record."""
    result = session.run(Query(cypher, timeout=timeout_seconds))
    edge_sets: list[set[str]] = []
    for record in result:
        _, relationships = get_match_elements([dict(record)])
        edge_sets.append({edge.element_id for edge in relationships})
    return edge_sets


def resolve_feasible_rsp_target(
    session,
    pattern: str,
    query: str,
    timeout_seconds: int,
    requested_target: str | None = None,
    max_candidate_survivors: int = 100,
) -> dict[str, Any] | None:
    """Find a nontrivial target with a manageable conditioned search."""
    pattern_units = load_edge_sets(session, pattern, timeout_seconds)
    query_matches = load_edge_sets(session, query, timeout_seconds)
    if not pattern_units or not query_matches:
        return None

    if requested_target:
        requested_ids = {
            item.strip() for item in requested_target.split(",") if item.strip()
        }
        target_indices = [
            i for i, edges in enumerate(pattern_units)
            if edges == requested_ids
        ]
    else:
        target_indices = list(range(len(pattern_units)))

    best: dict[str, Any] | None = None
    for target_index in target_indices:
        target_edges = pattern_units[target_index]
        if not target_edges:
            continue

        W_plus = [
            w for w, edges in enumerate(query_matches)
            if target_edges & edges
        ]
        if not W_plus:
            continue

        W_zero = [
            w for w, edges in enumerate(query_matches)
            if not (target_edges & edges)
        ]
        if not W_zero or len(W_plus) > max_candidate_survivors:
            continue

        feasible_survivor = None
        for survivor in W_plus:
            survivor_edges = query_matches[survivor]
            eligible_edge_union: set[str] = set()
            for unit_index, unit_edges in enumerate(pattern_units):
                if unit_index == target_index or unit_edges & survivor_edges:
                    continue
                eligible_edge_union.update(unit_edges)

            if all(query_matches[w] & eligible_edge_union for w in W_zero):
                feasible_survivor = survivor
                break

        if feasible_survivor is None:
            continue

        candidate = {
            "target": ",".join(sorted(target_edges)),
            "target_index": target_index,
            "candidate_surviving_matches": len(W_plus),
            "target_unaffected_matches": len(W_zero),
            "preflight_survivor": feasible_survivor,
        }
        # One candidate survivor is the smallest nontrivial RSP branch count,
        # so no later target can improve this objective.
        if candidate["candidate_surviving_matches"] == 1:
            return candidate
        if best is None or (
            candidate["candidate_surviving_matches"],
            candidate["target_index"],
        ) < (
            best["candidate_surviving_matches"],
            best["target_index"],
        ):
            best = candidate

    return best


def score_rounding_gap_for_target(
    session,
    pattern: str,
    query: str,
    timeout_seconds: int,
    *,
    target_index: int,
    survivor_index: int,
) -> dict[str, Any]:
    """Score one conditioned LP as a proxy for LP-random vs rounding.

    LP-random selects every positive-support unit with probability 1/2,
    whereas rounding selects unit u with probability
    1 - (1 - x_u)^lambda.  Their expected initial selection sizes therefore
    expose settings in which diffuse LP support should hurt LP-random.
    """
    pattern_units = load_edge_sets(session, pattern, timeout_seconds)
    query_matches = load_edge_sets(session, query, timeout_seconds)
    print(
        "      scoring conditioned LP: "
        f"{len(query_matches)} matches, {len(pattern_units)} units",
        flush=True,
    )
    if not (0 <= target_index < len(pattern_units)):
        raise ValueError("preflight target index is out of range")
    if not (0 <= survivor_index < len(query_matches)):
        raise ValueError("preflight survivor index is out of range")

    target_edges = pattern_units[target_index]
    survivor_edges = query_matches[survivor_index]
    W_zero = [
        w for w, edges in enumerate(query_matches)
        if not (target_edges & edges)
    ]
    eligible_units = [
        u for u, edges in enumerate(pattern_units)
        if u != target_index and not (edges & survivor_edges)
    ]
    if not W_zero or not eligible_units:
        raise ValueError("conditioned LP has no matches or eligible units")

    units_of_edge: dict[str, list[int]] = defaultdict(list)
    edge_ids: set[str] = set()
    unique_W_zero = []
    seen_match_edges = set()
    for w in W_zero:
        key = frozenset(query_matches[w])
        if key in seen_match_edges:
            continue
        seen_match_edges.add(key)
        unique_W_zero.append(w)
        edge_ids.update(query_matches[w])
    for u in eligible_units:
        for edge in pattern_units[u]:
            units_of_edge[edge].append(u)
            edge_ids.add(edge)

    problem = LpProblem(
        f"RSP_preflight_{target_index}_{survivor_index}",
        LpMinimize,
    )
    x_unit = {
        u: LpVariable(f"x_{u}", 0, 1, cat="Continuous")
        for u in eligible_units
    }
    d_edge = {
        edge: LpVariable(f"d_{i}", 0, 1, cat="Continuous")
        for i, edge in enumerate(edge_ids)
    }
    primary_objective = lpSum(x_unit.values())
    problem += primary_objective

    for i, edge in enumerate(edge_ids):
        problem += d_edge[edge] <= lpSum(
            x_unit[u] for u in units_of_edge.get(edge, [])
        ), f"edge_upper_{i}"
    for u in eligible_units:
        for edge in pattern_units[u]:
            problem += d_edge[edge] >= x_unit[u]
    for w in unique_W_zero:
        problem += lpSum(
            d_edge[edge]
            for edge in query_matches[w]
            if edge in d_edge
        ) >= 1

    print(
        "      solving primary LP with "
        f"{len(unique_W_zero)} unique match constraints...",
        flush=True,
    )
    problem.solve(PULP_CBC_CMD(msg=0, timeLimit=timeout_seconds))
    status = LpStatus[problem.status]
    if status != "Optimal":
        raise RuntimeError(f"conditioned LP status: {status}")

    lp_value = float(value(primary_objective) or 0.0)
    if lp_value <= 0.0:
        raise RuntimeError("conditioned LP returned a nonpositive objective")

    tolerance = max(1e-7, 1e-7 * max(1.0, lp_value))
    problem += primary_objective <= lp_value + tolerance
    max_x = LpVariable("preflight_max_x", 0, 1, cat="Continuous")
    for u in eligible_units:
        problem += x_unit[u] <= max_x
    problem.setObjective(max_x)
    print("      spreading the primary-optimal LP solution...", flush=True)
    problem.solve(PULP_CBC_CMD(msg=0, timeLimit=timeout_seconds))
    spread_status = LpStatus[problem.status]
    if spread_status != "Optimal":
        raise RuntimeError(f"LP spreading status: {spread_status}")

    x_values = {
        u: min(1.0, max(0.0, float(variable.varValue or 0.0)))
        for u, variable in x_unit.items()
    }
    support_values = [x for x in x_values.values() if x > 1e-9]
    q = max(1, max(len(query_matches[w]) for w in W_zero))
    amplification = max(1.0, q * math.log(len(W_zero) / lp_value))
    expected_rounding = sum(
        1.0 if x >= 1.0
        else -math.expm1(amplification * math.log1p(-x))
        for x in support_values
    )
    expected_random = len(support_values) / 2.0
    gap = expected_random / max(expected_rounding, 1e-12)

    proxy_limit = 20000
    if len(W_zero) <= proxy_limit:
        proxy_W_zero = list(W_zero)
    else:
        step = len(W_zero) / proxy_limit
        proxy_W_zero = [W_zero[int(i * step)] for i in range(proxy_limit)]
    print(
        "      simulating final sizes on "
        f"{len(proxy_W_zero)} representative matches...",
        flush=True,
    )

    matches_of_edge: dict[str, set[int]] = defaultdict(set)
    for w in proxy_W_zero:
        for edge in query_matches[w]:
            matches_of_edge[edge].add(w)
    unit_matches = {}
    for u in eligible_units:
        covered: set[int] = set()
        for edge in pattern_units[u]:
            covered.update(matches_of_edge.get(edge, ()))
        unit_matches[u] = covered

    def greedy_cover(allowed_units: set[int]) -> tuple[int, int]:
        remaining = set(proxy_W_zero)
        available = set(allowed_units)
        selected = set()
        work = 0
        while remaining and available:
            work += len(available) * len(remaining)
            best_unit = max(
                available,
                key=lambda u: (
                    len(unit_matches[u] & remaining),
                    x_values.get(u, 0.0),
                    -u,
                ),
            )
            covered = unit_matches[best_unit] & remaining
            if not covered:
                break
            selected.add(best_unit)
            remaining.difference_update(covered)
            available.remove(best_unit)
        if remaining:
            raise RuntimeError("greedy proxy could not cover all matches")
        return len(selected), work

    def repair_and_prune(
        initial: set[int],
        *,
        prune: bool = True,
    ) -> tuple[int, int]:
        selected = set(initial)
        remaining = set(proxy_W_zero)
        work = len(eligible_units)
        for u in selected:
            remaining.difference_update(unit_matches[u])
        units_by_match: dict[int, set[int]] = defaultdict(set)
        for u in eligible_units:
            for w in unit_matches[u]:
                units_by_match[w].add(u)
        while remaining:
            w = min(remaining)
            candidates = units_by_match[w]
            work += len(candidates)
            if not candidates:
                raise RuntimeError("rounding proxy could not repair coverage")
            chosen = max(
                candidates,
                key=lambda u: (
                    len(unit_matches[u] & remaining),
                    x_values.get(u, 0.0),
                    -u,
                ),
            )
            selected.add(chosen)
            remaining.difference_update(unit_matches[chosen])

        if not prune:
            return len(selected), work

        cover_count = {w: 0 for w in proxy_W_zero}
        for u in selected:
            for w in unit_matches[u]:
                cover_count[w] += 1
        for u in sorted(
            selected,
            key=lambda item: (x_values.get(item, 0.0), item),
        ):
            covered = unit_matches[u]
            work += len(covered)
            if covered and all(cover_count[w] >= 2 for w in covered):
                selected.remove(u)
                for w in covered:
                    cover_count[w] -= 1
        return len(selected), work

    direct_greedy_size, direct_greedy_work = greedy_cover(
        set(eligible_units)
    )
    active_units = {u for u, x in x_values.items() if x > 1e-9}
    lp_greedy_size, lp_greedy_work = greedy_cover(active_units)
    rounding_sizes = []
    rounding_work = []
    lp_random_sizes = []
    direct_random_sizes = []
    for seed in (11, 23, 37, 53, 71):
        rng = random.Random(seed)
        sampled = {
            u for u in eligible_units
            if rng.random() < (
                1.0 if x_values.get(u, 0.0) >= 1.0
                else -math.expm1(
                    amplification * math.log1p(-x_values.get(u, 0.0))
                )
                if x_values.get(u, 0.0) > 0.0 else 0.0
            )
        }
        size, work = repair_and_prune(sampled)
        rounding_sizes.append(size)
        rounding_work.append(work)

        lp_random_sample = {
            u for u in active_units if rng.random() < 0.5
        }
        random_size, _ = repair_and_prune(
            lp_random_sample,
            prune=False,
        )
        lp_random_sizes.append(random_size)

        ordered_units = list(eligible_units)
        rng.shuffle(ordered_units)
        remaining = set(proxy_W_zero)
        direct_selected = set()
        for u in ordered_units:
            covered = unit_matches[u] & remaining
            if not covered:
                continue
            direct_selected.add(u)
            remaining.difference_update(covered)
            if not remaining:
                break
        if remaining:
            raise RuntimeError("direct-random proxy could not cover all matches")
        direct_random_sizes.append(len(direct_selected))

    mean_rounding_size = statistics.mean(rounding_sizes)
    mean_rounding_work = statistics.mean(rounding_work)
    best_greedy_size = min(lp_greedy_size, direct_greedy_size)
    faster_greedy_work = min(lp_greedy_work, direct_greedy_work)
    quality_ratio = mean_rounding_size / max(1, best_greedy_size)
    work_ratio = faster_greedy_work / max(1.0, mean_rounding_work)
    final_random_gap = statistics.mean(lp_random_sizes) / max(
        1.0, mean_rounding_size
    )
    final_direct_random_gap = statistics.mean(direct_random_sizes) / max(
        1.0, mean_rounding_size
    )

    return {
        "preflight_lp_objective": lp_value,
        "lp_support_size": len(support_values),
        "lp_max_x": max(support_values, default=0.0),
        "lp_mean_positive_x": (
            sum(support_values) / len(support_values)
            if support_values else 0.0
        ),
        "rounding_lambda": amplification,
        "expected_rounding_selection": expected_rounding,
        "expected_lp_random_selection": expected_random,
        "predicted_random_rounding_gap": gap,
        "preflight_rounding_size_mean": mean_rounding_size,
        "preflight_rounding_size_min": min(rounding_sizes),
        "preflight_rounding_size_max": max(rounding_sizes),
        "preflight_lp_greedy_size": lp_greedy_size,
        "preflight_direct_greedy_size": direct_greedy_size,
        "preflight_rounding_greedy_size_ratio": quality_ratio,
        "preflight_rounding_work": mean_rounding_work,
        "preflight_lp_greedy_work": lp_greedy_work,
        "preflight_direct_greedy_work": direct_greedy_work,
        "preflight_greedy_rounding_work_ratio": work_ratio,
        "preflight_lp_random_size_mean": statistics.mean(lp_random_sizes),
        "preflight_direct_random_size_mean": statistics.mean(
            direct_random_sizes
        ),
        "preflight_final_random_rounding_gap": final_random_gap,
        "preflight_final_direct_random_rounding_gap": final_direct_random_gap,
        "preflight_proxy_matches": len(proxy_W_zero),
        "preflight_unique_lp_constraints": len(unique_W_zero),
    }


def choose_rsp_setting_candidate(
    session,
    candidates: list[dict[str, str]],
    *,
    target_n: int,
    target_m: int,
    timeout_seconds: int,
    allow_smaller: bool,
    requested_target: str | None,
    max_candidate_survivors: int,
    candidate_policy: str = "first-feasible",
    min_lp_support: int = 0,
    min_random_rounding_gap: float = 0.0,
    min_final_random_rounding_gap: float = 0.0,
    min_final_direct_random_rounding_gap: float = 0.0,
    max_rounding_greedy_size_ratio: float = float("inf"),
    min_greedy_rounding_work_ratio: float = 0.0,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Select a capacity-valid and survivor-feasible candidate for one setting."""
    attempts: list[dict[str, Any]] = []
    best_smaller: tuple[float, dict[str, Any]] | None = None
    best_exact: tuple[float, dict[str, Any]] | None = None

    ordered_candidates = list(candidates)
    screened_policy = candidate_policy in {
        "rounding-favorable", "runtime-quality-favorable"
    }
    if screened_policy:
        ordered_candidates.sort(
            key=lambda candidate: not candidate.get("rounding_favorable", False)
        )

    for candidate in ordered_candidates:
        if (
            screened_policy
            and best_exact is not None
            and not candidate.get("rounding_favorable", False)
        ):
            break
        attempt, resolved = test_candidate(
            session,
            candidate,
            target_n=target_n,
            target_m=target_m,
            timeout_seconds=timeout_seconds,
        )
        attempts.append(attempt)
        if resolved is None:
            print(
                f"    {candidate['name']}: PREFLIGHT_FAILED "
                f"({attempt.get('error', '')})"
            )
            continue

        q_count = int(resolved["query_matches"])
        p_count = int(resolved["intervention_units"])
        exact = q_count == target_n and p_count == target_m
        print(
            f"    {candidate['name']}: "
            f"n={q_count}/{target_n}, m={p_count}/{target_m}"
        )

        if q_count <= 0 or p_count <= 0:
            attempt["status"] = "ZERO_CAPACITY"
            continue
        if not exact and not allow_smaller:
            attempt["status"] = "CAPACITY_SHORTFALL"
            continue

        try:
            print("      resolving a survivor-compatible target...", flush=True)
            rsp_info = resolve_feasible_rsp_target(
                session,
                resolved["pattern"],
                resolved["query"],
                timeout_seconds,
                requested_target=requested_target,
                max_candidate_survivors=max_candidate_survivors,
            )
        except Exception as exc:
            attempt["status"] = "RSP_PREFLIGHT_FAILED"
            attempt["error"] = f"{type(exc).__name__}: {exc}"
            continue

        if rsp_info is None:
            attempt["status"] = "RSP_INFEASIBLE"
            attempt["error"] = (
                "No nontrivial target has a survivor-compatible cover "
                f"with at most {max_candidate_survivors} candidate survivors"
            )
            print(f"      {attempt['error']}")
            continue

        resolved.update(rsp_info)
        attempt.update(rsp_info)

        if screened_policy:
            try:
                print("      running runtime/quality screening...", flush=True)
                gap_info = score_rounding_gap_for_target(
                    session,
                    resolved["pattern"],
                    resolved["query"],
                    timeout_seconds,
                    target_index=int(rsp_info["target_index"]),
                    survivor_index=int(rsp_info["preflight_survivor"]),
                )
            except Exception as exc:
                attempt["status"] = "ROUNDING_LP_FAILED"
                attempt["error"] = f"{type(exc).__name__}: {exc}"
                print(f"      {attempt['error']}")
                continue

            resolved.update(gap_info)
            attempt.update(gap_info)
            if (
                int(gap_info["lp_support_size"]) < min_lp_support
                or float(gap_info["predicted_random_rounding_gap"])
                < min_random_rounding_gap
                or float(gap_info["preflight_final_random_rounding_gap"])
                < min_final_random_rounding_gap
                or float(
                    gap_info["preflight_final_direct_random_rounding_gap"]
                ) < min_final_direct_random_rounding_gap
                or float(gap_info["preflight_rounding_greedy_size_ratio"])
                > max_rounding_greedy_size_ratio
                or float(gap_info["preflight_greedy_rounding_work_ratio"])
                < min_greedy_rounding_work_ratio
            ):
                attempt["status"] = "RUNTIME_QUALITY_GAP_TOO_SMALL"
                attempt["error"] = (
                    "Conditioned LP does not meet the requested screening "
                    f"thresholds (support>={min_lp_support}, "
                    f"random-gap>={min_random_rounding_gap:g}, "
                    "final-random-gap>="
                    f"{min_final_random_rounding_gap:g}, "
                    "final-direct-random-gap>="
                    f"{min_final_direct_random_rounding_gap:g}, "
                    "rounding/greedy-size<="
                    f"{max_rounding_greedy_size_ratio:g}, "
                    "greedy/rounding-work>="
                    f"{min_greedy_rounding_work_ratio:g})"
                )
                print(
                    "      rounding screen rejected: "
                    f"support={gap_info['lp_support_size']}, "
                    "predicted random/rounding gap="
                    f"{gap_info['predicted_random_rounding_gap']:.3f}, "
                    "rounding/greedy size="
                    f"{gap_info['preflight_rounding_greedy_size_ratio']:.3f}, "
                    "final random/rounding="
                    f"{gap_info['preflight_final_random_rounding_gap']:.3f}, "
                    "final direct-random/rounding="
                    f"{gap_info['preflight_final_direct_random_rounding_gap']:.3f}, "
                    "greedy/rounding work="
                    f"{gap_info['preflight_greedy_rounding_work_ratio']:.3f}"
                )
                continue

        attempt["status"] = "RSP_FEASIBLE"
        attempt["error"] = ""
        print(
            "      RSP feasible: "
            f"target={rsp_info['target_index']}, "
            f"candidate survivors={rsp_info['candidate_surviving_matches']}"
        )

        if screened_policy:
            print(
                "      rounding screen passed: "
                f"LP={resolved['preflight_lp_objective']:.3f}, "
                f"support={resolved['lp_support_size']}, "
                f"E[random]={resolved['expected_lp_random_selection']:.2f}, "
                f"E[rounding]={resolved['expected_rounding_selection']:.2f}, "
                f"gap={resolved['predicted_random_rounding_gap']:.3f}"
                ", rounding/greedy size="
                f"{resolved['preflight_rounding_greedy_size_ratio']:.3f}, "
                "final random/rounding="
                f"{resolved['preflight_final_random_rounding_gap']:.3f}, "
                "final direct-random/rounding="
                f"{resolved['preflight_final_direct_random_rounding_gap']:.3f}, "
                "greedy/rounding work="
                f"{resolved['preflight_greedy_rounding_work_ratio']:.3f}"
            )

        if exact:
            if not screened_policy:
                return resolved, attempts
            score = float(resolved["preflight_greedy_rounding_work_ratio"])
            if best_exact is None or score > best_exact[0]:
                best_exact = (score, resolved)
            continue

        score = min(q_count / target_n, p_count / target_m)
        if best_smaller is None or score > best_smaller[0]:
            best_smaller = (score, resolved)

    if best_exact is not None:
        return best_exact[1], attempts
    return (best_smaller[1] if best_smaller else None), attempts


def test_candidate(
    session,
    candidate: dict[str, str],
    *,
    target_n: int,
    target_m: int,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    pattern = render(candidate["pattern"], n=target_n, m=target_m)
    query = render(candidate["query"], n=target_n, m=target_m)

    attempt: dict[str, Any] = {
        "candidate": candidate["name"],
        "description": candidate.get("description", ""),
        "rounding_favorable": bool(
            candidate.get("rounding_favorable", False)
        ),
        "target_query_matches": target_n,
        "target_intervention_units": target_m,
    }

    try:
        pattern_count = run_count(session, pattern, timeout_seconds)
        query_count = run_count(session, query, timeout_seconds)
    except Exception as exc:
        attempt.update(
            {
                "status": "PREFLIGHT_FAILED",
                "error": f"{type(exc).__name__}: {exc}",
                "query_matches": "",
                "intervention_units": "",
            }
        )
        return attempt, None

    attempt.update(
        {
            "status": "OK",
            "error": "",
            "query_matches": query_count,
            "intervention_units": pattern_count,
        }
    )

    resolved = {
        "name": candidate["name"],
        "description": candidate.get("description", ""),
        "rounding_favorable": bool(
            candidate.get("rounding_favorable", False)
        ),
        "query": query,
        "pattern": pattern,
        "query_matches": query_count,
        "intervention_units": pattern_count,
    }
    return attempt, resolved


def choose_dataset_candidate(
    session,
    candidates: list[dict[str, str]],
    *,
    target_n: int,
    target_m: int,
    timeout_seconds: int,
    allow_smaller: bool,
) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    """
    Select one query/pattern shape for the dataset using the largest setting.

    The same shape is then reused for all smaller settings so that scaling
    results within one dataset remain comparable.
    """
    attempts: list[dict[str, Any]] = []
    best: tuple[float, dict[str, str]] | None = None

    for candidate in candidates:
        attempt, resolved = test_candidate(
            session,
            candidate,
            target_n=target_n,
            target_m=target_m,
            timeout_seconds=timeout_seconds,
        )
        attempts.append(attempt)

        if resolved is None:
            print(
                f"    {candidate['name']}: PREFLIGHT_FAILED\n"
                f"      {attempt.get('error', '')}"
            )
            continue

        q_count = int(resolved["query_matches"])
        p_count = int(resolved["intervention_units"])
        print(
            f"    {candidate['name']}: "
            f"n={q_count}/{target_n}, m={p_count}/{target_m}"
        )

        if q_count == target_n and p_count == target_m:
            return candidate, attempts

        if q_count > 0 and p_count > 0:
            score = min(q_count / target_n, p_count / target_m)
            if best is None or score > best[0]:
                best = (score, candidate)

    if allow_smaller and best is not None:
        return best[1], attempts

    return None, attempts


def resolve_setting(
    session,
    candidate: dict[str, str],
    *,
    target_n: int,
    target_m: int,
    timeout_seconds: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    attempt, resolved = test_candidate(
        session,
        candidate,
        target_n=target_n,
        target_m=target_m,
        timeout_seconds=timeout_seconds,
    )
    return resolved, attempt


def terminate_process_group(
    process: subprocess.Popen[str],
    grace_seconds: int = 10,
) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    process.wait()


def parse_metrics(text: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}

    for key, pattern in METRIC_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue

        value: Any = match.group(1).strip()

        if key in {
            "query_matches",
            "intervention_units",
            "target_intervention_unit",
            "candidate_surviving_matches",
            "matches_requiring_contingency",
            "survivor_match",
            "feasible_survivor_lps",
            "contingency_size",
        }:
            value = int(value)
        elif key in {
            "responsibility",
            "lp_objective",
            "selected_survivor_lp_value",
            "algorithm_time",
            "hitting_set_time",
            "reported_total_time",
        }:
            value = float(value)

        parsed[key] = value

    return parsed


def run_rollout(
    command: list[str],
    *,
    timeout_seconds: int,
    stdout_path: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
        bufsize=1,
    )

    timed_out = False

    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_group(process)
        try:
            output, _ = process.communicate(timeout=5)
        except Exception:
            output = ""

    wall_time = time.perf_counter() - started
    stdout_path.write_text(output or "", encoding="utf-8", errors="replace")
    metrics = parse_metrics(output or "")

    if timed_out:
        outcome = "TIMEOUT"
    elif process.returncode == 0:
        outcome = "SUCCESS"
    else:
        outcome = "FAILED"

    return {
        "outcome": outcome,
        "exit_code": process.returncode,
        "wall_time": wall_time,
        "stdout_file": str(stdout_path),
        **metrics,
    }


def load_existing_keys(results_path: Path) -> set[tuple[Any, ...]]:
    keys: set[tuple[Any, ...]] = set()

    if not results_path.exists():
        return keys

    for line in results_path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue

        keys.add(
            (
                row.get("dataset"),
                row.get("setting"),
                row.get("candidate"),
                row.get("algorithm"),
                row.get("seed"),
                row.get("requested_target"),
                row.get("actual_query_matches"),
                row.get("actual_intervention_units"),
            )
        )

    return keys


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_preflight_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    fields = [
        "dataset",
        "phase",
        "setting",
        "candidate",
        "description",
        "candidate_policy",
        "rounding_favorable",
        "status",
        "error",
        "target_query_matches",
        "target_intervention_units",
        "query_matches",
        "intervention_units",
        "target",
        "target_index",
        "candidate_surviving_matches",
        "target_unaffected_matches",
        "preflight_survivor",
        "preflight_lp_objective",
        "lp_support_size",
        "lp_max_x",
        "lp_mean_positive_x",
        "rounding_lambda",
        "expected_rounding_selection",
        "expected_lp_random_selection",
        "predicted_random_rounding_gap",
        "preflight_rounding_size_mean",
        "preflight_rounding_size_min",
        "preflight_rounding_size_max",
        "preflight_lp_greedy_size",
        "preflight_direct_greedy_size",
        "preflight_rounding_greedy_size_ratio",
        "preflight_rounding_work",
        "preflight_lp_greedy_work",
        "preflight_direct_greedy_work",
        "preflight_greedy_rounding_work_ratio",
        "preflight_lp_random_size_mean",
        "preflight_direct_random_size_mean",
        "preflight_final_random_rounding_gap",
        "preflight_final_direct_random_rounding_gap",
        "preflight_proxy_matches",
        "preflight_unique_lp_constraints",
        "selected",
    ]

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def rewrite_csv_and_aggregates(
    results_path: Path,
    output_root: Path,
) -> None:
    rows: list[dict[str, Any]] = []

    if results_path.exists():
        for line in results_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    fields = [
        "dataset",
        "setting",
        "candidate",
        "candidate_policy",
        "rounding_favorable",
        "preflight_lp_objective",
        "lp_support_size",
        "lp_max_x",
        "lp_mean_positive_x",
        "rounding_lambda",
        "expected_rounding_selection",
        "expected_lp_random_selection",
        "predicted_random_rounding_gap",
        "target_query_matches",
        "target_intervention_units",
        "actual_query_matches",
        "actual_intervention_units",
        "algorithm",
        "seed",
        "requested_target",
        "target_intervention_unit",
        "candidate_surviving_matches",
        "matches_requiring_contingency",
        "survivor_match",
        "outcome",
        "exit_code",
        "status",
        "contingency_size",
        "responsibility",
        "lp_objective",
        "selected_survivor_lp_value",
        "feasible_survivor_lps",
        "algorithm_time",
        "hitting_set_time",
        "reported_total_time",
        "wall_time",
        "stdout_file",
        "query_file",
        "pattern_file",
    ]

    with (output_root / "summary.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    timeouts = [
        row
        for row in rows
        if row.get("outcome") == "TIMEOUT"
    ]

    with (output_root / "timeouts.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(timeouts)

    grouped: dict[
        tuple[Any, ...],
        list[dict[str, Any]],
    ] = defaultdict(list)

    for row in rows:
        grouped[
            (
                row.get("dataset"),
                row.get("setting"),
                row.get("candidate"),
                row.get("algorithm"),
                row.get("requested_target"),
            )
        ].append(row)

    aggregate_fields = [
        "dataset",
        "setting",
        "candidate",
        "candidate_policy",
        "rounding_favorable",
        "preflight_lp_objective",
        "lp_support_size",
        "lp_max_x",
        "lp_mean_positive_x",
        "rounding_lambda",
        "expected_rounding_selection",
        "expected_lp_random_selection",
        "predicted_random_rounding_gap",
        "algorithm",
        "requested_target",
        "runs",
        "successes",
        "timeouts",
        "failures",
        "mean_contingency_size",
        "std_contingency_size",
        "mean_responsibility",
        "std_responsibility",
        "mean_lp_objective",
        "mean_selected_survivor_lp_value",
        "mean_feasible_survivor_lps",
        "median_wall_time",
        "mean_reported_total_time",
        "actual_query_matches",
        "actual_intervention_units",
    ]

    aggregate_rows: list[dict[str, Any]] = []

    for key, group in sorted(grouped.items()):
        successes = [
            row
            for row in group
            if row.get("outcome") == "SUCCESS"
        ]

        contingency_sizes = [
            float(row["contingency_size"])
            for row in successes
            if row.get("contingency_size") is not None
        ]

        responsibilities = [
            float(row["responsibility"])
            for row in successes
            if row.get("responsibility") is not None
        ]

        lp_objectives = [
            float(row["lp_objective"])
            for row in successes
            if row.get("lp_objective") is not None
            and float(row["lp_objective"]) >= 0
        ]

        selected_lp_values = [
            float(row["selected_survivor_lp_value"])
            for row in successes
            if row.get("selected_survivor_lp_value") is not None
        ]

        feasible_lp_counts = [
            float(row["feasible_survivor_lps"])
            for row in successes
            if row.get("feasible_survivor_lps") is not None
        ]

        wall = [
            float(row["wall_time"])
            for row in successes
            if row.get("wall_time") is not None
        ]

        reported = [
            float(row["reported_total_time"])
            for row in successes
            if row.get("reported_total_time") is not None
        ]

        aggregate_rows.append(
            {
                "dataset": key[0],
                "setting": key[1],
                "candidate": key[2],
                "candidate_policy": group[0].get("candidate_policy", ""),
                "rounding_favorable": group[0].get(
                    "rounding_favorable", ""
                ),
                "preflight_lp_objective": group[0].get(
                    "preflight_lp_objective", ""
                ),
                "lp_support_size": group[0].get("lp_support_size", ""),
                "lp_max_x": group[0].get("lp_max_x", ""),
                "lp_mean_positive_x": group[0].get(
                    "lp_mean_positive_x", ""
                ),
                "rounding_lambda": group[0].get("rounding_lambda", ""),
                "expected_rounding_selection": group[0].get(
                    "expected_rounding_selection", ""
                ),
                "expected_lp_random_selection": group[0].get(
                    "expected_lp_random_selection", ""
                ),
                "predicted_random_rounding_gap": group[0].get(
                    "predicted_random_rounding_gap", ""
                ),
                "algorithm": key[3],
                "requested_target": key[4],
                "runs": len(group),
                "successes": len(successes),
                "timeouts": sum(
                    row.get("outcome") == "TIMEOUT"
                    for row in group
                ),
                "failures": sum(
                    row.get("outcome") == "FAILED"
                    for row in group
                ),
                "mean_contingency_size": (
                    statistics.mean(contingency_sizes)
                    if contingency_sizes
                    else ""
                ),
                "std_contingency_size": (
                    statistics.stdev(contingency_sizes)
                    if len(contingency_sizes) > 1
                    else 0.0
                    if contingency_sizes
                    else ""
                ),
                "mean_responsibility": (
                    statistics.mean(responsibilities)
                    if responsibilities
                    else ""
                ),
                "std_responsibility": (
                    statistics.stdev(responsibilities)
                    if len(responsibilities) > 1
                    else 0.0
                    if responsibilities
                    else ""
                ),
                "mean_lp_objective": (
                    statistics.mean(lp_objectives)
                    if lp_objectives
                    else ""
                ),
                "mean_selected_survivor_lp_value": (
                    statistics.mean(selected_lp_values)
                    if selected_lp_values
                    else ""
                ),
                "mean_feasible_survivor_lps": (
                    statistics.mean(feasible_lp_counts)
                    if feasible_lp_counts
                    else ""
                ),
                "median_wall_time": (
                    statistics.median(wall)
                    if wall
                    else ""
                ),
                "mean_reported_total_time": (
                    statistics.mean(reported)
                    if reported
                    else ""
                ),
                "actual_query_matches": group[0].get(
                    "actual_query_matches",
                    "",
                ),
                "actual_intervention_units": group[0].get(
                    "actual_intervention_units",
                    "",
                ),
            }
        )

    with (output_root / "aggregate_results.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=aggregate_fields,
        )
        writer.writeheader()
        writer.writerows(aggregate_rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve valid query/intervention patterns and run P-RSP "
            "algorithms across datasets with a hard per-rollout timeout."
        )
    )

    parser.add_argument("--config", default="experiment_settings.json")
    parser.add_argument("--algorithm-script", default="RSP-RD.py")
    parser.add_argument(
        "--target",
        default=None,
        help=(
            "Optional comma-separated relationship element IDs identifying "
            "the target intervention unit. If omitted, preflight resolves "
            "one unit and reuses it for every rollout. Use this "
            "only when the selected datasets share the specified IDs."
        ),
    )
    parser.add_argument(
        "--instance-root",
        default=os.getenv(
            "NEO4J_INSTANCE_ROOT",
            "~/neo4j-instances",
        ),
    )
    parser.add_argument(
        "--uri",
        default=os.getenv(
            "NEO4J_URI",
            "bolt://localhost:7687",
        ),
    )
    parser.add_argument(
        "--database",
        default=os.getenv(
            "NEO4J_DATABASE",
            "neo4j",
        ),
    )
    parser.add_argument(
        "--user",
        default=os.getenv(
            "NEO4J_USER",
            "neo4j",
        ),
    )
    parser.add_argument(
        "--password",
        default=os.getenv("NEO4J_PASSWORD"),
    )
    parser.add_argument(
        "--datasets",
        help=(
            "Comma-separated datasets. Default: datasets marked "
            "run_by_default."
        ),
    )
    parser.add_argument(
        "--settings",
        help="Comma-separated settings. Default: all settings.",
    )
    parser.add_argument(
        "--algorithms",
        default=(
            "rounding,greedy,random,direct-greedy,direct-random"
        ),
    )
    parser.add_argument(
        "--seeds",
        default="42",
        help=(
            "Seeds for stochastic methods. LP-guided and direct greedy "
            "run once."
        ),
    )
    parser.add_argument(
        "--timeout-minutes",
        type=float,
        default=30.0,
    )
    parser.add_argument(
        "--preflight-timeout-seconds",
        type=int,
        default=600,
    )
    parser.add_argument(
        "--max-candidate-survivors",
        type=int,
        default=100,
        help=(
            "Reject targets that require more conditioned LP branches. "
            "Default: 100."
        ),
    )
    parser.add_argument(
        "--allow-smaller",
        action="store_true",
        help=(
            "Use the best positive-capacity candidate when no candidate "
            "reaches a requested setting."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help=(
            "Resolve and save queries without launching the algorithms."
        ),
    )
    parser.add_argument(
        "--reuse-preflight-from",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Reuse resolved_queries artifacts from a prior preflight root. "
            "Repeat this option to search multiple roots. Missing or stale "
            "dataset/setting cells are recomputed."
        ),
    )
    parser.add_argument(
        "--output-root",
        default="rsp_experiment_results",
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        help="Repeat completed rollouts.",
    )
    parser.add_argument(
        "--reset-output",
        action="store_true",
        help="Delete prior output before starting.",
    )

    args = parser.parse_args()
    if args.max_candidate_survivors < 1:
        parser.error("--max-candidate-survivors must be at least 1")

    config_path = Path(args.config).expanduser().resolve()
    config = json.loads(
        config_path.read_text(encoding="utf-8")
    )

    algorithm_script = (
        Path(args.algorithm_script)
        .expanduser()
        .resolve()
    )

    if not algorithm_script.exists():
        parser.error(
            f"Algorithm script not found: {algorithm_script}"
        )

    instance_root = Path(args.instance_root).expanduser()
    available = list_dataset_instances(instance_root)

    if args.datasets:
        datasets = [
            normalize_dataset_name(item)
            for item in parse_csv_list(args.datasets)
        ]
    else:
        datasets = [
            name
            for name, item in config["datasets"].items()
            if item.get("run_by_default", True)
        ]

    missing = [
        name
        for name in datasets
        if name not in available
    ]

    if missing:
        parser.error(
            "Missing Neo4j instances under "
            f"{instance_root}: {', '.join(missing)}"
        )

    settings = (
        parse_csv_list(args.settings)
        or list(config["targets"])
    )

    for setting in settings:
        if setting not in config["targets"]:
            parser.error(f"Unknown setting: {setting}")

    algorithms = parse_csv_list(args.algorithms)
    seeds = [
        int(value)
        for value in parse_csv_list(args.seeds)
    ] or [42]

    password = args.password or getpass(
        "Neo4j password: "
    )

    output_root = (
        Path(args.output_root)
        .expanduser()
        .resolve()
    )

    if args.reset_output and output_root.exists():
        import shutil
        shutil.rmtree(output_root)

    output_root.mkdir(parents=True, exist_ok=True)

    reuse_preflight_roots = [
        Path(value).expanduser().resolve()
        for value in args.reuse_preflight_from
    ]

    results_path = output_root / "results.jsonl"
    preflight_jsonl = output_root / "preflight.jsonl"
    preflight_csv = output_root / "preflight.csv"

    if preflight_jsonl.exists():
        preflight_jsonl.unlink()

    existing_keys = (
        set()
        if args.rerun
        else load_existing_keys(results_path)
    )

    timeout_seconds = max(
        1,
        int(args.timeout_minutes * 60),
    )

    env = os.environ.copy()
    env.update(
        {
            "NEO4J_PASSWORD": password,
            "NEO4J_USER": args.user,
            "NEO4J_URI": args.uri,
            "NEO4J_DATABASE": args.database,
            "NEO4J_INSTANCE_ROOT": str(instance_root),
        }
    )

    preflight_rows: list[dict[str, Any]] = []
    resolved_all: dict[
        str,
        dict[str, dict[str, Any]],
    ] = {}

    for dataset in datasets:
        print(f"\n=== Dataset: {dataset} ===")

        driver = connect_to_dataset(
            dataset=dataset,
            user=args.user,
            password=password,
            uri=args.uri,
            database=args.database,
            instance_root=instance_root,
            auto_switch=True,
        )

        if driver is None:
            print(f"Skipping {dataset}: connection failed")
            continue

        resolved_by_setting: dict[str, dict[str, Any]] = {}

        try:
            with driver.session(database=args.database) as session:
                for setting in settings:
                    target = config["targets"][setting]
                    target_n = int(target["query_matches"])
                    target_m = int(target["intervention_units"])
                    reused, reused_root, reuse_reason = load_reused_preflight(
                        reuse_preflight_roots,
                        dataset=dataset,
                        setting=setting,
                        target_n=target_n,
                        target_m=target_m,
                        allow_smaller=args.allow_smaller,
                        requested_target=args.target,
                    )
                    if reused is not None:
                        print(
                            f"  {setting}: reused preflight from {reused_root} "
                            f"({reused['name']}, n={reused['query_matches']}, "
                            f"m={reused['intervention_units']})"
                        )
                        resolved_by_setting[setting] = reused
                        row = {
                            "dataset": dataset,
                            "phase": "rsp_setting_preflight_reused",
                            "setting": setting,
                            "candidate": reused["name"],
                            "description": reused.get("description", ""),
                            "candidate_policy": reused.get(
                                "candidate_policy",
                                target.get("candidate_policy", "first-feasible"),
                            ),
                            "rounding_favorable": reused.get(
                                "rounding_favorable", False
                            ),
                            "status": "REUSED_PREFLIGHT",
                            "error": "",
                            "target_query_matches": target_n,
                            "target_intervention_units": target_m,
                            "query_matches": reused["query_matches"],
                            "intervention_units": reused["intervention_units"],
                            "target": reused["target"],
                            "target_index": reused.get("target_index", -1),
                            "selected": True,
                        }
                        preflight_rows.append(row)
                        append_jsonl(preflight_jsonl, row)
                        continue
                    if reuse_preflight_roots and reuse_reason:
                        print(f"  {setting}: saved preflight not reused: {reuse_reason}")
                    print(
                        f"  Preflighting {setting}: "
                        f"n={target_n}, m={target_m}"
                    )

                    resolved, attempts = choose_rsp_setting_candidate(
                        session,
                        candidates_for_dataset(config, dataset),
                        target_n=target_n,
                        target_m=target_m,
                        timeout_seconds=args.preflight_timeout_seconds,
                        allow_smaller=args.allow_smaller,
                        requested_target=args.target,
                        max_candidate_survivors=args.max_candidate_survivors,
                        candidate_policy=target.get(
                            "candidate_policy", "first-feasible"
                        ),
                        min_lp_support=int(target.get("min_lp_support", 0)),
                        min_random_rounding_gap=float(
                            target.get("min_random_rounding_gap", 0.0)
                        ),
                        min_final_random_rounding_gap=float(
                            target.get(
                                "min_final_random_rounding_gap", 0.0
                            )
                        ),
                        min_final_direct_random_rounding_gap=float(
                            target.get(
                                "min_final_direct_random_rounding_gap", 0.0
                            )
                        ),
                        max_rounding_greedy_size_ratio=float(
                            target.get(
                                "max_rounding_greedy_size_ratio",
                                float("inf"),
                            )
                        ),
                        min_greedy_rounding_work_ratio=float(
                            target.get(
                                "min_greedy_rounding_work_ratio", 0.0
                            )
                        ),
                    )

                    selected_name = resolved["name"] if resolved else ""
                    for attempt in attempts:
                        row = {
                            "dataset": dataset,
                            "phase": "rsp_setting_preflight",
                            "setting": setting,
                            "candidate_policy": target.get(
                                "candidate_policy", "first-feasible"
                            ),
                            **attempt,
                            "selected": (
                                attempt["candidate"] == selected_name
                                and attempt.get("status") == "RSP_FEASIBLE"
                            ),
                        }
                        preflight_rows.append(row)
                        append_jsonl(preflight_jsonl, row)

                    if resolved is None:
                        print(
                            f"  {setting}: no capacity-valid, "
                            "survivor-feasible candidate; skipped"
                        )
                        continue

                    q_count = int(resolved["query_matches"])
                    p_count = int(resolved["intervention_units"])
                    exact = q_count == target_n and p_count == target_m
                    print(
                        f"  {setting}: selected {resolved['name']} "
                        f"(n={q_count}, m={p_count}, "
                        f"target={resolved['target_index']})"
                        + ("" if exact else " (smaller)")
                    )
                    resolved_by_setting[setting] = resolved

        finally:
            close_neo4j(driver)
        resolved_all[dataset] = resolved_by_setting

        for setting, resolved in resolved_by_setting.items():
            target = config["targets"][setting]
            query_dir = (
                output_root
                / "resolved_queries"
                / dataset
                / setting
            )
            query_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            query_file = query_dir / "query.cypher"
            pattern_file = query_dir / "pattern.cypher"
            metadata_file = query_dir / "metadata.json"

            query_file.write_text(
                resolved["query"] + "\n",
                encoding="utf-8",
            )
            pattern_file.write_text(
                resolved["pattern"] + "\n",
                encoding="utf-8",
            )
            metadata_file.write_text(
                json.dumps(
                    {
                        "dataset": dataset,
                        "setting": setting,
                        "candidate": resolved["name"],
                        "description": resolved.get(
                            "description",
                            "",
                        ),
                        "candidate_policy": target.get(
                            "candidate_policy", "first-feasible"
                        ),
                        "rounding_favorable": resolved.get(
                            "rounding_favorable", False
                        ),
                        "query_matches": resolved[
                            "query_matches"
                        ],
                        "intervention_units": resolved[
                            "intervention_units"
                        ],
                        "target": resolved["target"],
                        "target_index": resolved.get("target_index", -1),
                        "preflight_lp_objective": resolved.get(
                            "preflight_lp_objective"
                        ),
                        "lp_support_size": resolved.get("lp_support_size"),
                        "lp_max_x": resolved.get("lp_max_x"),
                        "lp_mean_positive_x": resolved.get(
                            "lp_mean_positive_x"
                        ),
                        "rounding_lambda": resolved.get("rounding_lambda"),
                        "expected_rounding_selection": resolved.get(
                            "expected_rounding_selection"
                        ),
                        "expected_lp_random_selection": resolved.get(
                            "expected_lp_random_selection"
                        ),
                        "predicted_random_rounding_gap": resolved.get(
                            "predicted_random_rounding_gap"
                        ),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    write_preflight_csv(
        preflight_csv,
        preflight_rows,
    )

    if args.preflight_only:
        rewrite_csv_and_aggregates(
            results_path,
            output_root,
        )
        print(
            "\nPreflight complete. No algorithms were run."
        )
        print(f"Preflight CSV: {preflight_csv}")
        print(
            "Resolved queries: "
            f"{output_root / 'resolved_queries'}"
        )
        return 0

    run_number = 0

    for dataset, resolved_by_setting in resolved_all.items():
        if not resolved_by_setting:
            continue

        # Switch once, then all algorithm subprocesses use --no-switch.
        driver = connect_to_dataset(
            dataset=dataset,
            user=args.user,
            password=password,
            uri=args.uri,
            database=args.database,
            instance_root=instance_root,
            auto_switch=True,
        )

        if driver is None:
            print(
                f"Skipping rollouts for {dataset}: "
                "could not reactivate dataset"
            )
            continue

        close_neo4j(driver)

        for setting, resolved in resolved_by_setting.items():
            target = config["targets"][setting]
            query_dir = (
                output_root
                / "resolved_queries"
                / dataset
                / setting
            )
            query_file = query_dir / "query.cypher"
            pattern_file = query_dir / "pattern.cypher"

            for algorithm in algorithms:
                rollout_seeds = (
                    [seeds[0]]
                    if algorithm in {"greedy", "direct-greedy"}
                    else seeds
                )

                for seed in rollout_seeds:
                    target_label = "target-" + hashlib.sha256(
                        resolved["target"].encode("utf-8")
                    ).hexdigest()[:12]
                    key = (
                        dataset,
                        setting,
                        resolved["name"],
                        algorithm,
                        seed,
                        resolved["target"],
                        resolved["query_matches"],
                        resolved["intervention_units"],
                    )

                    if key in existing_keys:
                        print(
                            "  skip completed: "
                            f"{dataset}/{setting}/"
                            f"{algorithm}/seed-{seed}"
                        )
                        continue

                    run_number += 1
                    print(
                        f"  run {run_number}: "
                        f"{dataset}/{setting}/"
                        f"{algorithm}/seed-{seed} "
                        f"(timeout={args.timeout_minutes:g} min)"
                    )

                    rollout_dir = (
                        output_root
                        / "rollouts"
                        / dataset
                        / setting
                        / target_label
                        / algorithm
                        / f"seed-{seed}"
                    )

                    stdout_path = (
                        rollout_dir / "stdout.log"
                    )
                    algorithm_log_root = (
                        rollout_dir / "algorithm_logs"
                    )

                    command = [
                        sys.executable,
                        str(algorithm_script),
                        resolved["query"],
                        resolved["pattern"],
                        "--dataset",
                        dataset,
                        "--no-switch",
                        "--instance-root",
                        str(instance_root),
                        "--uri",
                        args.uri,
                        "--database",
                        args.database,
                        "--user",
                        args.user,
                        "--approximation",
                        algorithm,
                        "--seed",
                        str(seed),
                        "--no-details",
                        "--log-root",
                        str(algorithm_log_root),
                    ]

                    command.extend(["--target", resolved["target"]])

                    rollout = run_rollout(
                        command,
                        timeout_seconds=timeout_seconds,
                        stdout_path=stdout_path,
                        env=env,
                    )

                    row = {
                        "dataset": dataset,
                        "setting": setting,
                        "candidate": resolved["name"],
                        "candidate_policy": target.get(
                            "candidate_policy", "first-feasible"
                        ),
                        "rounding_favorable": resolved.get(
                            "rounding_favorable", False
                        ),
                        "preflight_lp_objective": resolved.get(
                            "preflight_lp_objective"
                        ),
                        "lp_support_size": resolved.get("lp_support_size"),
                        "lp_max_x": resolved.get("lp_max_x"),
                        "lp_mean_positive_x": resolved.get(
                            "lp_mean_positive_x"
                        ),
                        "rounding_lambda": resolved.get("rounding_lambda"),
                        "expected_rounding_selection": resolved.get(
                            "expected_rounding_selection"
                        ),
                        "expected_lp_random_selection": resolved.get(
                            "expected_lp_random_selection"
                        ),
                        "predicted_random_rounding_gap": resolved.get(
                            "predicted_random_rounding_gap"
                        ),
                        "target_query_matches": target[
                            "query_matches"
                        ],
                        "target_intervention_units": target[
                            "intervention_units"
                        ],
                        "actual_query_matches": resolved[
                            "query_matches"
                        ],
                        "actual_intervention_units": resolved[
                            "intervention_units"
                        ],
                        "algorithm": algorithm,
                        "seed": seed,
                        "requested_target": resolved["target"],
                        "query_file": str(query_file),
                        "pattern_file": str(pattern_file),
                        **rollout,
                    }

                    append_jsonl(results_path, row)
                    existing_keys.add(key)

                    print(
                        f"    -> {row['outcome']} "
                        f"wall={row['wall_time']:.1f}s "
                        f"contingency="
                        f"{row.get('contingency_size', '')} "
                        f"responsibility="
                        f"{row.get('responsibility', '')}"
                    )

                    rewrite_csv_and_aggregates(
                        results_path,
                        output_root,
                    )

    rewrite_csv_and_aggregates(
        results_path,
        output_root,
    )

    print(f"\nResults saved under {output_root}")
    print(f"Preflight: {preflight_csv}")
    print(f"Raw records: {results_path}")
    print(f"Summary: {output_root / 'summary.csv'}")
    print(f"Timeouts: {output_root / 'timeouts.csv'}")
    print(
        "Aggregates: "
        f"{output_root / 'aggregate_results.csv'}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
