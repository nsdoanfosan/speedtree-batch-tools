"""Single Assembly NoTess/WPO identity, including verified legacy migration.

This module never saves, compiles or checks out assets. Migration callers own
admission and persistence. Metadata points to the original source, never to an
intermediate CArc/AspectBend derivative.
"""
import hashlib
import json

WPO_SWITCH = "Enable Foliage WPO"
WPO_GATE_OWNER = "SpeedTreeBatch/FoliageWPOGate/v1"
WPO_GATE_TAG = "SKBatchFoliageWPOGate"
WIND_TAG = "SKBatchAssemblyWindContract"
WIND_OWNER = "SpeedTreeBatch/AssemblyNoTessWind/v1"


def require(value, message):
    if not value:
        raise RuntimeError(message)


def package(asset):
    return (asset if isinstance(asset, str) else asset.get_path_name()).split('.', 1)[0]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def original_source(unreal, asset, *, reject_unmigrated_wind=False, helpers=None):
    """Resolve exact owned legacy identities; never guess from asset names."""
    library = unreal.EditorAssetLibrary
    seen = set()
    authored_legacy = False
    tags = (("SKBatchAssemblyPartNoTessOwner", {"SpeedTreeBatch/AssemblyPartNoTess/v1", "SpeedTreeBatch/AssemblyPartNoTess/v2"}, "SKBatchAssemblyPartNoTessSource"),
            ("CodexNormalizedAssemblyBendOwner", {"NormalizedAssemblyPartBend/20261010/v2"}, "CodexNormalizedAssemblyBendSourceMaterial"),
            ("CodexAssemblyLeafCArcOwner", {"AssemblyLeafCArcRootBinding/20261013/v1"}, "CodexAssemblyLeafCArcSourceMaterial"))
    while True:
        path = package(asset)
        require(path.casefold() not in seen and len(seen) < 64, "Assembly material source cycle")
        seen.add(path.casefold())
        source_path = None
        for owner_tag, owners, source_tag in tags:
            owner = str(library.get_metadata_tag(asset, owner_tag) or '')
            source = str(library.get_metadata_tag(asset, source_tag) or '')
            if owner in owners and source:
                authored_legacy = authored_legacy or owner_tag == 'CodexAssemblyLeafCArcOwner'
                source_path = source
                break
        if source_path is None:
            generated_roots = ('/game/material/tree/assettree/assemblybend/',
                               '/game/material/tree/assettree/assemblynotess/')
            require(not path.casefold().startswith(generated_roots),
                    'Assembly derivative lacks exact original source ownership; migrate it before reimport: ' + path)
            if reject_unmigrated_wind and authored_legacy:
                from assembly_part_notess import destination
                canonical = library.load_asset(destination(asset, master=isinstance(asset, unreal.Material)))
                require(canonical is not None and verify_wind(unreal, canonical, helpers=helpers),
                        'Legacy authored wind requires canonical _notess migration before reimport: ' + path)
            return asset
        source = library.load_asset(source_path)
        require(source is not None and type(source) is type(asset), "Assembly material original source is missing or changed class: " + source_path)
        asset = source


def snapshot(unreal, helpers, asset):
    if isinstance(asset, unreal.Material):
        return {"graph": helpers['graph'](asset), "roots": helpers['roots'](asset),
                "wpo_bound": helpers['property_text'](asset, 'max_world_position_offset_displacement'),
                "tessellation": asset.get_editor_property('enable_tessellation')}
    return helpers['mi_signature'](asset)


def stamp_wind(unreal, target, source, *, helpers=None, mode='authored'):
    """Freeze an admitted canonical appearance, after its final parent/values.

    Root migration may use mode='authored' for existing CArc appearance. The
    ingest-generated mode is 'normalized'. Both are verified without replacing
    their graph or artist wind tuning on later geometry/UV reimports.
    """
    from assembly_part_notess import destination, OWNER, OWNER_TAG, SOURCE_TAG
    if helpers is None:
        from normalized_assembly_bend import material_helpers
        helpers = material_helpers(unreal)
    source = original_source(unreal, source)
    require(package(target).casefold() == destination(source, master=isinstance(source, unreal.Material)).casefold(),
            "Assembly wind target is not its original source's canonical _notess destination")
    require(mode in ('authored', 'normalized'), 'Unknown canonical Assembly wind mode')
    library = unreal.EditorAssetLibrary
    prior_source = str(library.get_metadata_tag(target, SOURCE_TAG) or '')
    if prior_source:
        previous = library.load_asset(prior_source)
        require(previous is not None and original_source(unreal, previous) == source,
                'Canonical Assembly wind destination already belongs to a different original source')
    _assert_wind_policy(unreal, target)
    proof = {"owner": WIND_OWNER, "source": package(source), "mode": mode,
             "source_signature": digest(snapshot(unreal, helpers, source)),
             "variant_signature": digest(snapshot(unreal, helpers, target))}
    library.set_metadata_tag(target, OWNER_TAG, OWNER)
    library.set_metadata_tag(target, SOURCE_TAG, package(source))
    library.set_metadata_tag(target, WIND_TAG, json.dumps(proof, sort_keys=True))
    return proof


def _assert_wind_policy(unreal, asset):
    root, seen = asset, set()
    while isinstance(root, unreal.MaterialInstanceConstant):
        require(package(root).casefold() not in seen and len(seen) < 64, 'Canonical Assembly wind parent cycle')
        seen.add(package(root).casefold())
        base = root.get_editor_property('base_property_overrides')
        require(not (base.get_editor_property('override_b_enable_tessellation')
                     and base.get_editor_property('enable_tessellation')),
                'Canonical Assembly wind interface enables tessellation')
        root = root.get_editor_property('parent')
    edit = unreal.MaterialEditingLibrary
    require(isinstance(root, unreal.Material) and root.get_editor_property('enable_tessellation') is False,
            'Canonical Assembly wind master must be NoTess')
    require(unreal.EditorAssetLibrary.get_metadata_tag(root, WPO_GATE_TAG) == WPO_GATE_OWNER
            and edit.get_material_default_static_switch_parameter_value(root, WPO_SWITCH) is True,
            'Canonical Assembly wind master must enable the complete WPO gate')
    if isinstance(asset, unreal.MaterialInstanceConstant):
        require(edit.get_material_instance_static_switch_parameter_value(asset, WPO_SWITCH) is True,
                'Canonical Assembly wind interface disables its WPO gate')


def require_original_wpo_disabled(unreal, roots, instances=()):
    """Fail before generation when a source has not adopted the global policy."""
    library, edit = unreal.EditorAssetLibrary, unreal.MaterialEditingLibrary
    for root in roots:
        require(library.get_metadata_tag(root, WPO_GATE_TAG) == WPO_GATE_OWNER
                and edit.get_material_default_static_switch_parameter_value(root, WPO_SWITCH) is False,
                'Original foliage master must disable the complete WPO gate before Assembly generation: ' + package(root))
    for instance in instances:
        require(edit.get_material_instance_static_switch_parameter_value(instance, WPO_SWITCH) is False,
                'Original foliage MIC enables WPO outside Assembly NoTess: ' + package(instance))


def verify_wind(unreal, asset, *, helpers=None):
    """Return a frozen appearance proof, or None for an ordinary NoTess clone."""
    raw = str(unreal.EditorAssetLibrary.get_metadata_tag(asset, WIND_TAG) or '')
    if not raw:
        return None
    if helpers is None:
        from normalized_assembly_bend import material_helpers
        helpers = material_helpers(unreal)
    from assembly_part_notess import destination, OWNER, OWNER_TAG, SOURCE_TAG
    library = unreal.EditorAssetLibrary
    proof = json.loads(raw)
    require(isinstance(proof, dict), 'Canonical Assembly wind metadata must be an object')
    source = library.load_asset(proof.get('source', ''))
    require(source is not None and type(source) is type(asset) and proof.get('owner') == WIND_OWNER
            and proof.get('mode') in ('authored', 'normalized') and original_source(unreal, source) == source
            and library.get_metadata_tag(asset, OWNER_TAG) == OWNER
            and package(source).casefold() == str(library.get_metadata_tag(asset, SOURCE_TAG) or '').casefold()
            and package(asset).casefold() == destination(source, master=isinstance(source, unreal.Material)).casefold(),
            'Invalid canonical Assembly wind ownership/source identity: ' + package(asset))
    require(proof.get('source_signature') == digest(snapshot(unreal, helpers, source)),
            'Canonical Assembly wind original source changed: ' + package(source))
    require(proof.get('variant_signature') == digest(snapshot(unreal, helpers, asset)),
            'Canonical Assembly wind appearance changed after its verified checkpoint: ' + package(asset))
    _assert_wind_policy(unreal, asset)
    return proof


def install_wpo_gate(unreal, material, *, enabled=False, helpers=None):
    """Wrap the complete existing WPO output with a false-to-zero static gate.

    Use enabled=False for originals and enabled=True only on canonical NoTess
    masters. This changes no non-WPO output, parameter, tessellation or bounds.
    Caller must admit the exact asset before invoking this mutation.
    """
    require(isinstance(material, unreal.Material), 'WPO gate requires an exact Material')
    if enabled:
        from assembly_part_notess import DESTINATION
        require(package(material).casefold().startswith(DESTINATION.casefold() + '/')
                and package(material).casefold().endswith('_notess'),
                'WPO may only be enabled on a canonical Assembly _notess master')
    if helpers is None:
        from normalized_assembly_bend import material_helpers
        helpers = material_helpers(unreal)
    edit, library = unreal.MaterialEditingLibrary, unreal.EditorAssetLibrary
    roots = helpers['roots'](material)
    original = next(row for row in roots['root_inputs'] if row['property_name'] == 'MP_WorldPositionOffset')
    nodes = list(edit.get_material_expressions(material))
    stamped = str(library.get_metadata_tag(material, WPO_GATE_TAG) or '')
    gates = [node for node in nodes if node.get_class().get_name() == 'MaterialExpressionStaticSwitchParameter'
             and str(node.get_editor_property('parameter_name')) == WPO_SWITCH]
    require(not gates or stamped == WPO_GATE_OWNER and all(str(node.get_editor_property('desc')) == WPO_GATE_OWNER for node in gates),
            'Unowned foliage WPO gate')
    outer = [node for node in gates if original['source_name'] == node.get_name()]
    require(len(outer) <= 1, 'Ambiguous foliage WPO gate root')
    material.modify()
    if outer:
        gate = outer[0]
        for node in gates:
            node.set_editor_property('default_value', bool(enabled))
    else:
        old = next((node for node in nodes if node.get_name() == original['source_name']), None)
        if original.get('mask'):
            graph = helpers['graph'](material)
            row = next(item for item in graph['expressions'] if item['name'] == original['source_name'])
            output = row['outputs'][int(original['output_index'])]
            require(all(bool(original[field]) == bool(output[field]) for field in
                        ('mask', 'mask_r', 'mask_g', 'mask_b', 'mask_a')),
                    'Original WPO uses a custom mask that cannot be preserved by a named output connection')
        gate = edit.create_material_expression(material, unreal.MaterialExpressionStaticSwitchParameter, 160, 2600)
        zero = edit.create_material_expression(material, unreal.MaterialExpressionConstant3Vector, -120, 2800)
        require(gate is not None and zero is not None, 'Could not create foliage WPO gate')
        gate.set_editor_property('parameter_name', WPO_SWITCH)
        gate.set_editor_property('default_value', bool(enabled))
        gate.set_editor_property('desc', WPO_GATE_OWNER)
        zero.set_editor_property('constant', unreal.LinearColor(0, 0, 0, 0))
        zero.set_editor_property('desc', WPO_GATE_OWNER)
        require(edit.connect_material_expressions(zero, '', gate, 'False'), 'Could not connect WPO gate false-to-zero')
        if original['connected']:
            require(old is not None, 'Original WPO expression is unavailable')
            names = list(edit.get_material_expression_output_names(old))
            output = str(names[int(original['output_index'])])
            require(edit.connect_material_expressions(old, '' if output == 'None' else output, gate, 'True'),
                    'Could not preserve existing WPO on gate true branch')
        else:
            require(edit.connect_material_expressions(zero, '', gate, 'True'), 'Could not preserve original zero WPO')
        require(edit.connect_material_property(gate, '', unreal.MaterialProperty.MP_WORLD_POSITION_OFFSET),
                'Could not connect foliage WPO gate root')
        for node in gates:
            node.set_editor_property('default_value', bool(enabled))
    after = helpers['roots'](material)
    original_other = [row for row in roots['root_inputs'] if row['property_name'] != 'MP_WorldPositionOffset']
    require(original_other == [row for row in after['root_inputs'] if row['property_name'] != 'MP_WorldPositionOffset'],
            'Foliage WPO gate changed a non-WPO root')
    require(next(row for row in after['root_inputs'] if row['property_name'] == 'MP_WorldPositionOffset')['source_name'] == gate.get_name(),
            'Foliage WPO gate root readback differs')
    library.set_metadata_tag(material, WPO_GATE_TAG, WPO_GATE_OWNER)
    require(edit.get_material_default_static_switch_parameter_value(material, WPO_SWITCH) is bool(enabled),
            'Foliage WPO gate default readback differs')
    return {"material": package(material), "switch": WPO_SWITCH, "default": bool(enabled)}
