import sys
import unittest
from pathlib import Path


JOBS_DIR = Path(__file__).resolve().parents[1] / "jobs"
sys.path.insert(0, str(JOBS_DIR))

from vertex_color_contract import (
    NANITE_VERTEX_PAYLOAD_UV_NAME,
    inspect_object_vertex_colors,
    pack_speedtree_vertex_payload,
    summarize_rgba,
)


class FakeColorItem:
    def __init__(self, color):
        self.color_srgb = tuple(color)
        self.color = tuple(color)


class FakeColorData:
    def __init__(self, colors):
        self._items = [FakeColorItem(color) for color in colors]

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __iter__(self):
        return iter(self._items)

    def foreach_get(self, property_name, output):
        offset = 0
        for item in self._items:
            for value in getattr(item, property_name):
                output[offset] = value
                offset += 1

    def foreach_set(self, property_name, values):
        for index, item in enumerate(self._items):
            base = index * 4
            color = tuple(values[base : base + 4])
            item.color = color
            item.color_srgb = color


class FakeUVItem:
    def __init__(self, uv=(0.0, 0.0)):
        self.uv = tuple(uv)


class FakeUVData:
    def __init__(self, values):
        self._items = [FakeUVItem(value) for value in values]

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def foreach_get(self, property_name, output):
        offset = 0
        for item in self._items:
            for value in getattr(item, property_name):
                output[offset] = value
                offset += 1

    def foreach_set(self, property_name, values):
        for index, item in enumerate(self._items):
            base = index * 2
            setattr(item, property_name, tuple(values[base : base + 2]))


class FakeUVLayer:
    def __init__(self, name, values):
        self.name = name
        self.data = FakeUVData(values)


class FakeUVLayers(list):
    def __init__(self, layers, loop_count):
        super().__init__(layers)
        self.loop_count = loop_count

    def get(self, name):
        return next((item for item in self if item.name == name), None)

    def new(self, name):
        layer = FakeUVLayer(name, [(0.0, 0.0)] * self.loop_count)
        self.append(layer)
        return layer

    def remove(self, layer):
        super().remove(layer)


class FakeAttribute:
    def __init__(self, colors, name="color", data_type="BYTE_COLOR"):
        self.name = name
        self.domain = "CORNER"
        self.data_type = data_type
        self.data = FakeColorData(colors)


class FakeAttributes(list):
    def __init__(self, attributes):
        super().__init__(attributes)
        self.active_color = attributes[0] if attributes else None

    def get(self, name):
        return next((item for item in self if item.name == name), None)


class FakeMesh:
    def __init__(self, colors=None, uv_layers=None):
        attributes = [] if colors is None else [FakeAttribute(colors)]
        self.color_attributes = FakeAttributes(attributes)
        loop_count = len(colors or [])
        self.loops = [object()] * loop_count
        self.uv_layers = FakeUVLayers(
            [FakeUVLayer(name, values) for name, values in (uv_layers or [])],
            loop_count,
        )


class FakeObject:
    type = "MESH"

    def __init__(self, colors=None, name="Tree", uv_layers=None):
        self.name = name
        self.data = FakeMesh(colors, uv_layers=uv_layers)


class VertexColorContractTests(unittest.TestCase):
    def test_summarize_rgba_reports_green_distribution(self):
        report = summarize_rgba([0, 0, 0, 1, 1, 0.5, 0, 1])

        self.assertEqual(report["g"]["count"], 2)
        self.assertEqual(report["g"]["min"], 0)
        self.assertEqual(report["g"]["max"], 0.5)
        self.assertEqual(report["g"]["mean"], 0.25)
        self.assertEqual(report["g"]["zero_ratio"], 0.5)

    def test_tree_reports_zero_green_as_warning_without_blocking_valid_payload(self):
        report = inspect_object_vertex_colors(
            FakeObject([(0, 0, 0, 1), (1, 0, 0, 1)]),
            require_green_signal=True,
        )

        self.assertEqual(report["status"], "ok")
        self.assertNotIn("green_channel_has_no_signal", report["issues"])
        self.assertIn("green_channel_has_no_signal", report["warnings"])

    def test_sparse_but_nonzero_tree_green_is_reported_not_blocked(self):
        colors = [(0, 0, 0, 1)] * 9 + [(0, 1, 0, 1)]
        report = inspect_object_vertex_colors(
            FakeObject(colors),
            require_green_signal=True,
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["channels"]["g"]["zero_ratio"], 0.9)
        self.assertIn("green_channel_sparse_by_contract", report["warnings"])

    def test_missing_attribute_is_always_blocked(self):
        report = inspect_object_vertex_colors(FakeObject(None))

        self.assertEqual(report["status"], "blocked")
        self.assertIn("missing_color_attribute", report["issues"])

    def test_pack_blocks_unsupported_color_type_before_creating_uv2(self):
        obj = FakeObject(
            [(0.1, 0.75, 0.3, 1.0)],
            uv_layers=[
                ("uv0", [(0.0, 0.0)]),
                ("blend_ao", [(0.4, 0.55)]),
            ],
        )
        obj.data.color_attributes[0].data_type = "FLOAT_VECTOR"

        report = pack_speedtree_vertex_payload(obj, mirror_to_nanite_uv=True)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("unsupported_color_attribute_type", report["issues"])
        self.assertEqual(len(obj.data.uv_layers), 2)

    def test_pack_copies_blend_ao_v_to_alpha_and_mirrors_ga_to_uv2(self):
        colors = [
            (0.1, 0.0, 0.3, 1.0),
            (0.2, 0.5, 0.4, 1.0),
            (0.3, 1.0, 0.5, 1.0),
        ]
        obj = FakeObject(
            colors,
            uv_layers=[
                ("uv0", [(0.0, 0.0)] * 3),
                ("blend_ao", [(0.2, 0.25), (0.4, 0.6), (0.8, 0.9)]),
            ],
        )

        report = pack_speedtree_vertex_payload(obj, mirror_to_nanite_uv=True)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["payload_uv_index"], 2)
        self.assertEqual(report["before_channels"]["a"]["one_ratio"], 1.0)
        self.assertAlmostEqual(report["after_channels"]["a"]["mean"], (0.25 + 0.6 + 0.9) / 3)
        colors_after = [item.color_srgb for item in obj.data.color_attributes[0].data]
        for before, after in zip(colors, colors_after):
            for expected, actual in zip(before[:3], after[:3]):
                self.assertAlmostEqual(expected, actual)
        for expected, actual in zip([0.25, 0.6, 0.9], [color[3] for color in colors_after]):
            self.assertAlmostEqual(expected, actual)
        payload = obj.data.uv_layers.get(NANITE_VERTEX_PAYLOAD_UV_NAME)
        for expected, item in zip([(2.0, 0.75), (2.5, 0.4), (3.0, 0.1)], payload.data):
            for expected_value, actual_value in zip(expected, item.uv):
                self.assertAlmostEqual(expected_value, actual_value)

    def test_pack_prefers_canonical_color_over_another_active_corner_color(self):
        obj = FakeObject(
            [(0.1, 0.5, 0.3, 1.0)],
            uv_layers=[
                ("uv0", [(0.0, 0.0)]),
                ("blend_ao", [(0.4, 0.55)]),
            ],
        )
        active_other = FakeAttribute([(0.1, 0.9, 0.3, 0.8)], name="other")
        canonical = obj.data.color_attributes[0]
        obj.data.color_attributes = FakeAttributes([active_other, canonical])

        report = pack_speedtree_vertex_payload(obj, mirror_to_nanite_uv=True)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["color_attribute"], "color")
        payload = obj.data.uv_layers.get(NANITE_VERTEX_PAYLOAD_UV_NAME)
        self.assertAlmostEqual(payload.data[0].uv[0], 2.5)
        self.assertAlmostEqual(canonical.data[0].color_srgb[3], 0.55)
        self.assertAlmostEqual(active_other.data[0].color_srgb[3], 0.8)

    def test_pack_blocks_malformed_canonical_color_instead_of_using_active_other(self):
        obj = FakeObject(
            [(0.1, 0.5, 0.3, 1.0)],
            uv_layers=[
                ("uv0", [(0.0, 0.0)]),
                ("blend_ao", [(0.4, 0.55)]),
            ],
        )
        active_other = FakeAttribute([(0.1, 0.9, 0.3, 0.8)], name="other")
        canonical = obj.data.color_attributes[0]
        canonical.domain = "POINT"
        obj.data.color_attributes = FakeAttributes([active_other, canonical])

        report = pack_speedtree_vertex_payload(obj, mirror_to_nanite_uv=True)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("color_attribute_domain_must_be_corner", report["issues"])
        self.assertEqual(len(obj.data.uv_layers), 2)
        self.assertAlmostEqual(canonical.data[0].color_srgb[3], 1.0)
        self.assertAlmostEqual(active_other.data[0].color_srgb[3], 0.8)

    def test_pack_blocks_reversed_speedtree_uv_order(self):
        obj = FakeObject(
            [(0.1, 0.75, 0.3, 1.0)],
            uv_layers=[
                ("blend_ao", [(0.4, 0.55)]),
                ("uv0", [(0.0, 0.0)]),
            ],
        )

        report = pack_speedtree_vertex_payload(obj, mirror_to_nanite_uv=True)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("speedtree_uv0_must_be_index_0", report["issues"])
        self.assertEqual(len(obj.data.uv_layers), 2)

    def test_pack_is_idempotent_and_does_not_add_another_uv_layer(self):
        obj = FakeObject(
            [(0.1, 0.75, 0.3, 1.0)],
            uv_layers=[
                ("uv0", [(0.0, 0.0)]),
                ("blend_ao", [(0.4, 0.55)]),
            ],
        )

        first = pack_speedtree_vertex_payload(obj, mirror_to_nanite_uv=True)
        second = pack_speedtree_vertex_payload(obj, mirror_to_nanite_uv=True)

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(len(obj.data.uv_layers), 3)
        self.assertEqual(obj.data.uv_layers[2].name, NANITE_VERTEX_PAYLOAD_UV_NAME)

    def test_pack_blocks_when_blend_ao_is_missing_without_touching_alpha(self):
        obj = FakeObject(
            [(0.1, 0.75, 0.3, 1.0)],
            uv_layers=[("uv0", [(0.0, 0.0)])],
        )

        report = pack_speedtree_vertex_payload(obj)

        self.assertEqual(report["status"], "blocked")
        self.assertIn("missing_ao_uv_layer:blend_ao", report["issues"])
        self.assertEqual(obj.data.color_attributes[0].data[0].color_srgb[3], 1.0)
        self.assertEqual(len(obj.data.uv_layers), 1)


if __name__ == "__main__":
    unittest.main()
