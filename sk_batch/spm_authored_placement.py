"""Read authoritative SpeedTree node placement and export-state evidence.

SpeedTree serializes node positions in library units (one foot per unit).  The
assembly scene uses Blender meters, so positions are converted by exactly
0.3048.  The saved Node table is authoritative for identity, absolute
translation, lineage, and cull/delete/valid state.  It does not serialize an
instance quaternion or uniform scale; consumers must not claim otherwise.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from speedtree_pipeline_contract import read_spm_text


SPEEDTREE_UNIT_TO_METERS = 0.3048
AUTHORED_NODE_MATCH_TOLERANCE_METERS = 5.0e-5
AUTHORED_NODE_GLOBAL_ASSIGNMENT_TOLERANCE_METERS = 1.0e-2
AUTHORED_NODE_AMBIGUITY_EPSILON_METERS = 1.0e-9

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
NODE_POSITION_RE = re.compile(
    rf"_node_\(\s*X:\s*({_NUMBER})\s*,\s*"
    rf"Y:\s*({_NUMBER})\s*,\s*Z:\s*({_NUMBER})\s*\)"
)


class SpmAuthoredPlacementError(RuntimeError):
    """Raised when saved authored placement evidence is partial or ambiguous."""


def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]


def _direct_child(element, name):
    for child in list(element):
        if _local_name(child.tag) == name:
            return child
    return None


def _direct_text(element, name):
    child = _direct_child(element, name)
    return None if child is None else str(child.text or "").strip()


def _descendants(root, name):
    return [element for element in root.iter() if _local_name(element.tag) == name]


def _strict_bool(value, label):
    normalized = str(value or "").strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise SpmAuthoredPlacementError(
        f"authored Node {label} is missing or invalid: {value!r}"
    )


def _parse_anchor(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SpmAuthoredPlacementError(
            f"authored Node m_nAnchorIndex is missing or invalid: {value!r}"
        ) from exc


def _generator_mesh_ids(root):
    result = defaultdict(set)
    for generator in _descendants(root, "Generator"):
        guid = _direct_text(generator, "GUID") or ""
        if not guid:
            continue
        for prop in _descendants(generator, "Property"):
            name = _direct_text(prop, "Name") or ""
            value = _direct_text(prop, "Value") or ""
            if not name.casefold().endswith(":mesh"):
                continue
            try:
                mesh_id = int(value)
            except (TypeError, ValueError):
                continue
            if mesh_id >= 0:
                result[guid].add(mesh_id)
    return {guid: sorted(values) for guid, values in result.items()}


def parse_spm_authored_placement(path):
    """Return a strict, JSON-friendly snapshot of saved authored Node data.

    A truly absent legacy Node table is reported as unavailable.  Once any
    authored placement/state field exists, partial records fail closed rather
    than silently falling back to geometry fitting.
    """
    candidate = Path(path)
    try:
        root = ET.fromstring(read_spm_text(candidate))
    except (OSError, ET.ParseError, ValueError) as exc:
        raise SpmAuthoredPlacementError(
            f"cannot read authored Node table: {candidate}: {exc}"
        ) from exc
    node_elements = _descendants(root, "Node")
    if not node_elements:
        return {
            "status": "legacy_authored_node_data_absent",
            "available": False,
            "spm": str(candidate.resolve(strict=False)),
            "unit_contract": "speedtree_library_feet_to_blender_meters",
            "meters_per_speedtree_unit": SPEEDTREE_UNIT_TO_METERS,
            "nodes": [],
            "generator_mesh_ids": _generator_mesh_ids(root),
        }

    field_presence = False
    for element in node_elements:
        name = _direct_text(element, "Name") or ""
        extra = _direct_child(element, "Extra")
        if NODE_POSITION_RE.search(name) or (
            extra is not None
            and any(
                _direct_child(extra, field) is not None
                for field in (
                    "m_bValidPosition",
                    "m_bDeleted",
                    "m_bCulled",
                    "m_nAnchorIndex",
                )
            )
        ):
            field_presence = True
            break
    if not field_presence:
        return {
            "status": "legacy_authored_node_data_absent",
            "available": False,
            "spm": str(candidate.resolve(strict=False)),
            "unit_contract": "speedtree_library_feet_to_blender_meters",
            "meters_per_speedtree_unit": SPEEDTREE_UNIT_TO_METERS,
            "nodes": [],
            "generator_mesh_ids": _generator_mesh_ids(root),
        }

    records = []
    seen_guids = set()
    for node_index, element in enumerate(node_elements):
        guid = _direct_text(element, "GUID") or ""
        generator_guid = _direct_text(element, "GeneratorGUID") or ""
        parent_guid = _direct_text(element, "ParentGUID") or ""
        name = _direct_text(element, "Name") or ""
        match = NODE_POSITION_RE.search(name)
        extra = _direct_child(element, "Extra")
        if not guid or not generator_guid or match is None or extra is None:
            raise SpmAuthoredPlacementError(
                "authored Node table is partial: "
                f"index={node_index} guid={guid or '<missing>'} "
                f"generator_guid={generator_guid or '<missing>'} "
                f"position_present={match is not None} extra_present={extra is not None}"
            )
        if guid in seen_guids:
            raise SpmAuthoredPlacementError(
                f"authored Node GUID is duplicated: {guid}"
            )
        seen_guids.add(guid)
        position_units = [float(value) for value in match.groups()]
        if any(not math.isfinite(value) for value in position_units):
            raise SpmAuthoredPlacementError(
                f"authored Node position is non-finite: {guid}"
            )
        hidden_text = _direct_text(element, "Hidden")
        hidden = False if hidden_text in {None, ""} else _strict_bool(
            hidden_text, "Hidden"
        )
        valid_position = _strict_bool(
            _direct_text(extra, "m_bValidPosition"), "m_bValidPosition"
        )
        deleted = _strict_bool(
            _direct_text(extra, "m_bDeleted"), "m_bDeleted"
        )
        culled = _strict_bool(
            _direct_text(extra, "m_bCulled"), "m_bCulled"
        )
        anchor_index = _parse_anchor(
            _direct_text(extra, "m_nAnchorIndex")
        )
        excluded_reasons = []
        if hidden:
            excluded_reasons.append("hidden")
        if culled:
            excluded_reasons.append("culled")
        if deleted:
            excluded_reasons.append("deleted")
        if not valid_position:
            excluded_reasons.append("invalid_position")
        records.append({
            "node_index": node_index,
            "node_guid": guid,
            "generator_guid": generator_guid,
            "parent_guid": parent_guid,
            "anchor_index": anchor_index,
            "name": name,
            "position_speedtree_units": position_units,
            "position_meters": [
                value * SPEEDTREE_UNIT_TO_METERS
                for value in position_units
            ],
            "hidden": hidden,
            "valid_position": valid_position,
            "deleted": deleted,
            "culled": culled,
            "active": not excluded_reasons,
            "excluded_reasons": excluded_reasons,
        })

    excluded = Counter(
        reason
        for record in records
        for reason in record["excluded_reasons"]
    )
    return {
        "status": "ready",
        "available": True,
        "spm": str(candidate.resolve(strict=False)),
        "unit_contract": "speedtree_library_feet_to_blender_meters",
        "meters_per_speedtree_unit": SPEEDTREE_UNIT_TO_METERS,
        "node_count": len(records),
        "active_node_count": sum(record["active"] for record in records),
        "excluded_node_count": sum(not record["active"] for record in records),
        "excluded_reason_counts": dict(sorted(excluded.items())),
        "nodes": records,
        "generator_mesh_ids": _generator_mesh_ids(root),
    }


def match_authored_node(
    table,
    target_position_meters,
    target_mesh_id,
    claimed_node_guids=(),
    tolerance_meters=AUTHORED_NODE_MATCH_TOLERANCE_METERS,
):
    """Resolve one produced component to one surviving authored Node.

    Generator mesh support and export state are applied before spatial
    selection.  Equal-distance candidates use GUID ordering and the first
    unclaimed candidate, so XML iteration order never chooses a
    representative.
    """
    if not (table or {}).get("available"):
        raise SpmAuthoredPlacementError(
            "authored Node matching requested without authored Node data"
        )
    try:
        target = [float(value) for value in target_position_meters]
        mesh_id = int(target_mesh_id)
        threshold = float(tolerance_meters)
    except (TypeError, ValueError) as exc:
        raise SpmAuthoredPlacementError(
            "authored Node match target is invalid"
        ) from exc
    if (
        len(target) != 3
        or any(not math.isfinite(value) for value in target)
        or not math.isfinite(threshold)
        or threshold <= 0.0
    ):
        raise SpmAuthoredPlacementError(
            "authored Node match target is invalid"
        )
    mesh_ids_by_generator = table.get("generator_mesh_ids") or {}
    candidates = [
        record
        for record in table.get("nodes") or []
        if record.get("active")
        and mesh_id in set(
            mesh_ids_by_generator.get(record.get("generator_guid"), ())
        )
    ]
    if not candidates:
        raise SpmAuthoredPlacementError(
            "no surviving authored Node candidate supports target mesh: "
            f"target_mesh_id={mesh_id} active_nodes="
            f"{int(table.get('active_node_count') or 0)}"
        )
    ranked = sorted(
        (
            math.dist(target, record["position_meters"]),
            str(record["node_guid"]),
            record,
        )
        for record in candidates
    )
    nearest_distance, nearest_guid, nearest = ranked[0]
    evidence = {
        "target_mesh_id": mesh_id,
        "target_position_meters": target,
        "candidate_count_after_state_and_generator_filter": len(candidates),
        "nearest_distance_meters": nearest_distance,
        "threshold_meters": threshold,
        "ambiguity_epsilon_meters": AUTHORED_NODE_AMBIGUITY_EPSILON_METERS,
        "nearest_node_guid": nearest_guid,
    }
    if nearest_distance > threshold:
        raise SpmAuthoredPlacementError(
            "produced component has no bounded authored Node match: "
            + repr(evidence)
        )
    bounded = [row for row in ranked if row[0] <= threshold]
    tied = [
        row for row in ranked
        if abs(row[0] - nearest_distance)
        <= AUTHORED_NODE_AMBIGUITY_EPSILON_METERS
    ]
    claimed = set(claimed_node_guids or ())
    available = [row for row in bounded if row[1] not in claimed]
    if not available:
        raise SpmAuthoredPlacementError(
            "produced components exhausted bounded one-to-one authored Nodes: "
            + repr(evidence)
        )
    selected_distance, selected_guid, selected = available[0]
    if len(tied) > 1:
        evidence["equal_nearest_node_guids"] = [row[1] for row in tied]
        evidence["equal_nearest_tie_policy"] = (
            "lexicographic_guid_then_first_unclaimed_v1"
        )
    if selected_guid != nearest_guid:
        evidence["claimed_nearer_node_count"] = sum(
            row[1] in claimed
            for row in bounded
            if row[0] < selected_distance
            or abs(row[0] - selected_distance)
            <= AUTHORED_NODE_AMBIGUITY_EPSILON_METERS
        )
    evidence["selected_node_guid"] = selected_guid
    evidence["selected_distance_meters"] = selected_distance
    return {**selected, "match_evidence": evidence}


def assign_authored_nodes_to_components(
    table,
    components,
    tolerance_meters=AUTHORED_NODE_GLOBAL_ASSIGNMENT_TOLERANCE_METERS,
):
    """Deterministically assign surviving Nodes across a component cohort.

    The broad bound is the preferred identity-recovery pass.  Components left
    outside that bound are recovered against the remaining compatible
    state/mesh Nodes with a deterministic maximum-cardinality one-to-one pass.
    Distance remains diagnostic; a fixed positional tolerance must not discard
    an otherwise identifiable authored Node and silently replace its placement
    with a render-attachment fallback.
    """
    try:
        threshold = float(tolerance_meters)
    except (TypeError, ValueError) as exc:
        raise SpmAuthoredPlacementError(
            "authored Node global assignment threshold is invalid"
        ) from exc
    if not math.isfinite(threshold) or threshold <= 0.0:
        raise SpmAuthoredPlacementError(
            "authored Node global assignment threshold is invalid"
        )
    rows = []
    seen_ids = set()
    for row in components or []:
        component_id = str(row.get("component_id") or "")
        try:
            mesh_id = int(row.get("target_mesh_id"))
            position = [float(value) for value in row.get("position_meters")]
        except (TypeError, ValueError) as exc:
            raise SpmAuthoredPlacementError(
                "authored Node global assignment component is invalid"
            ) from exc
        if (
            not component_id
            or component_id in seen_ids
            or len(position) != 3
            or any(not math.isfinite(value) for value in position)
        ):
            raise SpmAuthoredPlacementError(
                "authored Node global assignment component is invalid"
            )
        seen_ids.add(component_id)
        rows.append({
            "component_id": component_id,
            "target_mesh_id": mesh_id,
            "position_meters": position,
        })
    mesh_ids_by_generator = (table or {}).get("generator_mesh_ids") or {}
    active_candidates = sorted(
        (
            record for record in (table or {}).get("nodes") or []
            if record.get("active")
        ),
        key=lambda record: str(record.get("node_guid") or ""),
    )
    candidates = sorted(
        (
            record for record in active_candidates
            if any(
                row["target_mesh_id"]
                in set(mesh_ids_by_generator.get(record.get("generator_guid"), ()))
                for row in rows
            )
        ),
        key=lambda record: str(record.get("node_guid") or ""),
    )
    edges = []
    compatible_edges = defaultdict(list)
    compatible_edge_records = {}
    nearest_by_component = {}
    for row in rows:
        for record in candidates:
            if row["target_mesh_id"] not in set(
                mesh_ids_by_generator.get(record.get("generator_guid"), ())
            ):
                continue
            distance = math.dist(
                row["position_meters"], record["position_meters"]
            )
            previous = nearest_by_component.get(row["component_id"])
            nearest = (distance, str(record["node_guid"]))
            if previous is None or nearest < previous:
                nearest_by_component[row["component_id"]] = nearest
            compatible_edges[row["component_id"]].append(
                (distance, str(record["node_guid"]))
            )
            compatible_edge_records[
                (row["component_id"], str(record["node_guid"]))
            ] = record
            if distance <= threshold:
                edges.append((
                    distance,
                    row["component_id"],
                    str(record["node_guid"]),
                    record,
                ))
    edges.sort(key=lambda edge: (edge[0], edge[1], edge[2]))
    adjacency = defaultdict(list)
    edge_records = {}
    for distance, component_id, node_guid, record in edges:
        adjacency[component_id].append((distance, node_guid))
        edge_records[(component_id, node_guid)] = record
    for component_id in compatible_edges:
        compatible_edges[component_id].sort()

    # A shortest-edge greedy pass can strand a component even when a complete
    # one-to-one assignment exists. Deterministic augmenting paths preserve
    # maximum cardinality; distance/GUID ordering disambiguates matches.
    def maximum_matching(current_adjacency, component_ids):
        node_owner = {}
        component_node = {}

        def claim(start_component_id):
            pending = [start_component_id]
            visited_components = {start_component_id}
            visited_nodes = set()
            component_parent_node = {}
            node_parent_component = {}
            free_node = None
            while pending and free_node is None:
                component_id = pending.pop(0)
                for _distance, node_guid in current_adjacency[component_id]:
                    if node_guid in visited_nodes:
                        continue
                    visited_nodes.add(node_guid)
                    node_parent_component[node_guid] = component_id
                    previous_owner = node_owner.get(node_guid)
                    if previous_owner is None:
                        free_node = node_guid
                        break
                    if previous_owner not in visited_components:
                        visited_components.add(previous_owner)
                        component_parent_node[previous_owner] = node_guid
                        pending.append(previous_owner)
            if free_node is None:
                return False
            node_guid = free_node
            while True:
                component_id = node_parent_component[node_guid]
                node_owner[node_guid] = component_id
                component_node[component_id] = node_guid
                if component_id == start_component_id:
                    return True
                node_guid = component_parent_node[component_id]

        order = sorted(
            component_ids,
            key=lambda component_id: (
                len(current_adjacency[component_id]),
                current_adjacency[component_id][0]
                if current_adjacency[component_id]
                else (float("inf"), ""),
                component_id,
            ),
        )
        for component_id in order:
            claim(component_id)
        return component_node, set(node_owner)

    all_component_ids = [row["component_id"] for row in rows]
    bounded_component_node, bounded_node_guids = maximum_matching(
        adjacency, all_component_ids
    )

    # Keep every successful bounded identity, then recover only the remaining
    # components against unclaimed compatible Nodes.  This preserves precise
    # matches while preventing the old 1 cm cutoff from turning valid authored
    # placements into render-attachment fallbacks.
    recovery_adjacency = defaultdict(list)
    for row in rows:
        component_id = row["component_id"]
        if component_id in bounded_component_node:
            continue
        recovery_adjacency[component_id] = [
            (distance, node_guid)
            for distance, node_guid in compatible_edges[component_id]
            if node_guid not in bounded_node_guids
        ]
    state_recovery_ids = [
        component_id for component_id in all_component_ids
        if component_id not in bounded_component_node
    ]
    recovered_component_node, recovered_node_guids = maximum_matching(
        recovery_adjacency, state_recovery_ids
    )
    component_node = {
        **bounded_component_node,
        **recovered_component_node,
    }

    # Normalized provider plans can carry mesh IDs from a different SPM than
    # the Full tree.  When those numeric ID spaces differ, exact authored Node
    # positions remain the shared identity.  Preserve all state/mesh matches,
    # then recover the unresolved cohort against remaining active Nodes.
    used_node_guids = bounded_node_guids | recovered_node_guids
    unresolved_ids = [
        component_id for component_id in all_component_ids
        if component_id not in component_node
    ]
    global_bounded_adjacency = defaultdict(list)
    global_edge_records = {}
    global_distances = {}
    nearest_global_by_component = {}
    rows_by_id = {row["component_id"]: row for row in rows}
    for component_id in unresolved_ids:
        row = rows_by_id[component_id]
        for record in active_candidates:
            node_guid = str(record["node_guid"])
            distance = math.dist(
                row["position_meters"], record["position_meters"]
            )
            nearest = nearest_global_by_component.get(component_id)
            candidate = (distance, node_guid)
            if nearest is None or candidate < nearest:
                nearest_global_by_component[component_id] = candidate
            if node_guid in used_node_guids:
                continue
            if distance <= threshold:
                global_bounded_adjacency[component_id].append(candidate)
                global_edge_records[(component_id, node_guid)] = record
                global_distances[(component_id, node_guid)] = distance
        global_bounded_adjacency[component_id].sort()
    (
        global_bounded_component_node,
        global_bounded_node_guids,
    ) = maximum_matching(global_bounded_adjacency, unresolved_ids)
    component_node.update(global_bounded_component_node)
    used_node_guids.update(global_bounded_node_guids)

    global_unbounded_ids = [
        component_id for component_id in unresolved_ids
        if component_id not in global_bounded_component_node
    ]
    # Every unresolved component is compatible with every remaining active
    # Node in this final global recovery pass. Building that complete graph and
    # running augmenting paths is both unnecessary and quadratic in memory/time
    # (Chestnut 02 previously spent ~72 minutes on 6,665 x 3,139). A nearest
    # remaining-node scan has the same maximum cardinality on a complete graph
    # and preserves deterministic distance/GUID ordering.
    active_by_guid = {
        str(record["node_guid"]): record for record in active_candidates
    }
    remaining_node_guids = set(active_by_guid) - used_node_guids
    global_recovered_component_node = {}
    unique_recovery_order = sorted(
        global_unbounded_ids,
        key=lambda component_id: (
            nearest_global_by_component.get(
                component_id, (float("inf"), "")
            ),
            component_id,
        ),
    )
    for component_id in unique_recovery_order:
        if not remaining_node_guids:
            break
        row = rows_by_id[component_id]
        distance, node_guid = min(
            (
                math.dist(
                    row["position_meters"],
                    active_by_guid[candidate_guid]["position_meters"],
                ),
                candidate_guid,
            )
            for candidate_guid in remaining_node_guids
        )
        global_recovered_component_node[component_id] = node_guid
        global_edge_records[(component_id, node_guid)] = active_by_guid[
            node_guid
        ]
        global_distances[(component_id, node_guid)] = distance
        remaining_node_guids.remove(node_guid)
    global_recovered_node_guids = set(
        global_recovered_component_node.values()
    )
    component_node.update(global_recovered_component_node)
    used_node_guids.update(global_recovered_node_guids)

    # One authored generator Node can emit several disconnected render
    # components (multiple cards/role surfaces). Once every unique Node has
    # been used, bind additional components to their deterministic nearest
    # authored Node instead of degrading them to render-attachment placement.
    # Truly node-less targets remain unmatched and reportable.
    shared_component_node = {}
    for component_id in global_unbounded_ids:
        if component_id in global_recovered_component_node:
            continue
        nearest = nearest_global_by_component.get(component_id)
        if nearest is None:
            continue
        distance, node_guid = nearest
        shared_component_node[component_id] = node_guid
        global_edge_records[(component_id, node_guid)] = active_by_guid[
            node_guid
        ]
        global_distances[(component_id, node_guid)] = distance
    component_node.update(shared_component_node)
    shared_out_of_tolerance_count = sum(
        global_distances[(component_id, node_guid)] > threshold
        for component_id, node_guid in shared_component_node.items()
    )

    assignments = {}
    for component_id, node_guid in sorted(component_node.items()):
        state_mesh_compatible = component_id in (
            bounded_component_node | recovered_component_node
        )
        outside_threshold = (
            component_id in recovered_component_node
            or component_id in global_recovered_component_node
            or (
                component_id in shared_component_node
                and global_distances[(component_id, node_guid)] > threshold
            )
        )
        if state_mesh_compatible:
            record = compatible_edge_records[(component_id, node_guid)]
            selected_distance = next(
                distance
                for distance, candidate_guid in compatible_edges[component_id]
                if candidate_guid == node_guid
            )
        else:
            record = global_edge_records[(component_id, node_guid)]
            selected_distance = global_distances[(component_id, node_guid)]
        assignments[component_id] = {
            **record,
            "match_evidence": {
                "policy": (
                    "deterministic_state_mesh_then_global_position_"
                    "recovery_shared_components_v4"
                ),
                "threshold_meters": threshold,
                "selected_distance_meters": selected_distance,
                "outside_preferred_threshold": outside_threshold,
                "state_mesh_compatible": state_mesh_compatible,
                "candidate_scope": (
                    "state_mesh"
                    if state_mesh_compatible
                    else (
                        "global_authored_position_shared"
                        if component_id in shared_component_node
                        else "global_authored_position"
                    )
                ),
                "shared_authored_node": (
                    component_id in shared_component_node
                ),
                "component_id": component_id,
                "target_mesh_id": rows_by_id[component_id]["target_mesh_id"],
            },
        }
    unmatched = []
    for row in rows:
        if row["component_id"] in assignments:
            continue
        nearest = (
            nearest_global_by_component.get(row["component_id"])
            or nearest_by_component.get(row["component_id"])
        )
        unmatched.append({
            **row,
            "match_diagnostic": (
                "no_active_authored_node_candidate"
                if nearest is None
                else "global_one_to_one_candidate_exhausted"
            ),
            "nearest_distance_meters": nearest[0] if nearest else None,
            "nearest_node_guid": nearest[1] if nearest else None,
            "threshold_meters": threshold,
        })
    return {
        "policy": (
            "deterministic_state_mesh_then_global_position_recovery_"
            "shared_components_v4"
        ),
        "threshold_meters": threshold,
        "component_count": len(rows),
        "candidate_count": len(candidates),
        "global_candidate_count": len(active_candidates),
        "bounded_assigned_count": len(bounded_component_node),
        "state_mesh_out_of_tolerance_recovery_count": len(
            recovered_component_node
        ),
        "global_bounded_recovery_count": len(
            global_bounded_component_node
        ),
        "global_out_of_tolerance_recovery_count": len(
            global_recovered_component_node
        ),
        "shared_node_component_recovery_count": len(shared_component_node),
        "shared_node_reuse_count": len(shared_component_node),
        "shared_node_out_of_tolerance_count": (
            shared_out_of_tolerance_count
        ),
        "recovered_out_of_tolerance_count": (
            len(recovered_component_node)
            + len(global_recovered_component_node)
            + shared_out_of_tolerance_count
        ),
        "maximum_recovered_distance_meters": max(
            (
                (
                    next(
                        distance
                        for distance, candidate_guid
                        in compatible_edges[component_id]
                        if candidate_guid == node_guid
                    )
                    if component_id in recovered_component_node
                    else global_distances[(component_id, node_guid)]
                )
                for component_id, node_guid
                in {
                    **recovered_component_node,
                    **global_recovered_component_node,
                    **shared_component_node,
                }.items()
            ),
            default=0.0,
        ),
        "assigned_count": len(assignments),
        "unmatched_count": len(unmatched),
        "assignments": assignments,
        "unmatched": unmatched,
    }


__all__ = [
    "AUTHORED_NODE_MATCH_TOLERANCE_METERS",
    "AUTHORED_NODE_GLOBAL_ASSIGNMENT_TOLERANCE_METERS",
    "SPEEDTREE_UNIT_TO_METERS",
    "SpmAuthoredPlacementError",
    "assign_authored_nodes_to_components",
    "match_authored_node",
    "parse_spm_authored_placement",
]
