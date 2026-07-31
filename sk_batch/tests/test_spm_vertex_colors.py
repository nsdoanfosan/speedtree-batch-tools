import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SK_BATCH_DIR))

import spm_audit


def color_generator(guid, name, generator_type, *, include_red=True):
    red = ""
    if include_red:
        red = """
      <Property><Name>Vertex Color:Red:Style</Name><Value>1</Value></Property>
      <SplineProperty>
        <Name>Vertex Color:Red:Value</Name><Value>0</Value><Variance>0</Variance>
        <Relative>true</Relative>
        <CompoundParentSpline Count="1"><Spline><ControlPoint><X>0</X><Y>1</Y><TangentX>1</TangentX><TangentY>0</TangentY></ControlPoint><ControlPoint><X>1</X><Y>1</Y><TangentX>1</TangentX><TangentY>0</TangentY></ControlPoint></Spline></CompoundParentSpline>
        <ProfileSpline><ControlPoint><X>0</X><Y>1</Y><TangentX>1</TangentX><TangentY>0</TangentY></ControlPoint><ControlPoint><X>0.5</X><Y>0.25</Y><TangentX>1</TangentX><TangentY>1</TangentY></ControlPoint><ControlPoint><X>1</X><Y>1</Y><TangentX>1</TangentX><TangentY>0</TangentY></ControlPoint></ProfileSpline>
      </SplineProperty>"""
    return f"""
  <Generator Type="{generator_type}">
    <Name>{name}</Name><GUID>{guid}</GUID><Hidden>false</Hidden>
    <Properties>
      {red}
      <Property><Name>Vertex Color:Green:Style</Name><Value>1</Value></Property>
      <SplineProperty>
        <Name>Vertex Color:Green:Value</Name><Value>-0.5</Value><Variance>0</Variance>
        <Relative>true</Relative>
        <CompoundParentSpline Count="1"><Spline><ControlPoint><X>0</X><Y>1</Y></ControlPoint><ControlPoint><X>1</X><Y>1</Y></ControlPoint></Spline></CompoundParentSpline>
        <ProfileSpline><ControlPoint><X>0</X><Y>0</Y><TangentX>0</TangentX><TangentY>0</TangentY></ControlPoint><ControlPoint><X>1</X><Y>1</Y><TangentX>0</TangentX><TangentY>0</TangentY></ControlPoint></ProfileSpline>
      </SplineProperty>
    </Properties>
  </Generator>"""


def node(guid, node_type, generator_guid, parent_guid):
    return (
        f'<Node Type="{node_type}"><GUID>{guid}</GUID>'
        f"<GeneratorGUID>{generator_guid}</GeneratorGUID>"
        f"<ParentGUID>{parent_guid}</ParentGUID></Node>"
    )


def tree_xml(*, target_has_red=True):
    return """<SpeedTree><Generators>""" + "".join(
        [
            color_generator("trunk", "Trunk", "Branch"),
            color_generator(
                "leaf-parent", "Artist Named Stem", "Branch", include_red=target_has_red
            ),
            color_generator("other-branch", "Other", "Branch"),
            color_generator("leaf", "Any Artist Name", "Batched Leaf"),
            color_generator("frond", "Leaf-looking name", "Frond"),
        ]
    ) + "</Generators><Nodes>" + "".join(
        [
            node("trunk-node", "Branch", "trunk", ""),
            node("stem-node-a", "Branch", "leaf-parent", "trunk-node"),
            node("stem-node-b", "Branch", "leaf-parent", "trunk-node"),
            node("leaf-node-a", "Leaf Mesh", "leaf", "stem-node-a"),
            node("leaf-node-b", "Batched Leaf", "leaf", "stem-node-b"),
            node("other-node", "Branch", "other-branch", "trunk-node"),
            node("frond-node", "Frond", "frond", "other-node"),
        ]
    ) + "</Nodes></SpeedTree>"


def tree_xml_with_two_leaf_parents():
    source = tree_xml()
    source = source.replace(
        "</Generators>",
        color_generator("leaf-parent-2", "Second Stem", "Branch")
        + "</Generators>",
    )
    source = source.replace(
        "</Nodes>",
        node("stem-node-c", "Branch", "leaf-parent-2", "trunk-node")
        + node("leaf-node-c", "Leaf Mesh", "leaf", "stem-node-c")
        + "</Nodes>",
    )
    return source


def generator_by_guid(text, guid):
    root = ET.fromstring(text)
    return next(
        generator
        for generator in root.findall(".//Generator")
        if generator.findtext("GUID") == guid
    )


def prop(generator, name):
    return next(
        item
        for item in list(generator.find("Properties"))
        if item.findtext("Name") == name
    )


def mutate_target_profile(text, guid, point_index, tag, *, value=None, remove=False):
    root = ET.fromstring(text)
    target = next(
        generator
        for generator in root.findall(".//Generator")
        if generator.findtext("GUID") == guid
    )
    red = prop(target, "Vertex Color:Red:Value")
    point = red.findall("./ProfileSpline/ControlPoint")[point_index]
    element = point.find(tag)
    if remove:
        point.remove(element)
    else:
        element.text = value
    return ET.tostring(root, encoding="unicode")


class SpeedTreeVertexColorContractTests(unittest.TestCase):
    def test_direct_leaf_parent_gets_set_red_profile_and_green_is_preserved(self):
        source = tree_xml()
        green_before = spm_audit._vertex_color_channel_signature(source, "Green")

        patched, report = spm_audit.apply_leaf_parent_red_gradient(source)

        self.assertEqual(report["errors"], [])
        self.assertEqual(report["target_count"], 1)
        self.assertEqual(report["leaf_node_count"], 2)
        self.assertEqual(report["changed_generator_count"], 1)
        self.assertTrue(report["green_unchanged"])
        self.assertEqual(
            spm_audit._vertex_color_channel_signature(patched, "Green"), green_before
        )

        target = generator_by_guid(patched, "leaf-parent")
        self.assertEqual(
            prop(target, "Vertex Color:Red:Style").findtext("Value"), "0"
        )
        red = prop(target, "Vertex Color:Red:Value")
        self.assertEqual(red.findtext("Value"), "1")
        points = [
            (
                point.findtext("X"),
                point.findtext("Y"),
                point.findtext("TangentX"),
                point.findtext("TangentY"),
            )
            for point in red.findall("./ProfileSpline/ControlPoint")
        ]
        self.assertEqual(
            points,
            [("0", "0", "0", "0"), ("0.5", "0.5", "0", "0"), ("1", "1", "0", "0")],
        )

        # A Frond with a leaf-looking artist name is not a leaf generator.
        other = generator_by_guid(patched, "other-branch")
        self.assertEqual(
            prop(other, "Vertex Color:Red:Style").findtext("Value"), "1"
        )
        self.assertEqual(
            prop(other, "Vertex Color:Red:Value").findtext("Value"), "0"
        )

    def test_patch_is_idempotent(self):
        first, first_report = spm_audit.apply_leaf_parent_red_gradient(tree_xml())
        second, second_report = spm_audit.apply_leaf_parent_red_gradient(first)

        self.assertEqual(first_report["changed_generator_count"], 1)
        self.assertEqual(second_report["changed_generator_count"], 0)
        self.assertEqual(second, first)

    def test_high_precision_x_is_copied_to_y_without_rounding(self):
        source = mutate_target_profile(
            tree_xml(), "leaf-parent", 1, "X", value="0.333333"
        )

        patched, report = spm_audit.apply_leaf_parent_red_gradient(source)

        self.assertEqual(report["errors"], [])
        self.assertEqual(report["changed_generator_count"], 1)
        target = generator_by_guid(patched, "leaf-parent")
        red = prop(target, "Vertex Color:Red:Value")
        middle = red.findall("./ProfileSpline/ControlPoint")[1]
        self.assertEqual(middle.findtext("X"), "0.333333")
        self.assertEqual(middle.findtext("Y"), "0.333333")

    def test_missing_red_property_aborts_atomically(self):
        source = tree_xml(target_has_red=False)

        patched, report = spm_audit.apply_leaf_parent_red_gradient(source)

        self.assertEqual(patched, source)
        self.assertEqual(report["changed_generator_count"], 0)
        self.assertTrue(report["errors"])
        self.assertTrue(report["green_unchanged"])

    def test_malformed_profile_values_abort_atomically(self):
        cases = (
            ("missing_y", {"tag": "Y", "remove": True}),
            ("non_finite_tangent", {"tag": "TangentX", "value": "nan"}),
            ("out_of_range_x", {"tag": "X", "value": "1.25"}),
        )
        for label, mutation in cases:
            with self.subTest(label=label):
                source = mutate_target_profile(
                    tree_xml(), "leaf-parent", 1, **mutation
                )

                patched, report = spm_audit.apply_leaf_parent_red_gradient(source)

                self.assertEqual(patched, source)
                self.assertEqual(report["changed_generator_count"], 0)
                self.assertTrue(report["errors"])
                self.assertTrue(report["green_unchanged"])

    def test_one_malformed_target_aborts_all_targets(self):
        source = mutate_target_profile(
            tree_xml_with_two_leaf_parents(),
            "leaf-parent-2",
            1,
            "Y",
            remove=True,
        )

        patched, report = spm_audit.apply_leaf_parent_red_gradient(source)

        self.assertEqual(report["target_count"], 2)
        self.assertEqual(patched, source)
        self.assertEqual(report["changed_generator_count"], 0)
        self.assertTrue(report["errors"])
        first_target = generator_by_guid(patched, "leaf-parent")
        self.assertEqual(
            prop(first_target, "Vertex Color:Red:Style").findtext("Value"), "1"
        )
        self.assertEqual(
            prop(first_target, "Vertex Color:Red:Value").findtext("Value"), "0"
        )


if __name__ == "__main__":
    unittest.main()
