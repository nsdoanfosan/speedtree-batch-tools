"""Reject stale structural wind exports before publishing a new handoff.

The installed BWR exporter supplies the expected schema/recipe. Frozen handoff
replay remains governed by its existing byte fingerprints, not this policy.
"""
import ast
import hashlib
import json
import re
from pathlib import Path


KEY = "WindStructureModifierContract"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def installed_structure_identity(addon_directory=None):
    """Read declared exporter identity without importing bpy or NumPy in Tk GUI."""
    if addon_directory is None:
        from blender_addon_contract import discover_installed_addon_source
        addon_directory = discover_installed_addon_source("speedtree_bone_weight_repair")
    if addon_directory is None:
        raise RuntimeError("Installed SpeedTree Bone Weight Repair package not found; Repair/export is required")
    directory = Path(addon_directory)

    def literal(filename, name):
        try:
            tree = ast.parse((directory / filename).read_text(encoding="utf-8-sig"))
            nodes = [node.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
            if len(nodes) != 1: raise ValueError(f"Expected one literal {name}")
            return ast.literal_eval(nodes[0])
        except (OSError, ValueError, SyntaxError) as exc:
            raise RuntimeError(f"Cannot read current structural exporter identity; Repair/export is required: {exc}") from exc

    recipe = literal("evaluate_export_loads.py", "RECIPE")
    schema = literal("wind_structure_contract.py", "SCHEMA_VERSION")
    if not isinstance(recipe, dict) or not isinstance(recipe.get("id"), str) or type(schema) is not int:
        raise RuntimeError("Invalid installed structural recipe/schema; Repair/export is required")
    # Exact serialization used by the exporter RECIPE_SHA256 declaration.
    digest = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": schema, "recipe_id": recipe["id"], "recipe_sha256": digest}


def cached_manifest_structure_status(item, *, addon_directory=None):
    """Freshness for selecting an old export into a NEW Push, not queue replay."""
    policy = item.get("wind_policy") or {}
    if policy.get("requires_json") is False:
        return {"status": "not_required", "current": True}
    identity = item.get("wind_file") or {}
    fingerprint_path = identity.get("path") if isinstance(identity, dict) else None
    path = item.get("wind_json") or fingerprint_path
    try:
        if not path:
            raise RuntimeError("Cached export has no authoritative Wind JSON; Run Repair/export before Push")
        if fingerprint_path and Path(path).resolve() != Path(fingerprint_path).resolve():
            raise RuntimeError("Cached Wind JSON paths disagree; Run Repair/export before Push")
        result = require_current_structure_contract(path, **installed_structure_identity(addon_directory))
        return {**result, "current": True}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"status": "repair_export_required", "current": False, "reason": str(exc)}


def require_current_structure_contract(path, *, schema_version, recipe_id, recipe_sha256):
    path = Path(path)

    def stale(reason):
        raise RuntimeError(
            f"Structural wind export is stale or invalid ({reason}): {path}. "
            "Run Repair/export with the current SpeedTree Bone Weight Repair add-on before Push."
        )

    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        stale(f"cannot read Wind JSON: {exc}")
    if not isinstance(document, dict):
        stale("Wind JSON must be an object")
    contract = document.get(KEY)
    if not isinstance(contract, dict):
        stale("missing WindStructureModifierContract")
    if type(contract.get("SchemaVersion")) is not int or contract["SchemaVersion"] != schema_version:
        stale("SchemaVersion mismatch")
    if contract.get("RecipeId") != recipe_id or contract.get("RecipeSha256") != recipe_sha256:
        stale("recipe mismatch")
    if contract.get("Mode") != "BoundedArtApproximation" or contract.get("ValidationOnly") is not True:
        stale("validation mode metadata missing")
    if any(type(contract.get(key)) not in (int, float) or contract[key] != 1.0
           for key in ("BendRateScale", "TorsionGain", "FlutterGain")):
        stale("unvalidated response channels must remain neutral")
    hashes = contract.get("SourceHashes")
    if (not isinstance(hashes, dict) or not hashes or
            any(not isinstance(key, str) or not isinstance(value, str) or not _HEX64.fullmatch(value)
                for key, value in hashes.items())):
        stale("invalid source hashes")
    digest = hashlib.sha256(json.dumps(
        hashes, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()
    if contract.get("SourceDigest") != digest:
        stale("source digest mismatch")
    skeleton = document.get("SkeletonContract")
    bones = contract.get("Bones")
    final_bones = skeleton.get("Bones") if isinstance(skeleton, dict) else None
    if (not isinstance(bones, list) or not bones or not isinstance(final_bones, list)
            or len(bones) != len(final_bones)
            or contract.get("BoneNameIndexParentSha1") != skeleton.get("BoneNameIndexParentSha1")):
        stale("incomplete final skeleton coverage")
    return {
        "status": "current", "schema_version": schema_version,
        "recipe_id": recipe_id, "recipe_sha256": recipe_sha256,
        "source_digest": digest, "bone_count": len(bones), "validation_only": True,
    }
