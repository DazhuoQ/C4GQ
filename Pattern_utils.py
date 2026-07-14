"""Build sparse relationship incidence maps for P-RSP and P-RES."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Iterable

from neo4j.graph import Relationship


def _relationship_ids(record: Mapping[str, Any]) -> list[str]:
    """Return each relationship element ID in a Neo4j record once."""
    ids: list[str] = []
    seen: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Relationship):
            if value.element_id not in seen:
                seen.add(value.element_id)
                ids.append(value.element_id)
        elif isinstance(value, Mapping):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for nested in value:
                collect(nested)
        elif hasattr(value, "relationships"):
            for relationship in value.relationships:
                collect(relationship)

    collect(record)
    return ids


def element_match_mapping(
    elements: Iterable[Relationship],
    matches: Iterable[Mapping[str, Any]],
    pattern_units: Iterable[Mapping[str, Any]],
):
    """Return sparse unit/match edge maps and the inverse unit incidence.

    ``elements`` is retained for compatibility with the original project API.
    The maps use Neo4j relationship ``element_id`` values, which are stable for
    the duration of a database transaction/session but must not be treated as
    portable identifiers across database reloads.
    """
    pattern_unit_edges = {
        index: _relationship_ids(record)
        for index, record in enumerate(pattern_units)
    }
    match_edges = {
        index: _relationship_ids(record)
        for index, record in enumerate(matches)
    }

    patterns_of_edge: defaultdict[str, set[int]] = defaultdict(set)
    for unit, edge_ids in pattern_unit_edges.items():
        for edge_id in edge_ids:
            patterns_of_edge[edge_id].add(unit)

    # Preserve empty entries for known relationships so callers can use .get
    # without distinguishing query-only edges from unknown edges.
    for relationship in elements:
        if isinstance(relationship, Relationship):
            patterns_of_edge[relationship.element_id]

    return pattern_unit_edges, match_edges, dict(patterns_of_edge)


def match_hitting_set(
    match_edges: Mapping[int, Iterable[str]],
    pattern_unit_edges: Mapping[int, Iterable[str]],
) -> dict[int, set[int]]:
    """Map each query match to intervention units sharing a relationship.

    A P-RES intervention unit destroys a query match exactly when their
    relationship sets overlap. Empty sets therefore identify infeasible query
    matches for the supplied intervention pattern.
    """
    units_of_edge: defaultdict[str, set[int]] = defaultdict(set)
    for unit, edge_ids in pattern_unit_edges.items():
        for edge_id in edge_ids:
            units_of_edge[edge_id].add(unit)

    return {
        match: set().union(
            *(units_of_edge.get(edge_id, set()) for edge_id in edge_ids)
        )
        for match, edge_ids in match_edges.items()
    }
