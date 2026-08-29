"""Strict reader for evidence emitted by the native SpeedTree exporter.

This module never opens or interprets an SPM.  The receipt is produced while
SpeedTree Modeler is operating on its already-parsed runtime model and while
its FBX serializer is assigning geometry vertices and deform clusters.
"""

import base64
import bisect
import json
import math
from array import array
from collections import defaultdict
from pathlib import Path


RECEIPT_KIND = "speedtree_native_export_receipt"
RECEIPT_SCHEMA_VERSION = 5
PREVIOUS_RECEIPT_SCHEMA_VERSION = 3
LEGACY_RECEIPT_SCHEMA_VERSION = 2
NATIVE_UNIT_TO_METER = 0.3048
_EXACT_INDEX_STORAGE_BUDGET_BYTES = 64 * 1024 * 1024
_UINT32_MAX = (1 << 32) - 1
BLENDER_XYZ_FROM_NATIVE_XYZ = (
    "x*0.3048",
    "y*0.3048",
    "z*0.3048",
)
LEGACY_DECLARED_BLENDER_XYZ_FROM_NATIVE_XYZ = (
    "x*0.3048",
    "z*0.3048",
    "-y*0.3048",
)


class NativeReceiptError(RuntimeError):
    """Raised when native evidence is stale, incomplete, or inconsistent."""


def _windows_write_time_100ns(path):
    stat = Path(path).stat()
    return stat.st_mtime_ns // 100 + 116444736000000000


def _guid(value, context):
    text = str(value or "")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise NativeReceiptError(f"{context} is not a valid runtime GUID") from exc
    if len(decoded) != 16:
        raise NativeReceiptError(f"{context} is not a 16-byte runtime GUID")
    return text


def _float3(value, context):
    try:
        row = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise NativeReceiptError(f"{context} is not a float3") from exc
    if len(row) != 3 or not all(math.isfinite(item) for item in row):
        raise NativeReceiptError(f"{context} is not a finite float3")
    return row


def _unit_float3(value, context):
    row = _float3(value, context)
    length = math.sqrt(sum(item * item for item in row))
    if abs(length - 1.0) > 2.0e-5:
        raise NativeReceiptError(f"{context} is not a unit direction")
    return row


def native_position_to_blender_world(receipt, coordinate):
    """Convert an exact Modeler runtime position to Blender meter space.

    SpeedTree's runtime node positions and the imported FBX mesh use the same
    XYZ axis order.  Only the native foot-to-meter unit conversion belongs at
    this boundary; FBX/Unreal axis conversion happens later.
    """
    contract = (receipt or {}).get("coordinate_contract") or {}
    try:
        scale = float(contract.get("native_unit_to_meter"))
        mapping = tuple(contract.get("blender_xyz_from_native_xyz") or ())
    except (TypeError, ValueError) as exc:
        raise NativeReceiptError(
            "native SpeedTree coordinate contract is invalid"
        ) from exc
    try:
        schema_version = int((receipt or {}).get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise NativeReceiptError(
            "native SpeedTree receipt schema is invalid"
        ) from exc
    current_contract = (
        schema_version in {
            PREVIOUS_RECEIPT_SCHEMA_VERSION,
            RECEIPT_SCHEMA_VERSION,
        }
        and mapping == BLENDER_XYZ_FROM_NATIVE_XYZ
    )
    legacy_contract = (
        schema_version == LEGACY_RECEIPT_SCHEMA_VERSION
        and mapping == LEGACY_DECLARED_BLENDER_XYZ_FROM_NATIVE_XYZ
    )
    if (
        not math.isfinite(scale)
        or abs(scale - NATIVE_UNIT_TO_METER) > 1.0e-12
        or not (current_contract or legacy_contract)
    ):
        raise NativeReceiptError(
            "native SpeedTree coordinate contract is unsupported"
        )
    native = _float3(coordinate, "authored node position")
    return tuple(value * scale for value in native)


def native_tangent_to_blender_world(receipt, direction):
    """Return Modeler's exact runtime tangent in Blender's matching XYZ axes."""
    native_position_to_blender_world(receipt, (0.0, 0.0, 0.0))
    return _unit_float3(direction, "authored node tangent")


def load_native_export_receipt(path, *, source_spm=None):
    """Load one fresh native receipt without any SPM-side reconstruction."""
    receipt_path = Path(path).resolve()
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeReceiptError(
            f"native SpeedTree receipt cannot be read: {receipt_path}"
        ) from exc
    if (
        payload.get("kind") != RECEIPT_KIND
        or payload.get("status") != "ready"
        or int(payload.get("schema_version") or 0)
        not in {
            LEGACY_RECEIPT_SCHEMA_VERSION,
            PREVIOUS_RECEIPT_SCHEMA_VERSION,
            RECEIPT_SCHEMA_VERSION,
        }
    ):
        raise NativeReceiptError("native SpeedTree receipt contract is unsupported")
    if (
        int(payload["schema_version"]) == RECEIPT_SCHEMA_VERSION
        and payload.get("identity_policy")
        != "modeler_runtime_pose_tangent_and_fbx_serializer_records_v5"
    ):
        raise NativeReceiptError(
            "native SpeedTree receipt identity policy is unsupported"
        )
    id_zero_cluster_write = str(
        payload.get("id_zero_cluster_write") or "legacy_unreported"
    )
    if id_zero_cluster_write not in {
        "legacy_unreported",
        "native_exact_bone_record",
        "omitted_no_exact_bone_record",
        "not_applicable_boneless_export",
    }:
        raise NativeReceiptError(
            "native SpeedTree ID-0 cluster-write contract is unsupported"
        )
    payload["id_zero_cluster_write"] = id_zero_cluster_write
    native_position_to_blender_world(payload, (0.0, 0.0, 0.0))
    payload["coordinate_contract_interpretation"] = {
        "status": "exact",
        "native_axis_order": ["x", "y", "z"],
        "unit_scale": NATIVE_UNIT_TO_METER,
        "legacy_declared_axis_map_corrected": (
            int(payload["schema_version"]) == LEGACY_RECEIPT_SCHEMA_VERSION
        ),
        "evidence": (
            "native runtime positions match imported FBX attachment vertices"
        ),
    }

    source = payload.get("source") or {}
    source_path = Path(str(source.get("path") or "")).resolve()
    if source_spm is not None:
        expected = Path(source_spm).resolve()
        if str(source_path).casefold() != str(expected).casefold():
            raise NativeReceiptError("native SpeedTree receipt source path is stale")
        stat = expected.stat()
        if (
            int(source.get("size") or -1) != stat.st_size
            or int(source.get("last_write_time_100ns") or -1)
            != _windows_write_time_100ns(expected)
        ):
            raise NativeReceiptError("native SpeedTree receipt source identity is stale")

    geometries = list(payload.get("geometries") or [])
    geometry_count = payload.get("geometry_count")
    if int(geometry_count if geometry_count is not None else -1) != len(
        geometries
    ):
        raise NativeReceiptError("native SpeedTree geometry count is inconsistent")
    checked_geometries = []
    for expected_ordinal, row in enumerate(geometries):
        try:
            ordinal = int(row.get("ordinal"))
            vertex_count = int(row.get("vertex_count"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise NativeReceiptError("native SpeedTree geometry row is invalid") from exc
        if ordinal != expected_ordinal or vertex_count <= 0:
            raise NativeReceiptError("native SpeedTree geometry identity is invalid")
        checked_geometries.append({
            "ordinal": ordinal,
            "vertex_count": vertex_count,
        })

    checked_bones = []
    bone_ids = set()
    for row in payload.get("bones") or []:
        try:
            bone_id = int(row.get("id"))
            parent_id = int(row.get("parent_id"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise NativeReceiptError("native SpeedTree bone row is invalid") from exc
        if bone_id in bone_ids:
            raise NativeReceiptError("native SpeedTree bone IDs are not unique")
        bone_ids.add(bone_id)
        checked_bones.append({
            **row,
            "id": bone_id,
            "parent_id": parent_id,
            "start_native": _float3(
                row.get("start_native"), f"Bone {bone_id} start"
            ),
            "end_native": _float3(
                row.get("end_native"), f"Bone {bone_id} end"
            ),
        })

    checked_instances = []
    for row in payload.get("generated_instances") or []:
        try:
            geometry_ordinal = int(row.get("geometry_ordinal"))
            source_bone_id = int(row.get("source_bone_id"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise NativeReceiptError("native generated-instance row is invalid") from exc
        if not 0 <= geometry_ordinal < len(checked_geometries):
            raise NativeReceiptError("native generated-instance geometry is invalid")
        maximum = checked_geometries[geometry_ordinal]["vertex_count"] - 1
        checked_ranges = []
        previous_last = -1
        for value in row.get("vertex_ranges") or []:
            try:
                first, last = (int(value[0]), int(value[1]))
            except (IndexError, TypeError, ValueError) as exc:
                raise NativeReceiptError("native vertex range is invalid") from exc
            if first <= previous_last or first < 0 or last < first or last > maximum:
                raise NativeReceiptError("native vertex ranges are not exact and ordered")
            checked_ranges.append((first, last))
            previous_last = last
        if not checked_ranges:
            raise NativeReceiptError("native generated instance has no vertex range")

        checked = {
            **row,
            "geometry_ordinal": geometry_ordinal,
            "source_bone_id": source_bone_id,
            "vertex_ranges": checked_ranges,
        }
        if row.get("node_guid"):
            checked["node_guid"] = _guid(row.get("node_guid"), "node GUID")
            if row.get("parent_guid"):
                checked["parent_guid"] = _guid(
                    row.get("parent_guid"), "parent GUID"
                )
            if row.get("generator_guid"):
                checked["generator_guid"] = _guid(
                    row.get("generator_guid"), "generator GUID"
                )
            checked["authored_position_native"] = _float3(
                row.get("authored_position_native"), "authored node position"
            )
            if int(payload["schema_version"]) == RECEIPT_SCHEMA_VERSION:
                checked["authored_tangent_native_unit"] = _unit_float3(
                    row.get("authored_tangent_native_unit"),
                    "authored node tangent",
                )
            influences = []
            for influence in row.get("authored_position_influences") or []:
                try:
                    weight = float(influence.get("weight"))
                    mapping_bone_id = int(influence.get("bone_id"))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise NativeReceiptError("native influence is invalid") from exc
                cluster_name = str(
                    influence.get("exported_cluster_name") or ""
                )
                if (
                    not math.isfinite(weight)
                    or weight <= 0.0
                    or influence.get("mapping_node") != "start"
                    or (
                        not cluster_name
                        and not (
                            mapping_bone_id == 0
                            and influence.get("native_root") is True
                        )
                    )
                ):
                    raise NativeReceiptError("native influence identity is incomplete")
                influences.append({
                    **influence,
                    "bone_id": mapping_bone_id,
                    "weight": weight,
                    "exported_cluster_name": cluster_name,
                })
            if not influences or abs(sum(row["weight"] for row in influences) - 1.0) > 2.0e-6:
                raise NativeReceiptError("native authored-position weights are invalid")
            checked["authored_position_influences"] = influences
        checked_instances.append(checked)

    payload["receipt_path"] = str(receipt_path)
    payload["source"]["path"] = str(source_path)
    payload["geometries"] = checked_geometries
    payload["bones"] = checked_bones
    payload["generated_instances"] = checked_instances
    return payload


def exact_geometry_ordinal(receipt, vertex_count):
    """Resolve a Blender FBX mesh by exact native vertex count only."""
    count = int(vertex_count)
    matches = [
        row["ordinal"]
        for row in receipt.get("geometries") or []
        if int(row["vertex_count"]) == count
    ]
    if len(matches) != 1:
        raise NativeReceiptError(
            "FBX mesh has no unique exact native geometry identity: "
            f"vertex_count={count}, matches={matches}"
        )
    return matches[0]


ZERO_NATIVE_NODE_GUIDS = {
    "00000000-0000-0000-0000-000000000000",
    "AAAAAAAAAAAAAAAAAAAAAA==",
}


def _valid_native_node_guid(row):
    """Return a real runtime GUID, excluding SpeedTree's zero sentinel."""
    value = str(row.get("node_guid") or "").strip()
    return "" if not value or value in ZERO_NATIVE_NODE_GUIDS else value


def _native_runtime_owner_key(row, row_index, geometry_ordinal):
    """Identity of the native runtime Node that owns a serializer record.

    A non-zero ``node_guid`` is authoritative within one geometry.  SpeedTree's
    zero GUID is a missing value, not an owner: production receipts contain
    thousands of unrelated records with that sentinel.  ``native_instance_id``
    also repeats across unrelated nodes, so the no-GUID fallback includes the
    authored frame and lineage fields that stay constant across the per-bone
    serializer records of one actual node.
    """
    source_object_id = row.get("native_source_object_id")
    if source_object_id is not None and int(source_object_id) != 0:
        return (
            "native_source_object",
            int(geometry_ordinal),
            int(source_object_id),
        )
    node_guid = _valid_native_node_guid(row)
    if node_guid:
        return ("node_guid", int(geometry_ordinal), node_guid)
    native_instance_id = row.get("native_instance_id")
    if native_instance_id is not None:
        return (
            "native_instance_frame",
            int(geometry_ordinal),
            int(native_instance_id),
            str(row.get("parent_guid") or ""),
            str(row.get("generator_guid") or ""),
            str(row.get("source_rtti") or ""),
            tuple(float(value) for value in (
                row.get("authored_position_native") or []
            )),
            tuple(float(value) for value in (
                row.get("authored_tangent_native_unit") or []
            )),
        )
    return ("serializer_record", int(row_index))


class _ExactGeometryRangeIndex:
    """Compact exact point lookup over one native serializer geometry.

    Production receipts normally partition every native vertex with disjoint
    serializer ranges.  That common case uses one packed row ID per vertex.
    Overlapping rows cannot use that representation because dropping any row
    would hide an owner or metadata conflict, so bounded overlaps use CSR and
    unusually large overlaps retain an interval index instead.
    """

    _UINT32_ITEMSIZE = array("I").itemsize

    def __init__(
        self,
        vertex_count,
        ranges,
        *,
        storage_budget_bytes=_EXACT_INDEX_STORAGE_BUDGET_BYTES,
    ):
        self.vertex_count = max(0, int(vertex_count))
        self.mode = "interval"
        self._dense_rows = None
        self._csr_offsets = None
        self._csr_rows = None
        self._ranges = sorted(
            (
                int(first),
                int(last),
                int(row_index),
                int(range_index),
            )
            for first, last, row_index, range_index in ranges
        )
        self._range_starts = [row[0] for row in self._ranges]
        self._range_prefix_maximum = []
        maximum = -1
        previous_last = -1
        disjoint = True
        expanded_entry_count = 0
        for first, last, _row_index, _range_index in self._ranges:
            maximum = max(maximum, last)
            self._range_prefix_maximum.append(maximum)
            if first <= previous_last:
                disjoint = False
            previous_last = max(previous_last, last)
            expanded_entry_count += last - first + 1

        budget = max(0, int(storage_budget_bytes))
        dense_bytes = self._UINT32_ITEMSIZE * self.vertex_count
        if (
            disjoint
            and dense_bytes <= budget
            and len(self._ranges) <= _UINT32_MAX
        ):
            self._build_dense()
            return

        csr_bytes = self._UINT32_ITEMSIZE * (
            self.vertex_count + 1 + expanded_entry_count
        )
        if (
            not disjoint
            and csr_bytes <= budget
            and expanded_entry_count <= _UINT32_MAX
        ):
            self._build_csr(expanded_entry_count)

    @property
    def persistent_storage_bytes(self):
        buffers = (
            self._dense_rows,
            self._csr_offsets,
            self._csr_rows,
        )
        return sum(
            value.buffer_info()[1] * value.itemsize
            for value in buffers
            if value is not None
        )

    def _build_dense(self):
        # Store row_index + 1 so zero remains an exact unowned sentinel.
        dense = array("I", [0]) * self.vertex_count
        for first, last, row_index, _range_index in self._ranges:
            encoded = row_index + 1
            if encoded > _UINT32_MAX:
                return
            dense[first:last + 1] = array("I", [encoded]) * (
                last - first + 1
            )
        self._dense_rows = dense
        self.mode = "dense_single_row"

    def _build_csr(self, expanded_entry_count):
        delta = array("i", [0]) * (self.vertex_count + 1)
        for first, last, _row_index, _range_index in self._ranges:
            delta[first] += 1
            if last + 1 < self.vertex_count:
                delta[last + 1] -= 1
        offsets = array("I", [0]) * (self.vertex_count + 1)
        running = 0
        total = 0
        for vertex_index in range(self.vertex_count):
            running += delta[vertex_index]
            offsets[vertex_index] = total
            total += running
        offsets[self.vertex_count] = total
        if total != expanded_entry_count or total > _UINT32_MAX:
            return

        cursors = array("I", offsets[:-1])
        entries = array("I", [0]) * total
        # Fill in global serializer-record order.  Point queries nevertheless
        # sort/deduplicate defensively for raw unit-test receipts.
        for first, last, row_index, _range_index in sorted(
            self._ranges,
            key=lambda row: (row[2], row[3], row[0], row[1]),
        ):
            for vertex_index in range(first, last + 1):
                destination = cursors[vertex_index]
                entries[destination] = row_index
                cursors[vertex_index] = destination + 1
        self._csr_offsets = offsets
        self._csr_rows = entries
        self.mode = "csr_multiple_rows"

    def records_at(self, vertex_index):
        vertex_index = int(vertex_index)
        if not 0 <= vertex_index < self.vertex_count:
            return ()
        if self._dense_rows is not None:
            encoded = int(self._dense_rows[vertex_index])
            return () if encoded == 0 else (encoded - 1,)
        if self._csr_offsets is not None:
            first = int(self._csr_offsets[vertex_index])
            last = int(self._csr_offsets[vertex_index + 1])
            return tuple(sorted(set(self._csr_rows[first:last])))

        # Memory-bounded exact fallback.  Prefix maxima prune every earlier
        # interval once none can still contain the queried integer vertex.
        cursor = bisect.bisect_right(self._range_starts, vertex_index) - 1
        matches = []
        while (
            cursor >= 0
            and self._range_prefix_maximum[cursor] >= vertex_index
        ):
            first, last, row_index, _range_index = self._ranges[cursor]
            if first <= vertex_index <= last:
                matches.append(row_index)
            cursor -= 1
        return tuple(sorted(set(matches)))


class ExactNativeReceiptIndex:
    """One-run exact index over every serializer record in a receipt."""

    def __init__(
        self,
        receipt,
        *,
        storage_budget_bytes=_EXACT_INDEX_STORAGE_BUDGET_BYTES,
    ):
        self._receipt = receipt
        self._rows = tuple((receipt or {}).get("generated_instances") or ())
        self._owner_keys = tuple(
            _native_runtime_owner_key(
                row,
                row_index,
                int(row["geometry_ordinal"]),
            )
            for row_index, row in enumerate(self._rows)
        )
        vertex_counts = {
            int(row["ordinal"]): int(row["vertex_count"])
            for row in (receipt or {}).get("geometries") or ()
        }
        ranges_by_geometry = defaultdict(list)
        for row_index, row in enumerate(self._rows):
            geometry_ordinal = int(row["geometry_ordinal"])
            for range_index, (first, last) in enumerate(
                row.get("vertex_ranges") or ()
            ):
                first = int(first)
                last = int(last)
                ranges_by_geometry[geometry_ordinal].append(
                    (first, last, row_index, range_index)
                )
                vertex_counts[geometry_ordinal] = max(
                    vertex_counts.get(geometry_ordinal, 0),
                    last + 1,
                )
        self._geometries = {
            geometry_ordinal: _ExactGeometryRangeIndex(
                vertex_count,
                ranges_by_geometry.get(geometry_ordinal, ()),
                storage_budget_bytes=storage_budget_bytes,
            )
            for geometry_ordinal, vertex_count in vertex_counts.items()
        }

    def belongs_to(self, receipt):
        return receipt is self._receipt

    @property
    def storage_modes(self):
        return {
            geometry_ordinal: geometry.mode
            for geometry_ordinal, geometry in self._geometries.items()
        }

    @property
    def persistent_storage_bytes(self):
        return sum(
            geometry.persistent_storage_bytes
            for geometry in self._geometries.values()
        )

    def records_at(self, geometry_ordinal, vertex_index):
        geometry = self._geometries.get(int(geometry_ordinal))
        if geometry is None:
            return ()
        return geometry.records_at(vertex_index)

    def owner_keys_at(self, geometry_ordinal, vertex_index):
        return frozenset(
            self._owner_keys[row_index]
            for row_index in self.records_at(
                geometry_ordinal,
                vertex_index,
            )
        )

    def intersecting_records(self, geometry_ordinal, vertex_indices):
        vertices = sorted({int(value) for value in vertex_indices})
        matched_by_record = defaultdict(list)
        for vertex_index in vertices:
            for row_index in self.records_at(
                geometry_ordinal,
                vertex_index,
            ):
                matched_by_record[row_index].append(vertex_index)
        return vertices, [
            (
                row_index,
                self._rows[row_index],
                matched_by_record[row_index],
            )
            for row_index in sorted(matched_by_record)
        ]


def build_exact_native_receipt_index(
    receipt,
    *,
    storage_budget_bytes=_EXACT_INDEX_STORAGE_BUDGET_BYTES,
):
    return ExactNativeReceiptIndex(
        receipt,
        storage_budget_bytes=storage_budget_bytes,
    )


def exact_generated_instance(
    receipt,
    geometry_ordinal,
    vertex_indices,
    *,
    receipt_index=None,
):
    """Return the sole native runtime node intersecting supplied vertices.

    SpeedTree can clip the authored attachment/origin vertex out of the FBX
    subset and can leave serializer-only vertices without a Node record.  The
    surviving Node-owned vertices remain exact local indices.  Accept them
    only when their native ranges identify one and only one runtime Node;
    there is deliberately no coverage ranking or fallback selection.
    """
    vertices = sorted({int(value) for value in vertex_indices})
    if not vertices:
        raise NativeReceiptError("target component has no vertices")
    if receipt_index is None:
        receipt_index = build_exact_native_receipt_index(receipt)
    if not receipt_index.belongs_to(receipt):
        raise NativeReceiptError(
            "native SpeedTree exact index belongs to a different receipt"
        )
    vertices, intersecting_records = receipt_index.intersecting_records(
        geometry_ordinal,
        vertices,
    )
    matches_by_owner = {}
    for row_index, row, matched in intersecting_records:
        owner_key = _native_runtime_owner_key(
            row, row_index, geometry_ordinal
        )
        owner = matches_by_owner.get(owner_key)
        if owner is None:
            owner = {
                "row": row,
                "matched": set(),
                "record_indices": [],
                "native_instance_ids": set(),
                "matched_rows": [],
            }
            matches_by_owner[owner_key] = owner
        owner["matched"].update(matched)
        owner["record_indices"].append(row_index)
        owner["matched_rows"].append(row)
        if row.get("native_instance_id") is not None:
            owner["native_instance_ids"].add(
                int(row["native_instance_id"])
            )
    matches = list(matches_by_owner.values())
    if len(matches) != 1:
        raise NativeReceiptError(
            "target component has no sole intersecting native runtime owner: "
            f"geometry={geometry_ordinal}, matches={len(matches)}"
        )
    owner = matches[0]
    matched_rows = owner["matched_rows"]
    stable_fields = (
        "geometry_ordinal",
        "parent_guid",
        "generator_guid",
        "source_rtti",
        "authored_position_native",
        "authored_tangent_native_unit",
    )
    for field in stable_fields:
        values = {
            json.dumps(row.get(field), sort_keys=True, ensure_ascii=False)
            for row in matched_rows
        }
        if len(values) > 1:
            raise NativeReceiptError(
                "one native runtime owner has inconsistent attachment "
                f"metadata: field={field}"
            )
    influence_values = {
        json.dumps(
            row.get("authored_position_influences") or [],
            sort_keys=True,
            ensure_ascii=False,
        )
        for row in matched_rows
    }
    if len(influence_values) > 1:
        raise NativeReceiptError(
            "one native runtime owner has ambiguous authored attachment "
            "influences for the queried vertices"
        )
    # The selected row is now safe: every serializer record intersecting the
    # queried attachment agrees on its authored frame and full influence set.
    row = matched_rows[0]
    matched = sorted(owner["matched"])
    matched_set = set(matched)
    return {
        **row,
        "matched_native_vertex_indices": matched,
        "queried_native_vertex_count": len(vertices),
        "unowned_native_vertex_count": sum(
            vertex not in matched_set for vertex in vertices
        ),
        "native_instance_ids": sorted(owner["native_instance_ids"]),
        "native_serializer_record_indices": list(owner["record_indices"]),
        "native_serializer_record_count": len(owner["record_indices"]),
        "owner_selection_policy": (
            "sole_exact_native_runtime_owner_range_intersection_v3"
        ),
    }
