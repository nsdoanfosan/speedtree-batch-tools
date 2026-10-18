"""NONE interfaces retain zero WPO under an already authored shared wind root."""
from copy import deepcopy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))
import assembly_part_notess as nt
import assembly_wpo_contract as wpo
import normalized_assembly_bend as bend
from sk_batch.tests.test_assembly_part_notess import Harness, Material, MIC, Mesh, Slot


class WindInheritanceHarness(Harness):
    def __init__(self):
        super().__init__()
        self.switch_writes = []
        self.admissions = []
        self.dirty = set()
        self.u.EditorLoadingAndSavingUtils.get_dirty_content_packages = lambda: [
            SimpleNamespace(get_path_name=lambda path=path: path) for path in self.dirty]
        edit = self.u.MaterialEditingLibrary
        edit.get_material_default_static_switch_parameter_value = lambda asset, name: self.effective_switch(asset, name)
        edit.get_material_instance_static_switch_parameter_value = self.effective_switch
        edit.set_material_instance_static_switch_parameter_value = self.set_switch
        edit.is_material_instance_parameter_overridden = lambda asset, name: name in getattr(asset, 'switch_overrides', {})
        self.helpers['SWITCH'] = 'Enable Assembly Part Bend'

    @staticmethod
    def effective_switch(asset, name):
        while isinstance(asset, MIC):
            if name in getattr(asset, 'switch_overrides', {}):
                return asset.switch_overrides[name]
            asset = asset.values['parent']
        return asset.values.get('wpo_enabled', False) if name == wpo.WPO_SWITCH else asset.values.get('bend_declared', False)

    def wpo_enabled(self, asset):
        return self.effective_switch(asset, wpo.WPO_SWITCH)

    def set_switch(self, asset, name, value):
        self.switch_writes.append((asset.path, name, value))
        asset.switch_overrides = {**getattr(asset, 'switch_overrides', {}), name: value}
        return False  # UE's setter result is not success proof.

    def signature(self, asset):
        value = super().signature(asset)
        value['effective_static_switches'] = {wpo.WPO_SWITCH: self.wpo_enabled(asset)}
        root = asset
        while isinstance(root, MIC):
            root = root.values['parent']
        if root.values.get('bend_declared'):
            value['effective_static_switches'][self.helpers['SWITCH']] = self.effective_switch(asset, self.helpers['SWITCH'])
        return value

    def runtime(self, paths):
        value = json.loads(super().runtime(paths))
        for row, path in zip(value['materials'], paths):
            row['relevance_uses_wpo'] = self.wpo_enabled(self.load(path))
        return json.dumps(value)

    def admit(self, plan):
        self.admissions.append(deepcopy(plan))
        return {'verified': True, 'materials': plan['materials']}


class NoTessWpoInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.h = WindInheritanceHarness()
        self.patch = patch.object(bend, 'material_helpers', return_value=self.h.helpers)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.raw_root = self.h.add(Material('/Game/M/M_SharedWindRoot'))
        self.raw_old = self.h.add(MIC('/Game/M/MI_Authored', self.raw_root))
        old_part = self.h.add(Mesh('/Game/Trees/OldPart', [Slot('Bark', self.raw_old)]))
        self.old_assembly = self.h.add(Mesh('/Game/Trees/OldAssembly', [Slot('Bark', self.raw_old)], [(old_part.path, [0])]))
        self.apply(self.old_assembly)
        self.wind_root = self.h.load(nt.destination(self.raw_root, master=True))
        self.wind_root.values.update(wpo_enabled=True, bend_declared=True)
        self.wind_root.tags[wpo.WPO_GATE_TAG] = wpo.WPO_GATE_OWNER
        self.wind_root.graph['expressions'].append('AuthoredAssemblyBend')
        wpo.stamp_wind(self.h.u, self.wind_root, self.raw_root, helpers=self.h.helpers)
        self.raw_new = self.h.add(MIC('/Game/M/MI_NewNone', self.raw_root))
        new_part = self.h.add(Mesh('/Game/Trees/NewNonePart', [Slot('Bark', self.raw_new)]))
        self.new_assembly = self.h.add(Mesh('/Game/Trees/NewNoneAssembly', [Slot('Bark', self.raw_new)], [(new_part.path, [0])]))
        self.h.switch_writes.clear()
        self.h.admissions.clear()
        self.h.writes.clear()

    def apply(self, assembly):
        return nt.apply_default_part_notess(self.h.u, assembly, prepare_dependencies=self.h.admit,
                                            prepare_prototypes=self.h.admit_prototypes)

    def test_new_none_mic_preserves_false_under_shared_true_master(self):
        before = deepcopy(self.h.signature(self.raw_new))
        result = self.apply(self.new_assembly)
        target = self.h.load(nt.destination(self.raw_new))
        self.assertIs(target.values['parent'], self.wind_root)
        self.assertFalse(self.h.wpo_enabled(target))
        self.assertEqual(target.switch_overrides, {wpo.WPO_SWITCH: False})
        self.assertTrue(self.h.wpo_enabled(self.wind_root))
        self.assertEqual(self.h.signature(self.raw_new), before)
        self.assertNotIn(wpo.WIND_TAG, target.tags)
        self.assertEqual(result['created_packages'], [target.path])
        self.assertEqual(target.tags[nt.STATE_TAG], 'complete')

    def test_second_none_ingest_reuses_the_false_override_without_writes(self):
        self.apply(self.new_assembly)
        self.h.switch_writes.clear()
        self.h.admissions.clear()
        self.h.writes.clear()
        result = self.apply(self.new_assembly)
        self.assertFalse(self.h.wpo_enabled(self.h.load(nt.destination(self.raw_new))))
        self.assertEqual(self.h.switch_writes, [])
        self.assertEqual(self.h.admissions, [])
        self.assertEqual(result['created_packages'], [])

    def test_existing_authored_interface_shared_by_parts_stays_true(self):
        target = self.h.load(nt.destination(self.raw_old))
        self.h.set_switch(target, wpo.WPO_SWITCH, True)
        wpo.stamp_wind(self.h.u, target, self.raw_old, helpers=self.h.helpers)
        self.h.switch_writes.clear()
        other = self.h.add(Mesh('/Game/Trees/OtherRolePart', [Slot('Bark', self.raw_old)]))
        settings = self.old_assembly.values['nanite_settings']
        settings.text = settings.text.replace('Parts=(', 'Parts=((MeshObjectPath="'+other.get_path_name()+'",MaterialRemap=(0)),', 1)
        before = deepcopy(target.tags)
        self.apply(self.old_assembly)
        self.assertTrue(self.h.wpo_enabled(target))
        self.assertEqual(target.tags, before)
        self.assertEqual(self.h.switch_writes, [])

    def test_missing_override_api_stops_before_admission_or_duplicate(self):
        del self.h.u.MaterialEditingLibrary.set_material_instance_static_switch_parameter_value
        with self.assertRaisesRegex(RuntimeError, 'WPO preservation API'):
            self.apply(self.new_assembly)
        self.assertEqual(self.h.admissions, [])
        self.assertEqual(self.h.writes, [])
        self.assertIsNone(self.h.load(nt.destination(self.raw_new)))

    def failed_override(self):
        self.h.u.MaterialEditingLibrary.set_material_instance_static_switch_parameter_value = lambda *args: False
        with self.assertRaisesRegex(RuntimeError, 'could not preserve the original disabled WPO gate'):
            self.apply(self.new_assembly)
        target = self.h.load(nt.destination(self.raw_new))
        self.assertEqual(target.tags[nt.STATE_TAG], 'building')
        self.assertNotIn(wpo.WIND_TAG, target.tags)
        return target

    def test_verified_dirty_incomplete_clone_can_resume_after_setter_failure(self):
        target = self.failed_override()
        self.h.dirty.add(target.path)
        self.h.u.MaterialEditingLibrary.set_material_instance_static_switch_parameter_value = self.h.set_switch
        self.h.writes.clear()
        result = self.apply(self.new_assembly)
        self.assertIs(self.h.load(target.path), target)
        self.assertEqual(self.h.writes, [])
        self.assertEqual(result['created_packages'], [])
        self.assertFalse(self.h.wpo_enabled(target))
        self.assertEqual(target.tags[nt.STATE_TAG], 'complete')

    def test_incomplete_clone_with_changed_appearance_stops_before_retry_writes(self):
        target = self.failed_override()
        target.params['tint'] = [1, 0, 1]
        self.h.dirty.add(target.path)
        self.h.writes.clear()
        self.h.admissions.clear()
        self.h.u.MaterialEditingLibrary.set_material_instance_static_switch_parameter_value = self.h.set_switch
        with self.assertRaisesRegex(RuntimeError, 'Incomplete NoTess clone changed protected source data'):
            self.apply(self.new_assembly)
        self.assertEqual(self.h.writes, [])
        self.assertEqual(self.h.admissions, [])
        self.assertTrue(self.h.wpo_enabled(target))

    def test_unowned_false_only_collision_is_not_repaired_or_overwritten(self):
        target = self.h.add(MIC(nt.destination(self.raw_new), self.wind_root))
        with self.assertRaisesRegex(RuntimeError, 'Existing NoTess derivative differs'):
            self.apply(self.new_assembly)
        self.assertEqual(target.tags, {})
        self.assertEqual(self.h.switch_writes, [])
        self.assertEqual(self.h.admissions, [])


if __name__ == '__main__':
    unittest.main()
