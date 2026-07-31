"""Full-tree core-v4 acceptance and fail-closed regressions for #42."""

import json
import re
import sys
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
for candidate in (REPO_DIR, REPO_DIR / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from stale_node_table_recovery import (  # noqa: E402
    AUTHORING_GRAPH_CORE_PROJECTION_VERSION,
    _authoring_graph_core_projection,
    _legacy_authoring_graph_core_v3_projection,
)


FIXTURES = Path(__file__).parent / "fixtures"
BEFORE = FIXTURES / "issue_42_no_edit_before.xml"
AFTER = FIXTURES / "issue_42_no_edit_after.xml"
EVIDENCE = FIXTURES / "issue_42_real_no_edit_evidence.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:(?<![a-z])[a-z]:[\\/]|users[\\/][^\\/]+)"
)
RAW_GUID_RE = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def project(text):
    return _authoring_graph_core_projection(text)["fingerprint"]


def default_specular_map(seed):
    def spline(length):
        return (
            '<Spline DrawMode="false">'
            f'<ControlPoint><X>0</X><Y>0</Y><TangentX>1</TangentX><TangentY>0</TangentY><Length>{length}</Length></ControlPoint>'
            f'<ControlPoint><X>1</X><Y>1</Y><TangentX>1</TangentX><TangentY>0</TangentY><Length>{length}</Length></ControlPoint>'
            '</Spline>'
        )

    scalars = {
        "TexFilename": "",
        "TexBrightness": "0",
        "TexContrast": "0",
        "TexSaturation": "0",
        "TexRed": "0",
        "TexGreen": "0",
        "TexBlue": "0",
        "TexMin": "0",
        "TexMax": "1",
        "TexEnabled": "true",
        "TexInvert": "false",
        "TexInvertRed": "false",
        "TexInvertGreen": "false",
        "TexInvertBlue": "false",
        "Normalize": "false",
        "TexSizeX": "0",
        "TexSizeY": "0",
        "ColorX": "0.75",
        "ColorY": "0.75",
        "ColorZ": "0.75",
        "TexSource": "0",
        "TexToLinear": "true",
    }
    scalar_xml = "".join(
        f"<{name}>{value}</{name}>" for name, value in scalars.items()
    )
    generate = (
        '<Generate Type="0">'
        '<File ColorHigh="ffffffff" ColorLow="ff000000" Remap="0">'
        f'{spline("0")}</File>'
        '<Linear Angle="90" CenterX="0" CenterY="0" ColorHigh="ffffffff" ColorLow="ff000000" Distance="1">'
        f'{spline("0.45")}</Linear>'
        '<Radial CenterX="0.5" CenterY="0.5" ColorHigh="ffffffff" ColorLow="ff000000" Distance="0.5">'
        f'{spline("0.45")}</Radial>'
        f'<Noise CenterX="0.5" CenterY="0.5" ColorHigh="ffffffff" ColorLow="ff000000" Scale="1" Seed="{seed}">'
        f'{spline("0.45")}</Noise>'
        '</Generate>'
    )
    return f'<Map Name="Specular">{scalar_xml}{generate}</Map>'


class AuthoredTreeProjectionV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = BEFORE.read_text(encoding="utf-8")
        cls.after = AFTER.read_text(encoding="utf-8")
        cls.before_fingerprint = project(cls.before)
        cls.after_fingerprint = project(cls.after)

    def test_literal_no_edit_pair_has_one_core_v4_fingerprint(self):
        self.assertEqual(AUTHORING_GRAPH_CORE_PROJECTION_VERSION, 4)
        self.assertEqual(
            self.before_fingerprint,
            "66f241477cbae1118ec0f548bc88e3b8a1f3862ae1bd67973d888027e2cdb8d0",
        )
        self.assertEqual(self.before_fingerprint, self.after_fingerprint)

    def test_core_v3_is_frozen_for_historical_receipts(self):
        self.assertEqual(
            _legacy_authoring_graph_core_v3_projection(self.before)[
                "fingerprint"
            ],
            "d4d98ee30f3233b4d3cc5c5c4edcde9e58e1c9ec011b87fc155d53c8c6e1cbb5",
        )
        self.assertEqual(
            _legacy_authoring_graph_core_v3_projection(self.after)[
                "fingerprint"
            ],
            "649db337ae5cb7ca1e1e62de04e33d2bd8720d3d41af3abe10a5facdf589a525",
        )

    def test_real_evidence_is_sanitized_and_records_three_exact_pairs(self):
        raw = EVIDENCE.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["issue_number"], 42)
        self.assertEqual(data["projection_version"], 4)
        self.assertEqual(len(data["pairs"]), 3)
        self.assertIsNone(WINDOWS_PATH_RE.search(raw))
        self.assertIsNone(RAW_GUID_RE.search(raw))
        for pair in data["pairs"]:
            self.assertTrue(pair["equal"])
            self.assertRegex(pair["before_raw_sha256"], SHA256_RE)
            self.assertRegex(pair["after_raw_sha256"], SHA256_RE)
            self.assertRegex(pair["core_v4_fingerprint"], SHA256_RE)
            self.assertNotEqual(
                pair["before_raw_sha256"],
                pair["after_raw_sha256"],
            )

    def test_authored_attack_matrix_changes_the_fingerprint(self):
        replacements = {
            "light property": ("<Value>1</Value></Property></Properties></Light>", "<Value>9</Value></Property></Properties></Light>"),
            "fan property": ("<Value>2</Value></Property></Properties></Fan>", "<Value>9</Value></Property></Properties></Fan>"),
            "rule-script property": ("<Value>true</Value></Property></Properties></RuleScript>", "<Value>false</Value></Property></Properties></RuleScript>"),
            "root force property": ("<Value>3</Value></Property></Properties></Force>", "<Value>9</Value></Property></Properties></Force>"),
            "nested force property": ("<Value>4</Value></Property></Properties></Force>", "<Value>9</Value></Property></Properties></Force>"),
            "generator property": ("<Name>Custom:Density</Name><Value>1</Value>", "<Name>Custom:Density</Name><Value>2</Value>"),
            "nonfalse collection": ("<Name>Generation:Collections:authored</Name><Value>true</Value>", "<Name>Generation:Collections:authored</Name><Value>false</Value>"),
            "generator extra": ("<AuthoredExtra>keep</AuthoredExtra>", "<AuthoredExtra>changed</AuthoredExtra>"),
            "link endpoint": ("<TargetGUID>abcdefghijklmnopqrstuA==</TargetGUID>", "<TargetGUID>vvvvvvvvvvvvvvvvvvvvvA==</TargetGUID>"),
            "link subtree": ("<Weight>7</Weight>", "<Weight>8</Weight>"),
            "material texture": ("<TexFilename>leaf.png</TexFilename>", "<TexFilename>other.png</TexFilename>"),
            "mesh geometry": ("<VertexData>authored-vertices</VertexData>", "<VertexData>changed-vertices</VertexData>"),
            "collision setting": ("<Radius>1</Radius>", "<Radius>2</Radius>"),
            "global setting": ("<WindQuality>Best</WindQuality>", "<WindQuality>Fast</WindQuality>"),
            "unknown root": ("<AuthoredValue>preserve</AuthoredValue>", "<AuthoredValue>changed</AuthoredValue>"),
        }
        for name, (old, new) in replacements.items():
            with self.subTest(name=name):
                changed = self.before.replace(old, new, 1)
                self.assertNotEqual(changed, self.before)
                self.assertNotEqual(project(changed), self.before_fingerprint)

    def test_exact_default_shapes_are_narrow_and_fail_closed(self):
        nondefault_atlas = self.after.replace(
            'Rotation="0"/>',
            'Rotation="1"/>',
            1,
        )
        nonempty_lod = self.after.replace(
            "<Lod_1><Filename/></Lod_1>",
            "<Lod_1><Filename>authored.lod</Filename></Lod_1>",
            1,
        )
        namespaced_atlas = self.after.replace(
            'Rotation="0"/>',
            'Rotation="0" xmlns:future="urn:future" '
            'future:Weight="0.5"/>',
            1,
        )
        arbitrary_user_data = self.before.replace(
            '{"generator":"Atlas Leaf Mesh Builder","group":"leaf","kind":"mesh","scope":"0123456789abcdef0123456789abcdef"}',
            '{"owner":"artist"}',
            1,
        )
        for name, changed, baseline in (
            ("nondefault AtlasMaker", nondefault_atlas, self.after_fingerprint),
            ("nonempty LOD", nonempty_lod, self.after_fingerprint),
            ("namespaced AtlasMaker", namespaced_atlas, self.after_fingerprint),
            ("arbitrary UserData", arbitrary_user_data, self.before_fingerprint),
        ):
            with self.subTest(name=name):
                self.assertNotEqual(project(changed), baseline)

        default_a = default_specular_map(123)
        default_b = default_specular_map(987654321)
        with_default_a = self.after.replace(
            "</Material_V8>", default_a + "</Material_V8>", 1
        )
        with_default_b = self.after.replace(
            "</Material_V8>", default_b + "</Material_V8>", 1
        )
        self.assertEqual(project(with_default_a), self.after_fingerprint)
        self.assertEqual(project(with_default_b), self.after_fingerprint)
        near_default = self.after.replace(
            "</Material_V8>",
            default_a.replace(
                "<TexBrightness>0</TexBrightness>",
                "<TexBrightness>0.25</TexBrightness>",
                1,
            ) + "</Material_V8>",
            1,
        )
        self.assertNotEqual(project(near_default), self.after_fingerprint)

    def test_no_blanket_guid_float_or_namespace_normalization(self):
        authored_guid_a = self.before.replace(
            "<WindQuality>Best</WindQuality>",
            "<WindQuality>Best</WindQuality><AuthoredGUID>one</AuthoredGUID>",
            1,
        )
        authored_guid_b = authored_guid_a.replace(
            "<AuthoredGUID>one</AuthoredGUID>",
            "<AuthoredGUID>two</AuthoredGUID>",
            1,
        )
        authored_float_a = self.before.replace(
            "<WindQuality>Best</WindQuality>",
            "<WindQuality>Best</WindQuality><AuthoredFloat>0.1</AuthoredFloat>",
            1,
        )
        authored_float_b = authored_float_a.replace(
            "<AuthoredFloat>0.1</AuthoredFloat>",
            "<AuthoredFloat>0.10000000149011612</AuthoredFloat>",
            1,
        )
        namespaced_a = self.before.replace(
            "</UnknownRoot>",
            '<future:Authored xmlns:future="urn:future">one</future:Authored></UnknownRoot>',
            1,
        )
        namespaced_b = namespaced_a.replace(
            ">one</future:Authored>",
            ">two</future:Authored>",
            1,
        )
        spline_attribute_a = self.before.replace(
            '<ProfileSpline DrawMode="false">',
            '<ProfileSpline DrawMode="false" Authored="0.1">',
            1,
        )
        spline_attribute_b = spline_attribute_a.replace(
            'Authored="0.1"',
            'Authored="0.10000000149011612"',
            1,
        )
        nested_spline_value_a = self.before.replace(
            "</SplineProperty>",
            "<AuthoredNested><Value>0.1</Value></AuthoredNested>"
            "</SplineProperty>",
            1,
        )
        nested_spline_value_b = nested_spline_value_a.replace(
            "<AuthoredNested><Value>0.1</Value>",
            "<AuthoredNested><Value>0.10000000149011612</Value>",
            1,
        )
        nested_texture_size_a = self.before.replace(
            "<CutoutMeshID>130</CutoutMeshID>",
            "<Unknown><Map><TexSizeX>0.1</TexSizeX></Map></Unknown>"
            "<CutoutMeshID>130</CutoutMeshID>",
            1,
        )
        nested_texture_size_b = nested_texture_size_a.replace(
            "<TexSizeX>0.1</TexSizeX>",
            "<TexSizeX>0.10000000149011612</TexSizeX>",
            1,
        )
        for name, left, right in (
            ("GUID-like authored tag", authored_guid_a, authored_guid_b),
            ("authored float", authored_float_a, authored_float_b),
            ("future namespace", namespaced_a, namespaced_b),
            ("spline attribute", spline_attribute_a, spline_attribute_b),
            (
                "nested spline Value",
                nested_spline_value_a,
                nested_spline_value_b,
            ),
            (
                "nested material texture size",
                nested_texture_size_a,
                nested_texture_size_b,
            ),
        ):
            with self.subTest(name=name):
                self.assertNotEqual(project(left), project(right))

    def test_asset_partition_preserves_order_within_each_kind(self):
        first = '<Material_V8 ID="20"><Name>first</Name></Material_V8>'
        second = '<Material_V8 ID="21"><Name>second</Name></Material_V8>'
        ordered = self.after.replace(
            '<Material_V8 ID="10">',
            first + second + '<Material_V8 ID="10">',
            1,
        )
        swapped = ordered.replace(first + second, second + first, 1)
        self.assertNotEqual(project(ordered), project(swapped))


if __name__ == "__main__":
    unittest.main()
