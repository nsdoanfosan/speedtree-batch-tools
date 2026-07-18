import hashlib
import json
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


SK_BATCH_DIR = Path(__file__).resolve().parents[1]


def load_gui_module():
    path = SK_BATCH_DIR / "sk_batch_gui.pyw"
    loader = SourceFileLoader("sk_batch_gui_convenience_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeRoot:
    def __init__(self):
        self.clipboard = ""

    def clipboard_clear(self):
        self.clipboard = ""

    def clipboard_append(self, value):
        self.clipboard += value

    @staticmethod
    def update_idletasks():
        return None


class FakeVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class FakeTree:
    def __init__(self):
        self.labels = {}
        self.selected = ()
        self.focused = None
        self.has_keyboard_focus = False

    @staticmethod
    def identify_region(_x, _y):
        return "tree"

    @staticmethod
    def identify_row(y):
        return y

    def item(self, iid, **values):
        if "text" in values:
            self.labels[iid] = values["text"]

    def selection_set(self, iid):
        self.selected = (iid,)

    def selection(self):
        return self.selected

    def focus(self, iid):
        self.focused = iid

    def focus_set(self):
        self.has_keyboard_focus = True


class SkBatchUiConvenienceTests(unittest.TestCase):
    def test_verified_xml_bone_count_is_read_only_and_content_matched(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spm = root / "SK_tree_manual_01.spm"
            spm.write_bytes(b"current-spm")
            xml_path = root / "xml" / f"{spm.stem}.xml"
            xml_path.parent.mkdir()
            xml_path.write_text(
                (
                    f'<SpeedTreeRaw Source="{spm}"><Bones Count="2">'
                    '<Bone ID="0" Generator="Branch" />'
                    '<Bone ID="1" Generator="Branch 2" />'
                    "</Bones></SpeedTreeRaw>"
                ),
                encoding="utf-8",
            )
            receipt_path = (
                xml_path.parent
                / ".speedtree_export_cache"
                / f"{xml_path.name}.json"
            )
            receipt_path.parent.mkdir()
            receipt_path.write_text(
                json.dumps(
                    {
                        "inputs": {
                            "spm": {
                                "path": str(spm),
                                "size": spm.stat().st_size,
                                "sha256": hashlib.sha256(spm.read_bytes()).hexdigest(),
                            }
                        },
                        "artifacts": [
                            {
                                "relative_path": xml_path.name,
                                "size": xml_path.stat().st_size,
                                "sha256": hashlib.sha256(
                                    xml_path.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            before = (
                spm.read_bytes(),
                spm.stat().st_mtime_ns,
                xml_path.read_bytes(),
                xml_path.stat().st_mtime_ns,
            )

            measurement = gui.current_speedtree_bone_measurement(spm)

            self.assertEqual(measurement["count"], 2)
            self.assertTrue(measurement["current"])
            self.assertEqual(
                before,
                (
                    spm.read_bytes(),
                    spm.stat().st_mtime_ns,
                    xml_path.read_bytes(),
                    xml_path.stat().st_mtime_ns,
                ),
            )
            spm.write_bytes(b"changed-spm")
            stale = gui.current_speedtree_bone_measurement(spm)
            self.assertEqual(stale["count"], 2)
            self.assertFalse(stale["current"])

    def test_compact_table_keeps_full_selected_row_detail(self):
        gui = load_gui_module()
        full = "수동 본 유지 🔒 · SpeedTree 본 282개 (현재 SPM과 일치하는 XML)"

        compact = gui.compact_table_status(full, max_chars=24)
        detail = gui.selected_row_detail_text(
            Path("SK_tree_birch_paper_03.spm"),
            {
                "spm_status": full,
                "blend_status": "최신 ✓",
                "push_status": "준비됨 ✓",
            },
        )

        self.assertLessEqual(len(compact), 24)
        self.assertTrue(compact.endswith("…"))
        self.assertIn(full, detail)
        self.assertIn("SK_tree_birch_paper_03.spm", detail)

    def test_spm_check_uses_clear_bone_style_names(self):
        gui = load_gui_module()
        parts = gui.spm_check_status_parts(
            {
                "generators": [
                    {"style": 0.0, "bones": 2.0},
                    {"style": 1.0, "bones": 0.5},
                    {"style": 0.0, "bones": 0.0},
                ],
                "materials": [{"needs_prefix": True}],
                "bone_graph": {
                    "root_target_generator_count": 2,
                    "base_excluded_generator_count": 1,
                    "unknown_base_generators": [{"name": "Unknown"}],
                },
            }
        )

        self.assertEqual(
            parts,
            [
                "고정 본(Absolute) 1개",
                "자동 본(Relative) 1개",
                "본 꺼짐 1개",
                "M_ 필요 1개",
                "자동 대상 2 / Base 제외 1",
                "Base 미분류 1",
            ],
        )
        self.assertNotIn("미보정", " · ".join(parts))

    def test_first_click_isolates_row_and_ctrl_c_copies_that_spm_path(self):
        gui = load_gui_module()
        with tempfile.TemporaryDirectory() as temporary:
            spms = [Path(temporary) / f"SK_tree_{index}.spm" for index in range(3)]
            app = gui.App.__new__(gui.App)
            app.root = FakeRoot()
            app.tree = FakeTree()
            app.worker = None
            app.cell_editor = None
            app.progress_var = FakeVar()
            app.items = {
                str(spm): {
                    "spm": spm,
                    "checked": True,
                    "manual_bones_locked": False,
                }
                for spm in spms
            }
            app.checked_rows = gui.CheckedRowController(
                app.items, app._redraw_checked_row
            )
            app.checked_rows.sync_after_reload()

            clicked = str(spms[1])
            event = type("Event", (), {"x": 0, "y": clicked})()
            self.assertEqual(app._on_click(event), "break")

            self.assertEqual(
                [entry["checked"] for entry in app.items.values()],
                [False, True, False],
            )
            self.assertEqual(app.tree.selection(), (clicked,))
            self.assertEqual(app.tree.focused, clicked)
            self.assertTrue(app.tree.has_keyboard_focus)

            self.assertEqual(app.copy_selected_paths(), "break")
            self.assertEqual(app.root.clipboard, str(spms[1].resolve()))
            self.assertIn("1개", app.progress_var.value)


if __name__ == "__main__":
    unittest.main()
