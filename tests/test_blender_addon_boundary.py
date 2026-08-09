import ast
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_ADDONS = {
    "atlas_leaf_mesh_builder",
    "send2ue",
    "speedtree_bone_weight_repair",
    "speedtree_cluster_normalizer",
    "ue_unique_export_names_addon",
}
ALLOWED_IMPLEMENTATION_FILE = REPO_ROOT / "blender_addon_gateway.py"


def external_root(module_name):
    if not module_name:
        return None
    root = module_name.split(".", 1)[0]
    return root if root in EXTERNAL_ADDONS else None


class BlenderAddonBoundaryTests(unittest.TestCase):
    def test_only_gateway_imports_or_enables_external_addons(self):
        violations = []
        for path in sorted(REPO_ROOT.rglob("*.py")):
            if path == ALLOWED_IMPLEMENTATION_FILE:
                continue
            relative = path.relative_to(REPO_ROOT)
            if (
                ".git" in relative.parts
                or "__pycache__" in relative.parts
                or "tests" in relative.parts
            ):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as exc:
                self.fail(f"could not parse {relative}: {exc}")
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = external_root(alias.name)
                        if root:
                            violations.append(
                                f"{relative}:{node.lineno}: direct import {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    root = external_root(node.module)
                    if root:
                        violations.append(
                            f"{relative}:{node.lineno}: direct import from {node.module}"
                        )
                elif isinstance(node, ast.Call):
                    function = node.func
                    if (
                        isinstance(function, ast.Attribute)
                        and isinstance(function.value, ast.Name)
                        and function.value.id == "addon_utils"
                        and function.attr in {"enable", "disable", "check"}
                    ):
                        violations.append(
                            f"{relative}:{node.lineno}: direct addon_utils.{function.attr}"
                        )
        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
