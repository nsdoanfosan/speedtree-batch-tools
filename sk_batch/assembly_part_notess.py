"""Default part-only NoTess delivery after Assembly wind/material normalization.

The original table (and therefore the Base) is never overwritten. Prototype
materials follow the same rule; geometry, UVs, nodes and skeletons are read-only.
Dependencies are admitted/saved by the
ingest caller before the final Assembly save. Importing this module is inert.
"""
from copy import deepcopy
import hashlib
import json
import re

DESTINATION = "/Game/Material/Tree/AssetTree/AssemblyNoTess"
OWNER = "SpeedTreeBatch/AssemblyPartNoTess/v2"
LEGACY_OWNER = "SpeedTreeBatch/AssemblyPartNoTess/v1"
OWNER_TAG = "SKBatchAssemblyPartNoTessOwner"
SOURCE_TAG = "SKBatchAssemblyPartNoTessSource"
SIGNATURE_TAG = "SKBatchAssemblyPartNoTessSignature"
CONTRACT_TAG = "SKBatchAssemblyPartNoTessContract"


def require(value, message):
    if not value:
        raise RuntimeError(message)


def package(value):
    return (value if isinstance(value, str) else value.get_path_name()).split(".", 1)[0]


def identity(value):
    """UE package identity is case-insensitive; authored graph values are not."""
    return package(value).casefold()


def legacy_destination(source, *, master=False):
    path = package(source)
    require(path.startswith("/Game/"), "NoTess source must be project-owned")
    prefix = "M_" if master else "MI_"
    stem = path.rsplit("/", 1)[-1].removeprefix(prefix)
    # Reuse the derivatives already adopted by the 51 production Assemblies.
    return DESTINATION + "/" + prefix + stem + "_PartNoTess_" + hashlib.sha256(path.encode()).hexdigest()[:12]


def destination(source, *, master=False):
    """One explicit, stable name for every assembly material, including OFF foliage."""
    path = package(source)
    require(identity(path).startswith('/game/'), 'NoTess source must be project-owned')
    stem = path.rsplit('/', 1)[-1]
    if identity(path).startswith(identity(DESTINATION) + '/') and stem.casefold().endswith('_notess'):
        return path
    if stem.casefold().endswith('_notess'):
        # A legacy OFF source outside this folder must not share a case-folded
        # short name with its independent assembly-only derivative.
        stem = stem[:-7] + '_Assembly'
    require(stem.casefold().startswith('m_' if master else 'mi_'), 'Unexpected NoTess material prefix')
    return DESTINATION + '/' + stem + '_notess'


def original_interface(unreal, asset):
    """Canonical derivatives retain their exact source, never resolve by short name."""
    path = package(asset)
    if not (identity(path).startswith(identity(DESTINATION) + '/') and path.casefold().endswith('_notess')):
        return asset
    library = unreal.EditorAssetLibrary
    require(library.get_metadata_tag(asset, OWNER_TAG) == OWNER, 'Unowned canonical NoTess material: ' + path)
    source = str(library.get_metadata_tag(asset, SOURCE_TAG) or '')
    require(source and identity(source) != identity(path), 'Canonical NoTess material lacks exact source: ' + path)
    original = library.load_asset(source)
    require(original is not None and type(original) is type(asset), 'NoTess original is missing or changed class')
    require(identity(destination(original, master=isinstance(original, unreal.Material))) == identity(path),
            'NoTess source identity/name collision: ' + path)
    return original


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _normalize(value, identities):
    if isinstance(value, dict):
        return {k: _normalize(v, identities) for k, v in value.items() if k != "active"}
    if isinstance(value, (list, tuple)):
        return [_normalize(v, identities) for v in value]
    if isinstance(value, str):
        # Only complete, known material package/object references are folded.
        # Parameter names, shader source and arbitrary string values stay exact.
        return identities.get(value, identities.get(value.casefold(), value))
    return value


def _signature(unreal, helpers, asset, identities):
    if isinstance(asset, unreal.Material):
        value = {"graph": helpers["graph"](asset), "roots": helpers["roots"](asset),
                 "wpo_bound": helpers["property_text"](asset, "max_world_position_offset_displacement")}
    else:
        value = deepcopy(helpers["mi_signature"](asset))
        value.pop("parent", None)
        value.get("layers", {}).pop("material_interface", None)
        # Unlike the bend helper, preserve WPO bounds and every other override.
        text = value["base_property_overrides"]
        value["base_property_overrides"] = re.sub(
            r"(?<![A-Za-z0-9_])(bOverride_bEnableTessellation|bEnableTessellation|EnableTessellation)=(True|False|true|false|0|1)",
            lambda m: m.group(1) + "=False", text)
    return _normalize(value, identities)


def _runtime(unreal, paths):
    from nanite_assembly_materials import _json_result
    data = _json_result(unreal.CodexMaterialToolsLibrary.dump_nanite_displacement_runtime(list(paths)))
    rows = {identity(r["path"]): r for r in data.get("materials", [])}
    require(set(rows) == {identity(p) for p in paths} and all(r.get("loaded") for r in rows.values()), "Incomplete NoTess material runtime audit")
    return {path: rows[identity(path)] for path in paths}


def _name_collision(unreal, sources, targets, conflicting_sources):
    """An explicit diagnostic precedes any checkout, duplication or save."""
    library = unreal.EditorAssetLibrary
    details = []
    for source in sorted(conflicting_sources, key=identity):
        asset, target = sources[source], targets[source]
        existing = library.load_asset(target) if library.does_asset_exist(target) else None
        parent = asset.get_editor_property('parent') if isinstance(asset, unreal.MaterialInstanceConstant) else None
        existing_parent = existing.get_editor_property('parent') if isinstance(existing, unreal.MaterialInstanceConstant) else None
        details.append({'source': source, 'source_parent': package(parent) if parent else None,
            'expected_target': target, 'existing_target': package(existing) if existing else None,
            'existing_parent': package(existing_parent) if existing_parent else None,
            'existing_owner': library.get_metadata_tag(existing, OWNER_TAG) if existing else None,
            'existing_source': library.get_metadata_tag(existing, SOURCE_TAG) if existing else None})
    raise RuntimeError('NoTess canonical name collision: ' + json.dumps(details, sort_keys=True))


def _off(row):
    return row.get("interface_tessellation_enabled") is False and row.get("base_tessellation_enabled") is False


def _assert_equivalent_runtime(source, target):
    require(_off(target) and target.get("relevance_uses_displacement") is False, "Assembly part master/interface is not NoTess")
    for field in ("relevance_uses_wpo", "interface_max_wpo_displacement", "interface_displacement_scaling"):
        require(source.get(field) == target.get(field), "NoTess delivery changed " + field)


def inspect_materials(unreal, source_paths, *, read_only=False, preserve_verified_legacy=False, legacy=False):
    """Read-only complete hierarchy and exact existing-derivative collision guard."""
    from normalized_assembly_bend import material_helpers
    helpers, library = material_helpers(unreal), unreal.EditorAssetLibrary
    references = {}
    for requested in source_paths:
        asset = library.load_asset(requested)
        require(asset is not None, "Assembly part material is missing: " + requested)
        references[package(requested)] = asset
        references.setdefault(package(asset), asset)
    actual_runtime = _runtime(unreal, list(dict.fromkeys(package(a) for a in references.values())))
    runtime = {path: actual_runtime[package(asset)] for path, asset in references.items()}
    sources, requested_sources, bindings, targets = {}, {}, {}, {}
    for path, asset in references.items():
        if legacy and _off(runtime[path]):
            bindings[path] = package(asset)
            continue
        if not legacy:
            asset = original_interface(unreal, asset)
        requested_source = package(asset)
        chain, seen = [], set()
        while asset is not None:
            current = package(asset)
            require(identity(current) not in seen and len(chain) < 64, "NoTess material parent cycle")
            seen.add(identity(current)); chain.append(asset)
            if isinstance(asset, unreal.Material):
                break
            require(isinstance(asset, unreal.MaterialInstanceConstant), "Unsupported NoTess parent class")
            asset = asset.get_editor_property("parent")
        require(chain and isinstance(chain[-1], unreal.Material), "NoTess material lacks a base master")
        for asset in reversed(chain):
            current = package(asset)
            if legacy and isinstance(asset, unreal.Material) and not asset.get_editor_property("enable_tessellation"):
                continue
            sources.setdefault(current, asset)
            targets[current] = (legacy_destination if legacy else destination)(asset, master=isinstance(asset, unreal.Material))
        requested_sources[path] = requested_source
    groups = {}
    for source, target in targets.items():
        groups.setdefault(identity(target), []).append(source)
    conflicts = [source for group in groups.values() if len(group) > 1 for source in group]
    if conflicts:
        _name_collision(unreal, sources, targets, conflicts)
    for source, target in targets.items():
        if not library.does_asset_exist(target):
            continue
        asset = library.load_asset(target)
        require(asset is not None and identity(asset) == identity(target), 'NoTess target identity changed: ' + target)
        targets[source] = package(asset)
        stamped_source = library.get_metadata_tag(asset, SOURCE_TAG)
        if type(asset) is not type(sources[source]) or (stamped_source and identity(stamped_source) != identity(source)):
            _name_collision(unreal, sources, targets, [source])
    bindings.update({path: targets[source] for path, source in requested_sources.items()})
    identities = {}
    for source, target in targets.items():
        for p in (source, target):
            identities[p] = "<material:" + source + ">"
            identities[p + "." + p.rsplit("/", 1)[-1]] = identities[p]
            identities[p.casefold()] = identities[p]
            identities[(p + "." + p.rsplit("/", 1)[-1]).casefold()] = identities[p]
    signatures = {p: _signature(unreal, helpers, a, identities) for p, a in sources.items()}
    dirty = {identity(p) for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    dirty_sources = [p for p in sources if identity(p) in dirty]
    require(read_only or not dirty_sources, "NoTess original source material is dirty: " + str(sorted(dirty_sources)))
    existing, writes = {}, []
    for source, target in targets.items():
        require(read_only or identity(target) not in dirty, "NoTess derivative has unsaved edits: " + target)
        if not library.does_asset_exist(target):
            writes.append(target)
            continue
        asset = library.load_asset(target)
        require(type(asset) is type(sources[source]), "NoTess destination class collision: " + target)
        require(_signature(unreal, helpers, asset, identities) == signatures[source],
                "Existing NoTess derivative differs from its source; preserve its changes: " + target)
        if isinstance(asset, unreal.MaterialInstanceConstant):
            parent = sources[source].get_editor_property("parent")
            expected = targets.get(package(parent), package(parent))
            require(identity(asset.get_editor_property("parent")) == identity(expected), "NoTess parent chain was flattened/changed")
        require(_off(_runtime(unreal, [target])[target]), "Existing derivative base/interface is TessON")
        owner = library.get_metadata_tag(asset, OWNER_TAG)
        require(not owner or owner == (LEGACY_OWNER if legacy else OWNER), "NoTess destination has another owner")
        if owner:
            require(identity(str(library.get_metadata_tag(asset, SOURCE_TAG) or '')) == identity(source) and
                    library.get_metadata_tag(asset, SIGNATURE_TAG) == digest(signatures[source]),
                    "NoTess ownership/source signature changed")
        elif not (legacy and preserve_verified_legacy):
            # Legacy 51-root derivatives are adopted only after exact graph,
            # overrides, chain, runtime and deterministic-path verification.
            # Canonical prototype interfaces always need their exact source
            # stamp so the next import can safely unwrap them.
            writes.append(target)
        existing[source] = asset
    return {"helpers": helpers, "sources": sources, "targets": targets, "bindings": bindings,
            "signatures": signatures, "identities": identities, "existing": existing,
            "write_packages": writes, "runtime_before": runtime,
            "dirty_inputs_observed": sorted(p for p in set(sources) | set(targets.values()) if identity(p) in dirty), "read_only": bool(read_only)}


def plan_remaps(snapshot, bindings, slot_counts):
    """Stable part-only aliases; original slots and Base are retained verbatim."""
    before = snapshot["current_materials"]
    appends, aliases, parts = [], {}, []
    for row in snapshot["current_parts"]:
        old = list(row["expected_remap"])
        indices = old or list(range(slot_counts[row["mesh"]]))
        require(len(indices) == slot_counts[row["mesh"]], "Part local/remap length mismatch")
        target = []
        for index in indices:
            require(type(index) is int and 0 <= index < len(before), "Invalid part source index")
            source = before[index]; replacement = bindings[source]
            if identity(replacement) == identity(source):
                target.append(index)
                continue
            if index not in aliases:
                aliases[index] = len(before) + len(appends)
                appends.append({"source_slot": index, "material": replacement})
            target.append(aliases[index])
        parts.append({**row, "target_remap": target})
    return {"append_materials": appends, "parts": parts}


def authored_slot_export(text):
    """Keep authored UV overrides; ignore only non-overridden stream caches."""
    if not text.startswith("("):
        return text
    def fields(value):
        require(value.startswith("(") and value.endswith(")"), "Invalid exported material struct")
        chunks=[];start=1;depth=0;quoted=False;escaped=False
        for i,char in enumerate(value[1:-1],1):
            if quoted:
                if escaped:escaped=False
                elif char=='\\':escaped=True
                elif char=='"':quoted=False
            elif char=='"':quoted=True
            elif char=='(':depth+=1
            elif char==')':depth-=1
            elif char==',' and depth==0:chunks.append(value[start:i]);start=i+1
            require(depth>=0,"Unbalanced material struct")
        require(depth==0 and not quoted,"Unbalanced material export")
        chunks.append(value[start:-1])
        result=[]
        for chunk in chunks:
            key,sep,data=chunk.partition('=');require(sep,"Unnamed material field")
            result.append((key.strip(),data.strip()))
        require(len({k.casefold() for k,v in result})==len(result),"Duplicate material field")
        return result
    result=[]
    for key,value in fields(text):
        if key.casefold()=="uvchanneldata":
            uv=fields(value);overrides=[v.casefold() for k,v in uv if k.casefold()=="boverridedensities"]
            require(len(overrides)==1 and overrides[0] in ('true','false'),"Unverified UV density override")
            if overrides[0]=='false':
                uv=[(k,v) for k,v in uv if k.casefold()!='binitialized' and not k.casefold().startswith('localuvdensities')]
            value=uv
        result.append((key,value))
    return result


def set_alias_slot_names(slot, name):
    """Import the copied FName field; UE exposes it as Python read-only."""
    slot.set_editor_property("material_slot_name", name)
    text,count=re.subn(r'(?<![A-Za-z0-9_])ImportedMaterialSlotName=(?:"(?:\\.|[^"\\])*"|[^,)]+)',
        lambda match:'ImportedMaterialSlotName='+json.dumps(name,ensure_ascii=False),slot.export_text())
    require(count==1,"NoTess alias lacks one exact imported slot name field")
    slot.import_text(text)
    require(str(slot.get_editor_property("material_slot_name"))==name and
            str(slot.get_editor_property("imported_material_slot_name"))==name,"NoTess alias slot name readback differs")


def apply_default_part_notess(unreal, assembly, *, prepare_dependencies, preserve_verified_legacy=False,
                             prepare_prototypes=None, prepare_root=None):
    """Pipeline-owned dirty root allowed; exact native preflight, no source writes."""
    from nanite_assembly_materials import _json_result, parse_nanite_assembly_part_remaps
    from normalized_assembly_bend import check_dependency_admission
    library, edit = unreal.EditorAssetLibrary, unreal.MaterialEditingLibrary
    path = package(assembly)
    native = unreal.CodexAssemblyPartBendUvLibrary.configure_normalized_assembly_bend
    baseline = _json_result(native(path, json.dumps({"schema_version": 2, "assembly": path,
        "kind": "assembly_part_notess_remap_v1", "validate_only": True})))
    require(baseline.get("status") == "native_part_remap_snapshot_read_only", "NoTess native snapshot unavailable")
    slot_counts = {}; prototypes = {}; prototype_materials = []
    used = set()
    for row in baseline["current_parts"]:
        mesh = library.load_asset(row["mesh"])
        require(isinstance(mesh, unreal.SkeletalMesh), "NoTess prototype is missing")
        slot_counts[row["mesh"]] = len(mesh.get_editor_property("materials"))
        prototypes[package(mesh)] = mesh
        prototype_materials.extend(package(s.get_editor_property('material_interface')) for s in mesh.get_editor_property('materials'))
        used.update(row["expected_remap"] or range(slot_counts[row["mesh"]]))
    source_paths = list(dict.fromkeys([baseline["current_materials"][i] for i in sorted(used)] + prototype_materials))
    inspection = inspect_materials(unreal, source_paths, preserve_verified_legacy=preserve_verified_legacy)
    admission = None
    if inspection["write_packages"]:
        require(callable(prepare_dependencies), "NoTess dependencies require exact caller ownership")
        admission = prepare_dependencies({"assembly": path, "materials": inspection["write_packages"],
            "source_materials": list(inspection["sources"]), "source_parents": []})
        require(admission.get("verified") and admission["materials"] == inspection["write_packages"], "NoTess dependency admission mismatch")
    generated = dict(inspection["existing"]); created = []; compile_proofs = []
    for source, original in inspection["sources"].items():
        target_path = inspection["targets"][source]
        if target_path not in inspection["write_packages"]:
            continue
        check_dependency_admission(admission)
        require(_signature(unreal, inspection["helpers"], original, inspection["identities"]) == inspection["signatures"][source],
                "NoTess source changed after admission")
        asset = generated.get(source)
        if asset is None:
            asset = unreal.AssetToolsHelpers.get_asset_tools().duplicate_asset(
                target_path.rsplit("/", 1)[-1], target_path.rsplit("/", 1)[0], original)
            require(asset is not None and type(asset) is type(original), "NoTess native duplication failed")
            created.append(target_path)
            if isinstance(asset, unreal.Material):
                asset.set_editor_property("enable_tessellation", False)
            else:
                parent = original.get_editor_property("parent")
                edit.set_material_instance_parent(asset, generated.get(package(parent), parent))
                overrides = original.get_editor_property("base_property_overrides").copy()
                overrides.set_editor_property("override_b_enable_tessellation", False)
                overrides.set_editor_property("enable_tessellation", False)
                asset.set_editor_property("base_property_overrides", overrides)
            compile_proofs.append(inspection["helpers"]["compile_resource"](asset))
        generated[source] = asset
        require(_signature(unreal, inspection["helpers"], asset, inspection["identities"]) == inspection["signatures"][source],
                "NoTess clone changed authored graph/parameters/WPO")
        asset.modify()
        for key, value in ((OWNER_TAG, OWNER), (SOURCE_TAG, source), (SIGNATURE_TAG, digest(inspection["signatures"][source]))):
            library.set_metadata_tag(asset, key, value)
    runtime = _runtime(unreal, list(dict.fromkeys(inspection["bindings"].values())))
    for source, original in inspection["sources"].items():
        require(_signature(unreal, inspection["helpers"], original, inspection["identities"]) == inspection["signatures"][source],
                "NoTess generation changed an original material")
    for source, target in inspection["bindings"].items():
        _assert_equivalent_runtime(inspection["runtime_before"][source], runtime[target])
    prototype_changes = []
    for proto_path, mesh in prototypes.items():
        slots = list(mesh.get_editor_property('materials'))
        changes = [(i, inspection['bindings'][package(s.get_editor_property('material_interface'))])
                   for i, s in enumerate(slots)
                   if inspection['bindings'][package(s.get_editor_property('material_interface'))] != package(s.get_editor_property('material_interface'))]
        if changes:
            prototype_changes.append((proto_path, mesh, slots, changes))
    if prototype_changes:
        require(callable(prepare_prototypes), 'NoTess prototypes require exact caller ownership')
        admitted = prepare_prototypes([p for p, _, _, _ in prototype_changes])
        require(admitted is True, 'NoTess prototype admission failed')
    plan = plan_remaps(baseline, inspection["bindings"], slot_counts)
    contract = {"schema_version": 2, "assembly": path, "kind": "assembly_part_notess_remap_v1",
        "validate_only": True, "expected_materials": baseline["current_materials"],
        "expected_source_sha1": baseline["root_source_mesh_description_sha1"],
        "expected_nodes_sha1": baseline["nodes_binary_sha1"],
        "expected_skeleton_sha1": baseline["reference_skeleton_sha1"],
        "expected_materials_sha1": baseline["materials_authored_sha1"], **plan}
    preflight = _json_result(native(path, json.dumps(contract)))
    require(preflight.get("status") == "native_part_notess_plan_verified_read_only", "NoTess native plan was rejected")
    before_slots = list(assembly.get_editor_property("materials"))
    before_exports = [authored_slot_export(slot.export_text()) for slot in before_slots]
    remaps = [row["target_remap"] for row in plan["parts"]]
    # Native copies preserve full-precision transforms and protected node data.
    # An export_text/import_text round trip of MeshNaniteSettings does not.
    if plan['append_materials']:
        dirty = {package(p) for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
        if path in dirty:
            require(callable(prepare_root) and prepare_root(path) is True,
                    'NoTess dirty root requires exact caller preservation save')
        applied = _json_result(native(path, json.dumps({**contract, 'validate_only': False})))
        require(applied.get('status') == 'native_part_notess_remap_applied_unsaved' and not applied.get('returned_errors'),
                'NoTess native mutation failed: ' + str(applied))
    after = _json_result(native(path, json.dumps({"schema_version": 2, "assembly": path,
        "kind": "assembly_part_notess_remap_v1", "validate_only": True})))
    require(after.get("status") == "native_part_remap_snapshot_read_only" and not after.get("returned_errors"),
            "NoTess native readback failed")
    for field in ("root_source_mesh_description_sha1", "nodes_binary_sha1", "reference_skeleton_sha1"):
        require(after[field] == baseline[field], "NoTess changed original Base/geometry/nodes/skeleton")
    require([authored_slot_export(s.export_text()) for s in assembly.get_editor_property("materials")][:len(before_exports)] == before_exports,
            "NoTess changed an original material slot")
    require([r["remap"] for r in parse_nanite_assembly_part_remaps(assembly.get_editor_property("nanite_settings").export_text())] == remaps,
            "NoTess part remap readback differs")
    for proto_path, mesh, slots, changes in prototype_changes:
        updated_slots = []
        old_settings = mesh.get_editor_property('nanite_settings').export_text()
        for slot in slots:
            copied = type(slot)(); copied.import_text(slot.export_text()); updated_slots.append(copied)
        for index, target in changes:
            updated_slots[index].set_editor_property('material_interface', library.load_asset(target))
        mesh.modify(); mesh.set_editor_property('materials', updated_slots)
        require(mesh.get_editor_property('nanite_settings').export_text() == old_settings,
                'NoTess prototype edit changed Nanite geometry/settings')
        require([package(s.get_editor_property('material_interface')) for s in mesh.get_editor_property('materials')] ==
                [inspection['bindings'][package(s.get_editor_property('material_interface'))] for s in slots],
                'NoTess prototype material readback differs')
    proof = {"schema_version": 1, "assembly": path, "original_material_count": len(before_slots),
        "final_material_count": len(assembly.get_editor_property('materials')), "parts": plan["parts"], "aliases": plan["append_materials"]}
    if plan['append_materials']:
        library.set_metadata_tag(assembly, CONTRACT_TAG, json.dumps(proof, sort_keys=True))
    return {"status": "verified_part_notess", "assembly": path, "changed": bool(plan["append_materials"]),
        "base_and_original_slots_preserved": True, "prototypes_edited": bool(prototype_changes), "uvs_edited": False,
        "prototype_packages": [p for p, _, _, _ in prototype_changes],
        "bindings": inspection["bindings"], "material_packages": inspection["write_packages"],
        "created_packages": created, "dependency_checkout": admission, "compile": compile_proofs,
        "native_preflight": preflight, "native_after": after, "contract": proof}


def unwrap_previous_no_tess_slots(unreal, slots, *, read_only=False):
    """Recover source names/materials from verified part aliases during reimport.

    This also handles the legacy 51-root native alias suffix. No asset is changed;
    the exact raw old table remains the builder's concurrency fingerprint.
    """
    originals = list(slots); result = list(slots)
    for index, slot in enumerate(originals):
        interface = slot.get_editor_property("material_interface")
        if interface is None or not identity(interface).startswith(identity(DESTINATION) + "/"):
            continue
        match = re.fullmatch(r"(.+)_AssemblyNoTess_([0-9]+)(?:_[0-9]+)?", str(slot.get_editor_property("material_slot_name")), flags=re.IGNORECASE)
        if not match:
            continue  # An authored NoTess Base/source is not a generated alias.
        require(int(match[2]) < index, "Unproven NoTess alias source slot")
        source_slot = originals[int(match[2])]; source = source_slot.get_editor_property("material_interface")
        source_name = str(source_slot.get_editor_property("material_slot_name"))
        require(match[1].casefold() == (source_name if source_name.casefold() != 'none' else 'Material').casefold(), "NoTess alias source name differs")
        modern = package(interface).casefold().endswith('_notess')
        expected = (destination if modern else legacy_destination)(source, master=isinstance(source, unreal.Material))
        require(identity(interface) == identity(expected), "NoTess alias source path differs")
        # Legacy adoption is read-only and exact: clones cannot become arbitrary
        # user overrides or make their original Base slot inherit NoTess.
        check = inspect_materials(unreal, [package(source)], read_only=read_only, legacy=not modern)
        require(identity(check["bindings"][package(source)]) == identity(interface), "NoTess alias no longer represents its exact source")
        result[index] = source_slot
    return result
