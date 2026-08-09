"""Fail-closed recovery for a stale saved SpeedTree Node table.

The Modeler is opened only after an exact byte preimage and an immutable,
SHA-bound receipt have been created and verified.  A caller may provide the
bounded exact-PID semantic UIA session; it is invoked only after the stale,
nonzero-orphan, complete-scope, and unchanged-SHA gates pass.  The module never
edits the SPM, kills Modeler, simulates input, rolls back automatically, or
treats ``stale=false`` alone as permission to continue.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from process_lifecycle import external_handoff_popen

from speedtree_pipeline_contract import (
    SPM_AUTHORING_GRAPH_PROJECTION_VERSION,
    canonical_generator_guid,
    canonical_path_key,
    generator_guid_key,
)
from pcg_st9_texture_batch.speedtree_modeler_uia import (
    SEMANTIC_UIA_CONTRACT,
    SemanticModelerUIAError,
)

try:
    from .pcg_cluster_assembly_contract import _normalized_generator_delivery
    from .pcg_texture_audit import (
        _export_node_counts_from_text,
        generator_delivery_snapshot_from_spm_text,
    )
except ImportError:  # Direct script execution from the tool directory.
    from pcg_cluster_assembly_contract import _normalized_generator_delivery
    from pcg_texture_audit import (
        _export_node_counts_from_text,
        generator_delivery_snapshot_from_spm_text,
    )


SHA256_RE = re.compile(r"[0-9a-f]{64}")
RECOVERY_CONTRACT = "speedtree_stale_node_table_interactive_recovery_v2"
PREIMAGE_RECEIPT_KIND = "speedtree_stale_node_table_preimage_receipt"
BLOCKED_EVENT_KIND = "speedtree_stale_node_table_recovery_blocked"
CONTINUATION_CLAIM_KIND = "speedtree_stale_node_table_continuation_claim"
AUTHORING_GRAPH_CORE_PROJECTION_VERSION = 6
TARGET_BINDING_PROJECTION_VERSION = 2
TARGET_REQUIREMENTS_VERSION = 1
TARGET_REQUIREMENTS_POLICY = "explicit_sealed_scopes_v1"
TARGET_SCOPE_MODE_STRICT_LEGACY = "strict_legacy"
TARGET_SCOPE_MODE_EXPLICIT = "explicit_sealed_scopes"
_CURRENT_RECEIPT_DIALECT_KEY = (8, 1, 6, 1, 2, 1)
_KNOWN_RECEIPT_SCHEMAS = frozenset({2, 3, 4, 5, 6, 7, 8})
_KNOWN_UNSUPPORTED_RECEIPT_DIALECTS = frozenset({
    # A real Lauraceae receipt uses this tuple, but the historical core-v1
    # implementation is unavailable.  Its sanitized evidence fixture records
    # the explicit unsupported result; never alias it to v2/v3 or tampering.
    (3, 1, 1, 1, 1, None),
})
_AUTHORING_GRAPH_V1_IGNORED_SUBTREE_TAGS = frozenset({
    "thumbnail",
    "thumbnailsize",
    "preview",
    "statistics",
    "quicksavesettings2",
    "m_stimelinedata",
})
_AUTHORING_GRAPH_CORE_IGNORED_SUBTREE_TAGS = frozenset({
    "thumbnail",
    "thumbnailsize",
    "preview",
    "statistics",
    "quicksavesettings2",
    "m_stimelinedata",
})
_AUTHORING_GRAPH_CORE_IGNORED_ROOT_TAGS = frozenset({
    *_AUTHORING_GRAPH_CORE_IGNORED_SUBTREE_TAGS,
    "nodes",
    # Modeler-derived/session state rewritten by a no-edit Save.
    "treeinfo",
    "window",
})
_MATERIAL_AUTHORING_IGNORED_DIRECT_CHILD_TAGS = frozenset({
    # Rebuilt binary/display caches observed to change during a no-edit Save.
    "preview",
    "streamplaceholder",
})
_DEFAULT_MATERIAL_MAP_SCALARS = {
    "specular": {
        "colorx": "0.75",
        "colory": "0.75",
        "colorz": "0.75",
        "texsource": "0",
        "textolinear": "true",
    },
    "metallic": {
        "colorx": "0",
        "colory": "0",
        "colorz": "0",
        "texsource": "1",
        "textolinear": "false",
    },
    "custom": {
        "colorx": "0",
        "colory": "0",
        "colorz": "0",
        "texsource": "0",
        "textolinear": "true",
    },
    "custom2": {
        "colorx": "0",
        "colory": "0",
        "colorz": "0",
        "texsource": "0",
        "textolinear": "false",
    },
}


class StaleNodeTableRecoveryError(RuntimeError):
    """The interactive recovery failed closed with stable public evidence."""

    def __init__(self, reason_token, message, evidence=None):
        super().__init__(message)
        self.reason_token = str(reason_token)
        self.evidence = dict(evidence or {})

    def public_payload(self):
        return {
            "status": "blocked",
            "contract": RECOVERY_CONTRACT,
            "reason_token": self.reason_token,
            "evidence": dict(self.evidence),
        }


class StaleNodeTableRecoveryTimeout(StaleNodeTableRecoveryError):
    """The watched SPM did not reach a stable valid state in time."""


def _canonical_json_bytes(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _json_fingerprint(value):
    return _sha256_bytes(_canonical_json_bytes(value))


def _remove_authoring_graph_v1_volatile(parent, *, depth=0):
    """Apply the frozen graph-v1 projection policy in place."""
    for child in list(parent):
        tag = _local_name(child.tag).casefold()
        if (
            (depth == 0 and tag == "nodes")
            or tag in _AUTHORING_GRAPH_V1_IGNORED_SUBTREE_TAGS
        ):
            parent.remove(child)
            continue
        _remove_authoring_graph_v1_volatile(child, depth=depth + 1)


def _authoring_graph_projection_v1(text):
    """Reproduce the immutable historical authoring-graph v1 fingerprint."""
    projected = ET.fromstring(text)
    _remove_authoring_graph_v1_volatile(projected)
    payload = ET.tostring(
        projected,
        encoding="unicode",
        short_empty_elements=True,
    )
    payload = re.sub(r">\s+<", "><", payload).strip()
    envelope = {
        "contract": "speedtree_spm_authoring_graph_projection",
        "projection_version": 1,
        "source": payload,
    }
    return _sha256_bytes(json.dumps(
        envelope,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8"))


def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]


def _mesh_id(value):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalized_mesh_ids(values):
    return sorted({_mesh_id(value) for value in (values or ())} - {None})


def _resolve_target_scopes(
    expected_mesh_ids=(),
    *,
    authoring_mesh_ids=None,
    required_live_mesh_ids=None,
):
    legacy_values = tuple(expected_mesh_ids or ())
    if any(_mesh_id(value) is None for value in legacy_values):
        return None, "expected_target_mesh_ids_invalid"
    legacy_expected = _normalized_mesh_ids(expected_mesh_ids)
    explicit_requested = bool(
        authoring_mesh_ids is not None or required_live_mesh_ids is not None
    )
    if legacy_expected and explicit_requested:
        return None, "target_scope_mode_mixed"
    if legacy_expected:
        return {
            "mode": TARGET_SCOPE_MODE_STRICT_LEGACY,
            "authoring_mesh_ids": legacy_expected,
            "required_live_mesh_ids": list(legacy_expected),
        }, None
    if authoring_mesh_ids is None:
        return None, "authoring_mesh_ids_missing"
    if required_live_mesh_ids is None:
        return None, "required_live_mesh_ids_missing"
    authoring_values = tuple(authoring_mesh_ids or ())
    required_live_values = tuple(required_live_mesh_ids or ())
    if any(_mesh_id(value) is None for value in authoring_values):
        return None, "authoring_mesh_ids_invalid"
    if any(_mesh_id(value) is None for value in required_live_values):
        return None, "required_live_mesh_ids_invalid"
    authoring = _normalized_mesh_ids(authoring_values)
    required_live = _normalized_mesh_ids(required_live_values)
    if not authoring:
        return None, "authoring_mesh_ids_missing"
    if not set(required_live).issubset(authoring):
        return None, "required_live_scope_not_authoring_subset"
    return {
        "mode": TARGET_SCOPE_MODE_EXPLICIT,
        "authoring_mesh_ids": authoring,
        "required_live_mesh_ids": required_live,
    }, None


def _source_identity(spm):
    return {
        "asset_name": Path(spm).name,
        "source_identity_sha256": hashlib.sha256(
            canonical_path_key(spm).encode("utf-8")
        ).hexdigest(),
    }


def _public_hash_evidence(snapshot=None, **extra):
    evidence = {}
    if snapshot:
        evidence.update({
            "asset_name": snapshot["source_identity"]["asset_name"],
            "source_identity_sha256": snapshot["source_identity"][
                "source_identity_sha256"
            ],
            "after_sha256": snapshot.get("text_sha256"),
            "after_raw_sha256": snapshot.get("raw_sha256"),
        })
    evidence.update({key: value for key, value in extra.items() if value is not None})
    return evidence


def _decode_spm_bytes(raw_bytes):
    decoded = gzip.decompress(raw_bytes) if raw_bytes[:2] == b"\x1f\x8b" else raw_bytes
    return decoded.decode("utf-8", errors="strict")


def _stat_identity(stat):
    return (
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def _child_text(element, name):
    expected = str(name).casefold()
    for child in element:
        if _local_name(child.tag).casefold() == expected:
            return "".join(child.itertext()).strip()
    return ""


def _truthy(value):
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _normalized_spline_number(value):
    """Normalize insignificant Modeler spline float reserialization."""
    text = str(value or "").strip()
    if not re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
        text,
    ):
        return text
    number = float(text)
    if not math.isfinite(number):
        return text
    rounded = round(number, 5)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:.5f}"


def _normalized_float32_number(value):
    """Canonicalize Modeler's ordinary decimal-to-float32 reserialization."""
    text = str(value or "").strip()
    if not re.fullmatch(
        r"[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?",
        text,
    ):
        return text
    number = float(text)
    if not math.isfinite(number):
        return text
    value32 = struct.unpack("!f", struct.pack("!f", number))[0]
    return format(value32, ".9g")


def _default_or_empty_parent_spline(element):
    """Recognize the redundant default parent curve removed by Modeler Save."""
    splines = [
        child for child in element if _local_name(child.tag).casefold() == "spline"
    ]
    if not splines:
        return str(element.attrib.get("Count") or "0").strip() == "0"
    if len(splines) != 1:
        return False
    spline = splines[0]
    if str(spline.attrib.get("DrawMode") or "false").strip().casefold() not in {
        "0",
        "false",
    }:
        return False
    points = []
    for point in spline:
        if _local_name(point.tag).casefold() != "controlpoint":
            return False
        points.append(tuple(
            _normalized_spline_number(_child_text(point, field))
            for field in ("X", "Y", "TangentX", "TangentY", "Length")
        ))
    return points == [
        ("0.00000", "1.00000", "1.00000", "0.00000", "0.00000"),
        ("1.00000", "1.00000", "1.00000", "0.00000", "0.00000"),
    ]


def _default_modeler_atlas_maker(element):
    expected = {
        "weight": "0.5",
        "fullheight": "false",
        "updatemeshes": "true",
        "makeuvarea": "false",
        "translationx": "0",
        "translationy": "0",
        "scalex": "1",
        "scaley": "1",
        "rotation": "0",
    }
    observed = {
        _local_name(key).casefold(): str(value).strip().casefold()
        for key, value in element.attrib.items()
    }
    return (
        observed == expected
        and not list(element)
        and not str(element.text or "").strip()
    )


def _default_modeler_lod(element):
    return bool(
        len(element) == 1
        and _local_name(element[0].tag).casefold() == "filename"
        and not element[0].attrib
        and not list(element[0])
        and not str(element[0].text or "").strip()
    )


def _modeler_scalar_equal(observed, expected):
    observed = str(observed or "").strip()
    expected = str(expected or "").strip()
    try:
        observed32 = struct.unpack("!f", struct.pack("!f", float(observed)))[0]
        expected32 = struct.unpack("!f", struct.pack("!f", float(expected)))[0]
        return observed32 == expected32
    except (TypeError, ValueError):
        return observed.casefold() == expected.casefold()


def _default_modeler_map_spline(element, length):
    if (
        _local_name(element.tag).casefold() != "spline"
        or {
            _local_name(name).casefold(): str(value).strip().casefold()
            for name, value in element.attrib.items()
        } != {"drawmode": "false"}
        or len(element) != 2
    ):
        return False
    expected = (
        ("0", "0", "1", "0", length),
        ("1", "1", "1", "0", length),
    )
    for point, values in zip(element, expected):
        if _local_name(point.tag).casefold() != "controlpoint":
            return False
        children = list(point)
        if [_local_name(child.tag).casefold() for child in children] != [
            "x",
            "y",
            "tangentx",
            "tangenty",
            "length",
        ]:
            return False
        if any(
            child.attrib
            or list(child)
            or not _modeler_scalar_equal(child.text, expected_value)
            for child, expected_value in zip(children, values)
        ):
            return False
    return True


def _default_modeler_map_generate(element):
    attributes = {
        _local_name(name).casefold(): str(value).strip()
        for name, value in element.attrib.items()
    }
    if attributes != {"type": "0"}:
        return False
    expected = {
        "file": (
            {"colorhigh": "ffffffff", "colorlow": "ff000000", "remap": "0"},
            "0",
        ),
        "linear": (
            {
                "angle": "90",
                "centerx": "0",
                "centery": "0",
                "colorhigh": "ffffffff",
                "colorlow": "ff000000",
                "distance": "1",
            },
            "0.45",
        ),
        "radial": (
            {
                "centerx": "0.5",
                "centery": "0.5",
                "colorhigh": "ffffffff",
                "colorlow": "ff000000",
                "distance": "0.5",
            },
            "0.45",
        ),
        "noise": (
            {
                "centerx": "0.5",
                "centery": "0.5",
                "colorhigh": "ffffffff",
                "colorlow": "ff000000",
                "scale": "1",
            },
            "0.45",
        ),
    }
    children = list(element)
    if [_local_name(child.tag).casefold() for child in children] != list(expected):
        return False
    for child in children:
        child_key = _local_name(child.tag).casefold()
        expected_attributes, length = expected[child_key]
        observed_attributes = {
            _local_name(name).casefold(): str(value).strip()
            for name, value in child.attrib.items()
            if not (child_key == "noise" and _local_name(name).casefold() == "seed")
        }
        if set(observed_attributes) != set(expected_attributes) or any(
            not _modeler_scalar_equal(
                observed_attributes[name],
                expected_attributes[name],
            )
            for name in expected_attributes
        ):
            return False
        if child_key == "noise":
            seed_values = [
                value for name, value in child.attrib.items()
                if _local_name(name).casefold() == "seed"
            ]
            if len(seed_values) != 1:
                return False
            try:
                int(str(seed_values[0]).strip())
            except (TypeError, ValueError):
                return False
        if len(child) != 1 or not _default_modeler_map_spline(child[0], length):
            return False
    return True


def _default_modeler_material_map(element):
    """Recognize only the four absent defaults materialized by Modeler Save."""
    if (
        _local_name(element.tag).casefold() != "map"
        or set(_local_name(name).casefold() for name in element.attrib) != {"name"}
    ):
        return False
    map_name = str(element.attrib.get("Name") or "").strip().casefold()
    specific = _DEFAULT_MATERIAL_MAP_SCALARS.get(map_name)
    if specific is None:
        return False
    common = {
        "texfilename": "",
        "texbrightness": "0",
        "texcontrast": "0",
        "texsaturation": "0",
        "texred": "0",
        "texgreen": "0",
        "texblue": "0",
        "texmin": "0",
        "texmax": "1",
        "texenabled": "true",
        "texinvert": "false",
        "texinvertred": "false",
        "texinvertgreen": "false",
        "texinvertblue": "false",
        "normalize": "false",
        "texsizex": "0",
        "texsizey": "0",
    }
    expected = {**common, **specific}
    scalar_children = []
    generate_children = []
    for child in element:
        if _local_name(child.tag).casefold() == "generate":
            generate_children.append(child)
        else:
            scalar_children.append(child)
    if len(generate_children) != 1 or len(scalar_children) != len(expected):
        return False
    observed = {}
    for child in scalar_children:
        key = _local_name(child.tag).casefold()
        if key in observed or child.attrib or list(child):
            return False
        observed[key] = str(child.text or "").strip()
    return bool(
        set(observed) == set(expected)
        and all(
            _modeler_scalar_equal(observed[name], expected[name])
            for name in expected
        )
        and _default_modeler_map_generate(generate_children[0])
    )


def _material_v8_core_subtree(element):
    projected = copy.deepcopy(element)
    retained = []
    maps = []
    for child in list(projected):
        child_key = _local_name(child.tag).casefold()
        if child_key in _MATERIAL_AUTHORING_IGNORED_DIRECT_CHILD_TAGS:
            continue
        if child_key == "map" and _default_modeler_material_map(child):
            continue
        if child_key == "map":
            maps.append(child)
        else:
            retained.append(child)
    maps.sort(key=lambda child: str(child.attrib.get("Name") or "").casefold())
    projected[:] = retained + maps
    return _authoring_graph_core_subtree(projected, depth=1)


def _semantic_spline_subtree(element):
    return {
        "tag": _local_name(element.tag),
        "attributes": sorted(
            (
                _local_name(name),
                _normalized_spline_number(value),
            )
            for name, value in element.attrib.items()
        ),
        "text": _normalized_spline_number(element.text),
        "children": [
            _semantic_spline_subtree(child)
            for child in element
            if _local_name(child.tag).casefold() != "name"
            and not (
                _local_name(child.tag).casefold() == "compoundparentspline"
                and _default_or_empty_parent_spline(child)
            )
        ],
    }


def _authoring_graph_core_subtree(
    element,
    *,
    depth=0,
    spline_context=False,
    truthy_value=False,
):
    """Project one full XML subtree with only proven Save noise normalized."""
    tag = _local_name(element.tag)
    tag_key = tag.casefold()
    spline_context = spline_context or tag_key == "splineproperty"
    text = str(element.text or "").strip()
    if tag_key.endswith("guid"):
        text = generator_guid_key(text)
    elif truthy_value and tag_key == "value":
        try:
            text = "0" if float(text) == 0 else "1"
        except (TypeError, ValueError):
            pass
    elif spline_context:
        text = _normalized_spline_number(text)
    else:
        text = _normalized_float32_number(text)

    attributes = []
    for name, value in element.attrib.items():
        name_key = _local_name(name).casefold()
        if name_key == "m_nordervalue":
            continue
        normalized = str(value).strip()
        if name_key.endswith("guid"):
            normalized = generator_guid_key(normalized)
        elif spline_context:
            normalized = _normalized_spline_number(normalized)
        else:
            normalized = _normalized_float32_number(normalized)
        attributes.append((_local_name(name), normalized))

    property_name = (
        _child_text(element, "Name")
        if tag_key in {"property", "splineproperty"}
        else ""
    )
    child_truthy_value = property_name.casefold() == "random seeds:style"
    children = []
    for child in element:
        child_key = _local_name(child.tag).casefold()
        if child_key in _AUTHORING_GRAPH_CORE_IGNORED_SUBTREE_TAGS:
            continue
        if child_key == "m_nordervalue":
            continue
        if tag_key == "generator" and child_key == "extra":
            continue
        if tag_key in {"force", "rulescript", "fan", "light"} and child_key == "guid":
            continue
        if tag_key == "mesh" and (
            child_key == "userdata"
            or (
                child_key in {"lod_1", "lod_2"}
                and _default_modeler_lod(child)
            )
        ):
            continue
        if (
            tag_key == "material_v8"
            and child_key == "atlasmaker"
            and _default_modeler_atlas_maker(child)
        ):
            continue
        if child_key in {"property", "splineproperty"} and _child_text(
            child, "Name"
        ).casefold().startswith((
            "generation:collections:",
            "mesh:collections:",
        )):
            continue
        if (
            spline_context
            and child_key == "compoundparentspline"
            and _default_or_empty_parent_spline(child)
        ):
            continue
        children.append(_authoring_graph_core_subtree(
            child,
            depth=depth + 1,
            spline_context=spline_context,
            truthy_value=child_truthy_value,
        ))
    return {
        "tag": tag,
        "attributes": sorted(attributes),
        "text": text,
        "children": children,
    }


def _legacy_authoring_graph_core_v3_projection(text):
    """Reproduce the historical core-v3 projection without widening it.

    The projection retains stable global/settings subtrees, complete Generator,
    Force, RuleScript, Fan, and Light properties, Link endpoints, complete
    authored Material parameters, and complete Mesh geometry.
    It normalizes only representations observed to change on a no-edit Save:
    generated/session root blocks, graph-editor identities, rebuilt collection
    labels, float spellings, redundant defaults, and proven preview/stream
    caches.
    """
    root = ET.fromstring(text)
    global_settings = []
    generators = []
    links = []
    assets = []
    for element in root:
        tag = _local_name(element.tag).casefold()
        if tag in _AUTHORING_GRAPH_CORE_IGNORED_ROOT_TAGS:
            continue
        if tag == "generators":
            generators.extend(
                _authoring_graph_core_subtree(child, depth=1)
                for child in element
                if _local_name(child.tag).casefold() == "generator"
            )
        elif tag == "links":
            links.extend({
                "source": generator_guid_key(_child_text(child, "SourceGUID")),
                "target": generator_guid_key(_child_text(child, "TargetGUID")),
            } for child in element if _local_name(child.tag).casefold() == "link")
        elif tag == "assets":
            for child in element:
                child_tag = _local_name(child.tag).casefold()
                if child_tag in {"name", "guid", "hidden", "properties"}:
                    continue
                if child_tag == "material_v8":
                    assets.append(_material_v8_core_subtree(child))
                else:
                    assets.append(
                        _authoring_graph_core_subtree(child, depth=1)
                    )
        else:
            global_settings.append(
                _authoring_graph_core_subtree(element, depth=1)
            )
    global_settings.sort(key=_canonical_json_bytes)
    generators.sort(key=_canonical_json_bytes)
    links.sort(key=_canonical_json_bytes)
    assets.sort(key=_canonical_json_bytes)
    rows = {
        "global_settings": global_settings,
        "generators": generators,
        "links": links,
        "assets": assets,
    }
    return {
        "contract": "speedtree_spm_authoring_graph_core_projection",
        "version": 3,
        "generator_count": len(generators),
        "link_count": len(links),
        "asset_identity_count": len(assets),
        "global_setting_count": len(global_settings),
        "fingerprint": _json_fingerprint(rows),
        "_rows": rows,
    }


_AUTHORING_GRAPH_V4_EXCLUDED_ROOT_TAGS = frozenset({
    "Thumbnail",
    "ThumbnailSize",
    "Preview",
    "Statistics",
    "TreeInfo",
    "QuickSaveSettings2",
    "m_sTimelineData",
    "Window",
    "Nodes",
})
_AUTHORING_GRAPH_V4_GENERATED_GUID_PATHS = frozenset({
    ("SpeedTree", "Light", "GUID"),
    ("SpeedTree", "Fan", "GUID"),
    ("SpeedTree", "RuleScript", "GUID"),
    ("SpeedTree", "Force", "GUID"),
    ("SpeedTree", "Forces", "Force", "GUID"),
    ("SpeedTree", "Links", "Link", "GUID"),
    ("SpeedTree", "Assets", "GUID"),
})
_AUTHORING_GRAPH_V4_CANONICAL_GUID_PATHS = frozenset({
    ("SpeedTree", "Generators", "Generator", "GUID"),
    ("SpeedTree", "Links", "Link", "SourceGUID"),
    ("SpeedTree", "Links", "Link", "TargetGUID"),
})
_AUTHORING_GRAPH_V4_SPLINE_NUMBER_TAGS = frozenset({
    "X",
    "Y",
    "TangentX",
    "TangentY",
    "Length",
})
_AUTHORING_GRAPH_V4_SPLINE_PROPERTY_PATHS = frozenset({
    ("SpeedTree", "Fan", "Properties", "SplineProperty"),
    (
        "SpeedTree", "Generators", "Generator", "Properties",
        "SplineProperty",
    ),
})
_AUTHORING_GRAPH_V4_SPLINE_PROPERTY_NAMES = frozenset({
    "Branch Motion:Level 1:Distance",
    "Branch Motion:Level 2:Distance",
    "Global Motion:Distance",
    "Growth:Noise:Speed:Amount",
    "Growth:Noise:Wobble:Amount",
    "Growth:Transitions:Curl",
    "Growth:Transitions:Fold",
    "Growth:Transitions:Gravity",
    "Growth:Transitions:Roll",
    "Leaf Motion:Ripple 1:Distance",
    "Leaf Motion:Ripple 1:Frequency",
    "Leaf Motion:Ripple 2:Distance",
    "Leaf Motion:Ripple 2:Frequency",
    "Leaf Motion:Tumble 1:Flip",
    "Leaf Motion:Tumble 1:Frequency",
    "Leaf Motion:Tumble 1:Twist",
    "Leaf Motion:Tumble 1:Twitch:Frequency",
    "Leaf Motion:Tumble 1:Twitch:Throw",
    "Leaf Motion:Tumble 2:Twitch:Frequency",
    "Local Orientation:Align",
    "Physics:Bones",
    "Skin:Radius:Absolute",
    "Spine:Noise:Late:Amount",
    "Spine:Orientation:Start angle",
    "Spine:Path:Amount",
    "VFX Branch Motion:Shared:Bend",
    "VFX Branch Motion:Shared:Turbulence",
    "VFX Branch Motion:Shared:Twist",
    "VFX Leaf Motion:Group 1:Turbulence:Frequency",
})
_AUTHORING_GRAPH_V4_RANDOM_SEED_PROPERTY_PATH = (
    "SpeedTree",
    "Generators",
    "Generator",
    "Properties",
    "Property",
)
_AUTHORING_GRAPH_V6_ASSET_PATH_TEXT_PATHS = frozenset({
    (
        "SpeedTree", "Assets", "Material_v8", "Map", "TexFilename",
    ),
    ("SpeedTree", "Assets", "Roughness", "Filename"),
})


def _v4_tag_name(tag):
    if tag is ET.Comment:
        return "{xml-special}comment"
    if tag is ET.ProcessingInstruction:
        return "{xml-special}processing-instruction"
    return str(tag)


def _v4_parse(text):
    parser = ET.XMLParser(target=ET.TreeBuilder(
        insert_comments=True,
        insert_pis=True,
    ))
    return ET.fromstring(text, parser=parser)


def _v4_plain_tag(element, expected):
    """Match a namespace-free SpeedTree tag without collapsing namespaces."""
    tag = _v4_tag_name(element.tag)
    return "}" not in tag and tag == str(expected)


def _v4_path_key(path):
    keys = []
    for tag in path:
        tag = _v4_tag_name(tag)
        if "}" in tag:
            return None
        keys.append(tag)
    return tuple(keys)


def _v4_namespace_free_subtree(element):
    return all(
        "}" not in _v4_tag_name(descendant.tag)
        and all("}" not in str(name) for name in descendant.attrib)
        for descendant in element.iter()
    )


def _v4_has_significant_text_or_tail(element):
    return bool(
        str(element.text or "").strip()
        or str(element.tail or "").strip()
    )


def _v4_has_significant_tail(element):
    return bool(str(element.tail or "").strip())


def _v4_generator_guid_spelling(value):
    """Canonicalize only the proven truncated/padded spelling difference."""
    text = str(value or "")
    if text != text.strip():
        return text
    return canonical_generator_guid(text)


def _v4_direct_child_text(element, name):
    matches = [child for child in element if _v4_plain_tag(child, name)]
    if len(matches) != 1:
        return None
    child = matches[0]
    if child.attrib or list(child):
        return None
    return str(child.text or "")


def _v4_physics_bones_spline_property(element, path_key):
    return bool(
        path_key in _AUTHORING_GRAPH_V4_SPLINE_PROPERTY_PATHS
        and _v4_plain_tag(element, "SplineProperty")
        and not element.attrib
        and _v4_direct_child_text(element, "Name")
        in _AUTHORING_GRAPH_V4_SPLINE_PROPERTY_NAMES
        and sum(_v4_plain_tag(child, "Value") for child in element) <= 1
    )


def _v4_random_seed_style_property(element, path_key):
    children = list(element)
    return bool(
        path_key == _AUTHORING_GRAPH_V4_RANDOM_SEED_PROPERTY_PATH
        and _v4_plain_tag(element, "Property")
        and not element.attrib
        and len(children) == 2
        and _v4_plain_tag(children[0], "Name")
        and _v4_plain_tag(children[1], "Value")
        and not any(child.attrib or list(child) for child in children)
        and str(children[0].text or "") == "Random Seeds:Style"
    )


def _v4_default_modeler_lod(element):
    children = list(element)
    return bool(
        not element.attrib
        and not _v4_has_significant_text_or_tail(element)
        and len(children) == 1
        and _v4_plain_tag(children[0], "Filename")
        and not children[0].attrib
        and not list(children[0])
        and not _v4_has_significant_text_or_tail(children[0])
    )


def _v4_default_parent_spline(element):
    if (
        set(element.attrib) != {"Count"}
        or element.attrib.get("Count") not in {"0", "1"}
        or _v4_has_significant_text_or_tail(element)
    ):
        return False
    children = list(element)
    if element.attrib["Count"] == "0":
        return not children
    if len(children) != 1 or not _v4_plain_tag(children[0], "Spline"):
        return False
    spline = children[0]
    if (
        spline.attrib != {"DrawMode": "false"}
        or _v4_has_significant_text_or_tail(spline)
        or len(spline) != 2
    ):
        return False
    expected = (
        ("0", "1", "1", "0", "0"),
        ("1", "1", "1", "0", "0"),
    )
    fields = ("X", "Y", "TangentX", "TangentY", "Length")
    for point, expected_values in zip(spline, expected):
        if (
            not _v4_plain_tag(point, "ControlPoint")
            or point.attrib
            or _v4_has_significant_text_or_tail(point)
            or len(point) != len(fields)
        ):
            return False
        for child, field, expected_value in zip(
            point, fields, expected_values
        ):
            if (
                not _v4_plain_tag(child, field)
                or child.attrib
                or list(child)
                or _v4_has_significant_tail(child)
                or str(child.text or "") != expected_value
            ):
                return False
    return True


def _v4_default_modeler_atlas_maker(element):
    expected = {
        "Weight": "0.5",
        "FullHeight": "false",
        "UpdateMeshes": "true",
        "MakeUvArea": "false",
        "TranslationX": "0",
        "TranslationY": "0",
        "ScaleX": "1",
        "ScaleY": "1",
        "Rotation": "0",
    }
    return bool(
        element.attrib == expected
        and not list(element)
        and not _v4_has_significant_text_or_tail(element)
    )


def _v4_default_map_spline(element, length):
    if (
        not _v4_plain_tag(element, "Spline")
        or element.attrib != {"DrawMode": "false"}
        or _v4_has_significant_text_or_tail(element)
        or len(element) != 2
    ):
        return False
    fields = ("X", "Y", "TangentX", "TangentY", "Length")
    expected = (
        ("0", "0", "1", "0", length),
        ("1", "1", "1", "0", length),
    )
    for point, expected_values in zip(element, expected):
        if (
            not _v4_plain_tag(point, "ControlPoint")
            or point.attrib
            or _v4_has_significant_text_or_tail(point)
            or len(point) != len(fields)
        ):
            return False
        for child, field, expected_value in zip(
            point, fields, expected_values
        ):
            if (
                not _v4_plain_tag(child, field)
                or child.attrib
                or list(child)
                or _v4_has_significant_tail(child)
                or str(child.text or "") != expected_value
            ):
                return False
    return True


def _v4_default_map_generate(element):
    if (
        not _v4_plain_tag(element, "Generate")
        or element.attrib != {"Type": "0"}
        or _v4_has_significant_text_or_tail(element)
    ):
        return False
    expected = (
        (
            "File",
            {"ColorHigh": "ffffffff", "ColorLow": "ff000000", "Remap": "0"},
            "0",
        ),
        (
            "Linear",
            {
                "Angle": "90",
                "CenterX": "0",
                "CenterY": "0",
                "ColorHigh": "ffffffff",
                "ColorLow": "ff000000",
                "Distance": "1",
            },
            "0.44999998807907104",
        ),
        (
            "Radial",
            {
                "CenterX": "0.5",
                "CenterY": "0.5",
                "ColorHigh": "ffffffff",
                "ColorLow": "ff000000",
                "Distance": "0.5",
            },
            "0.44999998807907104",
        ),
        (
            "Noise",
            {
                "CenterX": "0.5",
                "CenterY": "0.5",
                "ColorHigh": "ffffffff",
                "ColorLow": "ff000000",
                "Scale": "1",
            },
            "0.44999998807907104",
        ),
    )
    if len(element) != len(expected):
        return False
    for child, (tag, attributes, length) in zip(element, expected):
        if not _v4_plain_tag(child, tag):
            return False
        observed = dict(child.attrib)
        if tag == "Noise":
            seed = observed.pop("Seed", None)
            if seed is None or not re.fullmatch(r"[+-]?\d+", seed):
                return False
        if (
            observed != attributes
            or _v4_has_significant_text_or_tail(child)
            or len(child) != 1
            or not _v4_default_map_spline(child[0], length)
        ):
            return False
    return True


def _v4_default_material_map(element):
    map_name = element.attrib.get("Name")
    specific = {
        "Specular": {
            "ColorX": "0.75",
            "ColorY": "0.75",
            "ColorZ": "0.75",
            "TexSource": "0",
            "TexToLinear": "true",
        },
        "Metallic": {
            "ColorX": "0",
            "ColorY": "0",
            "ColorZ": "0",
            "TexSource": "1",
            "TexToLinear": "false",
        },
        "Custom": {
            "ColorX": "0",
            "ColorY": "0",
            "ColorZ": "0",
            "TexSource": "0",
            "TexToLinear": "true",
        },
        "Custom2": {
            "ColorX": "0",
            "ColorY": "0",
            "ColorZ": "0",
            "TexSource": "0",
            "TexToLinear": "false",
        },
    }.get(map_name)
    if (
        not _v4_plain_tag(element, "Map")
        or element.attrib != {"Name": map_name}
        or specific is None
        or _v4_has_significant_text_or_tail(element)
    ):
        return False
    expected_scalars = (
        ("ColorX", specific["ColorX"]),
        ("ColorY", specific["ColorY"]),
        ("ColorZ", specific["ColorZ"]),
        ("TexFilename", ""),
        ("TexSource", specific["TexSource"]),
        ("TexBrightness", "0"),
        ("TexContrast", "0"),
        ("TexSaturation", "0"),
        ("TexRed", "0"),
        ("TexGreen", "0"),
        ("TexBlue", "0"),
        ("TexMin", "0"),
        ("TexMax", "1"),
        ("TexEnabled", "true"),
        ("TexToLinear", specific["TexToLinear"]),
        ("TexInvert", "false"),
        ("TexInvertRed", "false"),
        ("TexInvertGreen", "false"),
        ("TexInvertBlue", "false"),
        ("Normalize", "false"),
        ("TexSizeX", "0"),
        ("TexSizeY", "0"),
    )
    expected_tags = tuple(name for name, _value in expected_scalars) + (
        "Generate",
    )
    children = list(element)
    if tuple(_v4_tag_name(child.tag) for child in children) != expected_tags:
        return False
    for child, (_name, expected_value) in zip(
        children[:-1], expected_scalars
    ):
        if (
            child.attrib
            or list(child)
            or _v4_has_significant_tail(child)
            or str(child.text or "") != expected_value
        ):
            return False
    return _v4_default_map_generate(children[-1])


def _v4_float32_token(value):
    raw_text = str(value or "")
    text = raw_text.strip()
    if raw_text != text:
        return raw_text
    if not re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
        text,
    ):
        return text
    try:
        number = float(text)
        if not math.isfinite(number):
            return text
        return "float32:" + struct.pack("!f", number).hex()
    except (OverflowError, TypeError, ValueError):
        return text


def _v4_generated_collection_default(element, prefix):
    if (
        not _v4_plain_tag(element, "Property")
        or element.attrib
        or _v4_has_significant_text_or_tail(element)
    ):
        return False
    children = list(element)
    if len(children) != 2 or not (
        _v4_plain_tag(children[0], "Name")
        and _v4_plain_tag(children[1], "Value")
    ):
        return False
    if any(
        child.attrib or list(child) or _v4_has_significant_tail(child)
        for child in children
    ):
        return False
    name = str(children[0].text or "")
    value = str(children[1].text or "")
    return bool(name.startswith(prefix) and name != prefix and value == "false")


def _v5_exact_default_spline(element, tag, points):
    if (
        not _v4_plain_tag(element, tag)
        or element.attrib != {"DrawMode": "false"}
        or _v4_has_significant_text_or_tail(element)
        or len(element) != len(points)
    ):
        return False
    fields = ("X", "Y", "TangentX", "TangentY", "Length")
    for point, expected_values in zip(element, points):
        if (
            not _v4_plain_tag(point, "ControlPoint")
            or point.attrib
            or _v4_has_significant_text_or_tail(point)
            or len(point) != len(fields)
        ):
            return False
        for child, field, expected_text in zip(
            point,
            fields,
            expected_values,
        ):
            if (
                not _v4_plain_tag(child, field)
                or child.attrib
                or list(child)
                or _v4_has_significant_tail(child)
                or str(child.text or "") != expected_text
            ):
                return False
    return True


def _v5_default_disabled_planar_2(element):
    if (
        not _v4_plain_tag(element, "SplineProperty")
        or element.attrib
        or _v4_has_significant_text_or_tail(element)
    ):
        return False
    children = list(element)
    expected_scalars = (
        ("Name", "Forces:Planar 2"),
        ("Value", "0.25"),
        ("Variance", "0"),
        ("Enabled", "false"),
        ("CohesionScale", "1"),
        ("CohesionOffset", "0"),
        ("Distribution", "0"),
        ("ForceBehaviorID", "-1"),
        ("Relative", "true"),
    )
    if len(children) != len(expected_scalars) + 2:
        return False
    for child, (expected_tag, expected_text) in zip(
        children,
        expected_scalars,
    ):
        if (
            not _v4_plain_tag(child, expected_tag)
            or child.attrib
            or list(child)
            or _v4_has_significant_tail(child)
            or str(child.text or "") != expected_text
        ):
            return False
    compound_parent = children[-2]
    if (
        not _v4_plain_tag(compound_parent, "CompoundParentSpline")
        or compound_parent.attrib != {"Count": "1"}
        or _v4_has_significant_text_or_tail(compound_parent)
        or len(compound_parent) != 1
        or not _v5_exact_default_spline(
            compound_parent[0],
            "Spline",
            (
                ("0", "1", "1", "0", "0"),
                ("1", "1", "1", "0", "0"),
            ),
        )
    ):
        return False
    return _v5_exact_default_spline(
        children[-1],
        "ProfileSpline",
        (
            ("0", "0", "1", "0", "0"),
            ("1", "1", "1", "0", "0"),
        ),
    )


def _v5_draw_flags_token(value):
    raw_text = str(value or "")
    text = raw_text.strip()
    if raw_text != text or not re.fullmatch(r"\d+", text):
        return raw_text
    try:
        value_without_view_bit = int(text, 10) & ~0x8
    except (TypeError, ValueError):
        return raw_text
    return f"draw-flags-without-view-bit-0x8:{value_without_view_bit}"


def _v6_asset_path_token(value, spm_directory):
    """Resolve one exact Modeler asset-path field without hiding retargets."""
    raw_text = str(value or "")
    text = raw_text.strip()
    if (
        spm_directory is None
        or not text
        or raw_text != text
        or "\x00" in text
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text)
    ):
        return raw_text
    candidate = Path(text.replace("/", os.sep))
    if not candidate.is_absolute():
        candidate = Path(spm_directory) / candidate
    return "modeler-asset-path:" + canonical_path_key(candidate)


def _v4_generated_atlas_mesh_user_data(element):
    if (
        not _v4_plain_tag(element, "UserData")
        or element.attrib
        or list(element)
    ):
        return False
    try:
        payload = json.loads(str(element.text or "").strip())
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(payload, dict)
        and set(payload) == {"generator", "group", "kind", "scope"}
        and payload.get("generator") == "Atlas Leaf Mesh Builder"
        and payload.get("kind") == "mesh"
        and isinstance(payload.get("group"), str)
        and bool(payload["group"].strip())
        and isinstance(payload.get("scope"), str)
        and re.fullmatch(r"[0-9a-f]{32}", payload["scope"])
    )


def _v4_child_is_excluded(
    parent_path,
    child,
    siblings=None,
    *,
    projection_version=AUTHORING_GRAPH_CORE_PROJECTION_VERSION,
):
    siblings = tuple(siblings or ())
    parent_key = _v4_path_key(parent_path)
    child_tag = _v4_tag_name(child.tag)
    child_key = child_tag if "}" not in child_tag else None
    if parent_key == ("SpeedTree",):
        return bool(
            child_key in _AUTHORING_GRAPH_V4_EXCLUDED_ROOT_TAGS
            and not _v4_has_significant_tail(child)
        )
    if parent_key == ("SpeedTree", "Generators", "Generator", "Properties"):
        name = _v4_direct_child_text(child, "Name")
        collection_duplicate_count = sum(
            _v4_plain_tag(candidate, "Property")
            and _v4_direct_child_text(candidate, "Name") == name
            for candidate in siblings
        )
        if _v4_generated_collection_default(
            child,
            "Generation:Collections:",
        ) and collection_duplicate_count == 1:
            return True
        planar_duplicate_count = sum(
            _v4_plain_tag(candidate, "SplineProperty")
            and _v4_direct_child_text(candidate, "Name") == name
            for candidate in siblings
        )
        return bool(
            projection_version >= 5
            and _v5_default_disabled_planar_2(child)
            and planar_duplicate_count == 1
        )
    if parent_key in {
        ("SpeedTree", "Forces", "Force", "Properties"),
        ("SpeedTree", "Force", "Properties"),
    }:
        name = _v4_direct_child_text(child, "Name")
        duplicate_count = sum(
            _v4_plain_tag(candidate, "Property")
            and _v4_direct_child_text(candidate, "Name") == name
            for candidate in siblings
        )
        return (
            _v4_generated_collection_default(child, "Mesh:Collections:")
            and duplicate_count == 1
        )
    if parent_key == ("SpeedTree", "Assets", "Material_v8"):
        if child_key in {"Preview", "StreamPlaceholder"}:
            return not _v4_has_significant_tail(child)
        if (
            child_key == "AtlasMaker"
            and _v4_namespace_free_subtree(child)
            and _v4_default_modeler_atlas_maker(child)
        ):
            return sum(
                _v4_plain_tag(candidate, "AtlasMaker")
                for candidate in siblings
            ) == 1
        if (
            child_key == "Map"
            and _v4_namespace_free_subtree(child)
            and _v4_default_material_map(child)
        ):
            map_name = child.attrib.get("Name")
            return sum(
                _v4_plain_tag(candidate, "Map")
                and candidate.attrib.get("Name") == map_name
                for candidate in siblings
            ) == 1
    if parent_key == ("SpeedTree", "Assets", "Mesh"):
        if child_key == "UserData" and _v4_generated_atlas_mesh_user_data(child):
            return sum(
                _v4_plain_tag(candidate, "UserData")
                for candidate in siblings
            ) == 1
        if (
            child_key in {"Lod_1", "Lod_2"}
            and _v4_namespace_free_subtree(child)
            and _v4_default_modeler_lod(child)
        ):
            return sum(
                _v4_plain_tag(candidate, child_key)
                for candidate in siblings
            ) == 1
    if (
        parent_key
        == (
            "SpeedTree",
            "Generators",
            "Generator",
            "Properties",
            "SplineProperty",
        )
        and child_key == "CompoundParentSpline"
        and _v4_namespace_free_subtree(child)
        and _v4_default_parent_spline(child)
    ):
        return sum(
            _v4_plain_tag(candidate, "CompoundParentSpline")
            for candidate in siblings
        ) == 1
    return False


def _v4_ordered_children(
    element,
    path,
    *,
    projection_version=AUTHORING_GRAPH_CORE_PROJECTION_VERSION,
):
    siblings = list(element)
    children = [
        child for child in siblings
        if not _v4_child_is_excluded(
            path,
            child,
            siblings,
            projection_version=projection_version,
        )
    ]
    if _v4_path_key(path) == ("SpeedTree", "Assets"):
        # A no-edit Modeler Save stably partitions direct Assets children by
        # kind. Unknown/future children are a fail-closed barrier: if any are
        # present, preserve the complete direct-child order unchanged.
        known_tags = {
            "Material_v8", "Mesh", "Roughness",
            "GUID", "Name", "Hidden", "Properties",
        }
        if any(_v4_tag_name(child.tag) not in known_tags for child in children):
            return children
        materials = [
            child for child in children if _v4_plain_tag(child, "Material_v8")
        ]
        meshes = [child for child in children if _v4_plain_tag(child, "Mesh")]
        others = [
            child for child in children
            if not _v4_plain_tag(child, "Material_v8")
            and not _v4_plain_tag(child, "Mesh")
        ]
        return materials + meshes + others
    return children


def _v4_projected_subtree(
    element,
    path=(),
    *,
    truthy_value=False,
    physics_bones_context=False,
    projection_version=AUTHORING_GRAPH_CORE_PROJECTION_VERSION,
    spm_directory=None,
):
    element_tag = _v4_tag_name(element.tag)
    path = tuple(path) + (element_tag,)
    path_key = _v4_path_key(path)
    tag_key = element_tag if "}" not in element_tag else None
    raw_text = str(element.text or "")
    text = "" if list(element) and not raw_text.strip() else raw_text
    if path_key in _AUTHORING_GRAPH_V4_GENERATED_GUID_PATHS:
        text = "<modeler-generated-guid>"
    elif path_key in _AUTHORING_GRAPH_V4_CANONICAL_GUID_PATHS:
        text = _v4_generator_guid_spelling(text)
    elif (
        projection_version >= 5
        and path_key == ("SpeedTree", "DrawFlags8", "DrawFlags")
    ):
        text = _v5_draw_flags_token(text)
    elif (
        projection_version >= 6
        and path_key in _AUTHORING_GRAPH_V6_ASSET_PATH_TEXT_PATHS
    ):
        text = _v6_asset_path_token(text, spm_directory)
    elif path_key in {
        (
            "SpeedTree", "Generators", "Generator", "Extra",
            "m_nOrderValue",
        ),
    }:
        text = "<modeler-graph-order>"
    elif path_key in {
        (
            "SpeedTree", "Generators", "Generator", "Extra",
            "m_vecBackgroundIconColor_r",
        ),
        (
            "SpeedTree", "Generators", "Generator", "Extra",
            "m_vecBackgroundIconColor_g",
        ),
        (
            "SpeedTree", "Generators", "Generator", "Extra",
            "m_vecBackgroundIconColor_b",
        ),
    }:
        text = _v4_float32_token(text)
    elif (
        physics_bones_context
        and path_key
        and path_key[:-1] in _AUTHORING_GRAPH_V4_SPLINE_PROPERTY_PATHS
        and path_key[-1] == "Value"
    ):
        text = _v4_float32_token(text)
    elif (
        physics_bones_context
        and path_key
        and (
            path_key[:-3] in _AUTHORING_GRAPH_V4_SPLINE_PROPERTY_PATHS
            and path_key[-3:-1] == ("ProfileSpline", "ControlPoint")
            or path_key[:-4] in _AUTHORING_GRAPH_V4_SPLINE_PROPERTY_PATHS
            and path_key[-4:-1]
            == ("CompoundParentSpline", "Spline", "ControlPoint")
        )
        and tag_key in _AUTHORING_GRAPH_V4_SPLINE_NUMBER_TAGS
    ):
        text = (
            _normalized_spline_number(text)
            if text == text.strip()
            else text
        )
    elif path_key == ("SpeedTree", "Assets", "Mesh", "Scale"):
        text = _v4_float32_token(text)
    elif (
        path_key
        in {
            ("SpeedTree", "Assets", "Material_v8", "Map", "TexSizeX"),
            ("SpeedTree", "Assets", "Material_v8", "Map", "TexSizeY"),
        }
    ):
        text = "<derived-texture-size>"
    elif truthy_value and tag_key == "Value":
        try:
            text = "0" if float(text) == 0 else "1"
        except (TypeError, ValueError):
            pass

    attributes = []
    for name, value in element.attrib.items():
        attributes.append((str(name), str(value)))

    child_truthy = _v4_random_seed_style_property(element, path_key)
    child_physics_bones = (
        physics_bones_context
        or _v4_physics_bones_spline_property(element, path_key)
    )
    projected = {
        "tag": element_tag,
        "attributes": sorted(attributes),
        "text": text,
        "children": [
            _v4_projected_subtree(
                child,
                path,
                truthy_value=child_truthy,
                physics_bones_context=child_physics_bones,
                projection_version=projection_version,
                spm_directory=spm_directory,
            )
            for child in _v4_ordered_children(
                element,
                path,
                projection_version=projection_version,
            )
        ],
    }
    tail = str(element.tail or "")
    if tail.strip():
        projected["tail"] = tail
    return projected


def _authoring_graph_core_projection_for_version(
    text,
    projection_version,
    *,
    spm_path=None,
):
    """Project the authored XML tree under one immutable core dialect."""
    root = _v4_parse(text)
    projected = _v4_projected_subtree(
        root,
        projection_version=projection_version,
        spm_directory=(
            Path(spm_path).expanduser().resolve(strict=False).parent
            if projection_version >= 6 and spm_path is not None
            else None
        ),
    )
    root_children = [
        child for child in root
        if not _v4_child_is_excluded(
            (_v4_tag_name(root.tag),),
            child,
            projection_version=projection_version,
        )
    ]
    generators = next(
        (child for child in root_children if _v4_plain_tag(child, "Generators")),
        None,
    )
    links = next(
        (child for child in root_children if _v4_plain_tag(child, "Links")),
        None,
    )
    assets = next(
        (child for child in root_children if _v4_plain_tag(child, "Assets")),
        None,
    )
    generator_count = sum(
        _v4_plain_tag(child, "Generator") for child in (generators or ())
    )
    link_count = sum(_v4_plain_tag(child, "Link") for child in (links or ()))
    asset_identity_count = sum(
        not any(_v4_plain_tag(child, name) for name in (
            "Name", "GUID", "Hidden", "Properties",
        ))
        for child in (assets or ())
    )
    global_setting_count = sum(
        not any(_v4_plain_tag(child, name) for name in (
            "Generators", "Links", "Assets",
        ))
        for child in root_children
    )
    rows = {"root": projected}
    return {
        "contract": "speedtree_spm_authoring_graph_core_projection",
        "version": projection_version,
        "generator_count": generator_count,
        "link_count": link_count,
        "asset_identity_count": asset_identity_count,
        "global_setting_count": global_setting_count,
        "fingerprint": _json_fingerprint(rows),
        "_rows": rows,
    }


def _legacy_authoring_graph_core_v4_projection(text):
    """Reproduce immutable core-v4 for historical schema-6 receipts."""
    return _authoring_graph_core_projection_for_version(text, 4)


def _authoring_graph_core_projection(text, *, spm_path=None):
    """Project the full authored XML tree under current fail-closed rules."""
    return _authoring_graph_core_projection_for_version(
        text,
        AUTHORING_GRAPH_CORE_PROJECTION_VERSION,
        spm_path=spm_path,
    )


def _legacy_authoring_graph_core_v2_subtree(
    element,
    *,
    depth=0,
    spline_context=False,
    truthy_value=False,
):
    """Reproduce the sealed v2 projection without widening current policy."""
    tag = _local_name(element.tag)
    tag_key = tag.casefold()
    spline_context = spline_context or tag_key == "splineproperty"
    text = str(element.text or "").strip()
    if tag_key.endswith("guid"):
        text = generator_guid_key(text)
    elif truthy_value and tag_key == "value":
        try:
            text = "0" if float(text) == 0 else "1"
        except (TypeError, ValueError):
            pass
    elif spline_context:
        text = _normalized_spline_number(text)

    attributes = []
    for name, value in element.attrib.items():
        name_key = _local_name(name).casefold()
        if name_key == "m_nordervalue":
            continue
        normalized = str(value).strip()
        if name_key.endswith("guid"):
            normalized = generator_guid_key(normalized)
        elif spline_context:
            normalized = _normalized_spline_number(normalized)
        attributes.append((_local_name(name), normalized))

    property_name = (
        _child_text(element, "Name")
        if tag_key in {"property", "splineproperty"}
        else ""
    )
    child_truthy_value = property_name.casefold() == "random seeds:style"
    children = []
    for child in element:
        child_key = _local_name(child.tag).casefold()
        if depth == 0 and child_key == "nodes":
            continue
        if child_key in _AUTHORING_GRAPH_CORE_IGNORED_SUBTREE_TAGS:
            continue
        if child_key == "m_nordervalue":
            continue
        if child_key in {"property", "splineproperty"} and _child_text(
            child, "Name"
        ).casefold().startswith("generation:collections:"):
            continue
        if (
            spline_context
            and child_key == "compoundparentspline"
            and _default_or_empty_parent_spline(child)
        ):
            continue
        children.append(_legacy_authoring_graph_core_v2_subtree(
            child,
            depth=depth + 1,
            spline_context=spline_context,
            truthy_value=child_truthy_value,
        ))
    return {
        "tag": tag,
        "attributes": sorted(attributes),
        "text": text,
        "children": children,
    }


def _legacy_authoring_graph_core_v2_projection(text):
    root = ET.fromstring(text)
    generators = []
    links = []
    assets = []
    for element in root.iter():
        tag = _local_name(element.tag).casefold()
        if tag == "generator":
            generator = copy.deepcopy(element)
            for child in list(generator):
                if _local_name(child.tag).casefold() == "extra":
                    generator.remove(child)
            generators.append(
                _legacy_authoring_graph_core_v2_subtree(generator, depth=1)
            )
        elif tag == "link":
            links.append({
                "source": generator_guid_key(_child_text(element, "SourceGUID")),
                "target": generator_guid_key(_child_text(element, "TargetGUID")),
            })
        elif tag in {"material_v8", "mesh"}:
            assets.append({
                "tag": _local_name(element.tag),
                "id": str(element.attrib.get("ID") or "").strip(),
                "name": str(
                    element.attrib.get("Name") or _child_text(element, "Name")
                ).strip(),
            })
    generators.sort(key=_canonical_json_bytes)
    links.sort(key=_canonical_json_bytes)
    assets.sort(key=_canonical_json_bytes)
    rows = {
        "generators": generators,
        "links": links,
        "assets": assets,
    }
    return {
        "contract": "speedtree_spm_authoring_graph_core_projection",
        "version": 2,
        "generator_count": len(generators),
        "link_count": len(links),
        "asset_identity_count": len(assets),
        "fingerprint": _json_fingerprint(rows),
        "_rows": rows,
    }


def _elementtree_node_evidence(text):
    """Independent Node/Generator counts from the same immutable XML text."""
    root = ET.fromstring(text)
    generator_guids = set()
    eligible = {}
    total = 0
    for element in root.iter():
        tag = _local_name(element.tag).casefold()
        if tag == "generator":
            guid = generator_guid_key(_child_text(element, "GUID"))
            if guid:
                generator_guids.add(guid)
        elif tag == "node":
            total += 1
            guid = generator_guid_key(_child_text(element, "GeneratorGUID"))
            hidden = _truthy(_child_text(element, "Hidden"))
            deleted = False
            culled = False
            for descendant in element.iter():
                descendant_tag = _local_name(descendant.tag).casefold()
                if descendant_tag == "m_bdeleted":
                    deleted = _truthy("".join(descendant.itertext()))
                elif descendant_tag == "m_bculled":
                    culled = _truthy("".join(descendant.itertext()))
            if guid and not hidden and not deleted and not culled:
                eligible[guid] = eligible.get(guid, 0) + 1
    orphan_guids = sorted(set(eligible) - generator_guids)
    return {
        "total_node_count": total,
        "generator_count": len(generator_guids),
        "eligible_owner_count": len(eligible),
        "eligible_node_count": sum(eligible.values()),
        "orphan_owner_count": len(orphan_guids),
        "orphan_node_count": sum(eligible[guid] for guid in orphan_guids),
        "eligible_owner_counts_fingerprint": _json_fingerprint(
            sorted(eligible.items())
        ),
        "generator_membership_fingerprint": _json_fingerprint(
            sorted(generator_guids)
        ),
        "_eligible_counts": eligible,
        "_generator_guids": generator_guids,
    }


def _projected_target_binding(row, *, canonical_guid=True):
    guid = str(row.get("generator_guid") or "").strip()
    return {
        "generator_guid": (
            generator_guid_key(guid) if canonical_guid else guid.casefold()
        ),
        "generator_type": str(row.get("generator_type") or "").strip(),
        "generator_name": str(row.get("generator_name") or "").strip(),
        "slot_prefix": str(row.get("slot_prefix") or "").strip(),
        "material_property": str(row.get("material_property") or "").strip(),
        "material_id": _mesh_id(row.get("material_id")),
        "mesh_property": str(row.get("mesh_property") or "").strip(),
        "mesh_id": _mesh_id(row.get("mesh_id")),
    }


def _target_binding_projection_v2(snapshot, expected_mesh_ids):
    """Reproduce the sealed target-v2 projection and authoritative fields."""
    requested = sorted({_mesh_id(value) for value in expected_mesh_ids} - {None})
    requested_set = set(requested)
    rows = []
    live_rows = []
    graph_visible_rows = []
    for row in snapshot.get("leaf_generator_bindings") or []:
        mesh_id = _mesh_id(row.get("mesh_id"))
        if mesh_id not in requested_set:
            continue
        projected = _projected_target_binding(row)
        rows.append(projected)
        if row.get("graph_visible") is True:
            graph_visible_rows.append(projected)
            if row.get("export_participates") is True:
                live_rows.append(projected)
    rows.sort(key=lambda row: _canonical_json_bytes(row))
    live_rows.sort(key=lambda row: _canonical_json_bytes(row))
    graph_visible_rows.sort(key=lambda row: _canonical_json_bytes(row))
    all_authoring = sorted({row["mesh_id"] for row in rows})
    authoring = sorted({row["mesh_id"] for row in graph_visible_rows})
    live = sorted({row["mesh_id"] for row in live_rows})
    missing = sorted(set(requested) - set(authoring))
    missing_authoring = sorted(set(requested) - set(all_authoring))
    return {
        "version": TARGET_BINDING_PROJECTION_VERSION,
        "requested_mesh_ids": requested,
        "expected_target_mesh_ids": authoring,
        "observed_target_mesh_ids": authoring,
        "live_export_target_mesh_ids": live,
        "missing_requested_mesh_ids": missing,
        "all_binding_target_mesh_ids": all_authoring,
        "missing_authoring_mesh_ids": missing_authoring,
        "binding_count": len(rows),
        "live_binding_count": len(live_rows),
        "complete": bool(requested and authoring == requested),
        "authoring_complete": bool(requested and all_authoring == requested),
        "fingerprint": _json_fingerprint(rows),
        "_rows": rows,
        "_live_rows": live_rows,
        "_graph_visible_rows": graph_visible_rows,
    }


def _target_binding_projection(snapshot, expected_mesh_ids):
    """Return the current target-binding projection (target-v2)."""
    return _target_binding_projection_v2(snapshot, expected_mesh_ids)


def _legacy_target_binding_fingerprint(snapshot, expected_mesh_ids):
    snapshot = snapshot.get("delivery", snapshot)
    expected = sorted({_mesh_id(value) for value in expected_mesh_ids} - {None})
    expected_set = set(expected)
    rows = [
        _projected_target_binding(row, canonical_guid=False)
        for row in snapshot.get("leaf_generator_bindings") or []
        if _mesh_id(row.get("mesh_id")) in expected_set
    ]
    rows.sort(key=_canonical_json_bytes)
    return _json_fingerprint(rows)


def _legacy_target_binding_fingerprints(snapshot, expected_mesh_ids):
    """Return the frozen schema-2 target-v1 fingerprint set."""
    return {
        candidate["fingerprint"]
        for candidate in _schema2_target_binding_v1_candidates(
            snapshot,
            expected_mesh_ids,
        )
    }


def _schema2_target_binding_v1_candidates(snapshot, expected_mesh_ids):
    """Reproduce schema-2 target-v1: every requested row and raw GUID spelling."""
    snapshot = snapshot.get("delivery", snapshot)
    expected = sorted({_mesh_id(value) for value in expected_mesh_ids} - {None})
    expected_set = set(expected)
    rows = [
        _projected_target_binding(row, canonical_guid=False)
        for row in snapshot.get("leaf_generator_bindings") or []
        if _mesh_id(row.get("mesh_id")) in expected_set
    ]
    rows.sort(key=_canonical_json_bytes)
    return [{
        "fingerprint": _json_fingerprint(rows),
        "binding_count": len(rows),
        "expected_mesh_ids": expected,
    }]


def _schema3_target_binding_v1_candidates(snapshot, expected_mesh_ids):
    """Reproduce schema-3/core-2 target-v1: visible rows and canonical GUIDs."""
    snapshot = snapshot.get("delivery", snapshot)
    requested = sorted({_mesh_id(value) for value in expected_mesh_ids} - {None})
    requested_set = set(requested)
    rows = [
        _projected_target_binding(row, canonical_guid=True)
        for row in snapshot.get("leaf_generator_bindings") or []
        if _mesh_id(row.get("mesh_id")) in requested_set
        and row.get("graph_visible") is True
    ]
    rows.sort(key=_canonical_json_bytes)
    projected = sorted({row["mesh_id"] for row in rows})
    return [{
        "fingerprint": _json_fingerprint(rows),
        "binding_count": len(rows),
        "expected_mesh_ids": projected,
    }]


def _generator_membership_projection_v1(snapshot, _expected_mesh_ids=()):
    """Recompute membership-v1 independently from the exact backup text."""
    evidence = _elementtree_node_evidence(snapshot["text"])
    return [{
        "fingerprint": evidence["generator_membership_fingerprint"],
        "count": evidence["generator_count"],
    }]


def _schema2_generator_membership_projection_v1(snapshot, _expected_mesh_ids=()):
    """Reproduce schema-2 membership-v1 from raw serialized GUID spelling."""
    root = ET.fromstring(snapshot["text"])
    guids = set()
    for element in root.iter():
        if _local_name(element.tag).casefold() != "generator":
            continue
        raw_guid = str(_child_text(element, "GUID") or "").strip().casefold()
        if raw_guid:
            guids.add(raw_guid)
    return [{
        "fingerprint": _json_fingerprint(sorted(guids)),
        "count": len(guids),
    }]


def _authoring_graph_projection_v1_candidates(snapshot, _expected_mesh_ids=()):
    return [{
        "fingerprint": _authoring_graph_projection_v1(snapshot["text"]),
    }]


def _authoring_graph_core_v2_candidates(snapshot, _expected_mesh_ids=()):
    projection = _legacy_authoring_graph_core_v2_projection(snapshot["text"])
    return [{
        key: value for key, value in projection.items()
        if not key.startswith("_")
    }]


def _authoring_graph_core_v3_candidates(snapshot, _expected_mesh_ids=()):
    projection = _legacy_authoring_graph_core_v3_projection(snapshot["text"])
    return [{
        key: value for key, value in projection.items()
        if not key.startswith("_")
    }]


def _authoring_graph_core_v4_candidates(snapshot, _expected_mesh_ids=()):
    projection = _legacy_authoring_graph_core_v4_projection(snapshot["text"])
    return [{
        key: value for key, value in projection.items()
        if not key.startswith("_")
    }]


def _authoring_graph_core_v5_candidates(snapshot, _expected_mesh_ids=()):
    projection = _authoring_graph_core_projection_for_version(
        snapshot["text"],
        5,
    )
    return [{
        key: value for key, value in projection.items()
        if not key.startswith("_")
    }]


def _authoring_graph_core_v6_candidates(snapshot, _expected_mesh_ids=()):
    projection = _authoring_graph_core_projection_for_version(
        snapshot["text"],
        6,
        spm_path=snapshot.get("spm"),
    )
    return [{
        key: value for key, value in projection.items()
        if not key.startswith("_")
    }]


def _target_binding_v1_candidates(snapshot, expected_mesh_ids):
    return _schema2_target_binding_v1_candidates(snapshot, expected_mesh_ids)


def _target_binding_v1_schema3_candidates(snapshot, expected_mesh_ids):
    return _schema3_target_binding_v1_candidates(snapshot, expected_mesh_ids)


def _target_binding_v2_candidates(snapshot, expected_mesh_ids):
    projection = _target_binding_projection_v2(
        snapshot.get("delivery", snapshot),
        expected_mesh_ids,
    )
    return [{
        "fingerprint": projection["fingerprint"],
        "binding_count": projection["binding_count"],
        "expected_mesh_ids": projection["expected_target_mesh_ids"],
        "missing_requested_mesh_ids": projection[
            "missing_requested_mesh_ids"
        ],
    }]


class _FrozenAudit:
    def __init__(self, snapshot):
        self.snapshot = copy.deepcopy(snapshot)

    def live_generator_delivery_snapshot(self, _path):
        return copy.deepcopy(self.snapshot)


def _normalization_evidence(snapshot, target_projection):
    """Exercise the production normalization classifier on frozen bytes."""
    rows = target_projection["_live_rows"]
    if not rows:
        node_table = snapshot.get("node_table") or {}
        if node_table.get("stale") is True:
            rows = target_projection["_graph_visible_rows"]
        else:
            return {
                "delivery_mode": "CONNECTION_INCOMPLETE",
                "delivery_decision": "blocked",
                "delivery_reason": "recovery_live_export_target_scope_empty",
                "errors": ["recovery_live_export_target_scope_empty"],
                "complete": False,
                "material_scope_count": 0,
            }
    material_ids = sorted({row["material_id"] for row in rows if row["material_id"]})
    if not material_ids or any(not row["material_id"] for row in rows):
        return {
            "delivery_mode": "CONNECTION_INCOMPLETE",
            "delivery_decision": "blocked",
            "delivery_reason": "recovery_target_material_scope_missing",
            "errors": ["recovery_target_material_scope_missing"],
            "complete": False,
        }
    scopes = []
    for material_id in material_ids:
        declared = []
        for row in rows:
            if row["material_id"] != material_id:
                continue
            declared_row = dict(row)
            declared_row["target_material_id"] = row["material_id"]
            declared_row["target_mesh_id"] = row["mesh_id"]
            declared.append(declared_row)
        mesh_ids = sorted({row["mesh_id"] for row in declared})
        payload = {
            "generator_connection": {
                "requested": True,
                "complete": True,
                "generator_variant_policy": "ensure_all_material_cutouts",
                "bindings": declared,
            }
        }
        evidence = _normalized_generator_delivery(
            _FrozenAudit(snapshot),
            snapshot["spm"],
            payload,
            {"material_id": material_id},
            [{"target_mesh_id": mesh_id} for mesh_id in mesh_ids],
        )
        scopes.append({
            "material_id": material_id,
            "target_mesh_ids": mesh_ids,
            "delivery_mode": evidence.get("delivery_mode"),
            "delivery_decision": evidence.get("delivery_decision"),
            "delivery_reason": evidence.get("delivery_reason"),
            "live_snapshot_sha256": evidence.get("live_snapshot_sha256"),
            "errors": list(evidence.get("errors") or []),
            "complete": bool(
                evidence.get("delivery_mode") == "render_connected"
                and evidence.get("delivery_decision") == "normalize_part"
                and evidence.get("generator_connection_complete") is True
                and not evidence.get("errors")
            ),
        })
    if len(scopes) == 1:
        return {**scopes[0], "material_scope_count": 1}
    complete = bool(scopes and all(scope["complete"] for scope in scopes))
    snapshot_hashes = {
        scope["live_snapshot_sha256"]
        for scope in scopes
        if scope["live_snapshot_sha256"]
    }
    return {
        "delivery_mode": "render_connected" if complete else "CONNECTION_INCOMPLETE",
        "delivery_decision": "normalize_part" if complete else "blocked",
        "delivery_reason": (
            "all_recovery_target_material_scopes_match_live_export"
            if complete
            else "recovery_target_material_scope_incomplete"
        ),
        "live_snapshot_sha256": (
            next(iter(snapshot_hashes)) if len(snapshot_hashes) == 1 else None
        ),
        "errors": sorted({
            error
            for scope in scopes
            for error in scope["errors"]
        }),
        "complete": complete,
        "material_scope_count": len(scopes),
        "material_scopes": scopes,
    }


def _required_live_normalization(snapshot, required_live_mesh_ids):
    required_live = _normalized_mesh_ids(required_live_mesh_ids)
    if not required_live:
        return {
            "applicable": False,
            "status": "not_required",
            "delivery_mode": "binding_continuity_only",
            "delivery_decision": "not_required",
            "delivery_reason": "sealed_policy_has_no_required_live_targets",
            "live_snapshot_sha256": snapshot.get("spm_text_sha256"),
            "errors": [],
            "material_scope_count": 0,
        }
    return {
        "applicable": True,
        **_normalization_evidence(
        snapshot,
        _target_binding_projection(snapshot, required_live),
        ),
    }


def _capture_immutable_snapshot(spm_path, expected_mesh_ids):
    """Capture stat+bytes+parse evidence without re-reading for sub-audits."""
    spm = Path(spm_path)
    try:
        before = spm.stat()
        raw_bytes = spm.read_bytes()
        after = spm.stat()
    except OSError as exc:
        raise StaleNodeTableRecoveryError(
            "source_snapshot_read_failed",
            "the operating SPM could not be captured",
            _source_identity(spm),
        ) from exc
    if _stat_identity(before) != _stat_identity(after) or len(raw_bytes) != after.st_size:
        raise StaleNodeTableRecoveryError(
            "source_changed_during_capture",
            "the operating SPM changed during immutable capture",
            _source_identity(spm),
        )
    try:
        text = _decode_spm_bytes(raw_bytes)
        ET.fromstring(text)
        delivery = generator_delivery_snapshot_from_spm_text(text, spm)
        regex_counts, regex_total = _export_node_counts_from_text(text)
        elementtree = _elementtree_node_evidence(text)
        target_projection = _target_binding_projection(delivery, expected_mesh_ids)
        normalization = _normalization_evidence(delivery, target_projection)
        authoring_fingerprint = _authoring_graph_projection_v1(text)
        authoring_core = _authoring_graph_core_projection(text, spm_path=spm)
    except (ET.ParseError, OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise StaleNodeTableRecoveryError(
            "source_snapshot_parse_failed",
            "the immutable SPM snapshot could not be parsed and audited",
            _source_identity(spm),
        ) from exc
    regex_summary = {
        "total_node_count": regex_total,
        "eligible_owner_count": len(regex_counts),
        "eligible_node_count": sum(regex_counts.values()),
        "eligible_owner_counts_fingerprint": _json_fingerprint(
            sorted(regex_counts.items())
        ),
    }
    parity = bool(
        regex_total == elementtree["total_node_count"]
        and regex_counts == elementtree["_eligible_counts"]
    )
    return {
        "spm": str(spm),
        "source_identity": _source_identity(spm),
        "raw_bytes": raw_bytes,
        "text": text,
        "raw_sha256": _sha256_bytes(raw_bytes),
        "text_sha256": delivery["spm_text_sha256"],
        "size": len(raw_bytes),
        "mtime_ns": int(after.st_mtime_ns),
        "stat_identity": _stat_identity(after),
        "quiescence_key": (
            int(after.st_size),
            int(after.st_mtime_ns),
            _sha256_bytes(raw_bytes),
        ),
        "delivery": delivery,
        "regex": regex_summary,
        "elementtree": elementtree,
        "regex_elementtree_parity": parity,
        "authoring_graph_fingerprint": authoring_fingerprint,
        "authoring_graph_core": authoring_core,
        "generator_membership_fingerprint": elementtree[
            "generator_membership_fingerprint"
        ],
        "target_projection": target_projection,
        "normalization": normalization,
    }


def _snapshot_with_path_resolution_source(snapshot, source_spm):
    """Bind backup bytes to their operating SPM base for core-v6 paths."""
    bound = dict(snapshot)
    bound["spm"] = str(Path(source_spm).expanduser().resolve(strict=False))
    bound["authoring_graph_core"] = _authoring_graph_core_projection(
        bound["text"],
        spm_path=bound["spm"],
    )
    return bound


def validate_repaired_snapshot(
    snapshot,
    expected_mesh_ids=(),
    required_live_mesh_ids=None,
):
    """Validate coherent Nodes plus sealed authoring and live scopes."""
    expected = _normalized_mesh_ids(expected_mesh_ids)
    required_live = (
        list(expected)
        if required_live_mesh_ids is None
        else _normalized_mesh_ids(required_live_mesh_ids)
    )
    required_live_is_subset = set(required_live).issubset(expected)
    errors = []
    node_table = snapshot.get("node_table") or {}
    if node_table.get("stale") is not False:
        errors.append("node_table_still_stale")
    if int(node_table.get("orphan_node_count") or 0):
        errors.append("orphan_nodes_remain")
    if node_table.get("orphan_generator_guids"):
        errors.append("orphan_owners_remain")

    expected_set = set(expected)
    authoring_rows = [
        dict(row)
        for row in snapshot.get("leaf_generator_bindings") or []
        if _mesh_id(row.get("mesh_id")) in expected_set
    ]
    observed = sorted({
        _mesh_id(row.get("mesh_id")) for row in authoring_rows
    } - {None})
    required_live_set = set(required_live)
    target_rows = [
        row for row in authoring_rows
        if _mesh_id(row.get("mesh_id")) in required_live_set
        and row.get("graph_visible") is True
    ]
    live = sorted({
        _mesh_id(row.get("mesh_id"))
        for row in target_rows
        if row.get("export_participates") is True
    } - {None})
    if not expected:
        errors.append("expected_target_mesh_ids_missing")
    if not required_live_is_subset:
        errors.append("required_live_scope_not_authoring_subset")
    if observed != expected:
        errors.append("required_target_binding_missing")
    if live != required_live:
        errors.append("live_target_mesh_set_incomplete")
    rows_by_material_mesh_pair = {}
    for row in target_rows:
        pair = (
            _mesh_id(row.get("material_id")),
            _mesh_id(row.get("mesh_id")),
        )
        rows_by_material_mesh_pair.setdefault(pair, []).append(row)
    for pair_rows in rows_by_material_mesh_pair.values():
        participating = [
            row for row in pair_rows
            if row.get("export_participates") is True
        ]
        # Required-live delivery is a Material/Mesh-pair property.  Once one
        # sibling delivers the pair, zero-node siblings cannot make the FBX or
        # its normalized material incomplete.
        rows_to_validate = participating or pair_rows
        if not participating:
            errors.append("target_binding_not_export_participating")
        for row in rows_to_validate:
            if row.get("graph_visible") is not True:
                errors.append("target_binding_not_graph_visible")
            if int(row.get("generated_node_count") or 0) <= 0:
                errors.append("target_binding_has_no_eligible_nodes")
            if row.get("export_evidence") != "node_table":
                errors.append("target_binding_evidence_not_current_node_table")
            if row.get("node_table_stale") is not False:
                errors.append("target_binding_reports_stale_node_table")
    return {
        "contract": RECOVERY_CONTRACT,
        "valid": not errors,
        "errors": sorted(set(errors)),
        "spm_text_sha256": snapshot.get("spm_text_sha256"),
        "node_table": {
            "generator_count": node_table.get("generator_count"),
            "node_table_generator_count": node_table.get("node_table_generator_count"),
            "orphan_generator_guid_count": len(
                node_table.get("orphan_generator_guids") or []
            ),
            "orphan_node_count": int(node_table.get("orphan_node_count") or 0),
            "total_node_count": node_table.get("total_node_count"),
            "stale": node_table.get("stale"),
        },
        "expected_target_mesh_ids": expected,
        "required_live_target_mesh_ids": required_live,
        "observed_target_mesh_ids": observed,
        "live_export_participating_target_mesh_ids": live,
        "target_binding_count": len(authoring_rows),
        "required_live_binding_count": len(target_rows),
    }


def _preimage_target_scopes_complete(snapshot, target_scopes):
    if snapshot["target_projection"]["authoring_complete"] is not True:
        return False
    required_live = target_scopes["required_live_mesh_ids"]
    return bool(
        not required_live
        or _target_binding_projection(
            snapshot["delivery"],
            required_live,
        )["complete"]
    )


def _sealed_target_requirements(preimage_receipt, preimage_snapshot):
    receipt_target = preimage_receipt.get("required_target_bindings") or {}
    try:
        version = int(receipt_target.get("version") or 1)
    except (TypeError, ValueError):
        version = 0
    sealed_scopes = _receipt_target_scopes(preimage_receipt)
    if sealed_scopes is None:
        return None
    if version == TARGET_BINDING_PROJECTION_VERSION:
        return {
            "version": version,
            **sealed_scopes,
            "authoring_fingerprint": receipt_target.get("fingerprint"),
        }
    if (
        preimage_snapshot is None
        or preimage_snapshot.get("raw_sha256")
        != preimage_receipt.get("exact_preimage", {}).get("raw_sha256")
    ):
        return None
    target = preimage_snapshot["target_projection"]
    return {
        "version": version,
        **sealed_scopes,
        "authoring_fingerprint": target["fingerprint"],
    }


def _snapshot_gate(
    snapshot,
    preimage_receipt,
    expected_mesh_ids=(),
    *,
    authoring_mesh_ids=None,
    required_live_mesh_ids=None,
    preimage_snapshot=None,
):
    sealed_target = _sealed_target_requirements(
        preimage_receipt,
        preimage_snapshot,
    )
    caller_scopes, caller_scope_error = _resolve_target_scopes(
        expected_mesh_ids,
        authoring_mesh_ids=authoring_mesh_ids,
        required_live_mesh_ids=required_live_mesh_ids,
    )
    if sealed_target is None:
        sealed_authoring_mesh_ids = []
        sealed_required_live_mesh_ids = []
        expected_target_fingerprint = None
        errors = ["sealed_target_projection_preimage_unavailable"]
    else:
        sealed_authoring_mesh_ids = sealed_target["authoring_mesh_ids"]
        sealed_required_live_mesh_ids = sealed_target[
            "required_live_mesh_ids"
        ]
        expected_target_fingerprint = sealed_target["authoring_fingerprint"]
        errors = list(validate_repaired_snapshot(
            snapshot["delivery"],
            sealed_authoring_mesh_ids,
            sealed_required_live_mesh_ids,
        )["errors"])
    if caller_scope_error:
        errors.append(caller_scope_error)
    elif (
        sealed_authoring_mesh_ids != caller_scopes["authoring_mesh_ids"]
        or sealed_required_live_mesh_ids
        != caller_scopes["required_live_mesh_ids"]
    ):
        errors.append("sealed_target_scope_differs_from_caller")
    if snapshot["regex_elementtree_parity"] is not True:
        errors.append("regex_elementtree_node_evidence_mismatch")
    exact_authoring_continuity = bool(
        snapshot["authoring_graph_fingerprint"]
        == preimage_receipt["authoring_graph_projection"]["fingerprint"]
    )
    receipt_core = preimage_receipt.get("authoring_graph_core_projection")
    if (
        receipt_core is not None
        and receipt_core.get("version") == AUTHORING_GRAPH_CORE_PROJECTION_VERSION
    ):
        expected_core_fingerprint = receipt_core.get("fingerprint")
    elif (
        preimage_snapshot is not None
        and preimage_snapshot.get("raw_sha256")
        == preimage_receipt.get("exact_preimage", {}).get("raw_sha256")
    ):
        expected_core_fingerprint = preimage_snapshot[
            "authoring_graph_core"
        ]["fingerprint"]
    else:
        expected_core_fingerprint = None
        errors.append("authoring_graph_core_preimage_unavailable")
    authoring_core_continuity = bool(
        expected_core_fingerprint
        and snapshot["authoring_graph_core"]["fingerprint"]
        == expected_core_fingerprint
    )
    if not authoring_core_continuity:
        errors.append("authoring_graph_changed_during_resave")
    if (
        preimage_snapshot is not None
        and preimage_snapshot.get("raw_sha256")
        == preimage_receipt.get("exact_preimage", {}).get("raw_sha256")
    ):
        # Historical schema-2 membership-v1 sealed raw serialized GUID
        # spelling.  Post-save continuity uses the current canonical
        # membership derived from those exact backup bytes, never the legacy
        # receipt dialect as a mutable-state baseline.
        expected_membership_fingerprint = preimage_snapshot[
            "generator_membership_fingerprint"
        ]
    else:
        expected_membership_fingerprint = preimage_receipt[
            "generator_membership"
        ]["fingerprint"]
    if (
        snapshot["generator_membership_fingerprint"]
        != expected_membership_fingerprint
    ):
        errors.append("generator_membership_changed_during_resave")
    target_binding_continuity = bool(
        expected_target_fingerprint
        and snapshot["target_projection"]["fingerprint"]
        == expected_target_fingerprint
    )
    if not target_binding_continuity:
        errors.append("required_target_bindings_changed_during_resave")
    if snapshot["target_projection"]["authoring_complete"] is not True:
        errors.append("required_target_manifest_incomplete_after_resave")
    normalization = _required_live_normalization(
        snapshot["delivery"],
        sealed_required_live_mesh_ids,
    )
    if (
        sealed_required_live_mesh_ids
        and normalization.get("complete") is not True
    ):
        errors.append("normalization_evidence_not_complete")
    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "after_sha256": snapshot["text_sha256"],
        "after_raw_sha256": snapshot["raw_sha256"],
        "regex_elementtree_parity": snapshot["regex_elementtree_parity"],
        "authoring_graph_continuity": authoring_core_continuity,
        "authoring_graph_exact_projection_continuity": exact_authoring_continuity,
        "authoring_graph_core_projection_version": (
            AUTHORING_GRAPH_CORE_PROJECTION_VERSION
        ),
        "generator_membership_continuity": (
            snapshot["generator_membership_fingerprint"]
            == expected_membership_fingerprint
        ),
        "required_target_binding_continuity": (
            target_binding_continuity
        ),
        "sealed_authoring_mesh_ids": sealed_authoring_mesh_ids,
        "sealed_required_delivery_mesh_ids": sealed_required_live_mesh_ids,
        "target_delivery": validate_repaired_snapshot(
            snapshot["delivery"],
            sealed_authoring_mesh_ids,
            sealed_required_live_mesh_ids,
        ),
        "normalization": normalization,
    }


def _atomic_write_new(path, payload):
    """Write one new artifact atomically; never replace an existing target."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    data = payload if isinstance(payload, bytes) else _canonical_json_bytes(payload)
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if path.exists():
            raise FileExistsError(str(path))
        os.rename(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _acquire_session_lock(recovery_root, source_identity):
    lock = recovery_root / (
        "session." + source_identity["source_identity_sha256"][:24] + ".lock.json"
    )
    payload = {
        "kind": "speedtree_stale_node_table_recovery_session_lock",
        "schema_version": 1,
        **source_identity,
        "session_token": uuid.uuid4().hex,
    }
    try:
        _atomic_write_new(lock, payload)
    except FileExistsError as exc:
        raise StaleNodeTableRecoveryError(
            "recovery_session_already_active",
            "another recovery session or an interrupted session lock exists",
            source_identity,
        ) from exc
    return lock, payload["session_token"]


def _release_session_lock(lock, token):
    try:
        current = json.loads(lock.read_text(encoding="utf-8"))
        if current.get("session_token") == token:
            lock.unlink()
    except (OSError, UnicodeError, json.JSONDecodeError):
        # Never delete an unverified lock belonging to another/interrupted run.
        pass


def _preimage_receipt(
    snapshot,
    target_scopes,
    backup_name,
):
    dialect = _RECEIPT_DIALECTS[_CURRENT_RECEIPT_DIALECT_KEY]
    (
        schema_version,
        graph_version,
        core_version,
        membership_version,
        target_version,
        requirements_version,
    ) = dialect.versions
    target = snapshot["target_projection"]
    authoring = list(target_scopes["authoring_mesh_ids"])
    required_live = list(target_scopes["required_live_mesh_ids"])
    graph_payload = dialect.graph.candidate_projector(snapshot, authoring)[0]
    core_payload = dialect.core.candidate_projector(snapshot, authoring)[0]
    membership_payload = dialect.membership.candidate_projector(
        snapshot,
        authoring,
    )[0]
    target_payload = dialect.targets.candidate_projector(
        snapshot,
        authoring,
    )[0]
    required_live_projection = _target_binding_projection(
        snapshot["delivery"],
        required_live,
    )
    return {
        "kind": PREIMAGE_RECEIPT_KIND,
        "schema_version": schema_version,
        "recovery_contract": RECOVERY_CONTRACT,
        **snapshot["source_identity"],
        "exact_preimage": {
            "raw_sha256": snapshot["raw_sha256"],
            "spm_text_sha256": snapshot["text_sha256"],
            "size": snapshot["size"],
            "backup_file": backup_name,
            "backup_raw_sha256": snapshot["raw_sha256"],
            "source_spm": str(
                Path(snapshot["spm"]).expanduser().resolve(strict=False)
            ),
        },
        "authoring_graph_projection": {
            "contract": "speedtree_spm_authoring_graph_projection",
            "version": graph_version,
            "fingerprint": graph_payload["fingerprint"],
            "excluded": [
                "generated Nodes table",
                "Thumbnail",
                "ThumbnailSize",
                "Preview",
                "Statistics",
                "QuickSaveSettings2",
                "m_sTimelineData",
            ],
        },
        "authoring_graph_core_projection": core_payload,
        "generator_membership": {
            "contract": "speedtree_generator_membership_projection",
            "version": membership_version,
            "count": membership_payload["count"],
            "fingerprint": membership_payload["fingerprint"],
        },
        "required_target_bindings": {
            "contract": "speedtree_required_target_binding_projection",
            "version": target_version,
            "requested_mesh_ids": authoring,
            "expected_mesh_ids": target_payload["expected_mesh_ids"],
            "binding_count": target_payload["binding_count"],
            "fingerprint": target_payload["fingerprint"],
            "missing_requested_mesh_ids": target_payload[
                "missing_requested_mesh_ids"
            ],
        },
        "target_requirements": {
            "contract": "speedtree_stale_node_target_requirements",
            "version": requirements_version,
            "policy": dialect.target_scope_policy,
            "authoring_mesh_ids": authoring,
            "required_live_mesh_ids": required_live,
        },
        "same_preimage_evidence": {
            "regex_elementtree_parity": snapshot["regex_elementtree_parity"],
            "regex": dict(snapshot["regex"]),
            "elementtree": {
                key: value
                for key, value in snapshot["elementtree"].items()
                if not key.startswith("_")
            },
            "authoring_manifest_complete": target["authoring_complete"],
            "required_live_manifest_complete": bool(
                not required_live or required_live_projection["complete"]
            ),
            "normalization": _required_live_normalization(
                snapshot["delivery"],
                required_live,
            ),
        },
        "safety_boundary": {
            "modeler_save_automation": True,
            "modeler_save_automation_mode": (
                "exact_owned_pid_document_menu_uia_invoke"
            ),
            "modeler_process_kill": False,
            "ui_keystroke_simulation": False,
            "direct_spm_xml_mutation": False,
            "automatic_rollback": False,
            "stale_false_alone_allows_continuation": False,
        },
    }


def _strict_receipt_version(value):
    """Return one sealed positive integer version without coercion."""
    return value if type(value) is int and value > 0 else None


def _receipt_requested_mesh_ids(receipt_targets):
    if not isinstance(receipt_targets, dict):
        return None
    values = receipt_targets.get(
        "requested_mesh_ids",
        receipt_targets.get("expected_mesh_ids"),
    )
    if not isinstance(values, (list, tuple)):
        return None
    parsed = [_mesh_id(value) for value in values]
    if any(value is None for value in parsed) or len(set(parsed)) != len(parsed):
        return None
    return sorted(parsed)


def _receipt_target_scopes(receipt):
    targets = receipt.get("required_target_bindings")
    requested = _receipt_requested_mesh_ids(targets)
    if not requested:
        return None
    schema_version = _strict_receipt_version(receipt.get("schema_version"))
    requirements = receipt.get("target_requirements")
    if schema_version in {2, 3, 4}:
        if requirements is not None:
            return None
        return {
            "mode": TARGET_SCOPE_MODE_STRICT_LEGACY,
            "authoring_mesh_ids": requested,
            "required_live_mesh_ids": list(requested),
        }
    if schema_version not in {5, 6, 7, 8}:
        return None
    return _target_requirements_v1_scopes(requirements, requested)


def _target_requirements_v1_scopes(requirements, requested):
    """Validate the frozen requirements-v1 policy literally."""
    if not isinstance(requirements, dict):
        return None
    authoring = _receipt_requested_mesh_ids({
        "requested_mesh_ids": requirements.get("authoring_mesh_ids")
    })
    required_values = requirements.get("required_live_mesh_ids")
    if not isinstance(required_values, (list, tuple)):
        return None
    required_live = [_mesh_id(value) for value in required_values]
    if (
        any(value is None for value in required_live)
        or len(set(required_live)) != len(required_live)
    ):
        return None
    required_live = sorted(required_live)
    if not (
        requirements.get("contract")
        == "speedtree_stale_node_target_requirements"
        and _strict_receipt_version(requirements.get("version"))
        == 1
        and requirements.get("policy") == "explicit_sealed_scopes_v1"
        and authoring == requested
        and set(required_live).issubset(authoring)
    ):
        return None
    return {
        "mode": TARGET_SCOPE_MODE_EXPLICIT,
        "authoring_mesh_ids": authoring,
        "required_live_mesh_ids": required_live,
    }


@dataclass(frozen=True)
class _ProjectionPolicy:
    block: str
    candidate_projector: object
    authoritative_fields: tuple


@dataclass(frozen=True)
class _ReceiptDialect:
    name: str
    versions: tuple
    graph: _ProjectionPolicy
    core: object
    membership: _ProjectionPolicy
    targets: _ProjectionPolicy
    target_scope_policy: str


_GRAPH_V1_POLICY = _ProjectionPolicy(
    "authoring_graph_projection",
    _authoring_graph_projection_v1_candidates,
    ("fingerprint",),
)
_CORE_V2_POLICY = _ProjectionPolicy(
    "authoring_graph_core_projection",
    _authoring_graph_core_v2_candidates,
    ("fingerprint", "generator_count", "link_count", "asset_identity_count"),
)
_CORE_V3_POLICY = _ProjectionPolicy(
    "authoring_graph_core_projection",
    _authoring_graph_core_v3_candidates,
    (
        "fingerprint",
        "generator_count",
        "link_count",
        "asset_identity_count",
        "global_setting_count",
    ),
)
_CORE_V4_POLICY = _ProjectionPolicy(
    "authoring_graph_core_projection",
    _authoring_graph_core_v4_candidates,
    (
        "fingerprint",
        "generator_count",
        "link_count",
        "asset_identity_count",
        "global_setting_count",
    ),
)
_CORE_V5_POLICY = _ProjectionPolicy(
    "authoring_graph_core_projection",
    _authoring_graph_core_v5_candidates,
    (
        "fingerprint",
        "generator_count",
        "link_count",
        "asset_identity_count",
        "global_setting_count",
    ),
)
_CORE_V6_POLICY = _ProjectionPolicy(
    "authoring_graph_core_projection",
    _authoring_graph_core_v6_candidates,
    (
        "fingerprint",
        "generator_count",
        "link_count",
        "asset_identity_count",
        "global_setting_count",
    ),
)
_MEMBERSHIP_V1_POLICY = _ProjectionPolicy(
    "generator_membership",
    _generator_membership_projection_v1,
    ("fingerprint", "count"),
)
_SCHEMA2_MEMBERSHIP_V1_POLICY = _ProjectionPolicy(
    "generator_membership",
    _schema2_generator_membership_projection_v1,
    ("fingerprint", "count"),
)
_TARGET_V1_POLICY = _ProjectionPolicy(
    "required_target_bindings",
    _target_binding_v1_candidates,
    ("fingerprint", "binding_count", "expected_mesh_ids"),
)
_SCHEMA3_TARGET_V1_POLICY = _ProjectionPolicy(
    "required_target_bindings",
    _target_binding_v1_schema3_candidates,
    ("fingerprint", "binding_count", "expected_mesh_ids"),
)
_TARGET_V2_POLICY = _ProjectionPolicy(
    "required_target_bindings",
    _target_binding_v2_candidates,
    (
        "fingerprint",
        "binding_count",
        "expected_mesh_ids",
        "missing_requested_mesh_ids",
    ),
)


_RECEIPT_DIALECTS = MappingProxyType({
    (2, 1, None, 1, 1, None): _ReceiptDialect(
        "schema2_graph1_target1",
        (2, 1, None, 1, 1, None),
        _GRAPH_V1_POLICY,
        None,
        _SCHEMA2_MEMBERSHIP_V1_POLICY,
        _TARGET_V1_POLICY,
        "strict_all_v1",
    ),
    (3, 1, 2, 1, 1, None): _ReceiptDialect(
        "schema3_graph1_core2_target1",
        (3, 1, 2, 1, 1, None),
        _GRAPH_V1_POLICY,
        _CORE_V2_POLICY,
        _MEMBERSHIP_V1_POLICY,
        _SCHEMA3_TARGET_V1_POLICY,
        "strict_all_v1",
    ),
    (4, 1, 3, 1, 2, None): _ReceiptDialect(
        "schema4_graph1_core3_target2",
        (4, 1, 3, 1, 2, None),
        _GRAPH_V1_POLICY,
        _CORE_V3_POLICY,
        _MEMBERSHIP_V1_POLICY,
        _TARGET_V2_POLICY,
        "strict_all_v1",
    ),
    (5, 1, 3, 1, 2, 1): _ReceiptDialect(
        "schema5_graph1_core3_target2_requirements1",
        (5, 1, 3, 1, 2, 1),
        _GRAPH_V1_POLICY,
        _CORE_V3_POLICY,
        _MEMBERSHIP_V1_POLICY,
        _TARGET_V2_POLICY,
        "explicit_sealed_scopes_v1",
    ),
    (6, 1, 4, 1, 2, 1): _ReceiptDialect(
        "schema6_graph1_core4_target2_requirements1",
        (6, 1, 4, 1, 2, 1),
        _GRAPH_V1_POLICY,
        _CORE_V4_POLICY,
        _MEMBERSHIP_V1_POLICY,
        _TARGET_V2_POLICY,
        "explicit_sealed_scopes_v1",
    ),
    (7, 1, 5, 1, 2, 1): _ReceiptDialect(
        "schema7_graph1_core5_target2_requirements1",
        (7, 1, 5, 1, 2, 1),
        _GRAPH_V1_POLICY,
        _CORE_V5_POLICY,
        _MEMBERSHIP_V1_POLICY,
        _TARGET_V2_POLICY,
        "explicit_sealed_scopes_v1",
    ),
    (8, 1, 6, 1, 2, 1): _ReceiptDialect(
        "schema8_graph1_core6_target2_requirements1",
        (8, 1, 6, 1, 2, 1),
        _GRAPH_V1_POLICY,
        _CORE_V6_POLICY,
        _MEMBERSHIP_V1_POLICY,
        _TARGET_V2_POLICY,
        "explicit_sealed_scopes_v1",
    ),
})


def _receipt_dialect_versions(receipt):
    graph = receipt.get("authoring_graph_projection")
    core = receipt.get("authoring_graph_core_projection")
    membership = receipt.get("generator_membership")
    targets = receipt.get("required_target_bindings")
    requirements = receipt.get("target_requirements")
    return (
        _strict_receipt_version(receipt.get("schema_version")),
        _strict_receipt_version(graph.get("version"))
        if isinstance(graph, dict) else None,
        _strict_receipt_version(core.get("version"))
        if isinstance(core, dict) else None,
        _strict_receipt_version(membership.get("version"))
        if isinstance(membership, dict) else None,
        _strict_receipt_version(targets.get("version"))
        if isinstance(targets, dict) else None,
        _strict_receipt_version(requirements.get("version"))
        if isinstance(requirements, dict) else None,
    )


def _resolve_receipt_dialect_spec(receipt, snapshot=None):
    evidence = _public_hash_evidence(snapshot) if snapshot else {}
    if not isinstance(receipt, dict) or "schema_version" not in receipt:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable receipt is malformed or incomplete",
            evidence,
        )
    schema_version = _strict_receipt_version(receipt.get("schema_version"))
    if schema_version not in _KNOWN_RECEIPT_SCHEMAS:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_schema_unsupported",
            "the immutable receipt schema is unknown or not a strict integer",
            evidence,
        )
    required_blocks = (
        (
            "authoring_graph_projection",
            "speedtree_spm_authoring_graph_projection",
        ),
        (
            "generator_membership",
            "speedtree_generator_membership_projection",
        ),
        (
            "required_target_bindings",
            "speedtree_required_target_binding_projection",
        ),
    )
    if schema_version >= 3:
        required_blocks += ((
            "authoring_graph_core_projection",
            "speedtree_spm_authoring_graph_core_projection",
        ),)
    if schema_version in {5, 6, 7, 8}:
        required_blocks += ((
            "target_requirements",
            "speedtree_stale_node_target_requirements",
        ),)
    for name, contract in required_blocks:
        block = receipt.get(name)
        if not (
            isinstance(block, dict)
            and block.get("contract") == contract
            and "version" in block
        ):
            raise StaleNodeTableRecoveryError(
                "preimage_receipt_verification_failed",
                "the immutable receipt projection structure is malformed",
                evidence,
            )
    if (
        (schema_version == 2 and receipt.get("authoring_graph_core_projection") is not None)
        or (schema_version < 5 and receipt.get("target_requirements") is not None)
    ):
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable receipt contains fields outside its sealed schema",
            evidence,
        )
    versions = _receipt_dialect_versions(receipt)
    if versions in _KNOWN_UNSUPPORTED_RECEIPT_DIALECTS:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_projection_version_unsupported",
            "the historical receipt projection is known but cannot be reproduced",
            evidence,
        )
    dialect = _RECEIPT_DIALECTS.get(versions)
    if dialect is None:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_projection_version_unsupported",
            "the receipt contains an unknown or malformed projection version tuple",
            evidence,
        )
    return dialect


def _resolve_receipt_dialect(receipt, snapshot=None):
    """Return the stable public dialect name after semantic resolution."""
    return _resolve_receipt_dialect_spec(receipt, snapshot).name


def _supported_receipt_projection_versions(receipt):
    try:
        _resolve_receipt_dialect(receipt)
    except StaleNodeTableRecoveryError:
        return False
    return _receipt_target_scopes(receipt) is not None


def _validate_receipt_binding(
    receipt,
    snapshot,
    backup_path,
    target_scopes,
    *,
    source_identity=None,
):
    expected = list(target_scopes["authoring_mesh_ids"])
    caller_required_live = list(target_scopes["required_live_mesh_ids"])
    if not isinstance(receipt, dict):
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the receipt is not a supported immutable evidence object",
            _public_hash_evidence(snapshot),
        )
    dialect = _resolve_receipt_dialect_spec(receipt, snapshot)
    targets = receipt.get("required_target_bindings")
    sealed_target_scopes = _receipt_target_scopes(receipt)
    exact = receipt.get("exact_preimage")
    authoring_graph = receipt.get("authoring_graph_projection")
    membership = receipt.get("generator_membership")
    identity = source_identity or snapshot["source_identity"]
    if dialect.name == "schema3_graph1_core2_target1":
        # Schema-3 target-v1 sealed only graph-visible projected IDs.  The
        # caller's strict requested scope can therefore be wider than the
        # receipt list for an authentic (but recovery-incomplete) preimage.
        # Validate that historical projection against the exact backup here;
        # the later preimage completeness gate remains responsible for
        # rejecting hidden/missing requested targets before launch.
        historical_target_binding_valid = bool(
            snapshot.get("target_projection", {}).get("requested_mesh_ids")
            == expected
            and caller_required_live == expected
            and _projection_policy_matches(
                dialect.targets,
                receipt,
                snapshot,
                expected,
            )
        )
    else:
        historical_target_binding_valid = bool(
            _receipt_requested_mesh_ids(targets) == expected
            and sealed_target_scopes is not None
            and sealed_target_scopes["authoring_mesh_ids"] == expected
            and sealed_target_scopes["required_live_mesh_ids"]
            == caller_required_live
        )
    source_spm_valid = bool(
        dialect.versions[0] < 8
        or (
            isinstance(exact, dict)
            and isinstance(exact.get("source_spm"), str)
            and canonical_path_key(exact["source_spm"])
            == canonical_path_key(snapshot.get("spm"))
            and Path(exact["source_spm"]).name == identity["asset_name"]
        )
    )
    valid = bool(
        receipt.get("kind") == PREIMAGE_RECEIPT_KIND
        and receipt.get("recovery_contract") == RECOVERY_CONTRACT
        and receipt.get("asset_name") == identity["asset_name"]
        and receipt.get("source_identity_sha256")
        == identity["source_identity_sha256"]
        and isinstance(authoring_graph, dict)
        and authoring_graph.get("contract")
        == "speedtree_spm_authoring_graph_projection"
        and _strict_receipt_version(authoring_graph.get("version")) == 1
        and isinstance(membership, dict)
        and membership.get("contract")
        == "speedtree_generator_membership_projection"
        and _strict_receipt_version(membership.get("version")) == 1
        and isinstance(exact, dict)
        and exact.get("raw_sha256") == snapshot["raw_sha256"]
        and exact.get("backup_raw_sha256") == snapshot["raw_sha256"]
        and exact.get("spm_text_sha256") == snapshot["text_sha256"]
        and exact.get("size") == snapshot["size"]
        and exact.get("backup_file") == Path(backup_path).name
        and source_spm_valid
        and historical_target_binding_valid
    )
    if not valid:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the receipt is not bound to this exact source, backup, target set, and supported projection dialect",
            _public_hash_evidence(snapshot),
        )


def _strict_receipt_mesh_id_list(value):
    if not isinstance(value, list):
        return None
    if any(type(item) is not int or item <= 0 for item in value):
        return None
    if len(set(value)) != len(value):
        return None
    return sorted(value)


def _sealed_projection_field(block, field):
    value = block.get(field)
    if field in {"expected_mesh_ids", "missing_requested_mesh_ids"}:
        return _strict_receipt_mesh_id_list(value)
    if field in {
        "binding_count",
        "generator_count",
        "link_count",
        "asset_identity_count",
        "global_setting_count",
        "count",
    }:
        return value if type(value) is int and value >= 0 else None
    return value


def _projection_policy_matches(policy, receipt, backup_snapshot, requested):
    block = receipt.get(policy.block)
    if not isinstance(block, dict):
        return False
    sealed = {
        field: _sealed_projection_field(block, field)
        for field in policy.authoritative_fields
    }
    if any(value is None for value in sealed.values()):
        return False
    candidates = policy.candidate_projector(backup_snapshot, requested)
    return any(
        all(candidate.get(field) == sealed[field] for field in sealed)
        for candidate in candidates
    )


def _verify_preimage_artifacts(artifacts, snapshot=None, *, capture_fn=None):
    """Recapture and verify the backup; never substitute operating bytes."""
    receipt = artifacts["receipt"]
    exact = receipt.get("exact_preimage") if isinstance(receipt, dict) else None
    expected_raw_sha = exact.get("raw_sha256") if isinstance(exact, dict) else None
    evidence = _public_hash_evidence(snapshot) if snapshot else {}
    if not isinstance(expected_raw_sha, str) or not expected_raw_sha:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt is malformed or incomplete",
            evidence,
        )
    try:
        receipt_bytes = artifacts["receipt_path"].read_bytes()
        receipt_on_disk = json.loads(receipt_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StaleNodeTableRecoveryError(
            "preimage_artifacts_missing_or_unreadable",
            "the exact backup or immutable receipt is missing or unreadable",
            evidence,
        ) from exc
    receipt_requested = _receipt_requested_mesh_ids(
        receipt.get("required_target_bindings")
    ) or []
    # Schema-3 target-v1 sealed the graph-visible projection in
    # expected_mesh_ids, not the caller's complete requested scope.  When the
    # caller supplied the exact immutable snapshot, retain its requested scope
    # for historical projection replay; otherwise the receipt list is the only
    # safe fail-closed input available.
    snapshot_projection = (
        snapshot.get("target_projection") if isinstance(snapshot, dict) else None
    )
    requested = (
        list(snapshot_projection.get("requested_mesh_ids") or [])
        if isinstance(snapshot_projection, dict)
        and snapshot.get("raw_sha256") == expected_raw_sha
        else receipt_requested
    )
    capture = capture_fn or _capture_immutable_snapshot
    try:
        backup_snapshot = capture(artifacts["backup_path"], requested)
    except StaleNodeTableRecoveryError as exc:
        raise StaleNodeTableRecoveryError(
            "preimage_backup_verification_failed",
            "the immutable preimage backup cannot be recaptured",
            evidence,
        ) from exc
    if backup_snapshot.get("raw_sha256") != expected_raw_sha:
        raise StaleNodeTableRecoveryError(
            "preimage_backup_verification_failed",
            "the immutable preimage backup no longer matches its receipt",
            evidence,
        )
    dialect = _resolve_receipt_dialect_spec(receipt, backup_snapshot)
    if dialect.versions[0] >= 8:
        source_spm = exact.get("source_spm")
        if not isinstance(source_spm, str) or not source_spm:
            raise StaleNodeTableRecoveryError(
                "preimage_receipt_verification_failed",
                "the core-v6 receipt has no sealed path-resolution source",
                evidence,
            )
        backup_snapshot = _snapshot_with_path_resolution_source(
            backup_snapshot,
            source_spm,
        )
    if receipt_on_disk != receipt:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt no longer matches sealed evidence",
            evidence,
        )
    receipt_sha256 = _sha256_bytes(receipt_bytes)
    expected_receipt_sha256 = artifacts.get("receipt_sha256")
    if (
        expected_receipt_sha256 is not None
        and receipt_sha256 != expected_receipt_sha256
    ):
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt SHA changed",
            evidence,
        )
    exact_valid = bool(
        exact.get("backup_raw_sha256") == backup_snapshot["raw_sha256"]
        and exact.get("spm_text_sha256") == backup_snapshot["text_sha256"]
        and type(exact.get("size")) is int
        and exact.get("size") == backup_snapshot["size"]
        and exact.get("backup_file")
        == Path(artifacts["backup_path"]).name
        and (
            dialect.versions[0] < 8
            or canonical_path_key(exact.get("source_spm"))
            == canonical_path_key(backup_snapshot.get("spm"))
        )
    )
    policies = [dialect.graph, dialect.membership, dialect.targets]
    if dialect.core is not None:
        policies.append(dialect.core)
    projections_valid = all(
        _projection_policy_matches(
            policy,
            receipt,
            backup_snapshot,
            requested,
        )
        for policy in policies
    )
    if not exact_valid or not projections_valid:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt projections do not match its backup",
            _public_hash_evidence(backup_snapshot),
        )
    return receipt_sha256


def _ensure_preimage_artifacts(
    snapshot,
    expected_mesh_ids,
    recovery_root,
    *,
    authoring_mesh_ids=None,
    required_live_mesh_ids=None,
):
    target_scopes, scope_error = _resolve_target_scopes(
        expected_mesh_ids,
        authoring_mesh_ids=authoring_mesh_ids,
        required_live_mesh_ids=required_live_mesh_ids,
    )
    if scope_error:
        raise StaleNodeTableRecoveryError(
            scope_error,
            "preimage target scopes are incomplete or inconsistent",
            _public_hash_evidence(snapshot),
        )
    base = f"{Path(snapshot['source_identity']['asset_name']).stem}.{snapshot['raw_sha256']}"
    backup = recovery_root / (base + ".preimage.spm")
    receipt_path = recovery_root / (base + ".receipt.json")

    if backup.exists():
        if _sha256_bytes(backup.read_bytes()) != snapshot["raw_sha256"]:
            raise StaleNodeTableRecoveryError(
                "preimage_backup_verification_failed",
                "the immutable preimage backup does not match its source SHA",
                _public_hash_evidence(snapshot),
            )
    else:
        _atomic_write_new(backup, snapshot["raw_bytes"])
    if _sha256_bytes(backup.read_bytes()) != snapshot["raw_sha256"]:
        raise StaleNodeTableRecoveryError(
            "preimage_backup_verification_failed",
            "the exact preimage backup failed post-write verification",
            _public_hash_evidence(snapshot),
        )

    if receipt_path.exists():
        try:
            receipt_bytes = receipt_path.read_bytes()
            sealed_receipt = json.loads(receipt_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StaleNodeTableRecoveryError(
                "preimage_receipt_verification_failed",
                "the immutable preimage receipt is unreadable",
                _public_hash_evidence(snapshot),
            ) from exc
    else:
        expected_receipt = _preimage_receipt(
            snapshot,
            target_scopes,
            backup.name,
        )
        _atomic_write_new(receipt_path, expected_receipt)
        receipt_bytes = receipt_path.read_bytes()
        sealed_receipt = expected_receipt
    try:
        receipt_on_disk = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt failed post-write verification",
            _public_hash_evidence(snapshot),
        ) from exc
    if receipt_on_disk != sealed_receipt:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt failed post-write verification",
            _public_hash_evidence(snapshot),
        )
    _validate_receipt_binding(
        sealed_receipt,
        snapshot,
        backup,
        target_scopes,
    )
    artifacts = {
        "backup_path": backup,
        "receipt_path": receipt_path,
        "receipt": sealed_receipt,
        "receipt_sha256": _sha256_bytes(receipt_bytes),
    }
    _verify_preimage_artifacts(artifacts, snapshot)
    return artifacts


def verify_sealed_resave(
    spm_path,
    backup_path,
    receipt_path,
    expected_mesh_ids=(),
    *,
    authoring_mesh_ids=None,
    required_live_mesh_ids=None,
):
    """Re-audit an interrupted Save against its immutable sealed preimage."""
    spm = Path(spm_path).expanduser().resolve(strict=False)
    backup = Path(backup_path).expanduser().resolve(strict=False)
    receipt_file = Path(receipt_path).expanduser().resolve(strict=False)
    target_scopes, scope_error = _resolve_target_scopes(
        expected_mesh_ids,
        authoring_mesh_ids=authoring_mesh_ids,
        required_live_mesh_ids=required_live_mesh_ids,
    )
    if scope_error:
        raise StaleNodeTableRecoveryError(
            scope_error,
            "sealed resave verification requires one complete target-scope mode",
            _source_identity(spm),
        )
    authoring = target_scopes["authoring_mesh_ids"]
    try:
        receipt_bytes = receipt_file.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StaleNodeTableRecoveryError(
            "preimage_receipt_verification_failed",
            "the immutable preimage receipt is missing or unreadable",
            _source_identity(spm),
        ) from exc
    preimage = _capture_immutable_snapshot(backup, authoring)
    preimage = _snapshot_with_path_resolution_source(preimage, spm)
    artifacts = {
        "backup_path": backup,
        "receipt_path": receipt_file,
        "receipt": receipt,
        "receipt_sha256": _sha256_bytes(receipt_bytes),
    }
    receipt_sha256 = _verify_preimage_artifacts(artifacts, preimage)
    _validate_receipt_binding(
        receipt,
        preimage,
        backup,
        target_scopes,
        source_identity=_source_identity(spm),
    )
    if (
        preimage["regex_elementtree_parity"] is not True
        or not _preimage_target_scopes_complete(preimage, target_scopes)
    ):
        raise StaleNodeTableRecoveryError(
            "preimage_reaudit_failed",
            "the immutable preimage no longer satisfies its recovery gates",
            _public_hash_evidence(preimage),
        )
    current = _capture_immutable_snapshot(spm, authoring)
    if current["raw_sha256"] == preimage["raw_sha256"]:
        raise StaleNodeTableRecoveryError(
            "file_content_not_changed",
            "the operating SPM still matches the sealed preimage",
            _public_hash_evidence(current),
        )
    verdict = _snapshot_gate(
        current,
        receipt,
        expected_mesh_ids,
        authoring_mesh_ids=authoring_mesh_ids,
        required_live_mesh_ids=required_live_mesh_ids,
        preimage_snapshot=preimage,
    )
    if not verdict["valid"]:
        raise StaleNodeTableRecoveryError(
            "sealed_resave_reaudit_failed",
            "the saved SPM does not satisfy every recovery gate",
            _public_hash_evidence(current, reason_tokens=verdict["errors"]),
        )
    return {
        "contract": RECOVERY_CONTRACT,
        "status": "sealed_resave_reaudit_valid",
        **_source_identity(spm),
        "preimage_sha256": preimage["text_sha256"],
        "preimage_raw_sha256": preimage["raw_sha256"],
        "after_sha256": current["text_sha256"],
        "after_raw_sha256": current["raw_sha256"],
        "preimage_receipt_sha256": receipt_sha256,
        "modeler_launched": False,
        "reaudit": verdict,
        "closure_gate": "cluster_normalization_and_unreal_push_pending",
    }


def launch_modeler_for_manual_save(speedtree_exe, spm_path):
    """Open a visible Modeler session without shell, Save, or input automation."""
    return external_handoff_popen(
        [str(speedtree_exe), str(spm_path)],
        source="pcg_st9_texture_batch.stale_node_table_recovery.manual_modeler",
        ownership="manual_modeler_handoff",
        cwd=str(Path(spm_path).parent),
        stdin=subprocess.DEVNULL,
    )


def wait_for_valid_resave(
    spm_path,
    preimage_snapshot,
    preimage_receipt,
    expected_mesh_ids=(),
    *,
    authoring_mesh_ids=None,
    required_live_mesh_ids=None,
    timeout=7200,
    poll_interval=2.0,
    stable_reads=3,
    capture_fn=_capture_immutable_snapshot,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
    guards=None,
):
    """Require repeated stat/size/SHA/parse quiescence and every safety gate."""
    target_scopes, scope_error = _resolve_target_scopes(
        expected_mesh_ids,
        authoring_mesh_ids=authoring_mesh_ids,
        required_live_mesh_ids=required_live_mesh_ids,
    )
    if scope_error:
        raise StaleNodeTableRecoveryError(
            scope_error,
            "resave waiting requires one complete target-scope mode",
            preimage_snapshot["source_identity"],
        )
    if timeout <= 0 or poll_interval <= 0 or stable_reads < 2:
        raise StaleNodeTableRecoveryError(
            "invalid_quiescence_configuration",
            "timeout/poll interval must be positive and stable reads at least two",
            preimage_snapshot["source_identity"],
        )
    deadline = monotonic_fn() + float(timeout)
    candidate_key = None
    candidate_reads = 0
    last_errors = ["file_content_not_changed"]
    last_snapshot = None
    while monotonic_fn() < deadline:
        # A valid Save may never arrive.  Poll lifecycle authority alongside
        # the file so Stop, app shutdown, stale job generations, and an
        # optional queue-lease loss do not strand the worker until timeout.
        _check_guards(guards, preimage_snapshot["source_identity"])
        try:
            snapshot = capture_fn(
                spm_path,
                target_scopes["authoring_mesh_ids"],
            )
        except StaleNodeTableRecoveryError as exc:
            last_errors = [exc.reason_token]
            sleep_fn(poll_interval)
            continue
        last_snapshot = snapshot
        if snapshot["raw_sha256"] == preimage_snapshot["raw_sha256"]:
            candidate_key = None
            candidate_reads = 0
            last_errors = ["file_content_not_changed"]
            sleep_fn(poll_interval)
            continue
        if snapshot["quiescence_key"] == candidate_key:
            candidate_reads += 1
        else:
            candidate_key = snapshot["quiescence_key"]
            candidate_reads = 1
        verdict = _snapshot_gate(
            snapshot,
            preimage_receipt,
            expected_mesh_ids,
            authoring_mesh_ids=authoring_mesh_ids,
            required_live_mesh_ids=required_live_mesh_ids,
            preimage_snapshot=preimage_snapshot,
        )
        last_errors = verdict["errors"]
        if candidate_reads >= stable_reads and verdict["valid"]:
            _check_guards(guards, snapshot["source_identity"])
            return snapshot, verdict
        sleep_fn(poll_interval)
    evidence = _public_hash_evidence(last_snapshot or preimage_snapshot)
    evidence["last_reason_tokens"] = sorted(set(last_errors))
    raise StaleNodeTableRecoveryTimeout(
        "valid_resave_quiescence_timeout",
        "the SPM did not reach a stable, valid, graph-continuous resave",
        evidence,
    )


def _validate_retry_contract(retry, job_id, job_generation, guards, identity):
    if retry is None:
        return
    missing = []
    if not str(job_id or "").strip():
        missing.append("job_id")
    if job_generation is None or str(job_generation).strip() == "":
        missing.append("job_generation")
    for name in ("is_cancelled", "is_app_open", "is_job_current"):
        if not callable((guards or {}).get(name)):
            missing.append(name)
    if missing:
        raise StaleNodeTableRecoveryError(
            "continuation_context_incomplete",
            "retry requires initiating job/generation and all lifecycle guards",
            {**identity, "missing_fields": sorted(missing)},
        )


def _check_guards(guards, identity):
    if not guards:
        return
    try:
        if guards["is_cancelled"]():
            token = "initiating_job_cancelled"
        elif not guards["is_app_open"]():
            token = "initiating_app_closed"
        elif not guards["is_job_current"]():
            token = "initiating_job_generation_stale"
        elif callable(guards.get("is_queue_current")) and not guards[
            "is_queue_current"
        ]():
            token = "initiating_queue_lease_lost"
        else:
            return
    except Exception as exc:
        raise StaleNodeTableRecoveryError(
            "continuation_guard_failed",
            "a continuation lifecycle guard raised an exception",
            identity,
        ) from exc
    raise StaleNodeTableRecoveryError(
        token,
        "the initiating job is no longer eligible for continuation",
        identity,
    )


def _claim_and_resume_once(
    spm,
    preimage_snapshot,
    after,
    verdict,
    artifacts,
    recovery_root,
    retry,
    job_id,
    job_generation,
    guards,
    expected_mesh_ids,
    authoring_mesh_ids,
    required_live_mesh_ids,
    capture_fn,
    continuation_commit_lock,
    on_continuation_claimed,
):
    target_scopes, scope_error = _resolve_target_scopes(
        expected_mesh_ids,
        authoring_mesh_ids=authoring_mesh_ids,
        required_live_mesh_ids=required_live_mesh_ids,
    )
    if scope_error:
        raise StaleNodeTableRecoveryError(
            scope_error,
            "continuation target scopes are incomplete or inconsistent",
            after["source_identity"],
        )
    commit_context = (
        continuation_commit_lock
        if continuation_commit_lock is not None
        else nullcontext()
    )
    # The GUI uses the same lock when it commits Stop.  Therefore either Stop
    # becomes visible to this final guard before any claim exists, or the
    # immutable claim plus its published UI state commits first.  The retry
    # callback itself runs outside the lock after that linearization point.
    with commit_context:
        # Lifecycle guards are effectful callbacks.  Run them before the final
        # operating-source recapture so a guard-side mutation cannot escape
        # the source SHA/gate checks below.
        _check_guards(guards, after["source_identity"])
        current = capture_fn(spm, target_scopes["authoring_mesh_ids"])
        if (
            current["raw_sha256"] != after["raw_sha256"]
            or current["text_sha256"] != after["text_sha256"]
        ):
            raise StaleNodeTableRecoveryError(
                "source_changed_before_continuation",
                "the source SHA changed after validation and before continuation",
                _public_hash_evidence(
                    current,
                    verified_after_sha256=after["text_sha256"],
                ),
            )
        current_verdict = _snapshot_gate(
            current,
            artifacts["receipt"],
            expected_mesh_ids,
            authoring_mesh_ids=authoring_mesh_ids,
            required_live_mesh_ids=required_live_mesh_ids,
            preimage_snapshot=preimage_snapshot,
        )
        if not current_verdict["valid"]:
            raise StaleNodeTableRecoveryError(
                "source_revalidation_failed_before_continuation",
                "the source no longer passes the frozen recovery gates",
                _public_hash_evidence(
                    current,
                    reason_tokens=current_verdict["errors"],
                ),
            )
        job_key = _json_fingerprint({
            "job_id": str(job_id),
            "job_generation": str(job_generation),
            "source_identity_sha256": after["source_identity"][
                "source_identity_sha256"
            ],
            "verified_after_raw_sha256": after["raw_sha256"],
        })
        claim = recovery_root / ("continuation." + job_key + ".claim.json")
        claim_payload = {
            "kind": CONTINUATION_CLAIM_KIND,
            "schema_version": 1,
            **after["source_identity"],
            "job_identity_sha256": _sha256_bytes(str(job_id).encode("utf-8")),
            "job_generation": str(job_generation),
            "verified_after_sha256": after["text_sha256"],
            "verified_after_raw_sha256": after["raw_sha256"],
            "preimage_receipt_sha256": artifacts["receipt_sha256"],
        }
        # All source/gate work is complete before the last immutable backup
        # check.  Nothing effectful may intervene between that verification
        # and publishing the once-only claim.
        _verify_preimage_artifacts(artifacts, preimage_snapshot)
        try:
            _atomic_write_new(claim, claim_payload)
        except FileExistsError as exc:
            raise StaleNodeTableRecoveryError(
                "continuation_already_claimed",
                "this job/generation/after-SHA continuation was already claimed",
                _public_hash_evidence(after, job_generation=str(job_generation)),
            ) from exc
        if on_continuation_claimed is not None:
            try:
                on_continuation_claimed(copy.deepcopy(claim_payload))
            except Exception as exc:
                raise StaleNodeTableRecoveryError(
                    "continuation_claim_publish_failed",
                    "the once-only claim exists but its UI commit hook failed",
                    _public_hash_evidence(
                        after,
                        job_generation=str(job_generation),
                    ),
                ) from exc
    continuation = {
        "contract": RECOVERY_CONTRACT,
        "asset_name": after["source_identity"]["asset_name"],
        "source_identity_sha256": after["source_identity"]["source_identity_sha256"],
        "job_identity_sha256": claim_payload["job_identity_sha256"],
        "job_generation": str(job_generation),
        "verified_after_sha256": after["text_sha256"],
        "verified_after_raw_sha256": after["raw_sha256"],
        "preimage_receipt_sha256": artifacts["receipt_sha256"],
        "recovery_verdict": copy.deepcopy(verdict),
    }
    try:
        return retry(continuation), claim.name
    except Exception as exc:
        raise StaleNodeTableRecoveryError(
            "continuation_callback_failed",
            "the once-only continuation callback failed and will not be replayed",
            _public_hash_evidence(after, job_generation=str(job_generation)),
        ) from exc


def _record_blocked_event(recovery_root, identity, error):
    evidence = dict(error.evidence)
    payload = {
        "kind": BLOCKED_EVENT_KIND,
        "schema_version": 1,
        "recovery_contract": RECOVERY_CONTRACT,
        "status": "blocked",
        "reason_token": error.reason_token,
        "asset_name": evidence.get("asset_name") or identity["asset_name"],
        "source_identity_sha256": (
            evidence.get("source_identity_sha256")
            or identity["source_identity_sha256"]
        ),
        "after_sha256": evidence.get("after_sha256"),
        "after_raw_sha256": evidence.get("after_raw_sha256"),
        "verified_after_sha256": evidence.get("verified_after_sha256"),
        "last_reason_tokens": sorted(evidence.get("last_reason_tokens") or []),
        "semantic_uia": (
            copy.deepcopy(evidence.get("semantic_uia"))
            if isinstance(evidence.get("semantic_uia"), dict)
            else None
        ),
    }
    path = recovery_root / ("blocked." + uuid.uuid4().hex + ".json")
    try:
        _atomic_write_new(path, payload)
        error.evidence["blocked_event"] = path.name
        error.evidence["blocked_event_sha256"] = _sha256_bytes(path.read_bytes())
    except (OSError, FileExistsError):
        error.evidence["blocked_event_write_failed"] = True


def _write_semantic_completion_receipt(
    recovery_root,
    identity,
    baseline,
    after,
    artifacts,
    verdict,
    target_scopes,
    save_evidence,
    close_evidence,
):
    """Persist bounded hashes, gates, UIA identity, and session outcome."""
    before_table = baseline["delivery"].get("node_table") or {}
    after_table = after["delivery"].get("node_table") or {}
    payload = {
        "kind": "speedtree_semantic_modeler_recovery_completion",
        "schema_version": 1,
        "recovery_contract": RECOVERY_CONTRACT,
        **identity,
        "preimage": {
            "raw_sha256": baseline["raw_sha256"],
            "spm_text_sha256": baseline["text_sha256"],
            "backup_file": artifacts["backup_path"].name,
            "receipt_file": artifacts["receipt_path"].name,
            "receipt_sha256": artifacts["receipt_sha256"],
            "node_table_stale": before_table.get("stale"),
            "orphan_generator_guid_count": len(
                before_table.get("orphan_generator_guids") or ()
            ),
            "orphan_node_count": int(
                before_table.get("orphan_node_count") or 0
            ),
        },
        "sealed_scope": {
            "mode": target_scopes["mode"],
            "authoring_mesh_ids": list(target_scopes["authoring_mesh_ids"]),
            "required_live_mesh_ids": list(
                target_scopes["required_live_mesh_ids"]
            ),
        },
        "postimage": {
            "raw_sha256": after["raw_sha256"],
            "spm_text_sha256": after["text_sha256"],
            "node_table_stale": after_table.get("stale"),
            "orphan_generator_guid_count": len(
                after_table.get("orphan_generator_guids") or ()
            ),
            "orphan_node_count": int(
                after_table.get("orphan_node_count") or 0
            ),
            "authoring_graph_continuity": verdict[
                "authoring_graph_continuity"
            ],
            "generator_membership_continuity": verdict[
                "generator_membership_continuity"
            ],
            "required_target_binding_continuity": verdict[
                "required_target_binding_continuity"
            ],
        },
        "semantic_uia": {
            "save": copy.deepcopy(save_evidence),
            "close": copy.deepcopy(close_evidence),
        },
    }
    path = Path(recovery_root) / (
        "completion."
        + identity["source_identity_sha256"][:16]
        + "."
        + after["raw_sha256"][:16]
        + "."
        + uuid.uuid4().hex
        + ".json"
    )
    try:
        _atomic_write_new(path, payload)
        receipt_sha256 = _sha256_bytes(path.read_bytes())
    except (OSError, FileExistsError) as exc:
        raise StaleNodeTableRecoveryError(
            "semantic_completion_receipt_write_failed",
            "the verified semantic recovery receipt could not be persisted",
            _public_hash_evidence(after),
        ) from exc
    return {
        "file": path.name,
        "sha256": receipt_sha256,
    }


def _validated_semantic_uia_evidence(
    evidence,
    spm,
    operation,
    *,
    expected_process_id=None,
):
    """Accept only the exact sanitized receipt emitted by the owned adapter."""
    action = "Save" if operation == "save" else "Close"
    process_id = evidence.get("owned_process_id") if isinstance(evidence, dict) else None
    valid = bool(
        isinstance(evidence, dict)
        and evidence.get("contract") == SEMANTIC_UIA_CONTRACT
        and type(process_id) is int
        and process_id > 0
        and (
            expected_process_id is None
            or process_id == expected_process_id
        )
        and evidence.get("document_accessible_name") == Path(spm).name
        and evidence.get("operation") == operation
        and evidence.get("menu_path") == ["File", action]
        and evidence.get("semantic_pattern") == "InvokePattern"
        and evidence.get("bridge_exit_code") == 0
        and (
            operation != "save"
            or (
                type(evidence.get("session_reused")) is bool
                and evidence.get("owned_process_alive_after_invoke") is True
            )
        )
        and (
            operation != "close"
            or (
                evidence.get("exact_document_closed") is True
                and evidence.get("owned_process_alive_after_close") is True
            )
        )
    )
    if not valid:
        raise StaleNodeTableRecoveryError(
            "semantic_uia_receipt_invalid",
            "the semantic UIA result is not bound to the exact PID, document, and menu",
            _source_identity(spm),
        )
    return copy.deepcopy(evidence)


def recover_stale_node_table(
    spm_path,
    speedtree_exe,
    expected_mesh_ids=(),
    *,
    authoring_mesh_ids=None,
    required_live_mesh_ids=None,
    timeout=7200,
    poll_interval=2.0,
    stable_reads=3,
    retry=None,
    job_id=None,
    job_generation=None,
    guards=None,
    recovery_root=None,
    capture_fn=_capture_immutable_snapshot,
    launch_fn=launch_modeler_for_manual_save,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
    expected_preimage_raw_sha256=None,
    continuation_commit_lock=None,
    on_continuation_claimed=None,
    modeler_session=None,
):
    """Seal, open, watch, audit, and resume a bound job at most once."""
    spm = Path(spm_path).expanduser().resolve(strict=False)
    executable = Path(speedtree_exe).expanduser().resolve(strict=False)
    identity = _source_identity(spm)
    if not spm.is_file() or spm.suffix.casefold() != ".spm":
        raise StaleNodeTableRecoveryError(
            "recovery_target_invalid",
            "the recovery target is not an existing SPM",
            identity,
        )
    if not executable.is_file():
        raise StaleNodeTableRecoveryError(
            "speedtree_modeler_executable_missing",
            "the configured SpeedTree Modeler executable is unavailable",
            identity,
        )
    target_scopes, scope_error = _resolve_target_scopes(
        expected_mesh_ids,
        authoring_mesh_ids=authoring_mesh_ids,
        required_live_mesh_ids=required_live_mesh_ids,
    )
    if scope_error:
        raise StaleNodeTableRecoveryError(
            scope_error,
            "recovery requires one complete target-scope mode",
            identity,
        )
    expected_preimage_sha = str(
        expected_preimage_raw_sha256 or ""
    ).strip().casefold()
    if expected_preimage_sha and not SHA256_RE.fullmatch(
        expected_preimage_sha
    ):
        raise StaleNodeTableRecoveryError(
            "invalid_expected_preimage_sha256",
            "the evidence-bound preimage SHA-256 is invalid",
            identity,
        )
    if on_continuation_claimed is not None and not callable(
        on_continuation_claimed
    ):
        raise StaleNodeTableRecoveryError(
            "continuation_claim_hook_invalid",
            "the continuation claim hook must be callable",
            identity,
        )
    if continuation_commit_lock is not None and not (
        hasattr(continuation_commit_lock, "__enter__")
        and hasattr(continuation_commit_lock, "__exit__")
    ):
        raise StaleNodeTableRecoveryError(
            "continuation_commit_lock_invalid",
            "the continuation commit lock must be a context manager",
            identity,
        )
    if modeler_session is not None and not (
        callable(getattr(modeler_session, "save_document", None))
        and callable(getattr(modeler_session, "close_document", None))
    ):
        raise StaleNodeTableRecoveryError(
            "semantic_modeler_session_invalid",
            "the semantic Modeler session does not expose exact Save and Close",
            identity,
        )
    authoring = target_scopes["authoring_mesh_ids"]
    required_live = target_scopes["required_live_mesh_ids"]
    _validate_retry_contract(
        retry,
        job_id,
        job_generation,
        guards,
        identity,
    )
    root = (
        Path(recovery_root).expanduser().resolve(strict=False)
        if recovery_root is not None
        else spm.parent / "_spm_backups" / "stale_node_table_recovery"
    )
    root.mkdir(parents=True, exist_ok=True)
    lock = None
    token = None
    try:
        lock, token = _acquire_session_lock(root, identity)
        baseline = capture_fn(spm, authoring)
        if (
            expected_preimage_sha
            and baseline["raw_sha256"] != expected_preimage_sha
        ):
            raise StaleNodeTableRecoveryError(
                "audit_preimage_identity_changed",
                "the current SPM bytes no longer match the blocking audit",
                _public_hash_evidence(
                    baseline,
                    expected_preimage_raw_sha256=expected_preimage_sha,
                ),
            )
        baseline_verdict = validate_repaired_snapshot(
            baseline["delivery"],
            authoring,
            required_live,
        )
        node_table = baseline["delivery"].get("node_table") or {}
        if node_table.get("stale") is not True:
            if not baseline_verdict["valid"]:
                raise StaleNodeTableRecoveryError(
                    "non_stale_delivery_gate_failed",
                    "the Node table is not stale but target delivery is invalid",
                    _public_hash_evidence(
                        baseline,
                        reason_tokens=baseline_verdict["errors"],
                    ),
                )
            return {
                "contract": RECOVERY_CONTRACT,
                "status": "already_repaired",
                **identity,
                "after_sha256": baseline["text_sha256"],
                "after_raw_sha256": baseline["raw_sha256"],
                "modeler_launched": False,
                "retry_invoked": False,
                "closure_gate": "operational_snapshot_valid_only",
            }
        orphan_guids = list(
            node_table.get("orphan_generator_guids") or ()
        )
        orphan_node_count = int(node_table.get("orphan_node_count") or 0)
        if not orphan_guids or orphan_node_count <= 0:
            raise StaleNodeTableRecoveryError(
                "stale_orphan_evidence_missing",
                "semantic Save is allowed only for a stale table with actual orphan ownership and Nodes",
                _public_hash_evidence(
                    baseline,
                    orphan_generator_guid_count=len(orphan_guids),
                    orphan_node_count=orphan_node_count,
                ),
            )
        if not baseline["regex_elementtree_parity"]:
            raise StaleNodeTableRecoveryError(
                "preimage_regex_elementtree_mismatch",
                "the same-byte preimage audits disagree",
                _public_hash_evidence(baseline),
            )
        if not _preimage_target_scopes_complete(baseline, target_scopes):
            raise StaleNodeTableRecoveryError(
                "preimage_target_manifest_incomplete",
                "the exact preimage does not contain the required target manifest",
                _public_hash_evidence(baseline),
            )
        artifacts = _ensure_preimage_artifacts(
            baseline,
            expected_mesh_ids,
            root,
            authoring_mesh_ids=authoring_mesh_ids,
            required_live_mesh_ids=required_live_mesh_ids,
        )
        # Lifecycle guards are effectful callbacks.  Run them before the final
        # operating-source recapture so their side effects are covered by the
        # SHA comparison below.
        _check_guards(guards, identity)
        # The exact source must still be the sealed preimage before Modeler is
        # opened; receipt creation is not authority if the source raced it.
        prelaunch = capture_fn(spm, authoring)
        if (
            prelaunch["raw_sha256"] != baseline["raw_sha256"]
            or prelaunch["text_sha256"] != baseline["text_sha256"]
        ):
            raise StaleNodeTableRecoveryError(
                "source_changed_before_modeler_launch",
                "the source changed after preimage sealing and before launch",
                _public_hash_evidence(prelaunch),
            )
        # This is the final effectful prelaunch check.  Modeler starts
        # immediately after the exact immutable backup is recaptured.
        _verify_preimage_artifacts(artifacts, prelaunch)
        process = None
        semantic_save_evidence = None
        semantic_close_evidence = None
        semantic_completion = None
        if modeler_session is None:
            process = launch_fn(executable, spm)
        else:
            try:
                semantic_save_evidence = modeler_session.save_document(
                    executable,
                    spm,
                )
            except SemanticModelerUIAError as exc:
                raise StaleNodeTableRecoveryError(
                    exc.reason_token,
                    "the exact semantic Modeler Save failed closed",
                    _public_hash_evidence(
                        prelaunch,
                        semantic_uia=exc.evidence,
                    ),
                ) from exc
            semantic_save_evidence = _validated_semantic_uia_evidence(
                semantic_save_evidence,
                spm,
                "save",
            )
        after, verdict = wait_for_valid_resave(
            spm,
            baseline,
            artifacts["receipt"],
            expected_mesh_ids,
            authoring_mesh_ids=authoring_mesh_ids,
            required_live_mesh_ids=required_live_mesh_ids,
            timeout=timeout,
            poll_interval=poll_interval,
            stable_reads=stable_reads,
            capture_fn=capture_fn,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            guards=guards,
        )
        if modeler_session is not None:
            try:
                semantic_close_evidence = modeler_session.close_document(spm)
            except SemanticModelerUIAError as exc:
                raise StaleNodeTableRecoveryError(
                    exc.reason_token,
                    "the exact verified recovery document could not be closed",
                    _public_hash_evidence(
                        after,
                        verified_after_sha256=after["raw_sha256"],
                        semantic_uia=exc.evidence,
                    ),
                ) from exc
            semantic_close_evidence = _validated_semantic_uia_evidence(
                semantic_close_evidence,
                spm,
                "close",
                expected_process_id=semantic_save_evidence[
                    "owned_process_id"
                ],
            )
            semantic_completion = _write_semantic_completion_receipt(
                root,
                identity,
                baseline,
                after,
                artifacts,
                verdict,
                target_scopes,
                semantic_save_evidence,
                semantic_close_evidence,
            )
        retry_result = None
        claim_name = None
        if retry is not None:
            retry_result, claim_name = _claim_and_resume_once(
                spm,
                baseline,
                after,
                verdict,
                artifacts,
                root,
                retry,
                job_id,
                job_generation,
                guards,
                expected_mesh_ids,
                authoring_mesh_ids,
                required_live_mesh_ids,
                capture_fn,
                continuation_commit_lock,
                on_continuation_claimed,
            )
        return {
            "contract": RECOVERY_CONTRACT,
            "status": (
                "repaired_reaudited_and_retried_once"
                if retry is not None
                else "repaired_reaudit_valid"
            ),
            **identity,
            "preimage_sha256": baseline["text_sha256"],
            "preimage_raw_sha256": baseline["raw_sha256"],
            "after_sha256": after["text_sha256"],
            "after_raw_sha256": after["raw_sha256"],
            "preimage_backup": artifacts["backup_path"].name,
            "preimage_receipt": artifacts["receipt_path"].name,
            "preimage_receipt_sha256": artifacts["receipt_sha256"],
            "modeler_launched": True,
            "modeler_process_observed_only": (
                modeler_session is None and process is not None
            ),
            "semantic_uia": (
                {
                    "save": semantic_save_evidence,
                    "close": semantic_close_evidence,
                }
                if modeler_session is not None
                else None
            ),
            "semantic_completion_receipt": semantic_completion,
            "reaudit": verdict,
            "retry_invoked": retry is not None,
            "continuation_claim": claim_name,
            "retry_result": retry_result,
            "closure_gate": "cluster_normalization_and_unreal_push_pending",
        }
    except StaleNodeTableRecoveryError as exc:
        _record_blocked_event(root, identity, exc)
        raise
    finally:
        if lock is not None and token is not None:
            _release_session_lock(lock, token)


def configured_speedtree_exe():
    """Use the existing SK Batch SpeedTree configuration."""
    from sk_batch.sk_common import load_config

    return Path(load_config().get("speedtree_exe") or "")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Seal a stale SPM preimage, open it in SpeedTree Modeler, wait "
            "for a manual save, and run a graph-continuous frozen audit."
        )
    )
    parser.add_argument("spm", nargs="+", help="Affected operating SPM path")
    parser.add_argument(
        "--speedtree-exe",
        help="Modeler executable; defaults to the existing SK Batch config",
    )
    target_mode = parser.add_mutually_exclusive_group(required=True)
    target_mode.add_argument(
        "--expected-mesh-id",
        action="append",
        type=int,
        help=(
            "Legacy strict target Mesh ID; repeat for each target. Every ID "
            "is sealed for both authoring continuity and live delivery."
        ),
    )
    target_mode.add_argument(
        "--authoring-mesh-id",
        action="append",
        type=int,
        help=(
            "Explicit authoring-binding Mesh ID; repeat for each target and "
            "also select a required-live mode."
        ),
    )
    live_mode = parser.add_mutually_exclusive_group()
    live_mode.add_argument(
        "--required-live-mesh-id",
        action="append",
        type=int,
        help=(
            "Explicit live/export Mesh ID subset; repeat as needed and use "
            "only with --authoring-mesh-id."
        ),
    )
    live_mode.add_argument(
        "--no-required-live-delivery",
        action="store_true",
        help=(
            "Seal explicit authoring continuity with no per-binding live "
            "delivery requirement."
        ),
    )
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--stable-reads", type=int, default=3)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    speedtree_exe = (
        Path(args.speedtree_exe)
        if args.speedtree_exe
        else configured_speedtree_exe()
    )
    results = []
    required_live_mesh_ids = (
        ()
        if args.no_required_live_delivery
        else args.required_live_mesh_id
    )
    try:
        for spm in args.spm:
            results.append(
                recover_stale_node_table(
                    spm,
                    speedtree_exe,
                    args.expected_mesh_id or (),
                    authoring_mesh_ids=args.authoring_mesh_id,
                    required_live_mesh_ids=required_live_mesh_ids,
                    timeout=args.timeout,
                    poll_interval=args.poll_interval,
                    stable_reads=args.stable_reads,
                )
            )
    except StaleNodeTableRecoveryError as exc:
        print(json.dumps(exc.public_payload(), indent=2, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "ok", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
