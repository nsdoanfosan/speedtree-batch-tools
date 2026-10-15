"""Read-only proof for an explicitly authored C-arc wind appearance.

An existing root's newer authored wind is not a request to regenerate the older
default normalized-bend materials during a geometry/UV reimport.
"""
import hashlib
import json
import re
from pathlib import Path

OWNER = "AssemblyLeafCArcRootBinding/20261013/v1"
PREFIX = "/Game/Material/Tree/AssetTree/AssemblyBend/CArc/"
OWNER_TAG = "CodexAssemblyLeafCArcOwner"
SOURCE_TAG = "CodexAssemblyLeafCArcSourceMaterial"
SIGNATURE_TAG = "CodexAssemblyLeafCArcSourceSignature"


def validate_alias(path, proof):
    source = proof.get("source") if isinstance(proof, dict) else None
    if (not isinstance(source, str) or not source.startswith("/Game/") or "." in source
            or proof.get("owner") != OWNER
            or any(re.fullmatch(r"[0-9a-f]{64}", str(proof.get(k) or "")) is None
                   for k in ("source_signature", "variant_signature"))):
        raise RuntimeError("Invalid authored C-arc material source proof")
    expected = PREFIX + source.rsplit("/", 1)[-1] + "_" + hashlib.sha1(source.encode()).hexdigest()[:12] + "_CArc"
    if path != expected:
        raise RuntimeError("Authored C-arc destination differs from its exact source")
    return source


def inspect_alias(unreal, asset, helpers):
    library = unreal.EditorAssetLibrary
    if library.get_metadata_tag(asset, OWNER_TAG) != OWNER:
        return None
    path = asset.get_path_name().split(".", 1)[0]
    source_path = str(library.get_metadata_tag(asset, SOURCE_TAG) or "")
    source = library.load_asset(source_path)
    if not isinstance(asset, unreal.MaterialInstanceConstant) or not isinstance(source, unreal.MaterialInstanceConstant):
        raise RuntimeError("Authored C-arc source/variant is not an exact MIC pair")
    proof = {"owner": OWNER, "source": source_path,
             "source_signature": str(library.get_metadata_tag(asset, SIGNATURE_TAG) or ""),
             "variant_signature": helpers["digest"](helpers["mi_signature"](asset))}
    validate_alias(path, proof)
    if helpers["digest"](helpers["mi_signature"](source)) != proof["source_signature"]:
        raise RuntimeError("Authored C-arc source changed; preserve its existing appearance")
    return proof


def verify_aliases(unreal, aliases):
    from normalized_assembly_bend import material_helpers
    helpers = material_helpers(unreal)
    for path, proof in aliases.items():
        asset = unreal.EditorAssetLibrary.load_asset(path)
        if inspect_alias(unreal, asset, helpers) != proof:
            raise RuntimeError("Authored C-arc appearance changed after read-only preflight")


def prototype_streams(unreal, paths):
    result = {}
    content = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())).resolve()
    dirty = {p.get_path_name().casefold() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    for path in paths:
        raw = unreal.CodexMaterialToolsLibrary.audit_skeletal_mesh_lod0_streams(path)
        values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        value = next((json.loads(x) for x in values if isinstance(x, str) and x.startswith("{")), None)
        if not value or value.get("ok") is not True:
            raise RuntimeError("Authored C-arc prototype lacks native stream evidence: " + path)
        file = content.joinpath(*path.split(".", 1)[0][len("/Game/"):].split("/")).with_suffix(".uasset")
        if path.split(".", 1)[0].casefold() in dirty or not file.is_file():
            raise RuntimeError("Authored C-arc prototype is not a clean saved reference: " + path)
        result[path] = {k: value[k] for k in ("vertex_count", "position_sha1", "tangent_sha1", "uv_hashes",
            "color_sha1", "index_sha1", "skin_sha1", "ref_skeleton_sha1", "materials")}
        result[path].update(package_file=str(file), package_md5=hashlib.md5(file.read_bytes()).hexdigest(),
                            package_size=file.stat().st_size, package_mtime_ns=file.stat().st_mtime_ns, live_clean=True)
    return result
