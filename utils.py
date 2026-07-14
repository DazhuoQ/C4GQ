"""
Utilities for running the algorithms against multiple local Neo4j datasets.

Expected VM layout
------------------
Each dataset has an independent Neo4j installation under::

    ~/neo4j-instances/<dataset>/

Only one instance normally runs at a time, and every instance exposes the same
connection endpoint (by default ``bolt://localhost:7687``).

Typical usage
-------------
Switch and connect in one call::

    driver = connect_to_dataset(
        dataset="star-wars",
        user="neo4j",
        password=password,
    )

Keep the currently active instance and verify it before connecting::

    driver = connect_to_dataset(
        dataset="ilpg",
        user="neo4j",
        password=password,
        auto_switch=False,
    )

The original ``connect_to_neo4j(uri, user, password)`` and
``run_query(driver, query)`` calls remain supported.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

from neo4j import GraphDatabase
from neo4j.graph import Node, Relationship


DEFAULT_NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
DEFAULT_NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
DEFAULT_INSTANCE_ROOT = Path(
    os.getenv("NEO4J_INSTANCE_ROOT", "~/neo4j-instances")
).expanduser()

# Canonical names correspond to directory names under ~/neo4j-instances.
DATASET_ALIASES = {
    "ilpg": "ilpg",
    "wwc": "wwc2019",
    "wwc2019": "wwc2019",
    "world-cup": "wwc2019",
    "worldcup": "wwc2019",
    "stackoverflow": "stackoverflow",
    "stack-overflow": "stackoverflow",
    "so": "stackoverflow",
    "fincen": "fincen",
    "icij": "icij-offshoreleaks",
    "offshoreleaks": "icij-offshoreleaks",
    "offshore-leaks": "icij-offshoreleaks",
    "icij-offshoreleaks": "icij-offshoreleaks",
    "starwars": "star-wars",
    "star-wars": "star-wars",
}


def normalize_dataset_name(dataset: str) -> str:
    """Return the canonical instance-directory name for a dataset."""
    normalized = dataset.strip().lower().replace("_", "-")
    return DATASET_ALIASES.get(normalized, normalized)


def list_dataset_instances(
    instance_root: Path | str = DEFAULT_INSTANCE_ROOT,
) -> dict[str, Path]:
    """
    List valid Neo4j dataset instances.

    A directory is considered an instance when ``bin/neo4j`` exists and is
    executable.
    """
    root = Path(instance_root).expanduser()
    if not root.is_dir():
        return {}

    instances: dict[str, Path] = {}
    for directory in sorted(root.iterdir()):
        neo4j_command = directory / "bin" / "neo4j"
        if directory.is_dir() and neo4j_command.is_file():
            instances[directory.name] = directory
    return instances


def _run_neo4j_command(
    neo4j_home: Path,
    command: str,
    *,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``bin/neo4j <command>`` without invoking a shell."""
    executable = neo4j_home / "bin" / "neo4j"
    if not executable.is_file():
        raise FileNotFoundError(f"Neo4j command not found: {executable}")

    return subprocess.run(
        [str(executable), command],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def is_instance_running(
    neo4j_home: Path | str,
    *,
    timeout: float = 15.0,
) -> bool:
    """Return whether a particular Neo4j instance is running."""
    result = _run_neo4j_command(
        Path(neo4j_home).expanduser(),
        "status",
        timeout=timeout,
    )
    return result.returncode == 0


def get_active_datasets(
    instance_root: Path | str = DEFAULT_INSTANCE_ROOT,
) -> list[str]:
    """
    Return all dataset instances that report themselves as running.

    Under the recommended setup this list should contain zero or one item.
    """
    active: list[str] = []
    for name, path in list_dataset_instances(instance_root).items():
        try:
            if is_instance_running(path):
                active.append(name)
        except (OSError, subprocess.SubprocessError):
            # A broken instance should not prevent checking the others.
            continue
    return active


def get_active_dataset(
    instance_root: Path | str = DEFAULT_INSTANCE_ROOT,
) -> Optional[str]:
    """
    Return the canonical active dataset name.

    Returns ``None`` when no managed instance is running and raises an error
    when more than one instance reports itself as running.
    """
    active_directories = get_active_datasets(instance_root)
    if not active_directories:
        return None
    if len(active_directories) > 1:
        raise RuntimeError(
            "Multiple Neo4j instances are running: "
            + ", ".join(active_directories)
        )
    return normalize_dataset_name(active_directories[0])


def _bolt_host_and_port(uri: str) -> tuple[str, int]:
    """Extract the host and port used for a Neo4j Bolt connection."""
    parsed = urlparse(uri)
    if not parsed.hostname:
        raise ValueError(f"Invalid Neo4j URI: {uri!r}")

    return parsed.hostname, parsed.port or 7687


def _wait_for_bolt(
    uri: str,
    *,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
) -> None:
    """Wait until the Bolt TCP port accepts connections."""
    host, port = _bolt_host_and_port(uri)
    deadline = time.monotonic() + timeout
    last_error: Optional[OSError] = None

    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(poll_interval)

    detail = f": {last_error}" if last_error else ""
    raise TimeoutError(
        f"Neo4j did not become available at {host}:{port} "
        f"within {timeout:.0f} seconds{detail}"
    )


def switch_neo4j_dataset(
    dataset: str,
    *,
    instance_root: Path | str = DEFAULT_INSTANCE_ROOT,
    uri: str = DEFAULT_NEO4J_URI,
    wait_timeout: float = 60.0,
) -> Path:
    """
    Stop other managed instances and start the requested dataset instance.

    This function only manages instances inside ``instance_root``. If another
    unmanaged Neo4j process occupies ports 7474 or 7687, startup can still fail.

    Returns:
        Path to the active dataset's Neo4j home directory.
    """
    canonical_name = normalize_dataset_name(dataset)
    instances = list_dataset_instances(instance_root)

    matching_instances = [
        (directory_name, path)
        for directory_name, path in instances.items()
        if normalize_dataset_name(directory_name) == canonical_name
    ]

    if not matching_instances:
        available = ", ".join(instances) if instances else "(none)"
        raise ValueError(
            f"Unknown Neo4j dataset {dataset!r}. "
            f"Expected an instance under "
            f"{Path(instance_root).expanduser()}. "
            f"Available datasets: {available}"
        )

    if len(matching_instances) > 1:
        duplicates = ", ".join(name for name, _ in matching_instances)
        raise RuntimeError(
            f"Multiple instance directories represent {canonical_name!r}: "
            f"{duplicates}. Keep only one of them."
        )

    target_directory_name, target = matching_instances[0]

    # Stop every other managed instance first.
    for name, path in instances.items():
        if name == target_directory_name:
            continue
        try:
            if is_instance_running(path):
                print(f"Stopping Neo4j dataset: {name}")
                result = _run_neo4j_command(path, "stop")
                if result.returncode != 0:
                    message = (result.stderr or result.stdout).strip()
                    raise RuntimeError(
                        f"Failed to stop dataset {name!r}: {message}"
                    )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Timed out while stopping dataset {name!r}"
            ) from exc

    if is_instance_running(target):
        print(f"✓ Neo4j dataset already active: {canonical_name}")
        return target

    print(f"Starting Neo4j dataset: {canonical_name}")
    result = _run_neo4j_command(target, "start")
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Failed to start dataset {canonical_name!r}: {message}\n"
            f"Check: {target / 'logs' / 'neo4j.log'}"
        )

    try:
        _wait_for_bolt(uri, timeout=wait_timeout)
    except Exception:
        log_path = target / "logs" / "neo4j.log"
        raise RuntimeError(
            f"Dataset {canonical_name!r} was started, but Bolt did not "
            f"become available. Check {log_path}"
        ) from None

    print(f"✓ Active Neo4j dataset: {canonical_name}")
    return target


def verify_active_dataset(
    expected_dataset: str,
    *,
    instance_root: Path | str = DEFAULT_INSTANCE_ROOT,
) -> bool:
    """
    Verify that the expected managed dataset is the only active instance.

    This checks local instance processes rather than modifying the graph with a
    metadata node.
    """
    expected = normalize_dataset_name(expected_dataset)
    active_directories = get_active_datasets(instance_root)
    active = [normalize_dataset_name(name) for name in active_directories]

    if active == [expected]:
        return True

    if not active:
        raise RuntimeError(
            f"No managed Neo4j dataset is running. Expected {expected!r}. "
            f"Run `neo4j-switch {expected}` or use "
            f"`connect_to_dataset(..., auto_switch=True)`."
        )

    if expected not in active:
        raise RuntimeError(
            f"Wrong Neo4j dataset is active. Expected {expected!r}, "
            f"but found: {', '.join(active_directories)}."
        )

    raise RuntimeError(
        "Multiple Neo4j dataset instances report themselves as running: "
        f"{', '.join(active_directories)}. "
        "Stop the extra instances before continuing."
    )


def connect_to_neo4j(
    uri: str,
    user: str,
    password: str,
    database: str = DEFAULT_NEO4J_DATABASE,
):
    """
    Connect to Neo4j using the provided URI, username, and password.

    This keeps the original three-argument API. The optional ``database``
    argument is used when testing the connection.

    Returns:
        A Neo4j driver, or ``None`` when the connection fails.
    """
    driver = None
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()

        # Also verify that the selected logical database can serve a query.
        with driver.session(database=database) as session:
            session.run("RETURN 1 AS test").consume()

        print("✓ Successfully connected to Neo4j!")
        print(f"  URI: {uri}")
        print(f"  Database: {database}")
        return driver
    except Exception as exc:
        if driver is not None:
            driver.close()
        print(f"✗ FAILED to connect to Neo4j: {exc}")
        print(f"  URI: {uri}")
        print(f"  User: {user}")
        print(f"  Database: {database}")
        return None


def connect_to_dataset(
    dataset: str,
    user: str,
    password: str,
    *,
    uri: str = DEFAULT_NEO4J_URI,
    database: str = DEFAULT_NEO4J_DATABASE,
    instance_root: Path | str = DEFAULT_INSTANCE_ROOT,
    auto_switch: bool = True,
    wait_timeout: float = 60.0,
):
    """
    Connect to one of the isolated dataset instances.

    Args:
        dataset: Dataset name or alias, for example ``"star-wars"`` or
            ``"icij"``.
        user: Neo4j username.
        password: Neo4j password.
        uri: Shared Bolt URI used by the local instances.
        database: Logical Neo4j database name, normally ``"neo4j"``.
        instance_root: Directory containing all dataset instances.
        auto_switch: When true, stop other managed instances and start the
            requested one. When false, only verify the active instance.
        wait_timeout: Maximum time to wait for Bolt after starting Neo4j.

    Returns:
        A Neo4j driver, or ``None`` when connection fails.
    """
    canonical_name = normalize_dataset_name(dataset)

    try:
        if auto_switch:
            switch_neo4j_dataset(
                canonical_name,
                instance_root=instance_root,
                uri=uri,
                wait_timeout=wait_timeout,
            )
        else:
            verify_active_dataset(
                canonical_name,
                instance_root=instance_root,
            )
    except Exception as exc:
        print(f"✗ FAILED to activate Neo4j dataset: {exc}")
        return None

    driver = connect_to_neo4j(
        uri=uri,
        user=user,
        password=password,
        database=database,
    )
    if driver is not None:
        print(f"  Dataset: {canonical_name}")
    return driver


def close_neo4j(driver) -> None:
    """Close a Neo4j driver when it is not ``None``."""
    if driver is not None:
        driver.close()


def run_query(
    driver,
    query: str,
    parameters: Optional[Mapping[str, Any]] = None,
    database: str = DEFAULT_NEO4J_DATABASE,
):
    """
    Run a Cypher query and return its records as dictionaries.

    The original ``run_query(driver, query)`` call remains valid.

    Args:
        driver: Neo4j driver.
        query: Cypher query.
        parameters: Optional Cypher parameters.
        database: Logical database name, normally ``"neo4j"``.
    """
    if driver is None:
        print("✗ Query failed")
        print("  Error: Neo4j driver is None")
        return None

    try:
        with driver.session(database=database) as session:
            result = session.run(query, parameters or {})
            matches = [dict(record) for record in result]
        print("✓ Query executed successfully")
        return matches
    except Exception as exc:
        print("✗ Query failed")
        print(f"  Error: {exc}")
        return None


def get_match_elements(matches: Iterable[Mapping[str, Any]]):
    """
    Return the unique nodes and relationships in a list of query records.

    Nested lists, tuples, sets, and dictionaries are traversed recursively,
    which also supports queries that return paths or collected elements.
    """
    nodes: set[Node] = set()
    elements: set[Relationship] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Relationship):
            elements.add(value)
        elif isinstance(value, Node):
            nodes.add(value)
        elif isinstance(value, Mapping):
            for nested_value in value.values():
                collect(nested_value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for nested_value in value:
                collect(nested_value)
        elif hasattr(value, "nodes") and hasattr(value, "relationships"):
            # Supports neo4j.graph.Path without requiring another import.
            for node in value.nodes:
                collect(node)
            for relationship in value.relationships:
                collect(relationship)

    for record in matches:
        for value in record.values():
            collect(value)

    return nodes, elements


# =================================
# For responsibility problems only
# =================================

def choose_target(elements):
    """
    Choose the first relationship as the target element.

    TODO: node targets can also be supported in the future.
    """
    for element in elements:
        if isinstance(element, Relationship):
            return element
    return None


def find_element_from_id(elements, element_id):
    """Return the relationship with the specified Neo4j element ID."""
    for element in elements:
        if (
            isinstance(element, Relationship)
            and element.element_id == element_id
        ):
            return element
    return None


def choose_target_from_pattern_units(patterns):
    """Choose the first pattern unit and return it with its index."""
    if patterns:
        return patterns[0], 0
    return None


def find_pattern_unit_from_edges_ids(
    patterns,
    target_pattern_unit_edges_ids,
):
    """Find a pattern unit whose relationship IDs exactly match the target."""
    target_ids = set(target_pattern_unit_edges_ids)

    for index, pattern in enumerate(patterns):
        edge_ids = {
            edge.element_id
            for edge in pattern.values()
            if isinstance(edge, Relationship)
        }
        if edge_ids == target_ids:
            return pattern, index
    return None
