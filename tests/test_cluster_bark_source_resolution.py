import gzip
import hashlib
import tempfile
from pathlib import Path
from unittest import mock

from cluster_bark_source_resolution import (
    _publish_cache_directory,
    prepare_isolated_bark_source,
    resolve_cluster_bark_source_spm,
)
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


def test_cache_publish_retries_transient_windows_access_denied():
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / "staging"
        cache_dir = Path(temporary) / "cache"
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


def test_content_addressed_bark_source_preserves_production_spm():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "Tree_elm"
        source = root / "Cluster" / "SK_branch_elm_01.spm"
        canonical = root / "SK_Tree_elm_01.spm"
        canonical_refs = [
            "texture/bark_elm_01_color.tga",
            "texture/bark_elm_01_normal.tga",
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
        for index, value in enumerate(canonical_refs, 1):
            texture = root / value
            texture.parent.mkdir(parents=True, exist_ok=True)
            texture.write_bytes(f"texture-{index}".encode("ascii"))
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
        canonical_refs = ["texture/bark_elm_01_color.tga"]
        _write_spm(
            source,
            "M_bark_common_end_01",
            ["generic/common_color.tga"],
        )
        source_texture = source.parent / "generic" / "common_color.tga"
        source_texture.parent.mkdir(parents=True, exist_ok=True)
        source_texture.write_bytes(b"generic")
        _write_spm(target, "M_bark_elm_01", canonical_refs)
        canonical_texture = root / canonical_refs[0]
        canonical_texture.parent.mkdir(parents=True, exist_ok=True)
        canonical_texture.write_bytes(b"canonical")
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
        canonical_refs = ["texture/bark_chestnut_01_color.tga"]
        _write_spm(canonical, "M_bark_chestnut_01", canonical_refs)
        canonical_texture = root / canonical_refs[0]
        canonical_texture.parent.mkdir(parents=True, exist_ok=True)
        canonical_texture.write_bytes(b"canonical")
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
        canonical_refs = ["texture/bark_chestnut_01_color.tga"]
        _write_spm(canonical, "M_bark_chestnut_01", canonical_refs)
        canonical_texture = root / canonical_refs[0]
        canonical_texture.parent.mkdir(parents=True, exist_ok=True)
        canonical_texture.write_bytes(b"canonical")
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
        assert rebased.resolve().read_bytes() == b"leaf"
        assert workspace / "Texture" / "chestnut" / "leaf_color.tif" == external_texture
