"""Full-tree core-v4 acceptance and fail-closed regressions for #42."""

import gzip
import hashlib
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
    _legacy_authoring_graph_core_v4_projection,
)


FIXTURES = Path(__file__).parent / "fixtures"
BEFORE = FIXTURES / "issue_42_no_edit_before.xml"
AFTER = FIXTURES / "issue_42_no_edit_after.xml"
EVIDENCE = FIXTURES / "issue_42_real_no_edit_evidence.json"
REAL_FIXTURES = FIXTURES / "issue_42" / "real"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:(?<![a-z])[a-z]:[\\/]|users[\\/][^\\/]+)"
)
RAW_GUID_RE = re.compile(
    r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def project(text):
    return _authoring_graph_core_projection(text)["fingerprint"]


def project_v4(text):
    return _legacy_authoring_graph_core_v4_projection(text)["fingerprint"]


def default_material_map(map_name, seed):
    def spline(length):
        return (
            '<Spline DrawMode="false">'
            f'<ControlPoint><X>0</X><Y>0</Y><TangentX>1</TangentX><TangentY>0</TangentY><Length>{length}</Length></ControlPoint>'
            f'<ControlPoint><X>1</X><Y>1</Y><TangentX>1</TangentX><TangentY>0</TangentY><Length>{length}</Length></ControlPoint>'
            '</Spline>'
        )

    specific = {
        "Specular": ("0.75", "0.75", "0.75", "0", "true"),
        "Metallic": ("0", "0", "0", "1", "false"),
        "Custom": ("0", "0", "0", "0", "true"),
        "Custom2": ("0", "0", "0", "0", "false"),
    }[map_name]
    scalars = (
        ("ColorX", specific[0]),
        ("ColorY", specific[1]),
        ("ColorZ", specific[2]),
        ("TexFilename", ""),
        ("TexSource", specific[3]),
        ("TexBrightness", "0"),
        ("TexContrast", "0"),
        ("TexSaturation", "0"),
        ("TexRed", "0"),
        ("TexGreen", "0"),
        ("TexBlue", "0"),
        ("TexMin", "0"),
        ("TexMax", "1"),
        ("TexEnabled", "true"),
        ("TexToLinear", specific[4]),
        ("TexInvert", "false"),
        ("TexInvertRed", "false"),
        ("TexInvertGreen", "false"),
        ("TexInvertBlue", "false"),
        ("Normalize", "false"),
        ("TexSizeX", "0"),
        ("TexSizeY", "0"),
    )
    scalar_xml = "".join(
        f"<{name}>{value}</{name}>" for name, value in scalars
    )
    generate = (
        '<Generate Type="0">'
        '<File ColorHigh="ffffffff" ColorLow="ff000000" Remap="0">'
        f'{spline("0")}</File>'
        '<Linear Angle="90" CenterX="0" CenterY="0" ColorHigh="ffffffff" ColorLow="ff000000" Distance="1">'
        f'{spline("0.44999998807907104")}</Linear>'
        '<Radial CenterX="0.5" CenterY="0.5" ColorHigh="ffffffff" ColorLow="ff000000" Distance="0.5">'
        f'{spline("0.44999998807907104")}</Radial>'
        f'<Noise CenterX="0.5" CenterY="0.5" ColorHigh="ffffffff" ColorLow="ff000000" Scale="1" Seed="{seed}">'
        f'{spline("0.44999998807907104")}</Noise>'
        '</Generate>'
    )
    return f'<Map Name="{map_name}">{scalar_xml}{generate}</Map>'


class AuthoredTreeProjectionV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = BEFORE.read_text(encoding="utf-8")
        cls.after = AFTER.read_text(encoding="utf-8")
        cls.before_fingerprint = project(cls.before)
        cls.after_fingerprint = project(cls.after)

    def test_literal_no_edit_pair_has_one_current_core_fingerprint(self):
        self.assertEqual(AUTHORING_GRAPH_CORE_PROJECTION_VERSION, 6)
        self.assertEqual(
            self.before_fingerprint,
            "8d5dc57396cfbd31e918f8f89a2c601fe60c46ffb65fc81ad50ea5656b7419bb",
        )
        self.assertEqual(self.before_fingerprint, self.after_fingerprint)

    def test_core_v3_is_frozen_for_historical_receipts(self):
        self.assertEqual(
            _legacy_authoring_graph_core_v3_projection(self.before)[
                "fingerprint"
            ],
            "73f9a3e4d09489f2818ad169beefba099ffa80ac2c2924b018f700f13504f182",
        )
        self.assertEqual(
            _legacy_authoring_graph_core_v3_projection(self.after)[
                "fingerprint"
            ],
            "f6c2ed0da01d5899eef71973612a4c1bcd0688839c7d6d183510070328ce3d9d",
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

    def test_three_sanitized_real_xml_pairs_execute_the_projector(self):
        manifest_text = (REAL_FIXTURES / "manifest.json").read_text(
            encoding="utf-8"
        )
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["issue_number"], 42)
        self.assertEqual(len(manifest["pairs"]), 3)
        self.assertNotRegex(
            manifest_text,
            r"(?i)(PARK|OneDrive|Forestportfolio|[A-Z]:[\\/])",
        )
        for pair in manifest["pairs"]:
            with self.subTest(pair=pair["pair_id"]):
                before_bytes = gzip.decompress(
                    (REAL_FIXTURES / pair["before_fixture"]).read_bytes()
                )
                after_bytes = gzip.decompress(
                    (REAL_FIXTURES / pair["after_fixture"]).read_bytes()
                )
                self.assertEqual(
                    hashlib.sha256(before_bytes).hexdigest(),
                    pair["sanitized_before_xml_sha256"],
                )
                self.assertEqual(
                    hashlib.sha256(after_bytes).hexdigest(),
                    pair["sanitized_after_xml_sha256"],
                )
                before_text = before_bytes.decode("utf-8")
                after_text = after_bytes.decode("utf-8")
                self.assertNotRegex(
                    before_text + after_text,
                    r"(?i)(PARK|OneDrive|Forestportfolio|[A-Z]:[\\/])",
                )
                self.assertEqual(
                    project_v4(before_text),
                    pair["expected_core_fingerprint"],
                )
                self.assertEqual(project_v4(before_text), project_v4(after_text))
                self.assertEqual(project(before_text), project(after_text))

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

        for map_name in ("Specular", "Metallic", "Custom", "Custom2"):
            with self.subTest(default_map=map_name):
                default_a = default_material_map(map_name, 123)
                default_b = default_material_map(map_name, 987654321)
                with_default_a = self.after.replace(
                    "</Material_v8>", default_a + "</Material_v8>", 1
                )
                with_default_b = self.after.replace(
                    "</Material_v8>", default_b + "</Material_v8>", 1
                )
                self.assertEqual(
                    project(with_default_a),
                    self.after_fingerprint,
                )
                self.assertEqual(
                    project(with_default_b),
                    self.after_fingerprint,
                )
                near_default = self.after.replace(
                    "</Material_v8>",
                    default_a.replace(
                        "<TexBrightness>0</TexBrightness>",
                        "<TexBrightness>0.25</TexBrightness>",
                        1,
                    ) + "</Material_v8>",
                    1,
                )
                self.assertNotEqual(
                    project(near_default),
                    self.after_fingerprint,
                )

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
        comment_a = self.before.replace(
            "<UnknownRoot>",
            "<!--authored-one--><UnknownRoot>",
            1,
        )
        comment_b = comment_a.replace("authored-one", "authored-two", 1)
        processing_instruction_a = self.before.replace(
            "<UnknownRoot>",
            "<?future authored-one?><UnknownRoot>",
            1,
        )
        processing_instruction_b = processing_instruction_a.replace(
            "authored-one",
            "authored-two",
            1,
        )
        mixed_tail_a = self.before.replace(
            "<AuthoredValue>preserve</AuthoredValue>",
            "<AuthoredValue>preserve</AuthoredValue>authored-one",
            1,
        )
        mixed_tail_b = mixed_tail_a.replace("authored-one", "authored-two", 1)
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
            ("XML comment", comment_a, comment_b),
            (
                "processing instruction",
                processing_instruction_a,
                processing_instruction_b,
            ),
            ("mixed-content tail", mixed_tail_a, mixed_tail_b),
        ):
            with self.subTest(name=name):
                self.assertNotEqual(project(left), project(right))

    def test_asset_partition_preserves_order_within_each_kind(self):
        first = '<Material_v8 ID="20"><Name>first</Name></Material_v8>'
        second = '<Material_v8 ID="21"><Name>second</Name></Material_v8>'
        ordered = self.after.replace(
            '<Material_v8 ID="10">',
            first + second + '<Material_v8 ID="10">',
            1,
        )
        swapped = ordered.replace(first + second, second + first, 1)
        self.assertNotEqual(project(ordered), project(swapped))

    def test_normalization_allowlist_near_negatives_fail_closed(self):
        cases = []

        lowercase_preview_a = self.after.replace(
            "<Preview>after-material-preview</Preview>",
            "<preview>authored-one</preview>",
            1,
        )
        lowercase_preview_b = lowercase_preview_a.replace(
            "authored-one", "authored-two", 1
        )
        cases.append(("case-sensitive Preview QName", lowercase_preview_a,
                      lowercase_preview_b))

        preview_tail_a = self.after.replace(
            "</Preview><StreamPlaceholder>",
            "</Preview>authored-one<StreamPlaceholder>",
            1,
        )
        preview_tail_b = preview_tail_a.replace(
            "authored-one", "authored-two", 1
        )
        cases.append(("excluded child significant tail", preview_tail_a,
                      preview_tail_b))

        unknown_spline_a = self.before.replace(
            "</UnknownRoot>",
            "<Spline><ControlPoint><TangentX>0.1</TangentX>"
            "</ControlPoint></Spline></UnknownRoot>",
            1,
        )
        unknown_spline_b = unknown_spline_a.replace(
            "<TangentX>0.1</TangentX>",
            "<TangentX>0.10000000149011612</TangentX>",
            1,
        )
        cases.append(("unscoped spline numeric spelling", unknown_spline_a,
                      unknown_spline_b))

        random_seed_extra_a = self.before.replace(
            "<Name>Random Seeds:Style</Name><Value>919820633</Value>",
            "<Name>Random Seeds:Style</Name><Value>919820633</Value>"
            "<Authored>keep</Authored>",
            1,
        )
        random_seed_extra_b = random_seed_extra_a.replace(
            "<Value>919820633</Value>", "<Value>1</Value>", 1
        )
        cases.append(("Random Seeds extra child", random_seed_extra_a,
                      random_seed_extra_b))

        duplicate_parent_a = self.after.replace(
            '<CompoundParentSpline Count="0"/>',
            '<CompoundParentSpline Count="0"/>'
            '<CompoundParentSpline Count="0"/>',
            1,
        )
        duplicate_parent_b = duplicate_parent_a.replace(
            '<CompoundParentSpline Count="0"/>',
            '<CompoundParentSpline Count="0" Authored="one"/>',
            1,
        )
        cases.append(("duplicate parent spline", duplicate_parent_a,
                      duplicate_parent_b))

        for name, left, right in cases:
            with self.subTest(name=name):
                self.assertNotEqual(project(left), project(right))

        guid_case = self.before.replace(
            "<TargetGUID>abcdefghijklmnopqrstuA==</TargetGUID>",
            "<TargetGUID>ABCDEFGHIJKLMNOPQRSTUA==</TargetGUID>",
            1,
        )
        guid_padding = self.before.replace(
            "<TargetGUID>abcdefghijklmnopqrstuA==</TargetGUID>",
            "<TargetGUID>abcdefghijklmnopqrstu==</TargetGUID>",
            1,
        )
        self.assertNotEqual(project(guid_case), self.before_fingerprint)
        self.assertEqual(project(guid_padding), self.before_fingerprint)

        default_map = default_material_map("Specular", 123)
        duplicate_map = self.after.replace(
            "</Material_v8>",
            default_map + default_map + "</Material_v8>",
            1,
        )
        self.assertNotEqual(project(duplicate_map), self.after_fingerprint)

        material = '<Material_v8 ID="10">'
        mesh = '<Mesh ID="130">'
        barrier = '<FutureAsset Authored="one"/>'
        ordered = self.after.replace(material, barrier + material, 1)
        swapped = ordered.replace(
            barrier + material,
            material,
            1,
        ).replace(mesh, barrier + mesh, 1)
        self.assertNotEqual(project(ordered), project(swapped))


if __name__ == "__main__":
    unittest.main()
