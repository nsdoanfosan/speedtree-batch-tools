"""Issue #113: SK Batch Cluster scan must be exact and bounded."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[2]
SK_BATCH_DIR = REPO_DIR / "sk_batch"
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(SK_BATCH_DIR))

import sk_common  # noqa: E402
from pcg_st9_texture_batch import cluster_connection_index as connection_index  # noqa: E402
from pcg_st9_texture_batch import pcg_texture_audit as audit  # noqa: E402
from pcg_st9_texture_batch.pcg_startup_cache import (  # noqa: E402
    ContentAddressedJsonCache,
)


PRODUCTION_SHAPED_BUDGET_SECONDS = {
    "cold": 8.0,
    "warm": 4.0,
}


def write_connection_spm(path, materials=()):
    material_xml = []
    for material_id, name, refs in materials:
        texture_xml = "".join(
            f"<TexFilename>{ref}</TexFilename>" for ref in refs
        )
        material_xml.append(
            f'<Material_v8 ID="{material_id}" Name="{name}">'
            f"{texture_xml}</Material_v8>"
        )
    payload = (
        "<SpeedTree><Materials>"
        + "".join(material_xml)
        + "</Materials><Generators></Generators><Nodes></Nodes></SpeedTree>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def create_owner(root, owner_index, sk_count, cluster_count):
    owner = root / f"Tree_fixture_{owner_index:02d}"
    clusters = []
    for cluster_index in range(cluster_count):
        cluster = (
            owner / "Cluster"
            / f"cluster_fixture_{owner_index:02d}_{cluster_index:02d}.spm"
        )
        write_connection_spm(cluster)
        clusters.append(cluster)
    for sk_index in range(sk_count):
        cluster = clusters[sk_index % len(clusters)]
        write_connection_spm(
            owner / f"SK_Tree_fixture_{owner_index:02d}_{sk_index:02d}.spm",
            [(
                f"material-{owner_index}-{sk_index}",
                f"M_fixture_{owner_index:02d}_{sk_index:02d}",
                [f"Cluster/{cluster.stem}.tga"],
            )],
        )
    return owner, clusters


class Issue113ClusterScanTests(unittest.TestCase):
    def isolated_caches(self, cache_dir):
        return (
            mock.patch.object(
                connection_index,
                "CLUSTER_CONNECTION_CACHE_PATH",
                cache_dir / "connections.json",
            ),
            mock.patch.object(
                audit,
                "SPM_ANALYSIS_CACHE_PATH",
                cache_dir / "spm.json",
            ),
            mock.patch.object(audit, "_PERSISTENT_SPM_ANALYSIS", None),
            mock.patch.object(audit, "_PERSISTENT_SPM_ANALYSIS_DIRTY", False),
            mock.patch.object(audit, "_SPM_ANALYSIS_CACHE", {}),
        )

    def test_100_sk_54_cluster_cold_and_warm_scan_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cache = Path(temporary) / "cache"
            for owner_index in range(26):
                create_owner(
                    root,
                    owner_index,
                    sk_count=4 if owner_index < 22 else 3,
                    cluster_count=3 if owner_index < 2 else 2,
                )
            patches = self.isolated_caches(cache)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                cold_metrics = {}
                cold_started = time.perf_counter()
                sk_rows = sk_common.scan_sk_spms(root)
                cold = sk_common.scan_cluster_spm_sources(
                    root, metrics=cold_metrics
                )
                cold_elapsed = time.perf_counter() - cold_started

                warm_metrics = {}
                warm_started = time.perf_counter()
                warm = sk_common.scan_cluster_spm_sources(
                    root, metrics=warm_metrics
                )
                warm_elapsed = time.perf_counter() - warm_started

            self.assertEqual(len(sk_rows), 100)
            self.assertEqual(len(cold), 54)
            self.assertEqual(warm, cold)
            self.assertEqual(cold_metrics["owner_count"], 26)
            self.assertEqual(cold_metrics["inventory_file_count"], 100)
            self.assertEqual(cold_metrics["projection_cache_misses"], 100)
            self.assertEqual(cold_metrics["shared_analysis_hits"], 0)
            self.assertEqual(warm_metrics["projection_cache_hits"], 100)
            self.assertEqual(warm_metrics["projection_cache_misses"], 0)
            self.assertEqual(
                warm_metrics["content_identity_algorithm"],
                "sha256-full-v1",
            )
            self.assertLess(
                cold_elapsed, PRODUCTION_SHAPED_BUDGET_SECONDS["cold"]
            )
            self.assertLess(
                warm_elapsed, PRODUCTION_SHAPED_BUDGET_SECONDS["warm"]
            )

    def test_same_size_restored_mtime_content_change_invalidates_exact_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cache = Path(temporary) / "cache"
            owner = root / "Tree_exact"
            first_cluster = owner / "Cluster" / "cluster_first_01.spm"
            second_cluster = owner / "Cluster" / "cluster_other_01.spm"
            write_connection_spm(first_cluster)
            write_connection_spm(second_cluster)
            target = write_connection_spm(
                owner / "SK_Tree_exact_01.spm",
                [("material-1", "M_exact", ["Cluster/cluster_first_01.tga"])],
            )
            patches = self.isolated_caches(cache)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                first = sk_common.scan_cluster_spm_sources(root)
                before = target.stat()
                original_size = before.st_size
                write_connection_spm(
                    target,
                    [(
                        "material-1",
                        "M_exact",
                        ["Cluster/cluster_other_01.tga"],
                    )],
                )
                self.assertEqual(target.stat().st_size, original_size)
                os.utime(
                    target,
                    ns=(before.st_atime_ns, before.st_mtime_ns),
                )
                changed_metrics = {}
                changed = sk_common.scan_cluster_spm_sources(
                    root, metrics=changed_metrics
                )

            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["legacy_output_spm"], first_cluster)
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0]["legacy_output_spm"], second_cluster)
            self.assertEqual(changed_metrics["projection_cache_misses"], 1)

    def test_texture_existence_is_revalidated_on_projection_cache_hit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cache = Path(temporary) / "cache"
            owner = root / "Tree_texture"
            cluster = owner / "Cluster" / "cluster_texture_01.spm"
            write_connection_spm(cluster)
            write_connection_spm(
                owner / "SK_Tree_texture_01.spm",
                [(
                    "material-1",
                    "M_texture",
                    ["Cluster/cluster_texture_01.tga"],
                )],
            )
            texture = cluster.with_suffix(".tga")
            patches = self.isolated_caches(cache)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                missing = sk_common.scan_cluster_spm_sources(root)
                texture.write_bytes(b"current")
                current_metrics = {}
                current = sk_common.scan_cluster_spm_sources(
                    root, metrics=current_metrics
                )

            self.assertEqual(missing[0]["missing_output_textures"], (str(texture),))
            self.assertEqual(current[0]["missing_output_textures"], ())
            self.assertEqual(current_metrics["projection_cache_hits"], 1)

    def test_current_full_sha_can_seed_from_shared_pcg_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tree"
            cache = Path(temporary) / "cache"
            owner = root / "Tree_shared"
            cluster = owner / "Cluster" / "cluster_shared_01.spm"
            write_connection_spm(cluster)
            target = write_connection_spm(
                owner / "SK_Tree_shared_01.spm",
                [(
                    "material-1",
                    "M_shared",
                    ["Cluster/cluster_shared_01.tga"],
                )],
            )
            patches = self.isolated_caches(cache)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                audit._spm_analysis(target)
                audit.save_spm_analysis_cache()
                metrics = {}
                rows = sk_common.scan_cluster_spm_sources(
                    root, metrics=metrics
                )

            self.assertEqual(len(rows), 1)
            self.assertEqual(metrics["projection_cache_hits"], 0)
            self.assertEqual(metrics["shared_analysis_hits"], 1)
            self.assertEqual(metrics["shared_analysis_misses"], 0)
            self.assertEqual(metrics["projection_parse_workers"], 0)

    def test_combined_projection_matches_authoritative_pcg_semantics(self):
        fixture = (
            REPO_DIR / "pcg_st9_texture_batch" / "tests" / "fixtures"
            / "issue_41" / "schema5" / "after.spm"
        )
        text = fixture.read_text(encoding="utf-8")
        counts, total_nodes = audit._export_node_counts_from_text(text)
        expected_bindings = [
            {
                "generator_guid": row.get("generator_guid"),
                "material_id": row.get("material_id"),
                "export_participates": bool(row.get("export_participates")),
            }
            for row in audit._leaf_generator_bindings_from_text(
                text,
                export_node_counts=counts,
                total_nodes=total_nodes,
            )
        ]
        actual = connection_index._target_connection_semantics(text)

        self.assertEqual(
            actual["referenced_material_ids"],
            sorted(audit._referenced_material_ids_from_text(text)),
        )
        self.assertEqual(
            actual["visible_material_ids"],
            sorted(audit._visible_material_ids_from_text(
                text,
                export_node_counts=counts,
                total_nodes=total_nodes,
            )),
        )
        self.assertEqual(actual["leaf_generator_bindings"], expected_bindings)

    def test_multi_get_rejects_only_the_tampered_cache_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.json"
            cache = ContentAddressedJsonCache(
                path, "issue-113-test", max_entries=4
            )
            cache.put_many((
                ("first", "identity-a", {"value": 1}),
                ("second", "identity-b", {"value": 2}),
            ))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["entries"]["second"]["value"]["value"] = 99
            path.write_text(json.dumps(payload), encoding="utf-8")

            rows = cache.get_many((
                ("first", "identity-a"),
                ("second", "identity-b"),
            ))

            self.assertEqual(rows, {"first": {"value": 1}})


if __name__ == "__main__":
    unittest.main()
