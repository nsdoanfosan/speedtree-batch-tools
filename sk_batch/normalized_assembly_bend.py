"""Source-proven normalized part wind delivery for any Assembly species.

The manifest is the authority for part roles and order. This module never
creates UVs in Unreal and never invokes the legacy material script's run().
"""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import io
import json
import math
import marshal
import os
from pathlib import Path
import re
import subprocess
import sys

try:
    from .assembly_part_bend_policy import ASPECT_POLICY, DEFAULT_SCALARS, NORMALIZED_BEND_ROLE_CODES, package_path, normalization_part_bend_policy, part_bend_build_matches_policy
    from .nanite_assembly_materials import unreal_asset_identity
except ImportError:
    from assembly_part_bend_policy import ASPECT_POLICY, DEFAULT_SCALARS, NORMALIZED_BEND_ROLE_CODES, package_path, normalization_part_bend_policy, part_bend_build_matches_policy
    from nanite_assembly_materials import unreal_asset_identity

DESTINATION = "/Game/Material/Tree/AssetTree/AssemblyBend"
FUNCTION = DESTINATION + "/MF_NaniteAssemblyPartBend"
ASPECT_TAG = "CodexAssemblyPartBendAspectPolicy"
OWNER = "NormalizedAssemblyPartBend/20261010/v2"
OWNER_TAG = "CodexNormalizedAssemblyBendOwner"
SOURCE_TAG = "CodexNormalizedAssemblyBendSourceMaterial"
SIGNATURE_TAG = "CodexNormalizedAssemblyBendSignatureSHA256"
SOURCE_SIGNATURE_TAG = "CodexNormalizedAssemblyBendSourceSHA256"
CHAIN_SIGNATURE_TAG = "CodexNormalizedAssemblyBendSourceChainSHA256"
CONTRACT_TAG = "CodexNormalizedAssemblyBendContract"
STATE_TAG = "CodexAssemblyPartBendState"
RETURN_TAG = "CodexAssemblyPartBendReturnPolicy"
RETURN_OWNER = "AssemblyPartBendLocalAmplitudeResponse/20261010/v3"
RETURN_SCALARS = {"AssemblyWindRestThreshold", "AssemblyWindResponseScale"}
EXTRA_SCALARS = {
    "AssemblyBranchBendScale": 1.0, "AssemblyBranchOscillationScale": 1.0,
    "AssemblyBranchSpeedScale": 1.0, "AssemblyBranchNoiseScale": 0.001,
    "AssemblyWindAmplitudeLimit": 2.0,
    "AssemblyWindRestThreshold": 0.0, "AssemblyWindResponseScale": 1.0,
}
WPO_FIELDS = {"bOverride_MaxWorldPositionOffsetDisplacement", "MaxWorldPositionOffsetDisplacement"}


class NormalizedAssemblyBendError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise NormalizedAssemblyBendError(message)


def sha1_file(path):
    digest = hashlib.sha1()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def variant_path(source, master=False):
    source = package_path(source)
    require(source.casefold().startswith("/game/"), "bend source material must be project owned")
    suffix = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    return DESTINATION + "/" + source.rsplit("/", 1)[-1] + "_" + suffix + ("_AspectBend_Master" if master else "_AspectBend")


def validate_active_source_indices(source_count, indices):
    require(isinstance(source_count, int) and not isinstance(source_count, bool) and source_count > 0
            and isinstance(indices, list) and bool(indices)
            and all(isinstance(index, int) and not isinstance(index, bool) and 0 <= index < source_count for index in indices)
            and indices == sorted(set(indices)), "active source material indices must be nonempty, ordered, unique and in range")
    return list(indices)


def plan_active_source_indices(source_count, parts, actual_parts, current_material_count, previous_contract=None):
    """Decode native remaps and use only independently verified numeric bend roles."""
    validate_active_source_indices(source_count, list(range(source_count)))
    require(isinstance(current_material_count, int) and not isinstance(current_material_count, bool),
            "native material count is invalid")
    if current_material_count == source_count:
        mapping = list(range(source_count))
    elif previous_contract and "active_source_indices" in previous_contract:
        previous = validate_active_source_indices(source_count, previous_contract["active_source_indices"])
        require(current_material_count == source_count + len(previous), "compact native material table differs from its previous contract")
        mapping = list(range(source_count)) + previous
    else:
        require(current_material_count == source_count * 2, "native material count lacks an exact original/legacy contract")
        mapping = list(range(source_count)) * 2
    require(len(parts) == len(actual_parts) and bool(parts), "active material plan requires the complete native part order")
    active = set()
    for index, (part, actual) in enumerate(zip(parts, actual_parts)):
        require(type(part.get("index")) is int and part["index"] == index
                and unreal_asset_identity(part.get("mesh")) == unreal_asset_identity(actual.get("mesh"))
                and type(part.get("role")) is int and part["role"] in (0, 1, 2), "active material roles differ from the native part order")
        remaps = actual.get("remap")
        require(isinstance(remaps, list) and all(type(value) is int and 0 <= value < len(mapping) for value in remaps),
                "active material plan has an invalid native remap")
        if part["role"]:
            active.update(mapping[value] for value in remaps)
        else:
            require(all(value < source_count for value in remaps), "disabled parts must keep original source material remaps")
    return validate_active_source_indices(source_count, sorted(active))


def resolve_material_hierarchy(unreal, source_paths, *, active_source_indices=None):
    """Resolve the actual leaf identity and every MIC layer, without flattening."""
    require(source_paths, "normalized part bend requires source materials")
    library = unreal.EditorAssetLibrary
    active_indices = validate_active_source_indices(len(source_paths), list(range(len(source_paths)))
        if active_source_indices is None else active_source_indices)
    sources, chains, instances, roots, parents = [], [], {}, {}, {}
    source_parents = []
    for index, requested in enumerate(source_paths):
        source = library.load_asset(requested)
        require(isinstance(source, unreal.MaterialInstanceConstant),
                "normalized part bend requires source MaterialInstanceConstants: " + str(requested))
        sources.append(source)
        parent = source.get_editor_property("parent")
        require(isinstance(parent, (unreal.MaterialInstanceConstant, unreal.Material)),
                "material inheritance has a missing or unsupported parent: " + str(requested))
        source_parents.append(package_path(parent.get_path_name()))
        if index not in active_indices:
            chains.append([])
            continue
        chain, seen, current = [], set(), source
        while True:
            require(isinstance(current, (unreal.MaterialInstanceConstant, unreal.Material)),
                    "material inheritance has a missing or unsupported parent: " + str(requested))
            path = package_path(current.get_path_name())
            require(path.casefold().startswith("/game/"), "bend source material must be project owned: " + path)
            require(unreal_asset_identity(path) not in seen, "material inheritance cycle: " + path)
            require(library.load_asset(path) == current, "material inheritance asset is unavailable: " + path)
            seen.add(unreal_asset_identity(path))
            chain.append(path)
            if isinstance(current, unreal.Material):
                roots.setdefault(path, current)
                break
            parent = current.get_editor_property("parent")
            require(isinstance(parent, (unreal.MaterialInstanceConstant, unreal.Material)),
                    "material inheritance has a missing or unsupported parent: " + path)
            parents[path] = package_path(parent.get_path_name())
            current = parent
        # Every parent is inserted before its child; shared layers are cloned once.
        for path in reversed(chain[:-1]):
            instances.setdefault(path, library.load_asset(path))
        chains.append(chain)
    source_materials = [package_path(source.get_path_name()) for source in sources]
    plan = {
        "materials": [variant_path(path, master=True) for path in roots]
                     + [variant_path(path) for path in instances],
        "source_materials": source_materials,
        "source_parents": source_parents,
        "active_source_indices": active_indices,
        "active_source_materials": [source_materials[index] for index in active_indices],
        "source_chains": chains,
        "root_materials": list(roots),
        "source_instance_layers": list(instances),
    }
    require(len({unreal_asset_identity(path) for path in plan["materials"]}) == len(plan["materials"]), "material hierarchy destinations collide")
    return {"plan": plan, "sources": sources, "instances": instances,
            "roots": roots, "parents": parents}


def plan_material_packages(unreal, source_paths, *, active_source_indices=None):
    """Freeze root wrappers and all parent-first MIC destinations before writes."""
    return resolve_material_hierarchy(unreal, source_paths, active_source_indices=active_source_indices)["plan"]


def require_active_dependency_phase(phase_id, packages):
    """Use the installed queue's actual admission checks before heavy writes."""
    queue_path = Path.home() / "Documents/GitHub/workstation-queue"
    require((queue_path / "pipeline_bridge.py").is_file(), "workstation phase admission bridge is unavailable")
    if str(queue_path) not in sys.path:
        sys.path.insert(0, str(queue_path))
    from pipeline_bridge import require_active_phase, require_scopes
    phase = require_active_phase(phase_id, "unreal:myproject2")
    require(phase.get("workload") == "heavy", "normalized material shader compilation requires an active heavy phase")
    require_scopes(phase_id, ["asset:" + path for path in packages])
    return phase


def p4_records(*args, form=None):
    """Decode exact native Perforce records without shell command construction."""
    from process_lifecycle import owned_run
    result = owned_run(["p4", "-c", "UnrealProjects", "-G", *map(str, args)],
        source="sk_batch.normalized_assembly_bend.perforce", run_factory=subprocess.run,
        input=marshal.dumps(form, 0) if form is not None else None,
        capture_output=True, check=False)
    stream, rows = io.BytesIO(result.stdout), []
    def decode(value):
        if not isinstance(value, bytes):
            return value
        for encoding in ("utf-8", "cp949"):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                pass
        raise NormalizedAssemblyBendError("Perforce returned an undecodable path")
    while stream.tell() < len(result.stdout):
        row = marshal.load(stream)
        rows.append({decode(key): decode(value) for key, value in row.items()})
    return rows


def manual_material_changelist(paths, mappings, records, *, p4=p4_records):
    """Reuse only the exact manual derivative scope; never move opened files."""
    scope_hash = hashlib.sha256(json.dumps(sorted(paths), separators=(",", ":")).encode("utf-8")).hexdigest()
    marker = "Owner: " + OWNER + "\nScope SHA256: " + scope_hash
    depot_scope = {row["depotFile"] for row in mappings if row.get("depotFile") and not row.get("unmap")}
    require(len(depot_scope) == len(paths), "manual normalized material scope lacks exact depot mappings")
    opened = [row for row in records if row.get("action")]
    changes = {str(row.get("change")) for row in opened}
    require(not changes or len(changes) == 1 and next(iter(changes)).isdigit(),
            "manual normalized material scope is already open in default or several changelists")
    candidates = p4("changes", "-c", "UnrealProjects", "-s", "pending", "-l")
    require(not any(row.get("code") == "error" for row in candidates), "manual normalized material pending-CL query failed")
    for candidate in candidates:
        number = str(candidate.get("change") or "")
        if not number.isdigit() or marker not in str(candidate.get("desc") or ""):
            continue
        forms = p4("change", "-o", number)
        form = next((row for row in forms if row.get("Status") == "pending" and row.get("Client") == "UnrealProjects"
                     and marker in str(row.get("Description") or "")), None)
        if not form:
            continue
        form_files = {str(value).split("#", 1)[0] for key, value in form.items() if re.fullmatch(r"Files\d+", str(key))}
        require(form_files <= depot_scope, "matching manual normalized material changelist contains unrelated files")
        if changes:
            require(changes == {number}, "manual normalized material scope belongs to another changelist")
        return int(number)
    require(not changes, "opened normalized material changes lack the matching manual scope owner")
    created = p4("change", "-i", form={b"Change": b"new", b"Client": b"UnrealProjects",
        b"Description": ("SpeedTree normalized Assembly wind derivatives\n" + marker + "\n").encode("utf-8")})
    message = " ".join(str(row.get("data", "")) for row in created)
    match = re.search(r"Change (\d+) created", message)
    require(match is not None and not any(row.get("code") == "error" for row in created),
            "manual normalized material numbered changelist creation failed")
    return int(match.group(1))


def checkout_material_scope(unreal, paths, changelist, *, p4=p4_records, allow_manual=False, destination_root=DESTINATION):
    """Confirm mapping/open CL for every frozen destination before editing."""
    require((allow_manual and changelist is None) or isinstance(changelist, int) and not isinstance(changelist, bool) and changelist > 0,
            "normalized bend requires its explicit numbered UnrealProjects changelist")
    require(destination_root in (DESTINATION, "/Game/Material/Tree/AssetTree/AssemblyNoTess")
            and paths and len({unreal_asset_identity(path) for path in paths}) == len(paths)
            and all(path.casefold().startswith((destination_root + "/").casefold()) for path in paths),
            "normalized material checkout scope is malformed")
    content = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())).resolve()
    files = [content.joinpath(*path[len("/Game/"):].split("/")).with_suffix(".uasset") for path in paths]
    key = lambda path: os.path.normcase(os.path.abspath(str(path))).casefold()
    expected = {key(path) for path in files}
    mappings = p4("where", *files)
    mapped = [key(row["path"]) for row in mappings if row.get("path") and not row.get("unmap")]
    require(len(mapped) == len(files) and set(mapped) == expected,
            "normalized material destinations are not exactly mapped by UnrealProjects")
    records = p4("fstat", "-Ol", *files)
    errors = [row for row in records if row.get("code") == "error"]
    require(all("no such file" in str(row.get("data", "")).casefold() for row in errors),
            "normalized material Perforce metadata query failed: " + json.dumps(errors, sort_keys=True))
    file_records = [row for row in records if row.get("clientFile")]
    require(all(key(row["clientFile"]) in expected for row in file_records), "Perforce returned an unrelated material path")
    tracked = [row for row in file_records if row.get("headRev") and row.get("headAction") != "delete"]
    known_keys = {key(row["clientFile"]) for row in file_records}
    require(all(not path.exists() or key(path) in known_keys for path in files),
            "untracked normalized material file collision lacks a Perforce recovery point")
    edits = [row["clientFile"] for row in tracked if not row.get("action")]
    # Existing submitted material bytes must still match the server recovery
    # point. Existing same-CL files are already owned by this transaction.
    for row in tracked:
        path = Path(row["clientFile"])
        require(path.is_file(), "tracked normalized material is missing locally: " + str(path))
        if not row.get("action"):
            require(row.get("digest") and hashlib.md5(path.read_bytes()).hexdigest().casefold() == str(row["digest"]).casefold(),
                    "normalized material has unprotected local edits before checkout: " + str(path))
    if changelist is None:
        changelist = manual_material_changelist(paths, mappings, file_records, p4=p4)
    forms = p4("change", "-o", changelist)
    require(any(row.get("Status") == "pending" and row.get("Client") == "UnrealProjects" for row in forms),
            "normalized bend changelist is not pending in UnrealProjects")
    conflicts = [row for row in file_records if row.get("action") and str(row.get("change")) != str(changelist)]
    require(not conflicts, "normalized material changelist conflict; preserve existing edits: " + json.dumps(conflicts, sort_keys=True))
    if edits:
        edited = p4("edit", "-c", changelist, *edits)
        require(not any(row.get("code") == "error" for row in edited), "normalized material scoped Perforce checkout failed")
    tracked_keys = {key(row["clientFile"]) for row in tracked}
    if tracked_keys:
        reopened = p4("fstat", *[path for path in files if key(path) in tracked_keys])
        opened_keys = {key(row["clientFile"]) for row in reopened
            if row.get("clientFile") and row.get("action") == "edit" and str(row.get("change")) == str(changelist)}
        require(opened_keys == tracked_keys, "normalized material checkout did not verify the exact open CL")
    return {"verified": True, "client": "UnrealProjects", "changelist": changelist,
            "materials": list(paths), "package_files": [str(path) for path in files],
            "tracked_checked_out": [str(path) for path in files if key(path) in tracked_keys],
            "new_packages": [str(path) for path in files if key(path) not in tracked_keys]}


def register_new_material_packages(receipt, *, p4=p4_records):
    """Add only verified durable new derivatives to their frozen CL."""
    files = [Path(path) for path in receipt.get("new_packages") or []]
    if not files:
        return []
    require(all(file.is_file() and file.stat().st_size > 0 for file in files),
            "new normalized material package is missing after its verified save")
    rows = p4("fstat", *files)
    opened = [row for row in rows if row.get("clientFile") and row.get("action")]
    require(all(str(row.get("change")) == str(receipt["changelist"]) and row.get("action") == "add" for row in opened),
            "new normalized material package was opened in another CL/action")
    opened_keys = {os.path.normcase(os.path.abspath(row["clientFile"])).casefold() for row in opened}
    pending = [file for file in files if os.path.normcase(os.path.abspath(str(file))).casefold() not in opened_keys]
    if pending:
        result = p4("add", "-c", receipt["changelist"], *pending)
        require(not any(row.get("code") == "error" for row in result), "new normalized material scoped Perforce add failed")
    verified = p4("fstat", *files)
    verified_keys = {os.path.normcase(os.path.abspath(row["clientFile"])).casefold() for row in verified
        if row.get("clientFile") and row.get("action") == "add" and str(row.get("change")) == str(receipt["changelist"])}
    require(verified_keys == {os.path.normcase(os.path.abspath(str(file))).casefold() for file in files},
            "new normalized material add did not verify its exact open CL")
    return [str(file) for file in files]


def check_dependency_admission(receipt):
    phase_id = str((receipt or {}).get("phase_id") or "")
    if not phase_id:
        return
    require_active_dependency_phase(phase_id, [receipt["assembly"]] + receipt["materials"] + receipt["prototypes"])
    from pipeline_bridge import can_execute_handoff, heartbeat_handoff
    metadata = receipt.get("workstation_handoff")
    require(isinstance(metadata, dict) and metadata.get("phase_id") == phase_id and can_execute_handoff(metadata)
            and heartbeat_handoff(metadata), "normalized bend lost its exact active native Batch handoff")


def manifest_part_bend_role(part, mesh):
    """Prefer independently verified authoring intent over the capture role."""
    external = part.get("external_source") or {}
    keys = {"part_bend_role", "part_bend_policy", "part_bend_payload", "part_bend_source_proof"}
    present = {key for key in external if str(key).startswith("part_bend_")}
    if not present:
        return str(part.get("role") or "")
    require(present == keys, "normalized bend manifest metadata is incomplete: " + mesh)
    role, policy, payload, proof = (external[key] for key in
        ("part_bend_role", "part_bend_policy", "part_bend_payload", "part_bend_source_proof"))
    require(role in set(NORMALIZED_BEND_ROLE_CODES) | {"NONE"}, "normalized bend manifest role is invalid: " + mesh)
    require(external.get("kind") == "send_to_unreal_normalized_skeletal_part"
            and external.get("pivot_contract") == "normalized_attachment_origin_0_0_0",
            "explicit bend metadata requires the normalized attachment source: " + mesh)
    require(isinstance(proof, dict) and proof.get("schema_version") == 2,
            "normalized bend source proof schema is invalid: " + mesh)
    asset = mesh.rsplit("/", 1)[-1]
    source = external.get("source_blend") or {}
    require(str(proof.get("prototype_asset") or "").casefold() == asset.casefold()
            and (not part.get("asset_name") or str(part["asset_name"]).casefold() == asset.casefold())
            and isinstance(proof.get("prototype_index"), int) and not isinstance(proof["prototype_index"], bool)
            and proof["prototype_index"] > 0
            and (external.get("source_prototype_index") is None
                 or (type(external["source_prototype_index"]) is int
                     and proof["prototype_index"] == external["source_prototype_index"]))
            and isinstance(source, dict) and source.get("exists") is True and bool(source.get("path"))
            and type(source.get("size")) is int and source["size"] > 0
            and re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256") or "")) is not None
            and proof.get("source_blend") == source
            and all(re.fullmatch(r"[0-9a-f]{64}", str(proof.get(key) or "")) is not None
                    for key in ("normalization_build_sha256", "physical_capture_contract_sha256", "source_3d_contract_sha256")),
            "normalized bend source proof differs from its exact prototype binding: " + mesh)
    expected_policy = normalization_part_bend_policy(asset, role=role)
    require(policy == expected_policy and (policy is None or all(type(policy.get(key)) is int
            for key in ("schema_version", "role_code", "uv_index"))), "normalized bend manifest policy is stale: " + mesh)
    if policy is None:
        require(payload is None, "disabled normalized bend manifest contains an active payload: " + mesh)
    else:
        require(part_bend_build_matches_policy({"part_bend_role": role, "prototype_count": 1,
                "prototypes": [{"part_bend_payload": payload}]}, policy),
                "normalized bend manifest payload is stale or missing: " + mesh)
        expected_bounds = external.get("expected_normalized_bounds") or {}
        require(all(expected_bounds.get(key) == payload["normalized_bounds"].get(key)
                    for key in ("minimum", "maximum", "size")),
                "normalized bend manifest payload bounds differ from its source binding: " + mesh)
    return role


def validate_manifest_bend_metadata(manifest, asset_contract=None):
    """Validate every descriptor, including NONE, before any Assembly write."""
    roles = []
    mapping = (asset_contract or {}).get("parts") or {}
    for part in (manifest or {}).get("parts") or []:
        source_role = str(part.get("role") or "")
        require(source_role in {"branch", "cluster", "leaf", "leaf_side"}, "unknown Assembly manifest role: " + source_role)
        external = part.get("external_source") or {}
        has_metadata = any(str(key).startswith("part_bend_") for key in external)
        mesh = package_path(mapping.get(part.get("prototype_id"))) if asset_contract is not None else "/Game/" + str(part.get("asset_name") or "")
        require(not has_metadata or mesh.casefold().startswith("/game/") and bool(mesh[6:]),
                "normalized bend metadata has no exact prototype binding")
        roles.append(manifest_part_bend_role(part, mesh))
    return roles


def plan_manifest_parts(manifest, asset_contract, actual_parts):
    """Resolve native deduplicated part order; no species/name heuristics."""
    require(isinstance(manifest, dict) and isinstance(asset_contract, dict), "normalized bend requires a validated manifest and asset contract")
    grouped = {}
    mapping = asset_contract.get("parts") or {}
    for part in manifest.get("parts") or []:
        prototype = str(part.get("prototype_id") or "")
        mesh = package_path(mapping.get(prototype))
        require(mesh.casefold().startswith("/game/"), "manifest part has no resolved Unreal prototype: " + prototype)
        source_role = str(part.get("role") or "")
        require(source_role in {"branch", "cluster", "leaf", "leaf_side"}, "unknown Assembly manifest role: " + source_role)
        role = manifest_part_bend_role(part, mesh)
        if role in NORMALIZED_BEND_ROLE_CODES:
            external = part.get("external_source") or {}
            require(external.get("kind") == "send_to_unreal_normalized_skeletal_part", "part bend requires the actual normalized Send2UE source: " + mesh)
            if role == "cluster":
                require(external.get("pivot_contract") == "normalized_attachment_origin_0_0_0", "cluster bend requires the validated normalized attachment origin: " + mesh)
        code = NORMALIZED_BEND_ROLE_CODES.get(role, 0)
        identity = unreal_asset_identity(mesh)
        require(identity not in grouped or (grouped[identity]["role"] == code
                and grouped[identity]["source_role"] == source_role and grouped[identity]["bend_role"] == role),
                "one native part has conflicting manifest roles: " + mesh)
        grouped.setdefault(identity, {"mesh": mesh, "role": code, "source_role": source_role, "bend_role": role})
    expected = list(grouped)
    actual = [unreal_asset_identity(row.get("mesh")) for row in actual_parts]
    require(expected and actual == expected, "Assembly native paths/order differ from the validated manifest")
    # The native serialization remains authoritative evidence. Only comparison
    # keys ignore case; do not rewrite its paths or part/node ordering.
    return [{"index": index, **deepcopy(row), "mesh": package_path(actual_parts[index]["mesh"])}
            for index, row in enumerate(grouped.values())]


def require_native_proof(report, parts, *, validate_only=False):
    require(isinstance(report, dict) and report.get("ok") is True and not report.get("errors") and not report.get("returned_errors"), "normalized bend native verification failed: " + json.dumps(report, sort_keys=True))
    require(report.get("schema_version") == 2
            and report.get("operation") == "configure_normalized_assembly_bend_native",
            "normalized bend native proof has the wrong schema/operation")
    leaves = [row for row in parts if row["role"]]
    require(report.get("payload_checked_prototype_count") == len(leaves)
            and report.get("payload_verified_before_material_mutation") is True,
            "normalized bend native proof omitted a part payload")
    proofs = report.get("uv3_verifications") or []
    require(len(proofs) == len(leaves), "normalized bend UV3 proof count differs from manifest")
    for part, proof in zip(leaves, proofs):
        require(unreal_asset_identity(proof.get("prototype")) == unreal_asset_identity(part["mesh"]) and proof.get("role") == part["role"], "normalized bend UV3 proof paths/roles differ from manifest")
        require(proof.get("import_file_sha1") == part["source_sha1"]
                and proof.get("native_import_md5_matches") is True,
                "normalized bend proof lacks the exact imported FBX generation")
        ratio, correction = proof.get("aspect_ratio"), proof.get("correction_factor")
        require(proof.get("verified") is True and proof.get("uv_channel_count") == 4
                and isinstance(ratio, (int, float)) and isinstance(correction, (int, float))
                and math.isfinite(ratio) and ratio > 0 and math.isfinite(correction)
                and math.isclose(correction * correction, max(1.0, ratio), rel_tol=1e-5)
                and proof.get("analytical_pivot_mask") == 0.0,
                "normalized bend native proof lacks the verified v2 aspect payload")
    if not validate_only:
        require(report.get("root_source_mesh_description_preserved") is True
                and report.get("branch_materials_preserved") is True
                and report.get("production_prototype_materials_directly_applied") is True,
                "normalized bend native remap lacks root/branch/prototype preservation proof")
    return report


def material_helpers(unreal):
    """Load the audited graph/copy helpers, excluding every top-level action."""
    script = Path(unreal.Paths.project_dir()) / "Scripts/NaniteAssemblyPartBend/materials.py"
    require(script.is_file(), "isolated material authoring helpers are missing")
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    nodes = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign))]
    nodes = [node for node in nodes if not isinstance(node, ast.Assign) or all(not isinstance(target, ast.Name) or target.id != "REPORT" for target in node.targets)]
    scope = {"__name__": "normalized_bend_material_helpers"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(script), "exec"), scope)
    scope["REPORT"] = {"resumed_after_inspection": [], "setter_return_diagnostics": {}}
    return scope


def protected_base_fields(text):
    """Exclude only the two authorized top-level WPO bounds fields."""
    require(isinstance(text, str) and text.startswith("(") and text.endswith(")"), "invalid material base override export")
    result, start, depth, quoted, escaped = [], 1, 0, False, False
    for index in range(1, len(text) - 1):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            require(depth >= 0, "unbalanced material base override export")
        elif char == "," and depth == 0:
            result.append(text[start:index])
            start = index + 1
    require(depth == 0 and not quoted, "incomplete material base override export")
    result.append(text[start:-1])
    fields = [token.partition("=")[0].strip() for token in result]
    require(all(token.partition("=")[1] for token in result) and all(fields.count(field) == 1 for field in WPO_FIELDS), "material WPO bounds fields are missing or duplicated")
    return tuple(token for token, field in zip(result, fields) if field not in WPO_FIELDS)


def protected_mi_signature(unreal, helpers, asset):
    signature = helpers["mi_signature"](asset)
    signature.pop("parent", None)
    signature["base_property_overrides"] = protected_base_fields(signature["base_property_overrides"])
    ignored = set(DEFAULT_SCALARS) | set(EXTRA_SCALARS)
    signature["scalar_parameter_values"] = [helpers["stable_value"](row) for row in asset.get_editor_property("scalar_parameter_values")
        if not (str(row.get_editor_property("parameter_info").get_editor_property("name")) in ignored
                and row.get_editor_property("parameter_info").get_editor_property("association") == unreal.MaterialParameterAssociation.GLOBAL_PARAMETER)]
    signature["effective_static_switches"].pop(helpers["SWITCH"], None)
    signature["layers"].pop("material_interface", None)
    return signature


def inherited_layer_signature(helpers, asset):
    """An intermediate clone may change only parent and the new bend switch view."""
    signature = deepcopy(helpers["mi_signature"](asset))
    signature.pop("parent", None)
    signature["effective_static_switches"].pop(helpers["SWITCH"], None)
    signature["layers"].pop("material_interface", None)
    return signature


def material_root_snapshot(helpers, root):
    return {"graph": helpers["graph"](root), "roots": helpers["roots"](root),
            "wpo_bound": helpers["property_text"](root, "max_world_position_offset_displacement")}


def material_scalar_defaults(unreal, function):
    """v2 stays neutral; the two response inputs require the exact v3 function."""
    policy = str(unreal.EditorAssetLibrary.get_metadata_tag(function, RETURN_TAG) or "")
    require(not policy or policy == RETURN_OWNER, "bend function has an unknown local response policy")
    return {name: value for name, value in {**DEFAULT_SCALARS, **EXTRA_SCALARS}.items()
            if policy == RETURN_OWNER or name not in RETURN_SCALARS}


def source_chain_signatures(hierarchy, helpers, source_snapshots, root_snapshots):
    signatures = {}
    for path in hierarchy["instances"]:
        chain, current = [], path
        while current in hierarchy["instances"]:
            chain.append({"source": current, "signature": source_snapshots[current]})
            current = hierarchy["parents"][current]
        signatures[path] = helpers["digest"]({"instances": chain,
            "root": {"source": current, "signature": root_snapshots[current]}})
    return signatures


def destination_snapshot(unreal, helpers, path):
    library = unreal.EditorAssetLibrary
    if not library.does_asset_exist(path):
        return None
    asset = library.load_asset(path)
    require(isinstance(asset, (unreal.Material, unreal.MaterialInstanceConstant)),
            "normalized bend destination has the wrong class: " + path)
    tags = {key: library.get_metadata_tag(asset, key) for key in
            (OWNER_TAG, SOURCE_TAG, SIGNATURE_TAG, SOURCE_SIGNATURE_TAG, CHAIN_SIGNATURE_TAG,
             STATE_TAG, helpers["OWNER_TAG"], helpers["SOURCE_TAG"], helpers["GRAPH_TAG"])}
    signature = (material_root_snapshot(helpers, asset) if isinstance(asset, unreal.Material)
                 else helpers["mi_signature"](asset))
    return {"kind": "Material" if isinstance(asset, unreal.Material) else "MIC",
            "tags": tags, "signature": signature}


def verified_legacy_notess_parent(unreal, helpers, asset, expected_parent):
    """Retain the existing Willow NoTess parent without changing a shared MI.

    Only an exact graph/root/WPO clone of the expected master qualifies.
    Artist wind overrides are retained using the unchanged source stamp and
    non-wind parameter proof; ordinary source/inheritance guards still apply.
    """
    parent = asset.get_editor_property("parent")
    expected = unreal.EditorAssetLibrary.load_asset(expected_parent)
    if not (isinstance(parent, unreal.Material) and isinstance(expected, unreal.Material)
            and package_path(parent.get_path_name()) == package_path(expected_parent) + "_NoTess"):
        return False
    from assembly_part_notess import _normalize
    identities = {}
    for value in (parent, expected):
        path = package_path(value.get_path_name())
        identities[path] = "<master>"; identities[value.get_path_name()] = "<master>"
    require(parent.get_editor_property("enable_tessellation") is False and
            _normalize(material_root_snapshot(helpers, parent), identities) ==
            _normalize(material_root_snapshot(helpers, expected), identities),
            "legacy NoTess master differs from its original graph/WPO")
    current = helpers["mi_signature"](asset)
    previous = _normalize(deepcopy(current), {parent.get_path_name():expected.get_path_name(),
        package_path(parent.get_path_name()):package_path(expected.get_path_name())})
    previous["parent"] = expected.get_path_name()
    stored = unreal.EditorAssetLibrary.get_metadata_tag(asset, SIGNATURE_TAG)
    candidates = [helpers["digest"](current), helpers["digest"](previous)]
    # The adopted trial changed exactly these two Tessellation override bits
    # along with the parent. Reconstruct only those finite historical values;
    # any tint/texture/static/bend/WPO change still invalidates the digest.
    for override in ("False", "True"):
        for enabled in ("False", "True"):
            candidate = deepcopy(previous)
            text = candidate.get("base_property_overrides", "")
            text = re.sub(r"\bbOverride_bEnableTessellation=(?:True|False)",
                          "bOverride_bEnableTessellation="+override, text)
            text = re.sub(r"\bbEnableTessellation=(?:True|False)", "bEnableTessellation="+enabled, text)
            candidate["base_property_overrides"] = text
            candidates.append(helpers["digest"](candidate))
    if stored not in candidates:
        # Existing production leaf variants also have artist-tuned wind
        # overrides. They are legitimate part values, not a reason to flatten
        # the chain or restore TessON. Keep their exact values; all non-wind
        # source fields and the independently stored source stamp must match.
        library = unreal.EditorAssetLibrary
        source = library.load_asset(library.get_metadata_tag(asset, SOURCE_TAG))
        require(isinstance(source, unreal.MaterialInstanceConstant)
                and library.get_metadata_tag(asset, OWNER_TAG) == OWNER
                and library.get_metadata_tag(asset, SOURCE_SIGNATURE_TAG) == helpers["digest"](helpers["mi_signature"](source)),
                "legacy NoTess MI changed more than its verified parent")
        actual = protected_mi_signature(unreal, helpers, asset)
        original = protected_mi_signature(unreal, helpers, source)
        for signature in (actual, original):
            signature["base_property_overrides"] = tuple(token for token in signature["base_property_overrides"]
                if token.partition('=')[0].strip() not in {"bOverride_bEnableTessellation", "bEnableTessellation"})
        require(actual == original and
                unreal.MaterialEditingLibrary.get_material_instance_static_switch_parameter_value(asset, helpers["SWITCH"]),
                "legacy NoTess MI changed more than its verified parent")
        base = asset.get_editor_property("base_property_overrides")
        require(base.get_editor_property("override_max_world_position_offset_displacement") is True
                and math.isclose(base.get_editor_property("max_world_position_offset_displacement"), 300.0, abs_tol=1e-6),
                "legacy NoTess MI has an unverified WPO bound")
    return True


def inspect_material_variants(unreal, source_paths, *, function=None, active_source_indices=None):
    """Freeze every source layer and reject all destinations before checkout."""
    if function is not None:
        material_scalar_defaults(unreal, function)
    hierarchy = resolve_material_hierarchy(unreal, source_paths, active_source_indices=active_source_indices)
    helpers, library = material_helpers(unreal), unreal.EditorAssetLibrary
    plan = hierarchy["plan"]
    dirty = {unreal_asset_identity(package.get_path_name()) for package in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    require(not ({unreal_asset_identity(path) for path in plan["materials"]} & dirty), "normalized bend destination scope has dirty packages")
    ancestors = set(hierarchy["instances"]) - set(plan["active_source_materials"])
    require(not ({unreal_asset_identity(path) for path in ancestors} & dirty), "source material inheritance has unsaved intermediate MIC changes")
    source_snapshots = {path: helpers["mi_signature"](asset) for path, asset in hierarchy["instances"].items()}
    root_snapshots = {path: material_root_snapshot(helpers, asset) for path, asset in hierarchy["roots"].items()}
    chain_signatures = source_chain_signatures(hierarchy, helpers, source_snapshots, root_snapshots)
    for source_path, source in hierarchy["instances"].items():
        path = variant_path(source_path)
        if not library.does_asset_exist(path):
            continue
        asset = library.load_asset(path)
        require(isinstance(asset, unreal.MaterialInstanceConstant), "normalized bend destination is not a MIC: " + path)
        parent_path = hierarchy["parents"][source_path]
        expected_parent = variant_path(parent_path, master=parent_path in hierarchy["roots"])
        legacy_notess = verified_legacy_notess_parent(unreal, helpers, asset, expected_parent)
        require(library.get_metadata_tag(asset, OWNER_TAG) == OWNER
                and unreal_asset_identity(library.get_metadata_tag(asset, SOURCE_TAG)) == unreal_asset_identity(source_path)
                and library.load_asset(library.get_metadata_tag(asset, SOURCE_TAG)) == source
                and (legacy_notess or library.get_metadata_tag(asset, SIGNATURE_TAG) == helpers["digest"](helpers["mi_signature"](asset))),
                "existing normalized bend MI is unowned or changed: " + path)
        require(library.get_metadata_tag(asset, STATE_TAG) == "complete", "existing normalized bend MI is incomplete: " + path)
        require(library.get_metadata_tag(asset, SOURCE_SIGNATURE_TAG) == helpers["digest"](source_snapshots[source_path]),
                "source MI changed since generated bend variant: " + path)
        parent = asset.get_editor_property("parent")
        require(legacy_notess or parent is not None and unreal_asset_identity(parent.get_path_name()) == unreal_asset_identity(expected_parent)
                and library.load_asset(expected_parent) == parent,
                "existing normalized bend MI flattened or changed its immediate parent: " + path)
        stored_chain = library.get_metadata_tag(asset, CHAIN_SIGNATURE_TAG)
        # Legacy direct-parent derivatives have no chain tag. Their original
        # MI and root-wrapper guards still apply; a multi-layer chain needs one.
        require(stored_chain == chain_signatures[source_path] or not stored_chain and parent_path in hierarchy["roots"],
                "source material inheritance changed since generated bend variant: " + path)
        if source_path in plan["active_source_materials"]:
            require(protected_mi_signature(unreal, helpers, asset) == protected_mi_signature(unreal, helpers, source),
                    "existing bend clone changed protected source overrides: " + path)
        else:
            require(inherited_layer_signature(helpers, asset) == inherited_layer_signature(helpers, source),
                    "existing intermediate bend clone changed local source overrides: " + path)
    for path, root in hierarchy["roots"].items():
        snapshot = root_snapshots[path]
        existing = helpers["owned_existing"](variant_path(path, master=True),
            helpers["digest"]({"graph": snapshot["graph"], "roots": snapshot["roots"]}), allow_building=True)
        if existing:
            require(isinstance(existing, unreal.Material), "normalized bend root wrapper has the wrong class")
            if function is None:
                function = library.load_asset(FUNCTION)
            require(function is not None, "existing root wrapper preflight requires the installed bend function")
            helpers["validate_master_contract"](existing, function, snapshot["graph"], snapshot["roots"])
            require(helpers["property_text"](existing, "max_world_position_offset_displacement") == snapshot["wpo_bound"],
                    "generated root wrapper changed the source WPO bound")
    return {**hierarchy, "helpers": helpers, "source_snapshots": source_snapshots,
            "root_snapshots": root_snapshots, "chain_signatures": chain_signatures,
            "destinations": {path: destination_snapshot(unreal, helpers, path) for path in plan["materials"]},
            "function": function}


def verify_material_inspection(unreal, inspection, *, before_write=False):
    """Reject source/topology changes between planning, checkout and execution."""
    helpers, plan = inspection["helpers"], inspection["plan"]
    current = resolve_material_hierarchy(unreal, plan["source_materials"], active_source_indices=plan["active_source_indices"])
    require(current["plan"] == plan, "source material hierarchy changed after planning")
    for path, asset in current["instances"].items():
        require(helpers["mi_signature"](asset) == inspection["source_snapshots"][path],
                "source MIC changed after inspection: " + path)
    for path, asset in current["roots"].items():
        require(material_root_snapshot(helpers, asset) == inspection["root_snapshots"][path],
                "source root material changed after inspection: " + path)
    if before_write:
        dirty = {package.get_path_name() for package in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
        require(not (set(plan["materials"]) & dirty), "normalized bend destination became dirty before writes")
        ancestors = set(current["instances"]) - set(plan["active_source_materials"])
        require(not (ancestors & dirty), "source intermediate MIC became dirty before writes")
        for path in plan["materials"]:
            require(destination_snapshot(unreal, helpers, path) == inspection["destinations"][path],
                    "normalized bend destination changed after inspection: " + path)


def make_material_variants(unreal, source_paths, function, *, admission=None, inspection=None, active_source_indices=None):
    """Clone the full inheritance chain; only requested leaf MIs get bend defaults."""
    inspection = inspection or inspect_material_variants(unreal, source_paths, function=function, active_source_indices=active_source_indices)
    helpers, plan = inspection["helpers"], inspection["plan"]
    require(inspection["function"] is None or inspection["function"] == function,
            "bend function changed between root-wrapper inspection and generation")
    indices = plan["active_source_indices"] if active_source_indices is None else active_source_indices
    require(resolve_material_hierarchy(unreal, source_paths, active_source_indices=indices)["plan"] == plan, "material generation differs from inspected source hierarchy")
    if admission is not None:
        require(admission.get("verified") is True and admission.get("materials") == plan["materials"],
                "material generation lacks exact parent-chain dependency checkout")
    check_dependency_admission(admission)
    verify_material_inspection(unreal, inspection, before_write=True)
    library, edit = unreal.EditorAssetLibrary, unreal.MaterialEditingLibrary
    parameters = material_scalar_defaults(unreal, function)
    generated, wrappers, clones, compile_proofs = {}, {}, {}, []
    for source_path, root in inspection["roots"].items():
        check_dependency_admission(admission)
        snapshot = inspection["root_snapshots"][source_path]
        wrapper, created = helpers["make_master"](root, variant_path(source_path, master=True), function,
                                                  snapshot["graph"], snapshot["roots"])
        wrappers[source_path] = wrapper
        generated[package_path(wrapper.get_path_name())] = wrapper
    for source_path, source in inspection["instances"].items():
        check_dependency_admission(admission)
        target_path = variant_path(source_path)
        target = library.load_asset(target_path) if library.does_asset_exist(target_path) else library.duplicate_asset(source.get_path_name(), target_path)
        require(isinstance(target, unreal.MaterialInstanceConstant), "normalized bend MIC duplication failed")
        leaf = source_path in plan["active_source_materials"]
        preserved = (protected_mi_signature(unreal, helpers, source) if leaf
                     else inherited_layer_signature(helpers, source))
        parent_path = inspection["parents"][source_path]
        parent = wrappers[parent_path] if parent_path in wrappers else clones[parent_path]
        legacy_notess = verified_legacy_notess_parent(unreal, helpers, target, parent.get_path_name())
        if legacy_notess:
            parent = target.get_editor_property("parent")
        target.modify()
        edit.set_material_instance_parent(target, parent)
        require(target.get_editor_property("parent") == parent, "normalized bend immediate parent readback failed")
        if leaf:
            edit.set_material_instance_static_switch_parameter_value(target, helpers["SWITCH"], True)
            require(set(parameters) <= {str(name) for name in edit.get_scalar_parameter_names(target)}, "upgraded function does not expose the normalized bend scalar contract")
            for name, value in parameters.items():
                if legacy_notess and edit.is_material_instance_parameter_overridden(target, name):
                    continue  # Preserve the adopted production wind tuning.
                edit.set_material_instance_scalar_parameter_value(target, name, value)
                require(math.isclose(edit.get_material_instance_scalar_parameter_value(target, name), value, rel_tol=0, abs_tol=1e-6)
                        and edit.is_material_instance_parameter_overridden(target, name), "normalized bend scalar readback failed: " + name)
            base = target.get_editor_property("base_property_overrides")
            base.set_editor_property("override_max_world_position_offset_displacement", True)
            base.set_editor_property("max_world_position_offset_displacement", 300.0)
            target.set_editor_property("base_property_overrides", base)
        edit.update_material_instance(target)
        if leaf:
            require(edit.get_material_instance_static_switch_parameter_value(target, helpers["SWITCH"])
                    and edit.is_material_instance_parameter_overridden(target, helpers["SWITCH"]), "normalized bend static switch readback failed")
        actual = (protected_mi_signature(unreal, helpers, target) if leaf
                  else inherited_layer_signature(helpers, target))
        require(actual == preserved, "normalized bend MIC changed protected source overrides/inheritance: " + source_path)
        require(helpers["mi_signature"](source) == inspection["source_snapshots"][source_path], "normalized bend changed original source MIC")
        for key, value in {OWNER_TAG: OWNER, SOURCE_TAG: source_path,
                SOURCE_SIGNATURE_TAG: helpers["digest"](inspection["source_snapshots"][source_path]),
                CHAIN_SIGNATURE_TAG: inspection["chain_signatures"][source_path]}.items():
            library.set_metadata_tag(target, key, value)
        library.set_metadata_tag(target, SIGNATURE_TAG, helpers["digest"](helpers["mi_signature"](target)))
        clones[source_path] = target
        generated[target_path] = target
    require([unreal_asset_identity(path) for path in generated] == [unreal_asset_identity(path) for path in plan["materials"]], "material generation escaped its exact parent-chain plan")
    for path, asset in generated.items():
        check_dependency_admission(admission)
        compile_proofs.append(helpers["compile_resource"](asset))
        if isinstance(asset, unreal.MaterialInstanceConstant):
            source_path = library.get_metadata_tag(asset, SOURCE_TAG)
            source = inspection["instances"][source_path]
            leaf = source_path in plan["active_source_materials"]
            actual = (protected_mi_signature(unreal, helpers, asset) if leaf else inherited_layer_signature(helpers, asset))
            preserved = (protected_mi_signature(unreal, helpers, source) if leaf else inherited_layer_signature(helpers, source))
            require(actual == preserved and library.get_metadata_tag(asset, SIGNATURE_TAG) == helpers["digest"](helpers["mi_signature"](asset)),
                    "compiled bend clone changed authored inheritance/overrides: " + path)
        library.set_metadata_tag(asset, STATE_TAG, "complete")
        if isinstance(asset, unreal.Material):
            library.set_metadata_tag(asset, helpers["GRAPH_TAG"], helpers["graph_signature"](asset))
            library.set_metadata_tag(asset, helpers["STATE_TAG"], "complete")
    verify_material_inspection(unreal, inspection)
    return {"variants": [variant_path(path) if index in plan["active_source_indices"] else path
                         for index, path in enumerate(plan["source_materials"])],
            "packages": list(generated), "compile": compile_proofs}


def normalize_assembly(unreal, assembly, manifest, asset_contract, *, apply, allow_dirty, parse_parts, native_json, slot_record, audit, prepare_dependencies=None):
    """Preflight the delivery chain, compile isolated MIs, then route native parts."""
    path = package_path(assembly.get_path_name())
    before_parts = parse_parts(assembly.get_editor_property("nanite_settings").export_text())
    parts = plan_manifest_parts(manifest, asset_contract, before_parts)
    if not any(part["role"] for part in parts):
        return None
    library = unreal.EditorAssetLibrary
    require(unreal_asset_identity(asset_contract.get("assembly")) == unreal_asset_identity(path)
            and library.load_asset(asset_contract.get("assembly")) == assembly, "bend target differs from validated Assembly contract")
    native_by_identity = {unreal_asset_identity(part["mesh"]): part for part in parts}
    for authored in manifest.get("parts") or []:
        expected_path = asset_contract["parts"][authored["prototype_id"]]
        native_part = native_by_identity[unreal_asset_identity(expected_path)]
        actual_mesh = library.load_asset(native_part["mesh"])
        require(isinstance(actual_mesh, unreal.SkeletalMesh)
                and actual_mesh == library.load_asset(expected_path)
                and unreal_asset_identity(actual_mesh.get_path_name()) == unreal_asset_identity(expected_path),
                "normalized part native/manifest paths do not resolve to the same SkeletalMesh: " + expected_path)
    native = getattr(unreal, "CodexAssemblyPartBendUvLibrary", None)
    require(native is not None and callable(getattr(native, "configure_normalized_assembly_bend", None)), "normalized Assembly bend native API is unavailable; rebuild the visible editor first")
    function = library.load_asset(FUNCTION)
    require(function is not None and library.get_metadata_tag(function, ASPECT_TAG) == ASPECT_POLICY, "UV3 aspect payload v2 requires the installed v2 material function")
    dirty = {unreal_asset_identity(package.get_path_name()) for package in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    require(not (apply and not allow_dirty and unreal_asset_identity(path) in dirty), "Assembly already has unsaved user changes: " + path)
    for part in parts:
        if not part["role"]:
            continue
        mesh = library.load_asset(part["mesh"])
        require(isinstance(mesh, unreal.SkeletalMesh), "normalized part prototype is missing: " + part["mesh"])
        data = mesh.get_editor_property("asset_import_data")
        filenames = list(data.extract_filenames() or []) if data else []
        require(len(filenames) == 1, "normalized part needs one exact FBX import source")
        source = Path(filenames[0]).expanduser().absolute()
        require(source.is_file() and source.suffix.casefold() == ".fbx" and source.stem.casefold() == part["mesh"].rsplit("/", 1)[-1].casefold(), "normalized part is missing its exact source FBX: " + str(source))
        part.update(source_file=str(source), source_sha1=sha1_file(source))
    before_slots = list(assembly.get_editor_property("materials") or [])
    previous = str(library.get_metadata_tag(assembly, CONTRACT_TAG) or "")
    previous_contract = json.loads(previous) if previous else None
    if previous_contract:
        require(previous_contract.get("schema_version") == 2 and unreal_asset_identity(previous_contract.get("assembly")) == unreal_asset_identity(path), "normalized bend previous contract is invalid")
        source_paths = previous_contract["source_materials"]
    else:
        source_paths = []
        for slot in before_slots:
            material = slot.get_editor_property("material_interface")
            require(material is not None, "normalized bend Assembly has an unassigned material")
            source_paths.append(package_path(library.get_metadata_tag(material, SOURCE_TAG) or material.get_path_name()))
    active_indices = plan_active_source_indices(len(source_paths), parts, before_parts, len(before_slots), previous_contract)
    contract = {"schema_version": 2, "assembly": path, "parts": parts, "source_materials": source_paths,
                "active_source_indices": active_indices, "variant_materials": []}
    preflight = require_native_proof(native_json(native.configure_normalized_assembly_bend(path, json.dumps({**contract, "validate_only": True}))), parts, validate_only=True)
    require(preflight.get("active_source_indices") == active_indices
            and preflight.get("planned_material_count") == len(source_paths) + len(active_indices),
            "native active material plan differs from the verified numeric-role remaps")
    materials = {"variants": [variant_path(value) if index in active_indices else value
                              for index, value in enumerate(source_paths)], "packages": [], "compile": []}
    native_remap = None
    dependency_checkout = None
    if apply:
        require(callable(prepare_dependencies), "normalized bend requires exact dependency checkout ownership before material writes")
        plan = plan_material_packages(unreal, source_paths, active_source_indices=active_indices)
        inspection = inspect_material_variants(unreal, source_paths, function=function, active_source_indices=active_indices)
        require(inspection["plan"] == plan, "material hierarchy changed between planning and inspection")
        dependency_checkout = prepare_dependencies({"assembly": path,
            "prototypes": [part["mesh"] for part in parts if part["role"]], **plan})
        require(isinstance(dependency_checkout, dict) and dependency_checkout.get("verified") is True
                and dependency_checkout.get("materials") == plan["materials"],
                "normalized bend dependency checkout did not verify the frozen material scope")
        materials = make_material_variants(unreal, source_paths, function, admission=dependency_checkout, inspection=inspection,
                                          active_source_indices=active_indices)
        require(set(materials["packages"]) == set(plan["materials"]), "material generation escaped the frozen dependency scope")
        contract["variant_materials"] = materials["variants"]
        native_remap = require_native_proof(native_json(native.configure_normalized_assembly_bend(path, json.dumps(contract))), parts)
        require(native_remap.get("active_source_indices") == active_indices
                and native_remap.get("material_count") == len(source_paths) + len(active_indices),
                "native active material remap did not preserve its exact compact table")
        assembly.modify()
        library.set_metadata_tag(assembly, CONTRACT_TAG, json.dumps(contract, sort_keys=True))
    else:
        contract["variant_materials"] = materials["variants"]
    after_slots = list(assembly.get_editor_property("materials") or [])
    after_parts = parse_parts(assembly.get_editor_property("nanite_settings").export_text())
    allowed = list(dict.fromkeys(materials["packages"] + [part["mesh"] for part in parts if part["role"]]))
    dirty = {unreal_asset_identity(package.get_path_name()) for package in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    return {
        "assembly": assembly.get_path_name(), "apply_requested": bool(apply), "would_change": True,
        "changed": bool(apply), "before_material_count": len(before_slots), "after_material_count": len(after_slots),
        "global_slots": [slot_record(slot, index) for index, slot in enumerate(after_slots)],
        "parts": [{"index": row["index"], "mesh": row["mesh"], "existing_remap": before_parts[row["index"]]["remap"], "desired_remap": row["remap"]} for row in after_parts],
        "normalized_part_bend_policy": {"aspect_policy": ASPECT_POLICY, "contract": contract, "material_packages": materials["packages"]},
        "material_compile_proofs": materials["compile"], "uv3_verifications": (native_remap or preflight)["uv3_verifications"],
        "native_material_remap": native_remap, "native_preflight": preflight,
        "dependency_checkout": dependency_checkout,
        "dependent_packages_to_save": [value for value in allowed if unreal_asset_identity(value) in dirty] if apply else [],
        "dependency_save_status": "caller_owned" if apply else "not_requested",
        "assembly_section_audit": audit(unreal, path, len(after_slots)),
    }


def dependency_policy(report):
    """Validate exact normalized postbuild save ownership and native proof."""
    policy = report.get("normalized_part_bend_policy") or {}
    contract = policy.get("contract") or {}
    require(report.get("apply_requested") is True and policy.get("aspect_policy") == ASPECT_POLICY
            and contract.get("schema_version") == 2 and unreal_asset_identity(contract.get("assembly")) == unreal_asset_identity(report.get("assembly")), "normalized dependency policy does not match its Assembly")
    parts = contract.get("parts") or []
    require_native_proof(report.get("native_material_remap"), parts)
    prototypes = list(dict.fromkeys(part["mesh"] for part in parts if part["role"]))
    materials = policy.get("material_packages") or []
    require(isinstance(materials, list) and len({unreal_asset_identity(path) for path in materials}) == len(materials)
            and all(path.casefold().startswith((DESTINATION + "/").casefold()) and package_path(path) == path for path in materials), "normalized dependency material scope is malformed")
    variants = contract.get("variant_materials") or []
    if "active_source_indices" in contract:
        sources = contract.get("source_materials") or []
        indices = validate_active_source_indices(len(sources), contract["active_source_indices"])
        require(len(variants) == len(sources) and all(unreal_asset_identity(variants[index]) == unreal_asset_identity(source)
                for index, source in enumerate(sources) if index not in indices),
                "inactive material entries must preserve their original source identity")
        require(report["native_material_remap"].get("active_source_indices") == indices
                and report["native_material_remap"].get("material_count") == len(sources) + len(indices),
                "normalized dependency active map differs from its native proof")
        variants = [variants[index] for index in indices]
    require({unreal_asset_identity(path) for path in variants} <= {unreal_asset_identity(path) for path in materials}, "normalized dependency scope omits a material variant")
    compiled = report.get("material_compile_proofs") or []
    require({unreal_asset_identity(row.get("asset")) for row in compiled} == {unreal_asset_identity(path) for path in materials}
            and all(row.get("native_finish_compilation_waited") is True and row.get("shader_count", 0) > 0 and not row.get("recompile_errors") for row in compiled), "normalized dependency materials lack complete compilation receipts")
    return {"allowed": materials + prototypes, "prototypes": prototypes, "materials": materials}
