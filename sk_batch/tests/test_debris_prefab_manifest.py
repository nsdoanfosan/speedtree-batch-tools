import copy
import hashlib
import json
from pathlib import Path
import unittest

from sk_batch.debris_prefab_manifest import build_prefab_manifest, finalize_import_verified_manifest

FIXTURE=Path(__file__).parent/"fixtures/debris_prefab_audit_09_10.json"


def prepared(source):
    parts=source["parts"]
    groups=[]
    for row in source["groups"]:
        indexes=row["part_indexes"]
        suffix=f'G{row["group_id"]+1:02d}'
        name=f'SM_{source["name"]}_Terrain_{suffix}'
        groups.append({**row,"root_object":name,"mesh_object":name+"_Mesh",
                       "triangles":sum(parts[i]["triangle_count"] for i in indexes)})
    checks={name:True for name in ("whole_components_preserved","all_named_attributes_subset_equal",
            "material_slots_preserved","uv_layers_preserved","world_geometry_preserved",
            "corner_normals_preserved","geometry_counts_conserved","source_datablock_unchanged",
            "original_export_unchanged","requested_budget_respected")}
    receipt={"status":"built","group_count":len(groups),"groups":groups,"checks":checks,
             "source":{"file":source["source_file"],"baseline_file_sha256":source["source_sha256"],
                       "fingerprint_before_after":["a"*64,"a"*64],
                       "vertices":source["vertices"],"triangles":source["triangles"]},
             "source_global_min_z_m":min(p["world_aabb_min_m"][2] for p in parts),
             "wind_none_contract":{"scene_explicit":True,"scene_preset":"NONE",
                 "saved_contracts":[{"preset":"NONE","enabled":False,"sha256":"b"*64,"path":"fixture-contract.json"}]}}
    manifest={"items":[{"assets":[{"asset_data":{"_asset_type":"StaticMesh",
                        "empty_object_name":g["root_object"],"_mesh_object_name":g["mesh_object"],
                        "asset_path":"/Game/Fixtures/"+g["root_object"]}} for g in groups]}]}
    return receipt,manifest


class DebrisPrefabManifestTests(unittest.TestCase):
    def setUp(self):
        self.sources=json.loads(FIXTURE.read_text())["sources"]

    def test_09_and_10_actual_audit_membership_binds_without_source_name_branch(self):
        for source in self.sources:
            receipt,manifest=prepared(source)
            result=build_prefab_manifest(receipt,manifest,source_skeletal_mesh="/Game/Fixtures/SK_"+source["name"],
                                         source_evidence=source,material_overrides=["/Game/Fixtures/MI_Leaf"])
            self.assertEqual(result["group_count"],len(source["groups"]))
            self.assertEqual(sum(g["source_part_count"] for g in result["groups"]),len(source["parts"]))
            self.assertFalse(result["import_geometry_verified"])
            self.assertEqual(result["contact_policy_version"],2)
            self.assertEqual(result["source_floor_policy"],"group_min")
            for group,actual in zip(source["groups"],result["groups"]):
                x,y,z=group["pivot_world_m"]
                self.assertEqual(actual["relative_location_cm"],[100*x,-100*y,100*z])
                self.assertEqual(actual["component_name"],f'Group{actual["group_id"]:02d}')
                self.assertEqual(actual["material_overrides"],["/Game/Fixtures/MI_Leaf"])
                self.assertEqual(len(actual["probes_local_cm"]),5)
                self.assertTrue(all(p[2]==-actual["contact_offset_cm"] for p in actual["probes_local_cm"]))
                local_min=min(source["parts"][i]["world_aabb_min_m"][2] for i in group["part_indexes"])
                self.assertEqual(actual["source_floor_z_cm"],100*local_min)

    def test_explicit_legacy_global_floor_is_available_without_changing_component_transforms(self):
        source=self.sources[1]
        receipt,manifest=prepared(source)
        options={"source_skeletal_mesh":"/Game/Fixtures/SK_Leaf","source_evidence":source}
        current=build_prefab_manifest(receipt,manifest,**options)
        legacy=build_prefab_manifest(receipt,manifest,floor_policy="source_global",**options)
        self.assertEqual(legacy["contact_policy_version"],1)
        global_floor=100*receipt["source_global_min_z_m"]
        self.assertTrue(all(g["source_floor_z_cm"]==global_floor for g in legacy["groups"]))
        self.assertEqual([g["relative_location_cm"] for g in current["groups"]],
                         [g["relative_location_cm"] for g in legacy["groups"]])
        self.assertTrue(any(g["source_floor_z_cm"]>global_floor for g in current["groups"]))

    def test_export_binding_rejects_wrong_child_or_duplicate_parent(self):
        receipt,manifest=prepared(self.sources[0])
        for change in ("child","parent"):
            damaged=copy.deepcopy(manifest)
            data=damaged["items"][0]["assets"][0]["asset_data"]
            if change=="child":
                data["_mesh_object_name"]="WrongChild"
            else:
                data["empty_object_name"]=damaged["items"][0]["assets"][1]["asset_data"]["empty_object_name"]
            with self.assertRaises(ValueError):
                build_prefab_manifest(receipt,damaged,source_skeletal_mesh="/Game/Fixtures/SK_Leaf",
                                      source_evidence=self.sources[0])

    def test_source_wind_does_not_control_activation(self):
        receipt,manifest=prepared(self.sources[0])
        for enabled in (True,None):
            damaged=copy.deepcopy(receipt)
            damaged["wind_none_contract"]["saved_contracts"][0]["preset"]="WEED"
            damaged["wind_none_contract"]["saved_contracts"][0]["enabled"]=enabled
            payload=build_prefab_manifest(damaged,manifest,source_skeletal_mesh="/Game/Fixtures/SK_Leaf",
                                          source_evidence=self.sources[0])
            self.assertEqual(payload["output_mode"], "static_rest_pose")
            self.assertEqual(payload["provenance"]["wind_contracts"], damaged["wind_none_contract"]["saved_contracts"])

    def test_export_cannot_claim_native_import_verification_without_readback(self):
        receipt,manifest=prepared(self.sources[0])
        with self.assertRaisesRegex(ValueError,"receipt-bound"):
            build_prefab_manifest(receipt,manifest,source_skeletal_mesh="/Game/Fixtures/SK_Leaf",
                source_evidence=self.sources[0],geometry_verified=True)

    def test_duplicate_leaf_or_changed_source_rejected(self):
        receipt,manifest=prepared(self.sources[0])
        damaged=copy.deepcopy(receipt)
        damaged["groups"][0]["part_indexes"].append(damaged["groups"][1]["part_indexes"][0])
        with self.assertRaisesRegex(ValueError,"membership"):
            build_prefab_manifest(damaged,manifest,source_skeletal_mesh="/Game/Fixtures/SK_Leaf",
                                  source_evidence=self.sources[0])
        damaged=copy.deepcopy(receipt)
        damaged["source"]["fingerprint_before_after"][1]="c"*64
        with self.assertRaisesRegex(ValueError,"fingerprint"):
            build_prefab_manifest(damaged,manifest,source_skeletal_mesh="/Game/Fixtures/SK_Leaf",
                                  source_evidence=self.sources[0])

    def test_unrelated_leaf_name_and_one_group_payload_are_supported(self):
        source=copy.deepcopy(self.sources[0])
        source["name"]="oak_leaf_scatter"
        source["source_file"]="D:/Art/SK_oak_leaf_scatter.blend"
        source["groups"]=[{"group_id":0,"part_indexes":list(range(len(source["parts"]))),
                           "pivot_world_m":[0.,0.,min(p["world_aabb_min_m"][2] for p in source["parts"])]}]
        receipt,manifest=prepared(source)
        result=build_prefab_manifest(receipt,manifest,source_skeletal_mesh="/Game/Fixtures/SK_oak_leaf_scatter",
                                     source_evidence=source)
        self.assertEqual(result["group_count"],1)
        self.assertEqual(result["prefab_asset_path"],"/Game/Fixtures/BP_oak_leaf_scatter_TerrainPrefab")

    def geometry_proof(self):
        source=self.sources[0]
        receipt,manifest=prepared(source)
        payload=build_prefab_manifest(receipt,manifest,source_skeletal_mesh="/Game/Fixtures/SK_Leaf",source_evidence=source)
        header={"source_sha256_before":payload["source_hash"],"source_sha256_after":payload["source_hash"],
                "binary_sha256":"c"*64,"group_triangle_count":source["triangles"],"groups":[]}
        verified=[]
        for group,build in zip(payload["groups"],receipt["groups"]):
            path=group["mesh_asset_path"]
            header["groups"].append({"asset_path":path,"pivot_source_world_m":build["pivot_world_m"],
                                     "triangle_count":build["triangles"]})
            verified.append({"asset_path":path+"."+path.rsplit("/",1)[-1],"passed":True,
                             "triangle_count_equal":True,"expected_triangles":build["triangles"],
                             "actual_triangles":build["triangles"],"expected_to_actual":{"passed":True},
                             "actual_to_expected":{"passed":True}})
        raw=json.dumps(header).encode("utf-8")
        proof={"kind":"bidirectional_debris_group_source_model_verification","status":"passed",
               "passed":True,"group_geometry_passed":True,"triangle_total_equal":True,
               "no_coordinate_transform_inferred":True,"tolerance_cm":0.001,
               "expected_header_sha256":hashlib.sha256(raw).hexdigest(),"expected_binary_sha256":"c"*64,
               "actual_header_sha256":"d"*64,"actual_binary_sha256":"e"*64,
               "group_count":len(verified),"required_triangle_total":source["triangles"],
               "expected_group_triangles":source["triangles"],"actual_group_triangles":source["triangles"],
               "groups":verified}
        return payload,proof,raw

    def test_finalize_binds_source_assets_pivots_and_rehashes_without_mutating_export(self):
        payload,proof,raw=self.geometry_proof()
        result=finalize_import_verified_manifest(payload,proof,expected_geometry_header_bytes=raw)
        self.assertTrue(result["import_geometry_verified"])
        self.assertFalse(payload["import_geometry_verified"])
        self.assertNotEqual(result["payload_sha256"],payload["payload_sha256"])
        self.assertEqual(result["import_geometry_verification"]["source_hash"],payload["source_hash"])

    def test_finalize_rejects_stale_source_missing_asset_loose_tolerance_or_failed_direction(self):
        payload,proof,raw=self.geometry_proof()
        for mutation in ("source","asset","pivot","tolerance","direction","hash"):
            damaged=copy.deepcopy(proof)
            header=json.loads(raw)
            candidate_raw=raw
            if mutation=="source":
                header["source_sha256_after"]="f"*64
            elif mutation=="pivot":
                header["groups"][0]["pivot_source_world_m"][0]+=0.01
            elif mutation=="asset":
                damaged["groups"].pop()
            elif mutation=="tolerance":
                damaged["tolerance_cm"]=0.01
            elif mutation=="direction":
                damaged["groups"][0]["actual_to_expected"]["passed"]=False
            else:
                damaged["actual_binary_sha256"]="missing"
            if mutation in ("source","pivot"):
                candidate_raw=json.dumps(header).encode("utf-8")
                damaged["expected_header_sha256"]=hashlib.sha256(candidate_raw).hexdigest()
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                finalize_import_verified_manifest(payload,damaged,expected_geometry_header_bytes=candidate_raw)

    def test_native_material_binding_uses_exact_slot_identity_not_global_array_truncation(self):
        payload,proof,raw=self.geometry_proof()
        native={"binary_sha256":proof["actual_binary_sha256"],"groups":[]}
        for index,group in enumerate(payload["groups"]):
            names=["LeafB"] if index==0 else ["LeafA","LeafB"]
            native["groups"].append({"asset_path":group["mesh_asset_path"],"material_slots":[
                {"index":i,"material_slot_name":name,"material_path":"/Engine/Default.Default"}
                for i,name in enumerate(names)]})
        native_raw=json.dumps(native).encode("utf-8")
        proof["actual_header_sha256"]=hashlib.sha256(native_raw).hexdigest()
        bindings={"LeafA":"/Game/Fixtures/MI_A","LeafB":"/Game/Fixtures/MI_B"}
        result=finalize_import_verified_manifest(payload,proof,expected_geometry_header_bytes=raw,
            actual_geometry_header_bytes=native_raw,material_bindings=bindings)
        self.assertEqual(result["groups"][0]["material_overrides"],[bindings["LeafB"]])
        self.assertEqual(result["groups"][1]["material_overrides"],[bindings["LeafA"],bindings["LeafB"]])
        self.assertEqual(result["groups"][0]["material_slot_names"],["LeafB"])
        with self.assertRaisesRegex(ValueError,"canonical binding"):
            finalize_import_verified_manifest(payload,proof,expected_geometry_header_bytes=raw,
                actual_geometry_header_bytes=native_raw,material_bindings={"LeafA":bindings["LeafA"]})


if __name__=="__main__":
    unittest.main()
