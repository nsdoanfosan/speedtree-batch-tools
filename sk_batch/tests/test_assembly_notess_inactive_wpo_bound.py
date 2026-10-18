"""UE's ignored bound storage is equivalent only under two explicit gates."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))
import assembly_part_notess as nt
from assembly_wpo_contract import WPO_SWITCH


class Material:
    pass


class InactiveWpoBoundTests(unittest.TestCase):
    def signature(self, bound, *, gate=False, override='False', other='TwoSided=True', assume=False):
        fields = ([f'bOverride_MaxWorldPositionOffsetDisplacement={override}'] if override is not None else [])
        fields += [f'MaxWorldPositionOffsetDisplacement={bound}', other]
        raw = {'parent': '/Game/M/M_Raw', 'layers': {},
               'base_property_overrides': '(' + ','.join(fields) + ')',
               'effective_static_switches': {} if gate is None else {WPO_SWITCH: gate},
               'surface': {'texture': '/Game/T/Bark'}}
        before = deepcopy(raw)
        value = nt._signature(SimpleNamespace(Material=Material), {'mi_signature': lambda asset: raw}, object(), {},
                              assume_wpo_disabled=assume)
        self.assertEqual(raw, before, 'Signature normalization must never edit asset data')
        return value

    def test_disabled_wpo_and_disabled_bound_override_ignore_ue_parent_cache(self):
        self.assertEqual(self.signature('0.000000'), self.signature('300.000000'))
        self.assertIn('MaxWorldPositionOffsetDisplacement=0,', self.signature('300.0')['base_property_overrides'])

    def test_enabled_wpo_preserves_inherited_bound_storage(self):
        self.assertNotEqual(self.signature('0.0', gate=True), self.signature('300.0', gate=True))

    def test_explicit_bound_override_is_always_preserved(self):
        self.assertNotEqual(self.signature('0.0', override='True'), self.signature('300.0', override='True'))
        self.assertNotEqual(self.signature('300.0', override='False'), self.signature('300.0', override='True'))

    def test_missing_gate_or_missing_override_cannot_be_assumed_inactive(self):
        self.assertNotEqual(self.signature('0.0', gate=None), self.signature('300.0', gate=None))
        self.assertNotEqual(self.signature('0.0', override=None), self.signature('300.0', override=None))

    def test_other_base_fields_remain_exact(self):
        self.assertNotEqual(self.signature('0.0', other='TwoSided=True'), self.signature('300.0', other='TwoSided=False'))

    def test_numeric_false_flag_and_scientific_bound_export(self):
        self.assertEqual(self.signature('0', override='0'), self.signature('+3.00e+2', override='0'))

    def test_retry_assumption_normalizes_only_after_full_gate_is_forced_false(self):
        self.assertEqual(self.signature('0.0', gate=True, assume=True), self.signature('300.0', gate=True, assume=True))


if __name__ == '__main__':
    unittest.main()
