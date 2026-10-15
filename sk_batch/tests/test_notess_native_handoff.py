import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
from sk_batch import normalized_assembly_bend as bend


class NoTessHandoffTests(unittest.TestCase):
    def test_dependency_admission_keeps_the_exact_active_request(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'unreal_ingest.py').read_text(encoding='utf-8'))
        outer=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_finalize_assembly_part_notess')
        admit=next(n for n in outer.body if isinstance(n,ast.FunctionDef) and n.name=='admit')
        metadata={'phase_id':'phase','ticket_id':'ticket','request_id':'original-request','target':'source.blend'}
        active=lambda value:value==metadata
        bridge=SimpleNamespace(can_execute_handoff=active,heartbeat_handoff=active)
        context={'phase_id':'phase','assembly_path':'/Game/Assembly','item':{'normalized_bend_changelist':2693,'workstation_handoff':metadata},'DESTINATION':bend.DESTINATION,'unreal':object(),'deepcopy':deepcopy,'require_active_dependency_phase':lambda *a:True,'checkout_material_scope':lambda u,paths,cl,**kw:{'verified':True,'materials':paths}}
        exec(compile(ast.Module(body=[admit],type_ignores=[]),'<native_notess_admission>','exec'),context)
        receipt=context['admit']({'materials':['/Game/Material']})
        with patch.dict(sys.modules,{'pipeline_bridge':bridge}),patch.object(bend,'require_active_dependency_phase',lambda *a:True):
            bend.check_dependency_admission(receipt)
        self.assertEqual(receipt['workstation_handoff'],metadata)
        self.assertIsNot(receipt['workstation_handoff'],metadata)


if __name__=='__main__':
    unittest.main()
