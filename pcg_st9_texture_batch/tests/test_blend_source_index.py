import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "blend_source_index.py"
SPEC = importlib.util.spec_from_file_location("pcg_blend_source_index_test", MODULE_PATH)
INDEX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INDEX)


def authoritative_row(path, digest, *names):
    return {
        "schema_version": INDEX.SOURCE_INDEX_SCHEMA_VERSION,
        "status": "ok",
        "indexed_by_blender": True,
        "blend": str(Path(path).resolve()),
        "blend_sha256": digest,
        "images": [
            {"name": name, "filepath_raw": name, "filepath": str(Path(path).parent / name)}
            for name in names
        ],
    }


class BlendSourceIndexTests(unittest.TestCase):
    def test_raw_filename_bytes_never_authorize_a_source_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            blend = Path(temp_dir) / "raw.blend"
            blend.write_bytes(b"prefix Leaf_Color.png suffix Leaf_Opacity.png")
            session = INDEX.BlendSourceIndexSession({})
            self.assertEqual(session.lookup(blend), frozenset())
            self.assertEqual(len(session.pending_requests()), 1)

    def test_exact_current_sha_and_blender_row_are_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            blend = Path(temp_dir) / "indexed.blend"
            blend.write_bytes(b"current")
            digest = INDEX.file_sha256(blend)
            entries = {
                INDEX.path_key(blend): authoritative_row(
                    blend, digest, "Leaf_Color.png", "Leaf_Opacity.png"
                )
            }
            session = INDEX.BlendSourceIndexSession(entries)
            self.assertEqual(
                session.lookup(blend),
                frozenset({"leaf_color.png", "leaf_opacity.png"}),
            )
            self.assertEqual(session.pending_requests(), [])

    def test_same_size_restored_mtime_invalidates_persisted_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            blend = Path(temp_dir) / "changed.blend"
            blend.write_bytes(b"AAAA")
            stat = blend.stat()
            old_digest = INDEX.file_sha256(blend)
            entries = {
                INDEX.path_key(blend): authoritative_row(
                    blend, old_digest, "Leaf_Color.png"
                )
            }
            blend.write_bytes(b"BBBB")
            os.utime(blend, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            session = INDEX.BlendSourceIndexSession(entries)
            self.assertEqual(session.lookup(blend), frozenset())
            self.assertEqual(session.pending_requests()[0]["blend_sha256"], INDEX.file_sha256(blend))


    def test_final_pass_rehashes_after_index_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            blend = Path(temp_dir) / "between-passes.blend"
            blend.write_bytes(b"AAAA")
            original_stat = blend.stat()
            session = INDEX.BlendSourceIndexSession({})
            self.assertEqual(session.lookup(blend), frozenset())
            requests = session.pending_requests()
            session.install_report(
                {
                    "schema_version": 1,
                    "status": "ok",
                    "rows": [
                        authoritative_row(
                            blend, requests[0]["blend_sha256"], "Leaf_Color.png"
                        )
                    ],
                },
                requests,
            )
            blend.write_bytes(b"BBBB")
            os.utime(blend, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            session.begin_pass()
            self.assertEqual(session.lookup(blend), frozenset())
            self.assertEqual(
                session.pending_requests()[0]["blend_sha256"],
                INDEX.file_sha256(blend),
            )

    def test_partial_or_failed_index_report_is_rejected_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.blend"
            second = Path(temp_dir) / "second.blend"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            requests = [
                {"blend": str(first), "blend_sha256": INDEX.file_sha256(first)},
                {"blend": str(second), "blend_sha256": INDEX.file_sha256(second)},
            ]
            session = INDEX.BlendSourceIndexSession({})
            with self.assertRaises(INDEX.BlendSourceIndexError):
                session.install_report(
                    {"schema_version": 1, "status": "error", "error": "open failed", "rows": []},
                    requests,
                )
            with self.assertRaises(INDEX.BlendSourceIndexError):
                session.install_report(
                    {
                        "schema_version": 1,
                        "status": "ok",
                        "rows": [
                            authoritative_row(first, requests[0]["blend_sha256"], "A.png")
                        ],
                    },
                    requests,
                )

    def test_perf_fixture_indexes_only_pending_candidates_in_one_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unrelated = []
            for index in range(100):
                path = root / f"unrelated_{index:03d}.blend"
                path.write_bytes(f"unrelated-{index}".encode("ascii"))
                unrelated.append(path)
            pending = [root / "pending_a.blend", root / "pending_b.blend"]
            pending[0].write_bytes(b"pending-a")
            pending[1].write_bytes(b"pending-b")

            session = INDEX.BlendSourceIndexSession({})
            for path in pending:
                self.assertEqual(session.lookup(path), frozenset())
            calls = []

            def invoke(requests):
                calls.append(json.loads(json.dumps(requests)))
                return {
                    "schema_version": 1,
                    "status": "ok",
                    "rows": [
                        authoritative_row(
                            request["blend"],
                            request["blend_sha256"],
                            Path(request["blend"]).stem + "_Color.png",
                        )
                        for request in requests
                    ],
                }

            requests = session.pending_requests()
            report = invoke(requests)
            self.assertEqual(session.install_report(report, requests), 2)
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                {Path(row["blend"]).name for row in calls[0]},
                {"pending_a.blend", "pending_b.blend"},
            )
            self.assertTrue(
                all(path.name not in {Path(row["blend"]).name for row in calls[0]}
                    for path in unrelated)
            )


if __name__ == "__main__":
    unittest.main()
