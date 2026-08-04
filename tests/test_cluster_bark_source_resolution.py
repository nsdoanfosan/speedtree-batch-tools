import gzip
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from cluster_bark_source_resolution import (
    ClusterBarkSourceResolutionError,
    canonical_bark_record_plan,
    _copy_canonical_textures,
    _copy_source_external_meshes,
    _normalization_identity,
    _publish_cache_directory,
    _rebase_external_mesh_refs,
    prepare_isolated_bark_source,
    resolve_cluster_bark_source_spm,
)
from speedtree_texture_contract import REQUIRED_TEXTURE_ROLES
from pcg_st9_texture_batch.pcg_texture_audit import (
    extract_material_image_refs,
)
from pcg_st9_texture_batch.pcg_cluster_assembly_contract import (
    ClusterAssemblyReceiptAmbiguityError,
    ClusterAssemblyReceiptStaleError,
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
    generator_material_ids=("1",),
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
        '<Generators>'
        + "".join(
            '<Generator Type="Branch"><Properties><Property>'
            '<Name>Branches:Material</Name>'
            f'<Value>{material_id}</Value>'
            '</Property></Properties></Generator>'
            for material_id in generator_material_ids
        )
        + '</Generators></SpeedTree>'
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

        Path(rebuilt["speedtree_spm"]).unlink()
        missing_exact_rebuilt = prepare_isolated_bark_source(
            source,
            contract,
            rows,
            cache_parent=cache,
        )
        assert missing_exact_rebuilt["status"] == "prepared"
        assert Path(missing_exact_rebuilt["manifest"]).is_file()
        assert Path(missing_exact_rebuilt["speedtree_spm"]).is_file()
        assert _sha256(source) == source_hash


def test_final_bark_handoff_scopes_detached_materials_to_generator_consumers():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_elm"
        source = root / "Cluster" / "SK_branch_elm_01.spm"
        canonical = root / "SK_Tree_elm_01.spm"
        _write_spm(
            source,
            "M_bark_common_end_01",
            ["old/common_color.tga"],
            extra_material_refs=["old/detached_leaf.tif"],
        )
        canonical_refs = [_canonical_output(
            root,
            canonical,
            "1",
            "M_Bark_elm_01",
            "T_Bark_elm_01",
        )["color"]]
        _write_spm(canonical, "M_Bark_elm_01", canonical_refs)
        contract = {
            "handoff": {
                "canonical_bark": {
                    "status": "replacement_required",
                    "canonical_material": "M_Bark_elm_01",
                    "canonical_sources": [{
                        "spm": str(canonical),
                        "material_id": "1",
                        "material_name": "M_Bark_elm_01",
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
        required_rows = contract["handoff"]["canonical_bark"][
            "cluster_bark_sources"
        ]

        _signature, identity = _normalization_identity(
            contract, source, required_rows
        )

        handoff = identity["production_texture_handoff"]
        assert handoff["status"] == "ok"
        assert {
            row["material_id"]
            for row in handoff["skipped_unreferenced_materials"]
        } == {"2"}
        assert handoff["skipped_unreferenced_materials"][0]["refs"] == [
            "old/detached_leaf.tif"
        ]

        _write_spm(
            source,
            "M_bark_common_end_01",
            ["old/common_color.tga"],
            extra_material_refs=["old/detached_leaf.tif"],
            generator_material_ids=("1", "2"),
        )
        _signature, degraded = _normalization_identity(
            contract,
            source,
            required_rows,
        )
        assert any(
            issue.get("reason") == "material_canonical_output_unmapped"
            for issue in degraded["production_texture_handoff"].get(
                "issues"
            ) or []
        )


@pytest.mark.parametrize(
    ("available_roles", "expected_names", "availability"),
    [
        (
            ("color",),
            {"T_bark_elm_01_color.tga"},
            "partial",
        ),
        ((), set(), "textureless"),
    ],
)
def test_manifestless_partial_or_textureless_bark_still_prepares(
    tmp_path,
    available_roles,
    expected_names,
    availability,
):
    root = tmp_path / "Tree_elm"
    source = root / "Cluster" / "SK_branch_elm_01.spm"
    canonical = root / "SK_Tree_elm_01.spm"
    texture_root = root / "texture"
    texture_root.mkdir(parents=True)
    canonical_refs = [
        "texture/T_bark_elm_01_color.tga",
        "texture/T_bark_elm_01_normal.tga",
    ]
    for role in available_roles:
        (texture_root / f"T_bark_elm_01_{role}.tga").write_bytes(
            role.encode("ascii")
        )
    _write_spm(
        source,
        "M_bark_common_end_01",
        ["generic/common_color.tga", "generic/common_normal.tga"],
    )
    _write_spm(canonical, "M_bark_elm_01", canonical_refs)
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
    required = contract["handoff"]["canonical_bark"][
        "cluster_bark_sources"
    ]

    prepared = prepare_isolated_bark_source(
        source,
        contract,
        required,
        cache_parent=tmp_path / "cache",
    )

    assert prepared["status"] == "prepared"
    isolated = Path(prepared["speedtree_spm"])
    row = extract_material_image_refs(isolated)[0]
    assert {Path(value).name for value in row["refs"]} == expected_names
    manifest = json.loads(
        Path(prepared["manifest"]).read_text(encoding="utf-8")
    )
    assert (
        manifest["production_texture_handoff"]["texture_availability"]
        == availability
    )
    assert not (
        texture_root / "pcg_st9_canonical_outputs.json"
    ).exists()


def test_same_bytes_provider_rename_rebuilds_exact_filename_bundle():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_neutral"
        source = root / "Cluster" / "SK_branch_provider_legacy_01.spm"
        renamed = root / "Cluster" / "SK_branch_provider_current_01.spm"
        canonical = root / "SK_Tree_neutral_01.spm"
        canonical_outputs = _canonical_output(
            root,
            canonical,
            "1",
            "M_bark_neutral_01",
            "T_bark_neutral_01",
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
        _write_spm(canonical, "M_bark_neutral_01", canonical_refs)
        contract = {
            "handoff": {
                "canonical_bark": {
                    "status": "replacement_required",
                    "canonical_material": "M_bark_neutral_01",
                    "canonical_sources": [{
                        "spm": str(canonical),
                        "material_id": "1",
                        "material_name": "M_bark_neutral_01",
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
        legacy = prepare_isolated_bark_source(
            source,
            contract,
            rows,
            cache_parent=cache,
        )
        source_hash = _sha256(source)
        source.rename(renamed)
        assert _sha256(renamed) == source_hash
        rows[0]["cluster_spm"] = str(renamed)

        current_signature, _identity = _normalization_identity(
            contract,
            renamed,
            rows,
        )
        incompatible = cache / current_signature[:24]
        shutil.copytree(Path(legacy["manifest"]).parent, incompatible)

        current = prepare_isolated_bark_source(
            renamed,
            contract,
            rows,
            cache_parent=cache,
        )
        current_manifest = json.loads(
            Path(current["manifest"]).read_text(encoding="utf-8")
        )

        assert current["status"] == "prepared"
        assert current["signature"] != legacy["signature"]
        assert Path(current["speedtree_spm"]).name == renamed.name
        assert Path(current["speedtree_spm"]).is_file()
        assert current_manifest["source_spm"] == str(renamed.resolve())
        assert current_manifest["speedtree_spm"] == current["speedtree_spm"]
        assert current_manifest["output_filename"] == renamed.name
        assert current_manifest["provider_identity"]["stem"] == (
            renamed.stem.casefold()
        )
        assert Path(legacy["manifest"]).is_file()


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


@pytest.mark.parametrize(
    "authority_error",
    [
        FileNotFoundError("missing receipt"),
        ClusterAssemblyReceiptStaleError("stale receipt"),
        ClusterAssemblyReceiptAmbiguityError("divergent receipts"),
    ],
)
def test_persisted_authority_failure_without_live_audit_fails_closed(
    authority_error,
):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_elm"
        source = root / "Cluster" / "SK_leaf_elm_01.spm"
        target = root / "SK_Tree_elm_01.spm"
        _write_spm(source, "M_bark_common_end_01", [])
        _write_spm(target, "M_bark_elm_01", [])

        with (
            mock.patch(
                "cluster_bark_source_resolution."
                "locate_cluster_assembly_receipt",
                side_effect=authority_error,
            ),
            mock.patch(
                "cluster_bark_source_resolution."
                "prepare_isolated_bark_source",
            ) as prepare,
            pytest.raises(
                ClusterBarkSourceResolutionError,
                match="run a live Cluster Assembly audit",
            ),
        ):
            resolve_cluster_bark_source_spm(source, [target])

        prepare.assert_not_called()


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


def test_same_authority_texture_disagreement_is_quarantined_not_blocked(
    tmp_path,
):
    root = tmp_path / "Tree_shared"
    source = root / "Cluster" / "SK_branch_shared_01.spm"
    owner_a = root / "SK_tree_owner_a_01.spm"
    owner_b = root / "SK_tree_owner_b_01.spm"
    _write_spm(
        source,
        "M_bark_common_end_01",
        ["generic/common_color.tga"],
    )
    generic = source.parent / "generic" / "common_color.tga"
    generic.parent.mkdir(parents=True)
    generic.write_bytes(b"generic")
    refs_a = [_canonical_output(
        root,
        owner_a,
        "1",
        "M_bark_shared_01",
        "T_bark_owner_a_01",
    )["color"]]
    refs_b = [_canonical_output(
        root,
        owner_b,
        "1",
        "M_bark_shared_01",
        "T_bark_owner_b_01",
    )["color"]]
    _write_spm(owner_a, "M_bark_shared_01", refs_a)
    _write_spm(owner_b, "M_bark_shared_01", refs_b)

    def contract(target, refs):
        return {
            "dependencies": [{"spm": str(source)}],
            "handoff": {
                "canonical_bark": {
                    "status": "replacement_required",
                    "canonical_material": "M_bark_shared_01",
                    "canonical_sources": [{
                        "spm": str(target),
                        "material_id": "1",
                        "material_name": "M_bark_shared_01",
                        "refs": refs,
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
        [owner_a, owner_b],
        cache_parent=tmp_path / "cache",
        live_target_contracts=[
            {
                "target_spm": str(owner_a),
                "contract": contract(owner_a, refs_a),
            },
            {
                "target_spm": str(owner_b),
                "contract": contract(owner_b, refs_b),
            },
        ],
    )

    assert resolved["status"] == "prepared"
    isolated_row = extract_material_image_refs(
        Path(resolved["speedtree_spm"])
    )[0]
    assert isolated_row["refs"] == []
    manifest = json.loads(
        Path(resolved["manifest"]).read_text(encoding="utf-8")
    )
    assert (
        manifest["production_texture_handoff"]["texture_availability"]
        == "textureless"
    )


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
            generator_material_ids=("1", "2"),
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


class TestCanonicalBarkRecordPlan:
    """A lost manifest entry is bookkeeping, not missing texture work.

    On 2026-08-04 every canonical bark row blocked in production -- 24 rows
    across 8 assets -- had its complete six-role T_* set already on disk.  The
    gate still told the operator to generate those outputs in PCG ST9 Texture,
    which is work that does not exist.  The plan below is built only from what
    the row itself proves: its SPM, its material, and the refs that material
    actually points at.
    """

    def row(self, root, base="T_bark_test_01", roles=None):
        texture = root / "texture"
        texture.mkdir(parents=True, exist_ok=True)
        roles = roles or list(REQUIRED_TEXTURE_ROLES)
        refs = []
        for role in roles:
            path = texture / f"{base}_{role}.tga"
            path.write_bytes(b"tga")
            refs.append(f"texture\\{base}_{role}.tga")
        return {
            "spm": str(root / "cluster" / "SK_leaf_test_01.spm"),
            "material_id": "6",
            "material_name": "M_bark_test_01",
            "refs": refs,
        }

    def test_a_complete_on_disk_set_is_a_recordable_entry(self, tmp_path):
        row = self.row(tmp_path / "tree_test")

        plan = canonical_bark_record_plan(row)

        assert plan is not None
        assert plan["texture_base"] == "T_bark_test_01"
        assert Path(plan["asset_root"]).name == "tree_test"
        assert plan["material_targets"] == [{
            "spm": row["spm"],
            "material_id": "6",
            "material_name": "M_bark_test_01",
        }]
        assert len(plan["output_files"]) == len(REQUIRED_TEXTURE_ROLES)

    def test_an_absent_role_file_is_not_recordable(self, tmp_path):
        root = tmp_path / "tree_test"
        row = self.row(root)
        (root / "texture" / "T_bark_test_01_normal.tga").unlink()

        assert canonical_bark_record_plan(row) is None

    def test_an_empty_role_file_is_not_recordable(self, tmp_path):
        root = tmp_path / "tree_test"
        row = self.row(root)
        (root / "texture" / "T_bark_test_01_normal.tga").write_bytes(b"")

        assert canonical_bark_record_plan(row) is None

    def test_an_incomplete_role_set_is_not_recordable(self, tmp_path):
        row = self.row(tmp_path / "tree_test", roles=["color", "normal"])

        assert canonical_bark_record_plan(row) is None

    def test_two_texture_bases_are_never_guessed_apart(self, tmp_path):
        root = tmp_path / "tree_test"
        row = self.row(root)
        row["refs"][0] = "texture\\T_bark_other_01_color.tga"

        assert canonical_bark_record_plan(row) is None

    def test_a_non_canonical_base_is_refused(self, tmp_path):
        row = self.row(tmp_path / "tree_test", base="bark_test_01")

        assert canonical_bark_record_plan(row) is None

    def test_a_row_without_refs_proves_nothing(self, tmp_path):
        row = self.row(tmp_path / "tree_test")
        row["refs"] = []

        assert canonical_bark_record_plan(row) is None

    def test_the_asset_root_is_the_cluster_parent(self, tmp_path):
        root = tmp_path / "tree_test"
        row = self.row(root)

        plan = canonical_bark_record_plan(row)

        assert Path(plan["asset_root"]) == root.resolve()
