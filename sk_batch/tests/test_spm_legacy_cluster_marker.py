import gzip
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[2]
PCG_DIR = REPO_DIR / "pcg_st9_texture_batch"
SK_BATCH_DIR = REPO_DIR / "sk_batch"
for candidate in (REPO_DIR, PCG_DIR, SK_BATCH_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from pcg_texture_audit import (  # noqa: E402
    legacy_cluster_generator_candidates,
    mark_legacy_cluster_generators_once,
    prepare_sk,
)
from spm_legacy_cluster_marker import (  # noqa: E402
    MARKER_VALUES,
    inspect_legacy_cluster_state,
    marker_receipt_path,
)
from migrate_legacy_cluster_markers import (  # noqa: E402
    migrate_existing_sk_markers,
)


FOREGROUND = {
    "m_bSetForegroundIconColor": "false",
    "m_vecForegroundIconColor_r": "0",
    "m_vecForegroundIconColor_g": "0",
    "m_vecForegroundIconColor_b": "0",
    "m_vecForegroundIconColor_a": "0",
}
BACKGROUND = {
    "m_bSetBackgroundIconColor": "true",
    "m_vecBackgroundIconColor_r": "0.15",
    "m_vecBackgroundIconColor_g": "0.25",
    "m_vecBackgroundIconColor_b": "0.35",
    "m_vecBackgroundIconColor_a": "1",
}


def add_material(assets, material_id, name, mesh_id, color_ref):
    material = ET.SubElement(
        assets, "Material_v8", ID=str(material_id), Name=name
    )
    ET.SubElement(material, "CutoutMeshID").text = str(mesh_id)
    textures = ET.SubElement(material, "Textures")
    ET.SubElement(textures, "TexFilename").text = color_ref
    ET.SubElement(assets, "Mesh", ID=str(mesh_id), Name=f"mesh_{mesh_id}")


def add_generator(model, guid, material_id, mesh_id, *, hidden=False):
    generator = ET.SubElement(model, "Generator", Type="Leaf Mesh")
    ET.SubElement(generator, "GUID").text = guid
    ET.SubElement(generator, "Name").text = guid
    ET.SubElement(generator, "Hidden").text = "true" if hidden else "false"
    extra = ET.SubElement(generator, "Extra")
    for tag, value in {**FOREGROUND, **BACKGROUND}.items():
        ET.SubElement(extra, tag).text = value
    properties = ET.SubElement(generator, "Properties")
    for suffix, value in (("Material", material_id), ("Mesh", mesh_id)):
        prop = ET.SubElement(properties, "Property")
        ET.SubElement(prop, "Name").text = f"Leaves:Type:0:{suffix}"
        ET.SubElement(prop, "Value").text = str(value)
    node = ET.SubElement(model, "Node", Type="Leaf")
    ET.SubElement(node, "GeneratorGUID").text = guid
    ET.SubElement(node, "ParentGUID").text = "parent-guid"
    ET.SubElement(node, "Name").text = f"node-{guid}"
    ET.SubElement(node, "GUID").text = f"node-guid-{guid}"
    ET.SubElement(node, "Hidden").text = "false"
    node_extra = ET.SubElement(node, "Extra")
    ET.SubElement(node_extra, "m_bDeleted").text = "false"
    ET.SubElement(node_extra, "m_bCulled").text = "false"


def write_spm(path, *, include_cluster=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    cluster_dir = path.parent / "Cluster"
    texture_dir = path.parent / "texture"
    cluster_dir.mkdir(exist_ok=True)
    texture_dir.mkdir(exist_ok=True)
    (cluster_dir / "old_cluster_color.tga").write_bytes(b"cluster")
    (texture_dir / "new_atlas_color.tga").write_bytes(b"atlas")

    root = ET.Element("SpeedTreeModel")
    assets = ET.SubElement(root, "Assets")
    source_ref = (
        r"Cluster\old_cluster_color.tga"
        if include_cluster
        else r"texture\ordinary_leaf_color.tga"
    )
    add_material(assets, 2, "cluster_source", 2, source_ref)
    add_material(assets, 8, "M_leaf_atlas_01", 18, r"texture\new_atlas_color.tga")
    add_generator(root, "legacy-visible", 2, 2)
    add_generator(root, "legacy-hidden", 2, 2, hidden=True)
    add_generator(root, "atlas-generator", 8, 18)
    path.write_bytes(gzip.compress(ET.tostring(root, encoding="utf-8")))
    return path


def generator_fields(spm):
    root = ET.fromstring(gzip.decompress(spm.read_bytes()))
    result = {}
    for generator in root.findall("./Generator"):
        guid = generator.findtext("GUID")
        extra = generator.find("Extra")
        result[guid] = {child.tag: child.text for child in extra}
    return result


class LegacyClusterMarkerTests(unittest.TestCase):
    def test_marks_cluster_render_generators_once_and_preserves_background(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = write_spm(root / "SK_tree_marker.spm")
            before = spm.read_bytes()
            candidates = legacy_cluster_generator_candidates(spm)

            self.assertEqual(
                {row["generator_guid"] for row in candidates},
                {"legacy-visible", "legacy-hidden"},
            )
            self.assertTrue(
                next(
                    row for row in candidates
                    if row["generator_guid"] == "legacy-visible"
                )["visible"]
            )
            self.assertFalse(
                next(
                    row for row in candidates
                    if row["generator_guid"] == "legacy-hidden"
                )["visible"]
            )

            dry = mark_legacy_cluster_generators_once(spm, dry_run=True)
            self.assertEqual(dry["status"], "planned")
            self.assertEqual(spm.read_bytes(), before)
            self.assertFalse(marker_receipt_path(spm).exists())

            applied = mark_legacy_cluster_generators_once(spm)
            fields = generator_fields(spm)
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["generator_count"], 2)
            for guid in ("legacy-visible", "legacy-hidden"):
                self.assertEqual(
                    {tag: fields[guid][tag] for tag in MARKER_VALUES},
                    MARKER_VALUES,
                )
                self.assertEqual(
                    {tag: fields[guid][tag] for tag in BACKGROUND},
                    BACKGROUND,
                )
            self.assertEqual(
                {tag: fields["atlas-generator"][tag] for tag in FOREGROUND},
                FOREGROUND,
            )

            receipt = json.loads(
                marker_receipt_path(spm).read_text(encoding="utf-8")
            )
            self.assertFalse(receipt["material_preflight_integration"])
            self.assertEqual(
                receipt["invalidation_policy"], "normal_one_time_refresh"
            )
            self.assertTrue(Path(receipt["before"]["backup"]).is_file())
            stable = {
                path: (path.stat().st_size, path.stat().st_mtime_ns)
                for path in (spm, marker_receipt_path(spm))
            }
            second = mark_legacy_cluster_generators_once(spm)
            self.assertEqual(second["status"], "already_applied")
            self.assertEqual(
                stable,
                {
                    path: (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in stable
                },
            )

    def test_direct_leaf_texture_is_not_a_legacy_cluster_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = write_spm(
                Path(temporary) / "SK_tree_direct_leaf.spm",
                include_cluster=False,
            )

            self.assertEqual(legacy_cluster_generator_candidates(spm), [])
            result = mark_legacy_cluster_generators_once(spm)
            self.assertEqual(result["status"], "not_applicable")
            self.assertFalse(marker_receipt_path(spm).exists())

    def test_receipt_guid_classification_survives_reconnect_and_color_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = write_spm(root / "SK_tree_lineage.spm")
            mark_legacy_cluster_generators_once(spm)

            model = ET.fromstring(gzip.decompress(spm.read_bytes()))
            source = next(
                item for item in model.findall("./Assets/Material_v8")
                if item.attrib.get("ID") == "2"
            )
            source.find("./Textures/TexFilename").text = (
                r"texture\new_atlas_color.tga"
            )
            visible = next(
                item for item in model.findall("./Generator")
                if item.findtext("GUID") == "legacy-visible"
            )
            extra = visible.find("Extra")
            for tag, value in FOREGROUND.items():
                extra.find(tag).text = value
            spm.write_bytes(gzip.compress(ET.tostring(model, encoding="utf-8")))
            before = (spm.read_bytes(), spm.stat().st_mtime_ns)

            state = inspect_legacy_cluster_state(spm)
            candidates = legacy_cluster_generator_candidates(spm)

            self.assertTrue(state["receipt_valid"])
            self.assertEqual(
                set(state["classified_generator_guids"]),
                {"legacy-visible", "legacy-hidden"},
            )
            self.assertEqual(state["marker_drift_guids"], ["legacy-visible"])
            self.assertEqual(
                {row["generator_guid"] for row in candidates},
                {"legacy-visible", "legacy-hidden"},
            )
            self.assertTrue(all(
                row["classification_evidence"] == "legacy_marker_receipt"
                for row in candidates
            ))
            self.assertEqual(
                (spm.read_bytes(), spm.stat().st_mtime_ns), before
            )

    def test_color_without_receipt_is_never_classified(self):
        with tempfile.TemporaryDirectory() as temporary:
            spm = write_spm(
                Path(temporary) / "SK_tree_problem_color.spm",
                include_cluster=False,
            )
            model = ET.fromstring(gzip.decompress(spm.read_bytes()))
            generator = next(
                item for item in model.findall("./Generator")
                if item.findtext("GUID") == "legacy-visible"
            )
            extra = generator.find("Extra")
            for tag, value in MARKER_VALUES.items():
                extra.find(tag).text = value
            spm.write_bytes(gzip.compress(ET.tostring(model, encoding="utf-8")))

            state = inspect_legacy_cluster_state(spm)

            self.assertFalse(state["receipt_valid"])
            self.assertEqual(state["classified_generator_guids"], [])
            self.assertEqual(state["ambiguous_marker_guids"], [])
            self.assertEqual(legacy_cluster_generator_candidates(spm), [])

    def test_prepare_sk_applies_marker_only_when_the_sk_is_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = write_spm(folder / "tree_new_01.spm")
            before_source = source.read_bytes()

            created = prepare_sk(folder)
            sk_spm = Path(created["sk_spm"])
            marker = created["legacy_cluster_marker"]

            self.assertEqual(created["created"], str(sk_spm))
            self.assertEqual(marker["status"], "applied")
            self.assertEqual(marker["generator_count"], 2)
            self.assertTrue(marker_receipt_path(sk_spm).is_file())
            self.assertEqual(source.read_bytes(), before_source)

            receipt_stat = marker_receipt_path(sk_spm).stat()
            repeated = prepare_sk(folder)
            self.assertEqual(
                repeated["legacy_cluster_marker"]["status"],
                "not_run_existing_sk",
            )
            self.assertEqual(
                marker_receipt_path(sk_spm).stat().st_mtime_ns,
                receipt_stat.st_mtime_ns,
            )

    def test_prepare_sk_dry_run_plans_marker_without_creating_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = write_spm(folder / "tree_new_01.spm")
            before_source = source.read_bytes()

            preview = prepare_sk(folder, dry_run=True)
            target = folder / "SK_tree_new_01.spm"

            self.assertEqual(preview["would_create"], str(target))
            self.assertEqual(
                preview["legacy_cluster_marker"]["status"],
                "planned_for_new_sk",
            )
            self.assertEqual(
                preview["legacy_cluster_marker"]["generator_count"], 2
            )
            self.assertFalse(target.exists())
            self.assertFalse(marker_receipt_path(target).exists())
            self.assertEqual(source.read_bytes(), before_source)

    def test_prepare_sk_removes_new_target_when_creation_marker_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = write_spm(folder / "tree_new_01.spm")
            before_source = source.read_bytes()
            target = folder / "SK_tree_new_01.spm"

            with mock.patch(
                "pcg_texture_audit.mark_legacy_cluster_generators_once",
                side_effect=ValueError("synthetic marker failure"),
            ):
                with self.assertRaisesRegex(
                    ValueError, "synthetic marker failure"
                ):
                    prepare_sk(folder)

            self.assertFalse(target.exists())
            self.assertFalse(marker_receipt_path(target).exists())
            self.assertEqual(source.read_bytes(), before_source)

    def test_bulk_migration_excludes_backups_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = write_spm(root / "tree" / "SK_tree_marker.spm")
            ordinary = write_spm(
                root / "weed" / "SK_weed_direct.spm",
                include_cluster=False,
            )
            backup = write_spm(
                root / "_spm_backups" / "SK_tree_backup.spm"
            )
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (live, ordinary, backup)
            }

            preview = migrate_existing_sk_markers(root, dry_run=True)

            self.assertEqual(preview["summary"]["live_sk_spms"], 2)
            self.assertEqual(preview["summary"]["applicable_spms"], 1)
            self.assertEqual(preview["summary"]["generator_count"], 2)
            self.assertEqual(preview["summary"]["errors"], 0)
            self.assertEqual(
                before,
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in before
                },
            )

            applied = migrate_existing_sk_markers(root, dry_run=False)
            self.assertEqual(applied["summary"]["statuses"]["applied"], 1)
            self.assertEqual(
                applied["summary"]["statuses"]["not_applicable"], 1
            )
            self.assertTrue(marker_receipt_path(live).is_file())
            self.assertEqual(ordinary.read_bytes(), before[ordinary][0])
            self.assertEqual(backup.read_bytes(), before[backup][0])

            stable = {
                path: (path.stat().st_size, path.stat().st_mtime_ns)
                for path in (live, marker_receipt_path(live))
            }
            repeated = migrate_existing_sk_markers(root, dry_run=False)
            self.assertEqual(
                repeated["summary"]["statuses"]["already_applied"], 1
            )
            self.assertEqual(repeated["summary"]["changed_spms"], 0)
            self.assertEqual(
                stable,
                {
                    path: (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in stable
                },
            )

    def test_bulk_migration_records_error_and_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = write_spm(root / "good" / "SK_tree_good.spm")
            bad = root / "bad" / "SK_tree_bad.spm"
            bad.parent.mkdir(parents=True)
            bad.write_bytes(b"not an SPM")

            real_candidates = legacy_cluster_generator_candidates

            def candidates_or_error(spm):
                if Path(spm) == bad:
                    raise ValueError("synthetic inspection failure")
                return real_candidates(spm)

            with mock.patch(
                "migrate_legacy_cluster_markers.legacy_cluster_generator_candidates",
                side_effect=candidates_or_error,
            ):
                result = migrate_existing_sk_markers(root, dry_run=False)

            self.assertEqual(result["summary"]["live_sk_spms"], 2)
            self.assertEqual(result["summary"]["errors"], 1)
            self.assertTrue(marker_receipt_path(good).is_file())
            self.assertEqual(bad.read_bytes(), b"not an SPM")


if __name__ == "__main__":
    unittest.main()
