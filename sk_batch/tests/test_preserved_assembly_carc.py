"""Protect authored C-arc appearance during a source UV-only reimport."""
import copy
import hashlib
import json
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sk_batch import cluster_assembly_builder as builder
from sk_batch import preserved_assembly_carc as carc


def proof_for(source):
    path = carc.PREFIX + source.rsplit('/', 1)[-1] + '_' + hashlib.sha1(source.encode()).hexdigest()[:12] + '_CArc'
    return path, dict(owner=carc.OWNER, source=source, source_signature='a' * 64, variant_signature='b' * 64)


class PreservedCArcTests(unittest.TestCase):
    def test_exact_owner_source_destination_and_signatures_are_required(self):
        path, proof = proof_for('/Game/M/MI_Bark')
        self.assertEqual(carc.validate_alias(path, proof), proof['source'])
        for key, value in [('owner', 'other'), ('source', '/Game/M/MI_Other'),
                           ('source_signature', 'bad'), ('variant_signature', '')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                carc.validate_alias(path, {**proof, key: value})
        with self.assertRaises(RuntimeError):
            carc.validate_alias(path + '_Unowned', proof)

    def test_all_active_parts_keep_authored_carc_and_mixed_policy_rejects(self):
        source = '/Game/M/MI_Bark'
        path, proof = proof_for(source)
        incoming = dict(slot_name='Bark', imported_slot_name='Bark', material=source)
        previous = {**incoming, 'material': path}
        old_parts = [dict(mesh='/Game/Tree/Part', remap=[0])]
        parts = [dict(mesh='/Game/Tree/Part', bend_role=1, slots=[incoming])]
        args = dict(old_slots=[previous], old_parts=old_parts, base_slots=[incoming], parts=parts,
                    material_chains={}, owned_material_aliases={path: proof})
        plan = builder.plan_assembly_material_preservation(**args)
        self.assertEqual(plan['source_materials'], [path])
        self.assertEqual(plan['preserved_wind_policy']['prototypes'], ['/Game/Tree/Part'])
        self.assertEqual(plan['parts'][0]['desired_remap'], [0])
        builder.validate_assembly_material_preservation(plan)
        mixed = copy.deepcopy(args)
        mixed['parts'].append(dict(mesh='/Game/Tree/NewPart', bend_role=1, slots=[incoming]))
        with self.assertRaisesRegex(RuntimeError, 'Cannot mix'):
            builder.plan_assembly_material_preservation(**mixed)

    def test_source_signature_change_rejects_owned_variant(self):
        class MIC:
            def __init__(self, path): self.path = path
            def get_path_name(self): return self.path + '.' + self.path.rsplit('/', 1)[-1]
        source = MIC('/Game/M/MI_Bark')
        path, proof = proof_for(source.path)
        variant = MIC(path)
        tags = {carc.OWNER_TAG: carc.OWNER, carc.SOURCE_TAG: source.path,
                carc.SIGNATURE_TAG: hashlib.sha256(b'original').hexdigest()}
        unreal = SimpleNamespace(MaterialInstanceConstant=MIC, EditorAssetLibrary=SimpleNamespace(
            get_metadata_tag=lambda obj, key: tags.get(key, ''), load_asset=lambda p: source))
        signatures = {source.path: 'original', path: 'variant'}
        helpers = dict(mi_signature=lambda obj: signatures[obj.path],
                       digest=lambda value: hashlib.sha256(value.encode()).hexdigest())
        actual = carc.inspect_alias(unreal, variant, helpers)
        self.assertEqual(actual['source_signature'], tags[carc.SIGNATURE_TAG])
        signatures[source.path] = 'artist changed source'
        with self.assertRaisesRegex(RuntimeError, 'source changed'):
            carc.inspect_alias(unreal, variant, helpers)

    def test_native_stream_proof_requires_clean_saved_package_and_tracks_all_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'Trees' / 'Part.uasset'
            file.parent.mkdir()
            file.write_bytes(b'geometry-material-skeleton-native-bytes')
            path = '/Game/Trees/Part'
            audit = dict(ok=True, vertex_count=10, position_sha1='a'*40, tangent_sha1='b'*40,
                uv_hashes=[dict(channel=0, sha1='c'*40)], color_sha1='d'*40, index_sha1='e'*40,
                skin_sha1='f'*40, ref_skeleton_sha1='0'*40, materials=['original'])
            dirty = []
            unreal = SimpleNamespace(Paths=SimpleNamespace(project_content_dir=lambda: str(root),
                convert_relative_path_to_full=lambda p: p), EditorLoadingAndSavingUtils=SimpleNamespace(
                get_dirty_content_packages=lambda: dirty), CodexMaterialToolsLibrary=SimpleNamespace(
                audit_skeletal_mesh_lod0_streams=lambda p: json.dumps(audit)))
            before = carc.prototype_streams(unreal, [path])
            file.write_bytes(file.read_bytes() + b'changed')
            self.assertNotEqual(before, carc.prototype_streams(unreal, [path]))
            dirty.append(SimpleNamespace(get_path_name=lambda: path))
            with self.assertRaisesRegex(RuntimeError, 'clean saved reference'):
                carc.prototype_streams(unreal, [path])

    def test_final_receipt_accepts_only_unchanged_prototype_bytes(self):
        from sk_batch.prepared_rpc_resume import _validate_result_saves
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aliases = dict([proof_for('/Game/M/MI_Bark')])
            contract = dict(full_skeletal_mesh='/Game/Tree/Full', base_skeletal_mesh='/Game/Tree/Base',
                            assembly='/Game/Tree/Assembly', parts={'branch': '/Game/Tree/Part'})
            records = []
            for path in (contract[k] for k in ('full_skeletal_mesh', 'base_skeletal_mesh', 'assembly')):
                file = root / (path.rsplit('/', 1)[-1] + '.uasset')
                file.write_bytes(b'saved native root data')
                records.append(dict(sequence=len(records), package=path, asset=path, owner='native', role='mesh',
                    saved=True, dirty_after_save=False, package_file=str(file), size=file.stat().st_size,
                    mtime_ns=file.stat().st_mtime_ns))
            prototype = root / 'Part.uasset'
            prototype.write_bytes(b'original reference')
            streams = {'/Game/Tree/Part': dict(package_file=str(prototype), live_clean=True,
                package_size=prototype.stat().st_size, package_mtime_ns=prototype.stat().st_mtime_ns,
                package_md5=hashlib.md5(prototype.read_bytes()).hexdigest())}
            item = dict(mesh_path=contract['full_skeletal_mesh'], cluster_assembly=dict(
                ingest_plan=dict(status='ready', asset_contract=contract)))
            result = dict(durable_saves=dict(schema_version=1, records=records), cluster_assembly=dict(
                status='ok', runtime=dict(status='passed', begin=dict(success=True, status='pending', probe_token='native'),
                    finish=dict(success=True, status='passed')), build=dict(material_normalization=dict(
                    preserved_assembly_wind_policy=dict(owner=carc.OWNER, verified=True, aliases=aliases,
                        prototype_streams_before=streams, prototype_streams_after=copy.deepcopy(streams))))))
            checked = _validate_result_saves(item, result)
            self.assertEqual(checked['unchanged_reference_packages'], ['/Game/Tree/Part'])
            prototype.write_bytes(b'modified reference')
            with self.assertRaisesRegex(RuntimeError, 'prototype bytes changed'):
                _validate_result_saves(item, result)


if __name__ == '__main__':
    unittest.main()
