import gzip
import hashlib
import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from cluster_bark_source_resolution import (
    ClusterBarkSourceResolutionError,
    _copy_canonical_textures,
    _copy_source_external_meshes,
    _publish_cache_directory,
    _rebase_external_mesh_refs,
    prepare_isolated_bark_source,
    resolve_cluster_bark_source_spm,
)
from speedtree_texture_contract import REQUIRED_TEXTURE_ROLES
from pcg_st9_texture_batch.pcg_texture_audit import (
    extract_material_image_refs,
)


def _map(name, texture):
    return (
        f'<Map Name="{name}"><TexFilename>{texture}</TexFilename>'
        '<TexEnabled>true</TexEnabled></Map>'
    )


def _write_spm(
    path,
    material_name,
    refs,
    external_mesh="",
    extra_material_refs=(),
):
    maps = "".join(
        _map(f"Map{index}", value)
        for index, value in enumerate(refs)
    )
    extra_material = ""
    if extra_material_refs:
        extra_maps = "".join(
            _map(f"ExtraMap{index}", value)
            for index, value in enumerate(extra_material_refs)
        )
        extra_material = (
            '<Material_v8 ID="2" Name="M_leaf_preserved">'
            + extra_maps
            + "</Material_v8>"
        )
    payload = (
        '<SpeedTree><Materials>'
        f'<Material_v8 ID="1" Name="{material_name}">{maps}'
        '<SupplementalCutoutMeshIDs><CutoutMesh ID="1"/>'
        '</SupplementalCutoutMeshIDs></Material_v8>' + extra_material +
        '</Materials><Meshes><Mesh ID="1" Name="mesh">'
        + (
            f'<Embedded>false</Embedded><Filename>{external_mesh}</Filename>'
            if external_mesh
            else ""
        )
        + '</Mesh></Meshes>'
        '<Generators><Generator Type="Branch"><Properties><Property>'
        '<Name>Branches:Material</Name><Value>1</Value>'
        '</Property></Properties></Generator></Generators></SpeedTree>'
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as handle:
        handle.write(payload)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical_output(
    root,
    spm,
    material_id,
    material_name,
    texture_base,
):
    texture_root = Path(root) / "texture"
    texture_root.mkdir(parents=True, exist_ok=True)
    files = {}
    for role in REQUIRED_TEXTURE_ROLES:
        path = texture_root / f"{texture_base}_{role}.tga"
        path.write_bytes(f"{texture_base}:{role}".encode("utf-8"))
        files[role] = path.name
    manifest_path = texture_root / "pcg_st9_canonical_outputs.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {
            "kind": "pcg_st9_canonical_output_manifest",
            "schema_version": 1,
            "asset_root": str(Path(root).resolve()),
            "texture_root": str(texture_root.resolve()),
            "outputs": [],
        }
    manifest["outputs"] = [
        output
        for output in manifest["outputs"]
        if not (
            output["texture_base"].casefold() == texture_base.casefold()
            and any(
                Path(target["spm"]).resolve() == Path(spm).resolve()
                and str(target.get("material_id")) == str(material_id)
                for target in output["material_targets"]
            )
        )
    ]
    manifest["outputs"].append({
        "texture_base": texture_base,
        "required_roles": list(REQUIRED_TEXTURE_ROLES),
        "files": files,
        "material_targets": [{
            "spm": str(Path(spm).resolve()),
            "material_id": str(material_id),
            "material_name": material_name,
        }],
        "producer": {
            "tool": "PCG ST9 Texture",
            "source": "test",
        },
    })
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return {
        role: f"texture/{texture_base}_{role}.tga"
        for role in REQUIRED_TEXTURE_ROLES
    }


def test_cache_publish_retries_transient_windows_access_denied():
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / "staging"
        cache_dir = Path(temporary) / "cache"
        staging.mkdir()
        (staging / "bark_normalization_manifest.json").write_text(
            "{}",
            encoding="utf-8",
        )
        denied = PermissionError(13, "access denied", str(cache_dir))
        denied.winerror = 5

        with (
            mock.patch(
                "cluster_bark_source_resolution.os.replace",
                side_effect=[denied, None],
            ) as replace,
            mock.patch(
                "cluster_bark_source_resolution.time.sleep"
            ) as sleep,
        ):
            _publish_cache_directory(staging, cache_dir)

        assert replace.call_count == 2
        sleep.assert_called_once_with(0.25)


def test_cache_publish_falls_back_to_manifest_last_copy():
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / "staging"
        cache_dir = Path(temporary) / "cache"
        artifact = staging / "tree" / "cluster" / "source.spm"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"isolated")
        manifest = staging / "bark_normalization_manifest.json"
        manifest.write_text('{"status":"ready"}', encoding="utf-8")
        denied = PermissionError(13, "access denied", str(cache_dir))
        denied.winerror = 5
        real_replace = __import__("os").replace
        manifest_promoted_after_artifacts = []

        def replace(source, destination):
            source = Path(source)
            destination = Path(destination)
            if source == staging:
                raise denied
            manifest_promoted_after_artifacts.append(
                (cache_dir / "tree" / "cluster" / "source.spm").is_file()
                and not (
                    cache_dir / "bark_normalization_manifest.json"
                ).exists()
            )
            return real_replace(source, destination)

        with (
            mock.patch(
                "cluster_bark_source_resolution.os.replace",
                side_effect=replace,
            ),
            mock.patch(
                "cluster_bark_source_resolution.time.sleep"
            ),
        ):
            _publish_cache_directory(staging, cache_dir)

        assert (
            cache_dir / "tree" / "cluster" / "source.spm"
        ).read_bytes() == b"isolated"
        assert json.loads(
            (cache_dir / "bark_normalization_manifest.json").read_text(
                encoding="utf-8"
            )
        ) == {"status": "ready"}
        assert manifest_promoted_after_artifacts == [True]


def test_cache_publish_refuses_preexisting_partial_destination():
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / "staging"
        cache_dir = Path(temporary) / "cache"
        staging.mkdir()
        (staging / "bark_normalization_manifest.json").write_text(
            "{}",
            encoding="utf-8",
        )
        cache_dir.mkdir()
        partial = cache_dir / "partial.bin"
        partial.write_bytes(b"keep-for-diagnosis")

        with pytest.raises(
            FileExistsError,
            match="existing bark cache directory",
        ):
            _publish_cache_directory(staging, cache_dir)

        assert partial.read_bytes() == b"keep-for-diagnosis"
        assert not (
            cache_dir / "bark_normalization_manifest.json"
        ).exists()


def test_content_addressed_bark_source_preserves_production_spm():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_elm"
        source = root / "Cluster" / "SK_branch_elm_01.spm"
        canonical = root / "SK_Tree_elm_01.spm"
        canonical_outputs = _canonical_output(
            root,
            canonical,
            "1",
            "M_bark_elm_01",
            "T_bark_elm_01",
        )
        canonical_refs = [
            canonical_outputs["color"],
            canonical_outputs["normal"],
        ]
        _write_spm(
            source,
            "M_bark_common_end_01",
            ["generic/common_color.tga", "generic/common_normal.tga"],
        )
        for index, value in enumerate(
            ["generic/common_color.tga", "generic/common_normal.tga"],
            1,
        ):
            texture = source.parent / value
            texture.parent.mkdir(parents=True, exist_ok=True)
            texture.write_bytes(f"generic-{index}".encode("ascii"))
        _write_spm(canonical, "M_bark_elm_01", canonical_refs)
        source_hash = _sha256(source)
        contract = {
            "handoff": {
                "canonical_bark": {
                    "status": "replacement_required",
                    "canonical_material": "M_bark_elm_01",
                    "canonical_sources": [{
                        "spm": str(canonical),
                        "material_id": "1",
                        "material_name": "M_bark_elm_01",
                        "refs": canonical_refs,
                    }],
                    "cluster_bark_sources": [{
                        "cluster_spm": str(source),
                        "material_id": "1",
                        "material_name": "M_bark_common_end_01",
                        "replacement": "required",
                    }],
                },
            },
        }
        rows = contract["handoff"]["canonical_bark"][
            "cluster_bark_sources"
        ]
        cache = Path(temporary) / "cache"

        prepared = prepare_isolated_bark_source(
            source,
            contract,
            rows,
            cache_parent=cache,
        )
        isolated = Path(prepared["speedtree_spm"])
        isolated_rows = extract_material_image_refs(isolated)

        assert prepared["status"] == "prepared"
        assert prepared["production_source_mutated"] is False
        assert _sha256(source) == source_hash
        assert isolated.is_file()
        assert isolated_rows[0]["material_name"] == "M_bark_common_end_01"
        assert {
            Path(value).name for value in isolated_rows[0]["refs"]
        } == {
            Path(value).name for value in canonical_refs
        }

        cached = prepare_isolated_bark_source(
            source,
            contract,
            rows,
            cache_parent=cache,
        )
        assert cached["status"] == "cached"
        assert cached["speedtree_spm"] == str(isolated)
        assert _sha256(source) == source_hash

        Path(cached["manifest"]).unlink()
        rebuilt = prepare_isolated_bark_source(
            source,
            contract,
            rows,
            cache_parent=cache,
        )
        assert rebuilt["status"] == "prepared"
        assert Path(rebuilt["manifest"]).is_file()
        assert Path(rebuilt["speedtree_spm"]).is_file()
        assert _sha256(source) == source_hash


def test_live_owner_contract_overrides_missing_or_stale_receipt_cache():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_elm"
        source = root / "Cluster" / "SK_leaf_elm_01.spm"
        target = root / "SK_Tree_elm_01.spm"
        canonical_refs = [_canonical_output(
            root,
            target,
            "1",
            "M_bark_elm_01",
            "T_bark_elm_01",
        )["color"]]
        _write_spm(
            source,
            "M_bark_common_end_01",
            ["generic/common_color.tga"],
        )
        source_texture = source.parent / "generic" / "common_color.tga"
        source_texture.parent.mkdir(parents=True, exist_ok=True)
        source_texture.write_bytes(b"generic")
        _write_spm(target, "M_bark_elm_01", canonical_refs)
        contract = {
            "tree_source_identities": [{
                "target_spm": {"path": str(target)},
            }],
            "dependencies": [{
                "spm": str(source),
            }],
            "handoff": {
                "canonical_bark": {
                    "status": "replacement_required",
                    "canonical_material": "M_bark_elm_01",
                    "canonical_sources": [{
                        "spm": str(target),
                        "material_id": "1",
                        "material_name": "M_bark_elm_01",
                        "refs": canonical_refs,
                    }],
                    "cluster_bark_sources": [{
                        "cluster_spm": str(source),
                        "material_id": "1",
                        "material_name": "M_bark_common_end_01",
                        "replacement": "required",
                    }],
                },
            },
        }

        resolved = resolve_cluster_bark_source_spm(
            source,
            [target],
            cache_parent=Path(temporary) / "cache",
            live_target_contracts=[{
                "target_spm": str(target),
                "report": str(Path(temporary) / "live.json"),
                "policy": "live_audit_authoritative",
                "contract": contract,
            }],
        )

        assert resolved["status"] == "prepared"
        assert resolved["target_receipts"][0]["policy"] == (
            "live_audit_authoritative"
        )
        assert resolved["target_receipts"][0]["dependency_matched"] is True
        assert (
            resolved["target_receipts"][0]["required_material_count"] == 1
        )

def test_validated_isolated_capture_remains_the_provider_runtime_source():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_chestnut"
        source = root / "Cluster" / "SK_leaf_chestnut_01.spm"
        target = root / "SK_tree_chestnut_01.spm"
        canonical_refs = [_canonical_output(
            root,
            target,
            "1",
            "M_bark_chestnut_01",
            "T_bark_chestnut_01",
        )["color"]]
        _write_spm(
            source,
            "M_bark_common_end_01",
            ["generic/common_color.tga"],
        )
        source_texture = source.parent / "generic" / "common_color.tga"
        source_texture.parent.mkdir(parents=True, exist_ok=True)
        source_texture.write_bytes(b"generic")
        _write_spm(target, "M_bark_chestnut_01", canonical_refs)
        contract = {
            "tree_source_identities": [{
                "target_spm": {"path": str(target)},
            }],
            "dependencies": [{
                "spm": str(source),
            }],
            "handoff": {
                "canonical_bark": {
                    "status": "canonical",
                    "canonical_material": "M_bark_chestnut_01",
                    "canonical_sources": [{
                        "spm": str(target),
                        "material_id": "1",
                        "material_name": "M_bark_chestnut_01",
                        "refs": canonical_refs,
                    }],
                    "cluster_bark_sources": [{
                        "cluster_spm": str(source),
                        "material_id": "1",
                        "material_name": "M_bark_common_end_01",
                        "replacement": "isolated_capture_validated",
                    }],
                },
            },
        }
        live_contracts = [{
            "target_spm": str(target),
            "report": str(Path(temporary) / "live.json"),
            "policy": "live_audit_authoritative",
            "contract": contract,
        }]
        cache = Path(temporary) / "cache"

        prepared = resolve_cluster_bark_source_spm(
            source,
            [target],
            cache_parent=cache,
            live_target_contracts=live_contracts,
        )
        cached = resolve_cluster_bark_source_spm(
            source,
            [target],
            cache_parent=cache,
            live_target_contracts=live_contracts,
        )

        assert prepared["status"] == "prepared"
        assert cached["status"] == "cached"
        assert cached["speedtree_spm"] == prepared["speedtree_spm"]
        assert cached["target_receipts"][0][
            "required_material_count"
        ] == 1


def test_direct_owner_canonical_overrides_provider_only_target_provenance():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "weed_black_locast"
        source = root / "cluster" / "SK_branch_black_locast_01.spm"
        owner = root / "SK_bush_black_locast_01.spm"
        provider_only = root / "SK_tree_black_locast_01.spm"
        _write_spm(
            source,
            "M_bark_common_end_01",
            ["generic/common_color.tga"],
        )
        source_texture = source.parent / "generic" / "common_color.tga"
        source_texture.parent.mkdir(parents=True, exist_ok=True)
        source_texture.write_bytes(b"generic")
        owner_refs = [_canonical_output(
            root,
            owner,
            "1",
            "M_bark_black_locast_01",
            "T_bark_black_locast_01",
        )["color"]]
        _write_spm(
            owner,
            "M_bark_black_locast_01",
            owner_refs,
        )
        provider_refs = ["legacy/uktladjcw_Albedo.tif"]
        _write_spm(
            provider_only,
            "M_bark_black_locast_01",
            provider_refs,
        )
        provider_texture = root / provider_refs[0]
        provider_texture.parent.mkdir(parents=True, exist_ok=True)
        provider_texture.write_bytes(b"provider")

        def contract(target, refs, authority=None):
            source_row = {
                "spm": str(target),
                "material_id": "1",
                "material_name": "M_bark_black_locast_01",
                "refs": refs,
            }
            if authority:
                source_row["authority"] = authority
            return {
                "tree_source_identities": [{
                    "target_spm": {"path": str(target)},
                }],
                "dependencies": [{"spm": str(source)}],
                "handoff": {
                    "canonical_bark": {
                        "canonical_material": "M_bark_black_locast_01",
                        "canonical_sources": [source_row],
                        "cluster_bark_sources": [{
                            "cluster_spm": str(source),
                            "material_id": "1",
                            "material_name": "M_bark_common_end_01",
                            "replacement": "required",
                        }],
                    },
                },
            }

        resolved = resolve_cluster_bark_source_spm(
            source,
            [owner, provider_only],
            cache_parent=Path(temporary) / "cache",
            live_target_contracts=[
                {
                    "target_spm": str(owner),
                    "contract": contract(owner, owner_refs),
                },
                {
                    "target_spm": str(provider_only),
                    "contract": contract(
                        provider_only,
                        provider_refs,
                        "active_provider_texture_provenance",
                    ),
                },
            ],
        )

        assert resolved["status"] == "prepared"
        manifest = json.loads(
            Path(resolved["manifest"]).read_text(encoding="utf-8")
        )
        assert (
            manifest["normalization"]["canonical_material"]
            == "M_bark_black_locast_01"
        )
        assert manifest["copied_canonical_textures"] == []
        assert (
            manifest["production_texture_handoff"][
                "rewritten_reference_count"
            ]
            == 1
        )
        assert {
            Path(row["expected_output"]).name
            for row in manifest["production_texture_handoff"]["references"]
        } == {"T_bark_black_locast_01_color.tga"}


def test_live_pass_through_contract_does_not_fall_back_to_old_receipt():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_plain"
        source = root / "Cluster" / "SK_leaf_plain_01.spm"
        target = root / "SK_Tree_plain_01.spm"
        _write_spm(source, "M_leaf_plain", [])
        _write_spm(target, "M_bark_plain", [])

        resolved = resolve_cluster_bark_source_spm(
            source,
            [target],
            live_target_contracts=[{
                "target_spm": str(target),
                "report": str(Path(temporary) / "live.json"),
                "policy": "live_audit_authoritative_pass_through",
                "contract": None,
            }],
        )

        assert resolved["status"] == "not_required"
        assert resolved["target_receipts"] == [{
            "target_spm": str(target.resolve()),
            "receipt": str(Path(temporary) / "live.json"),
            "policy": "live_audit_authoritative_pass_through",
            "dependency_matched": False,
            "required_material_count": 0,
        }]


def test_isolated_bark_source_copies_relative_external_mesh_dependencies():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_chestnut"
        source = root / "Cluster" / "SK_leaf_chestnut_01.spm"
        canonical = root / "SK_tree_chestnut_01.spm"
        external_relative = "meshes/leaf_plate.fbx"
        _write_spm(
            source,
            "M_bark_common_end_01",
            ["generic/common_color.tga"],
            external_mesh=external_relative,
        )
        source_texture = source.parent / "generic" / "common_color.tga"
        source_texture.parent.mkdir(parents=True, exist_ok=True)
        source_texture.write_bytes(b"generic")
        external_mesh = source.parent / external_relative
        external_mesh.parent.mkdir(parents=True, exist_ok=True)
        external_mesh.write_bytes(b"external-fbx")
        canonical_refs = [_canonical_output(
            root,
            canonical,
            "1",
            "M_bark_chestnut_01",
            "T_bark_chestnut_01",
        )["color"]]
        _write_spm(canonical, "M_bark_chestnut_01", canonical_refs)
        contract = {
            "handoff": {
                "canonical_bark": {
                    "status": "replacement_required",
                    "canonical_material": "M_bark_chestnut_01",
                    "canonical_sources": [{
                        "spm": str(canonical),
                        "material_id": "1",
                        "material_name": "M_bark_chestnut_01",
                        "refs": canonical_refs,
                    }],
                    "cluster_bark_sources": [{
                        "cluster_spm": str(source),
                        "material_id": "1",
                        "material_name": "M_bark_common_end_01",
                        "replacement": "required",
                    }],
                },
            },
        }
        rows = contract["handoff"]["canonical_bark"][
            "cluster_bark_sources"
        ]

        prepared = prepare_isolated_bark_source(
            source,
            contract,
            rows,
            cache_parent=Path(temporary) / "cache",
        )
        isolated = Path(prepared["speedtree_spm"])

        assert (isolated.parent / external_relative).read_bytes() == b"external-fbx"


def test_isolated_bark_source_rebases_existing_mesh_outside_workspace():
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        production = (
            workspace / "assets" / "deep" / "Cluster" / "SK_branch_ivy.spm"
        )
        external_ref = "../../../shared/roof_set_broken_dummy.fbx"
        _write_spm(
            production,
            "M_bark_common_end_01",
            [],
            external_mesh=external_ref,
        )
        external_mesh = (
            production.parent / external_ref
        ).resolve()
        external_mesh.parent.mkdir(parents=True, exist_ok=True)
        external_mesh.write_bytes(b"shared-external-fbx")
        production_hash = _sha256(production)

        staging = workspace / "cache" / "staging"
        isolated = (
            staging / "Tree_ivy" / "Cluster" / production.name
        )
        isolated.parent.mkdir(parents=True, exist_ok=True)
        isolated.write_bytes(production.read_bytes())
        copied = _copy_source_external_meshes(
            {
                "source_external_meshes": [{
                    "source": str(external_mesh),
                    "sha256": _sha256(external_mesh),
                    "relative_to_spm": external_ref,
                }],
            },
            isolated,
            staging,
        )

        result = _rebase_external_mesh_refs(
            isolated,
            production,
            copied,
        )

        assert result["status"] == "rebased"
        assert result["rewritten_reference_count"] == 1
        assert _sha256(production) == production_hash
        isolated_text = gzip.decompress(isolated.read_bytes()).decode("utf-8")
        assert external_ref not in isolated_text
        rebased = (isolated.parent / copied[0]["spm_ref"]).resolve()
        assert rebased.is_relative_to(staging.resolve())
        assert rebased.read_bytes() == b"shared-external-fbx"


def test_canonical_bark_texture_outside_tree_is_copied_by_content():
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        canonical_spm = (
            workspace / "trees" / "Tree_owner" / "SK_tree_owner_01.spm"
        )
        canonical_spm.parent.mkdir(parents=True)
        canonical_spm.write_bytes(b"spm")
        shared_texture = workspace / "shared" / "bark_color.tga"
        shared_texture.parent.mkdir()
        shared_texture.write_bytes(b"shared-bark")
        isolated_tree = workspace / "isolated" / "Tree_provider"

        copied = _copy_canonical_textures(
            {
                "handoff": {
                    "canonical_bark": {
                        "canonical_sources": [{
                            "spm": str(canonical_spm),
                            "refs": [str(shared_texture)],
                        }],
                    },
                },
            },
            isolated_tree,
        )

        assert len(copied) == 1
        destination = Path(copied[0]["isolated"])
        assert destination.is_relative_to(isolated_tree)
        assert "_canonical_textures" in destination.parts
        assert destination.read_bytes() == b"shared-bark"


def test_isolated_bark_source_rebases_external_texture_dependencies():
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        root = workspace / "Tree_chestnut"
        source = root / "Cluster" / "SK_leaf_chestnut_01.spm"
        canonical = root / "SK_tree_chestnut_01.spm"
        external_ref = "../../Texture/chestnut/leaf_color.tif"
        _write_spm(
            source,
            "M_bark_common_end_01",
            ["generic/common_color.tga"],
            extra_material_refs=[external_ref],
        )
        source_texture = source.parent / "generic" / "common_color.tga"
        source_texture.parent.mkdir(parents=True, exist_ok=True)
        source_texture.write_bytes(b"generic")
        external_texture = workspace / "Texture" / "chestnut" / "leaf_color.tif"
        external_texture.parent.mkdir(parents=True, exist_ok=True)
        external_texture.write_bytes(b"leaf")
        canonical_refs = [_canonical_output(
            root,
            canonical,
            "1",
            "M_bark_chestnut_01",
            "T_bark_chestnut_01",
        )["color"]]
        leaf_outputs = _canonical_output(
            root,
            source,
            "2",
            "M_leaf_preserved",
            "T_leaf_chestnut_preserved",
        )
        _write_spm(canonical, "M_bark_chestnut_01", canonical_refs)
        contract = {
            "handoff": {
                "canonical_bark": {
                    "status": "replacement_required",
                    "canonical_material": "M_bark_chestnut_01",
                    "canonical_sources": [{
                        "spm": str(canonical),
                        "material_id": "1",
                        "material_name": "M_bark_chestnut_01",
                        "refs": canonical_refs,
                    }],
                    "cluster_bark_sources": [{
                        "cluster_spm": str(source),
                        "material_id": "1",
                        "material_name": "M_bark_common_end_01",
                        "replacement": "required",
                    }],
                },
            },
        }
        rows = contract["handoff"]["canonical_bark"][
            "cluster_bark_sources"
        ]

        prepared = prepare_isolated_bark_source(
            source,
            contract,
            rows,
            cache_parent=workspace / "cache",
        )
        isolated = Path(prepared["speedtree_spm"])
        leaf = next(
            row for row in extract_material_image_refs(isolated)
            if row["material_name"] == "M_leaf_preserved"
        )
        rebased = isolated.parent / Path(leaf["refs"][0])

        assert rebased.resolve().is_file()
        assert rebased.resolve() == (root / leaf_outputs["color"]).resolve()
        assert rebased.resolve().read_bytes() == (
            root / leaf_outputs["color"]
        ).read_bytes()
        assert workspace / "Texture" / "chestnut" / "leaf_color.tif" == external_texture
