"""Blender job for one normalized Cluster blend's ON/OFF relationships."""

import argparse
import hashlib
import json
import shutil
import sys
import traceback
from pathlib import Path

import bpy

REPO_DIR = Path(__file__).resolve().parents[2]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from cluster_export_handoff_contract import (
    atomic_write_json,
    cluster_export_contract_issues as inspect_cluster_export_contract,
    finalize_cluster_pipeline_payload,
)
from cluster_atlas_source_index import (
    bind_index_to_export_results,
    build_current_atlas_source_index,
)
from sk_batch.repair_push_evidence import export_object_postcondition
from cluster_normalization_sync import (
    ClusterNormalizationSyncError,
    validate_isolated_bark_recipe_bundle,
)
from cluster_physical_capture_contract import (
    validate_normalization_receipt,
    validate_physical_capture_manifest,
)
from generator_delivery_scope import (
    GeneratorDeliveryScopeError,
    validate_delivery_scope_intent,
    validate_resolved_delivery_scope,
)
from speedtree_pipeline_contract import read_spm_text
from blender_addon_gateway import prepare_runtime


_ATLAS_RUNTIME = None


def atlas_operation(name):
    if _ATLAS_RUNTIME is None:
        raise RuntimeError("Atlas add-on runtime was not negotiated")
    return _ATLAS_RUNTIME.operation("atlas_leaf_mesh_builder", name)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("sync", "remove"), required=True)
    parser.add_argument("--blend", required=True)
    parser.add_argument("--target", action="append", default=[], required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--normalization-recipe")
    return parser.parse_args(argv)


def key(path):
    return str(Path(path).expanduser().absolute()).casefold()


def restore_cleanups(cleanups):
    restored = []
    for cleanup in reversed(cleanups):
        backup = cleanup.get("backup")
        spm = cleanup.get("spm")
        if backup and spm and Path(backup).is_file():
            shutil.copy2(backup, spm)
            restored.append(str(spm))
    return restored


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cluster_export_contract_issues(cluster_source_stem):
    return inspect_cluster_export_contract(
        bpy.data,
        cluster_source_stem,
    )


def finalize_cluster_source_pipeline(recipe):
    if not recipe:
        return {"status": "not_applicable"}
    blend = Path(recipe["blend"]).expanduser().absolute()
    issues = cluster_export_contract_issues(blend.stem)
    if issues:
        raise RuntimeError(
            "Cluster Normalizer final Export contract failed: "
            + ", ".join(issues)
        )
    report_path = (
        blend.parent
        / "reports"
        / f"{blend.stem}_speedtree_repair_pipeline_report_codex.json"
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    blend_stat = blend.stat()
    normalization_receipt = json.loads(
        Path(recipe["receipt_path"]).read_text(encoding="utf-8")
    )
    recorded_blend_digest = str(
        normalization_receipt.get("output_blend_sha256") or ""
    ).casefold()
    recorded_blend = normalization_receipt.get("blend")
    if (
        not recorded_blend
        or key(recorded_blend) != key(blend)
        or normalization_receipt.get("output_blend_size")
        != blend_stat.st_size
        or normalization_receipt.get("output_blend_mtime_ns")
        != blend_stat.st_mtime_ns
        or len(recorded_blend_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in recorded_blend_digest
        )
    ):
        recorded_blend_digest = sha256_file(blend)
    source_blend_identity = {
        "path": str(blend.resolve()),
        "exists": True,
        "size": blend_stat.st_size,
        "mtime_ns": blend_stat.st_mtime_ns,
        "sha256": recorded_blend_digest,
    }
    try:
        finalized, changed = finalize_cluster_pipeline_payload(
            payload,
            export_issues=issues,
            expected_source_object=recipe.get("source_object"),
            export_postcondition=export_object_postcondition(bpy.data),
            source_blend_identity=source_blend_identity,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if changed:
        atomic_write_json(report_path, finalized)
    return {
        "status": "ok",
        "pipeline_report": str(report_path),
        "export_collection_issues": [],
    }


def load_normalization_recipe(path, blend, requested):
    if not path:
        return None
    recipe_path = Path(path).expanduser().absolute()
    payload = json.loads(recipe_path.read_text(encoding="utf-8"))
    if payload.get("kind") != "speedtree_cluster_sync_normalization_recipe":
        raise RuntimeError(
            f"Cluster normalization recipe kind is invalid: {recipe_path}"
        )
    if key(payload.get("blend")) != key(blend):
        raise RuntimeError(
            "Cluster normalization recipe belongs to a different blend: "
            f"{payload.get('blend')}"
        )
    recipe_targets = {key(path) for path in payload.get("target_spms") or []}
    missing = [str(path) for path in requested if key(path) not in recipe_targets]
    if missing:
        raise RuntimeError(
            "Cluster normalization recipe is missing requested target(s): "
            + ", ".join(missing)
        )
    try:
        validate_isolated_bark_recipe_bundle(payload)
    except ClusterNormalizationSyncError as exc:
        raise RuntimeError(str(exc)) from exc
    return payload


def validate_recipe_registry_contract(recipe, effective_paths):
    """Return bindings for exactly the requested live relation slice."""
    if not recipe:
        raise RuntimeError(
            "Cluster relationship Sync requires a normalization recipe."
        )
    effective = [key(path) for path in effective_paths]
    recipe_targets = [key(path) for path in recipe.get("target_spms") or []]
    bindings = recipe.get("target_material_bindings") or []
    binding_targets = [key(row.get("target_spm")) for row in bindings]
    if (
        len(effective) != len(set(effective))
        or len(recipe_targets) != len(set(recipe_targets))
        or len(binding_targets) != len(set(binding_targets))
        or set(recipe_targets) != set(binding_targets)
        or not set(effective).issubset(set(recipe_targets))
    ):
        raise RuntimeError(
            "Cluster normalization recipe must contain one material binding "
            "for every requested live relation target."
        )
    for binding in bindings:
        if binding.get("connect_generators") not in {True, False}:
            raise RuntimeError(
                "Cluster normalization material binding must declare "
                "connect_generators as an explicit boolean."
            )
    connected = [
        row for row in bindings if row.get("connect_generators") is True
    ]
    scoped = [
        row for row in connected
        if row.get("generator_delivery_scope_intent") is not None
    ]
    if scoped and len(scoped) != len(connected):
        raise RuntimeError(
            "Explicit Generator delivery scope must cover every connected "
            "target; mixed explicit/legacy recipes are rejected."
        )
    for binding in scoped:
        try:
            validate_delivery_scope_intent(
                binding["generator_delivery_scope_intent"],
                target_spm=binding["target_spm"],
                material_id=binding["source_material_id"],
                provider_blend=recipe["blend"],
            )
        except GeneratorDeliveryScopeError as exc:
            raise RuntimeError(
                "Cluster normalization Generator delivery scope is invalid "
                f"for {binding.get('target_spm')}: {exc}"
            ) from exc
    by_target = {key(row["target_spm"]): row for row in bindings}
    return {target: by_target[target] for target in effective}


def normalize_cluster_blend(recipe):
    try:
        validate_isolated_bark_recipe_bundle(recipe)
    except ClusterNormalizationSyncError as exc:
        raise RuntimeError(str(exc)) from exc
    if not recipe or not recipe.get("normalization_required"):
        return {
            "status": "current",
            "normalization_required": False,
            "receipt": (
                str(recipe.get("receipt_path"))
                if recipe
                else None
            ),
        }
    if not hasattr(bpy.types.Scene, "speedtree_cluster_normalizer"):
        raise RuntimeError("Could not enable speedtree_cluster_normalizer")
    if not hasattr(bpy.types.Scene, "atlas_leaf_builder"):
        raise RuntimeError("atlas_leaf_mesh_builder is not registered")

    scene = bpy.context.scene
    units = scene.unit_settings
    if units.system != "METRIC" or abs(float(units.scale_length) - 1.0) > 1.0e-12:
        raise RuntimeError(
            "Automatic Cluster normalization requires METRIC scale_length=1.0 "
            f"(got {units.system}, {units.scale_length})"
        )
    source = bpy.data.objects.get(str(recipe.get("source_object") or ""))
    if source is None or source.type != "MESH":
        raise RuntimeError(
            "SK Batch merged source mesh is missing from the blend: "
            f"{recipe.get('source_object')}"
        )
    source_xml = Path(recipe["source_xml"]).expanduser().absolute()
    unit_probe = Path(recipe["unit_probe"]).expanduser().absolute()
    target = Path(recipe["first_target_spm"]).expanduser().absolute()
    for required, label in (
        (source_xml, "SK Batch source XML"),
        (unit_probe, "verified unit probe"),
        (target, "first owner target SPM"),
    ):
        if not required.is_file():
            raise RuntimeError(f"Automatic normalization {label} is missing: {required}")

    unit_payload = json.loads(unit_probe.read_text(encoding="utf-8"))
    selected_unit = unit_payload.get("selected") or {}
    props = scene.speedtree_cluster_normalizer
    props.workflow_mode = "PHYSICAL_DIRECT_CAPTURE"
    props.source_object = source
    props.source_xml_path = str(source_xml)
    props.source_partition_mode = recipe["source_partition_mode"]
    props.plan_base_name = recipe["plan_base"]
    props.skeletal_base_name = recipe["skeletal_base"]
    props.plan_collection = recipe["plan_collection"]
    props.plan_material_name = recipe["material_name"]
    props.source_material_name = recipe["source_material_name"]
    props.source_material_id = int(recipe["source_material_id"])
    props.plan_margin_ratio = float(recipe["plan_margin_ratio"])
    props.plan_refinement_levels = int(recipe["plan_refinement_levels"])
    props.replace_generated = True
    props.configure_send2ue = False
    props.isolate_send2ue_export = True
    props.source_reference_collection = "Cluster_Source_Reference"
    props.capture_source_collection = recipe["capture_source_collection"]
    props.capture_output_dir = recipe["capture_output_dir"]
    props.capture_prefix = recipe["capture_prefix"]
    props.capture_resolution = int(recipe["capture_resolution"])
    props.capture_padding_ratio = float(recipe["capture_padding_ratio"])
    props.capture_plane = recipe["capture_plane"]
    props.capture_target_meters = float(recipe["capture_target_meters"])
    props.prepare_atlas_handoff = True
    props.unit_probe_contract_path = str(unit_probe)
    props.atlas_target_spm = str(target)
    # The host wrote every owner target before Blender started.  Preserve that
    # complete registry while Normalizer configures the first adoption mapping.
    props.atlas_only_target = False
    props.atlas_mesh_scale = float(
        selected_unit.get("mesh_geometry_scale") or 1.0
    )
    props.atlas_mesh_asset_scale = float(
        selected_unit.get("mesh_asset_scale") or 1.0
    )

    bpy.ops.object.select_all(action="DESELECT")
    source.select_set(True)
    bpy.context.view_layer.objects.active = source
    capture_result = bpy.ops.speedtree_cluster.bake_capture_maps()
    if set(capture_result) != {"FINISHED"}:
        raise RuntimeError(
            "Automatic Cluster eight-map capture did not finish: "
            + repr(sorted(capture_result))
        )
    build_result = bpy.ops.speedtree_cluster.build_normalized_assets()
    if set(build_result) != {"FINISHED"}:
        raise RuntimeError(
            "Automatic Cluster Normalizer did not finish: "
            + repr(sorted(build_result))
        )
    raw_report = str(
        scene.get("speedtree_cluster_normalizer_last_report") or ""
    )
    if not raw_report:
        raise RuntimeError("Cluster Normalizer did not persist its result report")
    build = json.loads(raw_report)
    cluster_handoff = (
        build.get("cluster_handoff")
        or build.get("atlas_handoff")
        or {}
    )
    if (
        build.get("workflow_mode") != "PHYSICAL_DIRECT_CAPTURE"
        or not build.get("geometry_committed")
        or not cluster_handoff.get("prepared")
    ):
        raise RuntimeError(
            "Automatic Cluster normalization/Cluster handoff is incomplete: "
            + json.dumps(
                {
                    "workflow_mode": build.get("workflow_mode"),
                    "geometry_committed": build.get("geometry_committed"),
                    "cluster_handoff": cluster_handoff,
                },
                ensure_ascii=False,
                default=str,
            )
        )
    capture_manifest = (
        Path(recipe["capture_output_dir"])
        / f"{recipe['capture_prefix']}_auto_capture_manifest.json"
    )
    # Do not save or publish a Blender result whose finalized capture does not
    # prove the recipe's exact plane/basis, 0.1m extent, full eight-map
    # coverage, and immutable file fingerprints.
    validate_physical_capture_manifest(
        capture_manifest,
        expected_blend=recipe["blend"],
        expected_plane=recipe["capture_plane"],
        expected_resolution=recipe["capture_resolution"],
        expected_target_meters=recipe["capture_target_meters"],
        expected_padding_ratio=recipe["capture_padding_ratio"],
    )
    bpy.ops.wm.save_as_mainfile(
        filepath=str(Path(recipe["blend"]).expanduser().absolute()),
        check_existing=False,
    )
    blend_path = Path(bpy.data.filepath).expanduser().absolute()
    blend_stat = blend_path.stat()
    blend_digest = sha256_file(blend_path)
    receipt_path = Path(recipe["receipt_path"]).expanduser().absolute()
    receipt = {
        "kind": "speedtree_cluster_sync_normalization",
        "version": 3,
        "status": "ready",
        "blend": str(blend_path),
        "output_blend_sha256": blend_digest,
        "output_blend_size": blend_stat.st_size,
        "output_blend_mtime_ns": blend_stat.st_mtime_ns,
        "canonical_spm": recipe["canonical_spm"],
        "source_spm_semantic_projection_version": recipe[
            "source_spm_semantic_projection_version"
        ],
        "source_spm_semantic_fingerprint": recipe[
            "source_spm_semantic_fingerprint"
        ],
        # Retain byte identity for diagnostics without making texture-path
        # rewrites an authoritative physical-normalization invalidator.
        "source_spm_sha256": recipe["source_spm_sha256"],
        "source_xml": str(source_xml),
        "unit_probe": str(unit_probe),
        "unit_probe_sha256": recipe["unit_probe_sha256"],
        "normalization_contract_sha256": recipe[
            "normalization_contract_sha256"
        ],
        "recipe_sha256": recipe["recipe_sha256"],
        "capture_manifest": str(capture_manifest),
        "capture_manifest_sha256": sha256_file(capture_manifest),
        "prototype_count": build.get("prototype_count"),
        "card_count": build.get("card_count"),
        "plan_collection": build.get("plan_collection"),
        "material": recipe["material_name"],
        "build": build,
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "rebuilt",
        "normalization_required": True,
        "receipt": str(receipt_path),
        "prototype_count": build.get("prototype_count"),
        "card_count": build.get("card_count"),
        "capture_manifest": receipt["capture_manifest"],
    }


def capture_normalization_source_index(recipe):
    """Capture Atlas' saved Blender index before any export mutation."""
    if not recipe:
        return None
    receipt_path = Path(recipe["receipt_path"]).expanduser().absolute()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    previous = receipt.get("source_blender_index") or {}
    previous_collection = previous.get("authoritative_collection") or {}
    kwargs = {
        "atlas_asset_name": recipe["material_name"],
        "expected_scope_id": previous_collection.get("export_scope_id"),
    }
    if _ATLAS_RUNTIME is not None:
        kwargs["addon_runtime"] = _ATLAS_RUNTIME
    return build_current_atlas_source_index(
        Path(recipe["blend"]).expanduser().absolute(),
        recipe["plan_collection"],
        **kwargs,
    )


def persist_normalization_source_index(recipe, identity):
    """Commit source identity only after Atlas publication has succeeded."""
    if not recipe or identity is None:
        return None
    publication = identity.get("publication") or {}
    targets = publication.get("targets")
    if (
        publication.get("status") != "bound"
        or not isinstance(targets, list)
        or not targets
        or publication.get("target_count") != len(targets)
    ):
        raise RuntimeError(
            "Atlas source identity cannot be persisted before publication binding"
        )
    receipt_path = Path(recipe["receipt_path"]).expanduser().absolute()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    changed = receipt.get("source_blender_index") != identity
    if changed:
        legacy = receipt.pop("source_blend_content_identity", None)
        if legacy is not None:
            receipt["superseded_source_blend_content_identity"] = legacy
        receipt["source_blender_index"] = identity
        atomic_write_json(receipt_path, receipt)
    return {
        "receipt": str(receipt_path),
        "changed": changed,
        **identity,
    }


def configure_cluster_export_properties(
    props,
    recipe,
    *,
    first_target_spm=None,
):
    """Rehydrate Atlas writer settings from the content-addressed recipe.

    A current normalization receipt can skip the Blender rebuild, so relying on
    whatever Atlas properties happen to be saved in the blend is unsafe.  The
    Cluster recipe is authoritative for the plan collection, capture maps,
    material name, target, and verified unit contract on every Sync run.
    """
    if not recipe:
        return None
    configure_external_plan_target = atlas_operation(
        "configure_external_plan_target"
    )

    manifest_path = (
        Path(recipe["capture_output_dir"])
        / f"{recipe['capture_prefix']}_auto_capture_manifest.json"
    )
    manifest_evidence = validate_physical_capture_manifest(
        manifest_path,
        expected_blend=recipe["blend"],
        expected_plane=recipe["capture_plane"],
        expected_resolution=recipe["capture_resolution"],
        expected_target_meters=recipe["capture_target_meters"],
        expected_padding_ratio=recipe["capture_padding_ratio"],
    )
    validate_normalization_receipt(
        recipe["receipt_path"],
        manifest_evidence=manifest_evidence,
        expected_blend=recipe["blend"],
        expected_normalization_contract_sha256=recipe[
            "normalization_contract_sha256"
        ],
    )
    maps_by_role = {
        row["role"]: row for row in manifest_evidence["maps"]
    }
    color_path = Path((maps_by_role.get("Color") or {}).get("path") or "")
    opacity_path = Path((maps_by_role.get("Opacity") or {}).get("path") or "")
    if not color_path.is_file() or not opacity_path.is_file():
        raise RuntimeError(
            "Cluster capture manifest has no current Color/Opacity maps: "
            f"{manifest_path}"
        )

    unit_probe_path = Path(recipe["unit_probe"]).expanduser().absolute()
    unit_probe = json.loads(unit_probe_path.read_text(encoding="utf-8"))
    first_target = str(
        first_target_spm or recipe["first_target_spm"]
    )
    first_binding = next(
        (
            row
            for row in recipe.get("target_material_bindings") or []
            if key(row.get("target_spm")) == key(first_target)
        ),
        None,
    )
    if first_binding is None:
        raise RuntimeError(
            "Cluster normalization recipe has no binding for its first target."
        )
    configured = configure_external_plan_target(
        props,
        collection_name=recipe["plan_collection"],
        generated_material_name=recipe["material_name"],
        source_material_name=recipe["source_material_name"],
        albedo_path=str(color_path),
        target_spm=first_target,
        source_material_id=recipe["source_material_id"],
        adopt_source_material=bool(recipe.get("adopt_source_material")),
        only_target=False,
        mesh_geometry_scale=float(
            (unit_probe.get("selected") or {}).get("mesh_geometry_scale")
            or 1.0
        ),
        mesh_asset_scale=float(
            (unit_probe.get("selected") or {}).get("mesh_asset_scale")
            or 1.0
        ),
        generator_variant_policy=first_binding.get(
            "generator_variant_policy"
        ),
        unit_probe_contract=unit_probe,
        connect_generators=(
            first_binding.get("connect_generators") is True
        ),
    )
    props.alpha_path = str(opacity_path)
    return {
        **configured,
        "capture_manifest": str(manifest_path),
        "opacity_path": str(opacity_path),
    }


def apply_recipe_source_material_mappings(
    props,
    recipe,
    *,
    effective_targets=None,
):
    if not recipe:
        return {"applied": [], "preserved": []}
    raw_text = str(
        getattr(props, "speedtree_source_materials_json", "") or ""
    ).strip()
    mapping = json.loads(raw_text) if raw_text else {}
    if not isinstance(mapping, dict):
        raise RuntimeError(
            "Source material mapping JSON must be an object keyed by target SPM path."
        )
    existing_by_key = {key(path): path for path in mapping}
    effective_keys = (
        {key(path) for path in effective_targets}
        if effective_targets is not None
        else None
    )
    applied = []
    preserved = []
    assets_only = []
    for binding in recipe.get("target_material_bindings") or []:
        target = str(binding.get("target_spm") or "")
        if not target:
            raise RuntimeError(
                "Cluster normalization material binding has no target SPM."
            )
        if (
            effective_keys is not None
            and key(target) not in effective_keys
        ):
            continue
        previous_key = existing_by_key.get(key(target))
        if binding.get("connect_generators") is not True:
            if previous_key is not None:
                mapping.pop(previous_key, None)
            assets_only.append({
                "target_spm": target,
                "generated_material_name": binding[
                    "generated_material_name"
                ],
                "mode": "material_mesh_assets_only",
            })
            continue
        if previous_key is not None and previous_key != target:
            mapping.pop(previous_key, None)
        request = {
            "source_material_names": [binding["source_material_name"]],
            "source_material_ids": [int(binding["source_material_id"])],
            "adopt_source_material": bool(
                binding.get("adopt_source_material")
            ),
            "generator_variant_policy": binding[
                "generator_variant_policy"
            ],
            "source_binding_repairs": list(
                binding.get("source_binding_repairs") or []
            ),
        }
        if binding.get("generator_delivery_scope_intent") is not None:
            request["generator_delivery_scope_intent"] = binding[
                "generator_delivery_scope_intent"
            ]
        if mapping.get(target) == request:
            preserved.append(target)
        else:
            mapping[target] = request
            applied.append({
                "target_spm": target,
                "generated_material_name": binding[
                    "generated_material_name"
                ],
                **request,
            })
    props.speedtree_source_materials_json = json.dumps(
        mapping,
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "applied": applied,
        "preserved": preserved,
        "assets_only": assets_only,
    }


def validate_producer_delivery_scope_results(results, bindings_by_key):
    """Require an exact producer seal whenever the recipe carried intent."""
    for result in results:
        binding = bindings_by_key.get(key(result.get("spm_path")))
        if not binding:
            continue
        intent = binding.get("generator_delivery_scope_intent")
        if intent is None:
            continue
        target_spm = Path(result["spm_path"]).expanduser().absolute()
        text_sha256 = hashlib.sha256(
            read_spm_text(target_spm).encode("utf-8")
        ).hexdigest()
        try:
            validated = validate_resolved_delivery_scope(
                result.get("generator_connection"),
                target_spm=target_spm,
                material_id=binding["source_material_id"],
                provider_blend=binding["generator_delivery_scope_intent"][
                    "target"
                ]["provider_blend"],
                target_spm_postwrite_sha256=text_sha256,
            )
        except GeneratorDeliveryScopeError as exc:
            raise RuntimeError(
                "Atlas producer did not return an exact sealed Generator "
                f"delivery scope for {target_spm}: {exc}"
            ) from exc
        expected_hash = str(intent.get("intent_sha256") or "").casefold()
        if validated["intent_sha256"] != expected_hash:
            raise RuntimeError(
                "Atlas producer echoed a different Generator delivery intent "
                f"for {target_spm}."
            )


def sync_targets(blend, requested, normalization_recipe=None):
    if key(bpy.data.filepath) != key(blend):
        raise RuntimeError(
            f"Loaded blend differs from requested Cluster blend: {bpy.data.filepath}"
        )
    execute_external_target_transaction = atlas_operation(
        "execute_external_target_transaction"
    )
    sync_spm_target_registry = atlas_operation(
        "sync_spm_target_registry"
    )
    load_target_registry = atlas_operation("load_target_registry")
    integration_contract = next(
        row.get("api_contract")
        for row in _ATLAS_RUNTIME.receipt["addons"]
        if row.get("id") == "atlas_leaf_mesh_builder"
    )
    required_native_capabilities = {
        "atomic_exact_target_slice_v1",
        "generator_adoption_reconciliation_v1",
        "structured_transaction_conflict_v1",
    }
    actual_native_capabilities = set(
        (integration_contract or {}).get("capabilities") or []
    )
    if not required_native_capabilities.issubset(
        actual_native_capabilities
    ):
        raise RuntimeError(
            "Atlas runtime receipt lost required native capabilities: "
            + ", ".join(
                sorted(
                    required_native_capabilities
                    - actual_native_capabilities
                )
            )
        )

    normalization = normalize_cluster_blend(normalization_recipe)
    source_index = capture_normalization_source_index(
        normalization_recipe
    )
    runtime_recipe = dict(normalization_recipe or {})
    if source_index is not None:
        runtime_recipe["plan_collection"] = source_index[
            "authoritative_collection"
        ]["collection_name"]
    props = bpy.context.scene.atlas_leaf_builder
    export_configuration = configure_cluster_export_properties(
        props,
        runtime_recipe,
        first_target_spm=requested[0],
    )
    registry = load_target_registry(blend)
    if registry is None:
        raise RuntimeError(f"Atlas target JSON does not exist for {blend}")
    registered_paths = [
        Path(path).expanduser().absolute()
        for path in registry["target_spms"]
    ]
    registered = {key(path) for path in registered_paths}
    missing = [str(path) for path in requested if key(path) not in registered]
    if missing:
        raise RuntimeError("Requested ON target is absent from Atlas JSON: " + ", ".join(missing))
    sync_spm_target_registry(props, initialize_missing=False)
    scene_targets = {key(item.path) for item in props.speedtree_spm_items}
    if scene_targets != registered:
        raise RuntimeError("Blender Atlas target list did not match the external JSON")
    bindings_by_key = validate_recipe_registry_contract(
        runtime_recipe,
        requested,
    )
    recipe_mapping_update = apply_recipe_source_material_mappings(
        props,
        runtime_recipe,
        effective_targets=requested,
    )
    connection_targets = [
        path
        for path in requested
        if bindings_by_key[key(path)].get("connect_generators") is True
    ]
    # Normalize each requested relation in place. The previous manifest and
    # Generator bindings are the migration input; detaching a valid relation
    # first destroys that input and turns an idempotent refresh into a second,
    # incompatible bootstrap path.
    #
    # The external registry remains the persistent ON/OFF authority. Stage only
    # this transaction's exact live relation targets in Blender so the addon
    # cannot widen execution back to unrelated registered rows.
    transaction = execute_external_target_transaction(
        props,
        requested,
        adoption_targets=connection_targets,
        adoption_blend_path=blend,
        preserve_explicit_material_name=True,
    )
    results = transaction["results"]
    mapping_update = transaction["mapping_update"]
    # Both contracts must be verified before the receipt is advanced: the
    # producer's sealed delivery scope proves the generated output is valid,
    # then the content identity binds that verified publication to its source.
    validate_producer_delivery_scope_results(results, bindings_by_key)
    source_content_identity = None
    if source_index is not None:
        bound_source_index = bind_index_to_export_results(
            source_index,
            results,
        )
        source_content_identity = persist_normalization_source_index(
            normalization_recipe,
            bound_source_index,
        )
    cluster_source_pipeline = finalize_cluster_source_pipeline(
        normalization_recipe
    )
    return {
        "mode": "sync",
        "blend": str(blend),
        "target_spms": [str(path) for path in requested],
        "atlas_integration_contract": integration_contract,
        "blender_addon_runtime": _ATLAS_RUNTIME.receipt,
        "atlas_transaction": transaction["transaction"],
        "normalization": normalization,
        "source_content_identity": source_content_identity,
        "cluster_export_configuration": export_configuration,
        "recipe_source_material_mapping_update": recipe_mapping_update,
        "source_material_mapping_update": mapping_update,
        "cluster_source_pipeline": cluster_source_pipeline,
        "results": results,
    }


def remove_targets(blend, requested):
    remove_blend_target_from_spm = atlas_operation(
        "remove_blend_target_from_spm"
    )
    load_target_registry = atlas_operation("load_target_registry")
    save_target_registry = atlas_operation("save_target_registry")

    registry = load_target_registry(blend)
    if registry is None:
        raise RuntimeError(f"Atlas target JSON does not exist for {blend}")
    registered = {key(path): Path(path).expanduser().absolute() for path in registry["target_spms"]}
    for target in requested:
        if key(target) not in registered:
            raise RuntimeError(f"Requested OFF target is not ON for this blend: {target}")
    cleanups = []
    registry_path = Path(registry["registry_path"])
    registry_bytes = registry_path.read_bytes()
    try:
        for target in requested:
            cleanups.append(remove_blend_target_from_spm(blend, target))
        removed = {key(path) for path in requested}
        remaining = [path for path_key, path in registered.items() if path_key not in removed]
        payload = save_target_registry(blend, remaining)
    except Exception:
        restore_cleanups(cleanups)
        registry_path.write_bytes(registry_bytes)
        raise
    return {
        "mode": "remove",
        "blend": str(blend),
        "target_spms": [str(path) for path in requested],
        "remaining_target_spms": payload["target_spms"],
        "results": cleanups,
        "blender_addon_runtime": _ATLAS_RUNTIME.receipt,
    }


def main():
    global _ATLAS_RUNTIME
    args = parse_args()
    report_path = Path(args.report).expanduser().absolute()
    report = {"status": "error"}
    try:
        _ATLAS_RUNTIME = prepare_runtime(
            "spm_generator_sync.jobs.cluster_relation_job",
            {
                "atlas_leaf_mesh_builder": (
                    "scene_generation_v1",
                    "target_registry_v1",
                    "source_index_v1",
                    "atomic_target_transaction_v1",
                ),
                "speedtree_cluster_normalizer": (
                    "cluster_normalization_v1",
                ),
            },
        )
        blend = Path(args.blend).expanduser().absolute()
        targets = [Path(value).expanduser().absolute() for value in args.target]
        normalization_recipe = load_normalization_recipe(
            args.normalization_recipe,
            blend,
            targets,
        )
        payload = (
            sync_targets(blend, targets, normalization_recipe)
            if args.mode == "sync"
            else remove_targets(blend, targets)
        )
        report = {"status": "ok", **payload}
    except Exception as exc:
        traceback_text = traceback.format_exc()
        report = {
            "status": "error",
            "error": str(exc),
            "traceback": traceback_text,
        }
        failure_contract = getattr(exc, "failure_contract", None)
        if isinstance(failure_contract, dict):
            report["failure_contract"] = failure_contract
        try:
            from cluster_blend_sync import (
                _persist_cluster_relation_failure,
            )

            persistent_failure_report = (
                _persist_cluster_relation_failure(
                    blend=args.blend,
                    targets=args.target,
                    enabled=args.mode == "sync",
                    phase="blender_worker_exception",
                    command=sys.argv,
                    snapshots=[],
                    artifact_recipe={
                        "normalization_recipe": (
                            args.normalization_recipe
                        ),
                    },
                    report=report,
                )
            )
            report["persistent_failure_report"] = str(
                persistent_failure_report
            )
            report["error"] += (
                "\nFailure diagnostic log: "
                + str(persistent_failure_report)
            )
        except Exception as diagnostic_exc:
            report["diagnostic_write_error"] = (
                f"{type(diagnostic_exc).__name__}: {diagnostic_exc}"
            )
            report["error"] += (
                "\nCOULD NOT write failure diagnostic log: "
                + report["diagnostic_write_error"]
            )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
