#!/usr/bin/env python3
"""Run P-RES on query/intervention artifacts resolved by the P-RSP workflow."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from getpass import getpass
from pathlib import Path
from typing import Any

from utils import (
    close_neo4j,
    connect_to_dataset,
    list_dataset_instances,
    normalize_dataset_name,
)


METRIC_PATTERNS = {
    "query_matches": re.compile(r"^Query matches:\s*(\d+)\s*$", re.MULTILINE),
    "intervention_units": re.compile(
        r"^Intervention units:\s*(\d+)\s*$", re.MULTILINE
    ),
    "status": re.compile(r"^Status:\s*(.+?)\s*$", re.MULTILINE),
    "objective_value": re.compile(
        r"^Objective Value \(Number of Pattern Units Selected\):\s*(-?\d+)\s*$",
        re.MULTILINE,
    ),
    "query_resilience": re.compile(
        r"^Query resilience:\s*(-?\d+(?:\.\d+)?)\s*$", re.MULTILINE
    ),
    "algorithm_time": re.compile(
        r"^Execution time:\s*([\d.]+)\s+seconds\s*$", re.MULTILINE
    ),
    "hitting_set_time": re.compile(
        r"^\+ Match hitting-set computation time:\s*([\d.]+)\s+seconds\s*$",
        re.MULTILINE,
    ),
    "reported_total_time": re.compile(
        r"^Total execution time:\s*([\d.]+)\s+seconds\s*$", re.MULTILINE
    ),
}


def parse_csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_shared_query(
    roots: list[Path],
    *,
    dataset: str,
    setting: str,
    target_n: int,
    target_m: int,
    allow_smaller: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load one exact RSP-resolved query/pattern pair without re-preflighting."""
    last_reason = None
    for root in roots:
        resolved_root = root if root.name == "resolved_queries" else root / "resolved_queries"
        query_dir = resolved_root / dataset / setting
        query_file = query_dir / "query.cypher"
        pattern_file = query_dir / "pattern.cypher"
        metadata_file = query_dir / "metadata.json"
        if not all(path.is_file() for path in (query_file, pattern_file, metadata_file)):
            continue

        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            query = query_file.read_text(encoding="utf-8").strip()
            pattern = pattern_file.read_text(encoding="utf-8").strip()
            query_matches = int(metadata["query_matches"])
            intervention_units = int(metadata["intervention_units"])
        except Exception as exc:
            last_reason = f"invalid resolved artifact: {type(exc).__name__}: {exc}"
            continue

        if metadata.get("dataset") != dataset or metadata.get("setting") != setting:
            last_reason = "resolved artifact has the wrong dataset or setting"
            continue
        exact = query_matches == target_n and intervention_units == target_m
        if not exact and not (
            allow_smaller
            and 0 < query_matches <= target_n
            and 0 < intervention_units <= target_m
        ):
            last_reason = (
                f"resolved counts n={query_matches},m={intervention_units} do not "
                f"match requested n={target_n},m={target_m}"
            )
            continue
        if not query or not pattern:
            last_reason = "resolved query or intervention pattern is empty"
            continue

        return {
            **metadata,
            "candidate": metadata.get("candidate", "shared_rsp_candidate"),
            "query": query,
            "pattern": pattern,
            "query_matches": query_matches,
            "intervention_units": intervention_units,
            "source_root": str(root),
        }, None

    return None, last_reason or "no resolved artifact was found"


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate a rollout and its solver children."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def parse_metrics(output: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, pattern in METRIC_PATTERNS.items():
        match = pattern.search(output)
        if match is None:
            parsed[key] = None
            continue
        raw = match.group(1).strip()
        if key in {"query_matches", "intervention_units", "objective_value"}:
            parsed[key] = int(raw)
        elif key in {
            "query_resilience",
            "algorithm_time",
            "hitting_set_time",
            "reported_total_time",
        }:
            parsed[key] = float(raw)
        else:
            parsed[key] = raw
    return parsed


def run_rollout(
    command: list[str],
    *,
    timeout_seconds: float,
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
        encoding="utf-8",
        errors="replace",
        env=env,
        start_new_session=True,
    )
    outcome = "FAILED"
    exit_code = None
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
        exit_code = process.returncode
        outcome = "SUCCESS" if exit_code == 0 else "FAILED"
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        terminate_process_group(process)
        remaining, _ = process.communicate()
        output = partial + (remaining or "")
        exit_code = process.returncode
        outcome = "TIMEOUT"

    wall_time = time.perf_counter() - started
    stdout_path.write_text(output, encoding="utf-8")
    return {
        "outcome": outcome,
        "exit_code": exit_code,
        "wall_time": wall_time,
        "stdout_file": str(stdout_path),
        **parse_metrics(output),
    }


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def load_existing_keys(path: Path) -> set[tuple[Any, ...]]:
    keys: set[tuple[Any, ...]] = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
                row.get("actual_query_matches"),
                row.get("actual_intervention_units"),
            )
        )
    return keys


def mean_or_blank(values: list[float]) -> float | str:
    return statistics.mean(values) if values else ""


def stdev_or_zero(values: list[float]) -> float | str:
    if not values:
        return ""
    return statistics.stdev(values) if len(values) > 1 else 0.0


def write_reports(results_path: Path, output_root: Path) -> None:
    rows: list[dict[str, Any]] = []
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    fields = [
        "dataset", "setting", "candidate", "source_root",
        "target_query_matches", "target_intervention_units",
        "actual_query_matches", "actual_intervention_units",
        "algorithm", "seed", "outcome", "exit_code", "status",
        "objective_value", "query_resilience", "algorithm_time",
        "hitting_set_time", "reported_total_time", "wall_time",
        "stdout_file", "query_file", "pattern_file",
    ]
    for filename, selected_rows in (
        ("summary.csv", rows),
        ("timeouts.csv", [row for row in rows if row.get("outcome") == "TIMEOUT"]),
    ):
        with (output_root / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(selected_rows)

    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            row.get("dataset"), row.get("setting"), row.get("candidate"),
            row.get("algorithm"), row.get("actual_query_matches"),
            row.get("actual_intervention_units"),
        )
        grouped.setdefault(key, []).append(row)

    aggregate_fields = [
        "dataset", "setting", "candidate", "algorithm",
        "actual_query_matches", "actual_intervention_units", "runs",
        "successes", "timeouts", "failures", "mean_objective_value",
        "std_objective_value", "mean_query_resilience",
        "std_query_resilience", "median_wall_time",
        "mean_reported_total_time",
    ]
    aggregate_rows = []
    for key, group in sorted(grouped.items()):
        successes = [row for row in group if row.get("outcome") == "SUCCESS"]
        objectives = [
            float(row["objective_value"])
            for row in successes if row.get("objective_value") is not None
        ]
        resilience = [
            float(row["query_resilience"])
            for row in successes if row.get("query_resilience") is not None
        ]
        wall = [
            float(row["wall_time"])
            for row in successes if row.get("wall_time") is not None
        ]
        reported = [
            float(row["reported_total_time"])
            for row in successes if row.get("reported_total_time") is not None
        ]
        aggregate_rows.append({
            "dataset": key[0], "setting": key[1], "candidate": key[2],
            "algorithm": key[3], "actual_query_matches": key[4],
            "actual_intervention_units": key[5], "runs": len(group),
            "successes": len(successes),
            "timeouts": sum(row.get("outcome") == "TIMEOUT" for row in group),
            "failures": sum(row.get("outcome") == "FAILED" for row in group),
            "mean_objective_value": mean_or_blank(objectives),
            "std_objective_value": stdev_or_zero(objectives),
            "mean_query_resilience": mean_or_blank(resilience),
            "std_query_resilience": stdev_or_zero(resilience),
            "median_wall_time": statistics.median(wall) if wall else "",
            "mean_reported_total_time": mean_or_blank(reported),
        })
    with (output_root / "aggregate_results.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=aggregate_fields)
        writer.writeheader()
        writer.writerows(aggregate_rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run P-RES with the exact query/intervention artifacts selected "
            "by the P-RSP experiment preflight."
        )
    )
    parser.add_argument("--config", default="experiment_settings.json")
    parser.add_argument("--algorithm-script", default="RES-RD.py")
    parser.add_argument(
        "--resolved-query-root", action="append", required=True, metavar="PATH",
        help=(
            "RSP preflight/result root containing resolved_queries. Repeat to "
            "combine separately generated small and large preflight roots."
        ),
    )
    parser.add_argument("--datasets")
    parser.add_argument("--settings")
    parser.add_argument("--algorithms", default="rounding,greedy,random")
    parser.add_argument("--seeds", default="42")
    parser.add_argument("--timeout-minutes", type=float, default=30.0)
    parser.add_argument("--allow-smaller", action="store_true")
    parser.add_argument("--output-root", default="pres_experiment_results")
    parser.add_argument("--rerun", action="store_true")
    parser.add_argument("--reset-output", action="store_true")
    parser.add_argument(
        "--instance-root", default=os.getenv("NEO4J_INSTANCE_ROOT", "~/neo4j-instances")
    )
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD"))
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    algorithm_script = Path(args.algorithm_script).expanduser().resolve()
    if not algorithm_script.is_file():
        parser.error(f"P-RES algorithm script not found: {algorithm_script}")

    roots = [Path(value).expanduser().resolve() for value in args.resolved_query_root]
    datasets = parse_csv_list(args.datasets) or [
        name for name, details in config["datasets"].items()
        if details.get("run_by_default", False)
    ]
    datasets = [normalize_dataset_name(name) for name in datasets]
    unknown_datasets = [name for name in datasets if name not in config["datasets"]]
    if unknown_datasets:
        parser.error("Unknown dataset(s): " + ", ".join(unknown_datasets))

    settings = parse_csv_list(args.settings) or list(config["targets"])
    unknown_settings = [name for name in settings if name not in config["targets"]]
    if unknown_settings:
        parser.error("Unknown setting(s): " + ", ".join(unknown_settings))

    algorithms = parse_csv_list(args.algorithms)
    invalid_algorithms = [
        name for name in algorithms if name not in {"rounding", "greedy", "random"}
    ]
    if invalid_algorithms:
        parser.error("Unknown P-RES algorithm(s): " + ", ".join(invalid_algorithms))
    seeds = [int(value) for value in parse_csv_list(args.seeds)]
    if not seeds:
        parser.error("--seeds must contain at least one integer")
    if args.timeout_minutes <= 0:
        parser.error("--timeout-minutes must be positive")

    instance_root = Path(args.instance_root).expanduser()
    available = list_dataset_instances(instance_root)
    missing = [name for name in datasets if name not in available]
    if missing:
        parser.error(
            "Neo4j instance directories are missing for: " + ", ".join(missing)
        )

    output_root = Path(args.output_root).expanduser().resolve()
    if args.reset_output and output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    results_path = output_root / "results.jsonl"
    existing_keys = set() if args.rerun else load_existing_keys(results_path)
    password = args.password or getpass("Neo4j password: ")
    env = {
        **os.environ,
        "NEO4J_PASSWORD": password,
        "NEO4J_USER": args.user,
        "NEO4J_URI": args.uri,
        "NEO4J_DATABASE": args.database,
        "NEO4J_INSTANCE_ROOT": str(instance_root),
    }

    resolved_all: dict[str, dict[str, dict[str, Any]]] = {}
    for dataset in datasets:
        resolved_all[dataset] = {}
        for setting in settings:
            target = config["targets"][setting]
            resolved, reason = load_shared_query(
                roots,
                dataset=dataset,
                setting=setting,
                target_n=int(target["query_matches"]),
                target_m=int(target["intervention_units"]),
                allow_smaller=args.allow_smaller,
            )
            if resolved is None:
                print(f"{dataset}/{setting}: skipped ({reason})")
                continue
            resolved_all[dataset][setting] = resolved
            destination = output_root / "resolved_queries" / dataset / setting
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "query.cypher").write_text(
                resolved["query"] + "\n", encoding="utf-8"
            )
            (destination / "pattern.cypher").write_text(
                resolved["pattern"] + "\n", encoding="utf-8"
            )
            (destination / "metadata.json").write_text(
                json.dumps(
                    {
                        **{
                            key: value for key, value in resolved.items()
                            if key not in {"query", "pattern"}
                        },
                        "problem": "P-RES",
                        "shared_with": "P-RSP",
                    },
                    indent=2,
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )
            print(
                f"{dataset}/{setting}: loaded {resolved['candidate']} "
                f"(n={resolved['query_matches']}, m={resolved['intervention_units']})"
            )

    timeout_seconds = args.timeout_minutes * 60.0
    run_number = 0
    for dataset, by_setting in resolved_all.items():
        if not by_setting:
            continue
        driver = connect_to_dataset(
            dataset=dataset, user=args.user, password=password, uri=args.uri,
            database=args.database, instance_root=instance_root, auto_switch=True,
        )
        if driver is None:
            print(f"{dataset}: skipped because Neo4j could not be activated")
            continue
        close_neo4j(driver)

        for setting, resolved in by_setting.items():
            target = config["targets"][setting]
            query_dir = output_root / "resolved_queries" / dataset / setting
            for algorithm in algorithms:
                rollout_seeds = [seeds[0]] if algorithm == "greedy" else seeds
                for seed in rollout_seeds:
                    key = (
                        dataset, setting, resolved["candidate"], algorithm, seed,
                        resolved["query_matches"], resolved["intervention_units"],
                    )
                    if key in existing_keys:
                        print(f"skip completed: {dataset}/{setting}/{algorithm}/seed-{seed}")
                        continue
                    run_number += 1
                    print(
                        f"run {run_number}: {dataset}/{setting}/{algorithm}/seed-{seed}"
                    )
                    rollout_dir = (
                        output_root / "rollouts" / dataset / setting
                        / resolved["candidate"] / algorithm / f"seed-{seed}"
                    )
                    command = [
                        sys.executable, str(algorithm_script), resolved["query"],
                        resolved["pattern"], "--dataset", dataset, "--no-switch",
                        "--instance-root", str(instance_root), "--uri", args.uri,
                        "--database", args.database, "--user", args.user,
                        "--approximation", algorithm, "--seed", str(seed),
                        "--no-details", "--log-root",
                        str(rollout_dir / "algorithm_logs"),
                    ]
                    rollout = run_rollout(
                        command,
                        timeout_seconds=timeout_seconds,
                        stdout_path=rollout_dir / "stdout.log",
                        env=env,
                    )
                    row = {
                        "dataset": dataset,
                        "setting": setting,
                        "candidate": resolved["candidate"],
                        "source_root": resolved["source_root"],
                        "target_query_matches": target["query_matches"],
                        "target_intervention_units": target["intervention_units"],
                        "actual_query_matches": resolved["query_matches"],
                        "actual_intervention_units": resolved["intervention_units"],
                        "algorithm": algorithm,
                        "seed": seed,
                        "query_file": str(query_dir / "query.cypher"),
                        "pattern_file": str(query_dir / "pattern.cypher"),
                        **rollout,
                    }
                    append_jsonl(results_path, row)
                    existing_keys.add(key)
                    write_reports(results_path, output_root)

    write_reports(results_path, output_root)
    print(f"Results: {output_root / 'summary.csv'}")
    print(f"Aggregates: {output_root / 'aggregate_results.csv'}")
    print(f"Timeouts: {output_root / 'timeouts.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
