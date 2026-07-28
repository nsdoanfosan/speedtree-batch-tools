import tempfile
from pathlib import Path

import pytest

from speedtree_export_options_contract import (
    SpeedTreeExportOptionsError,
    inspect_speedtree_export_options,
    require_texture_skip_writing,
)


def _preset(path, value):
    Path(path).write_text(
        "[Options]\n"
        "Filetype=Autodesk FBX (*.fbx)\n"
        f"TextureSkipWriting={value}\n",
        encoding="utf-8",
    )


def test_texture_skip_writing_true_is_required_without_mutating_preset():
    with tempfile.TemporaryDirectory() as temporary:
        preset = Path(temporary) / "Options_MA_Fbx.ini"
        _preset(preset, "true")
        before = preset.read_bytes()

        inspected = require_texture_skip_writing(
            preset, purpose="production FBX"
        )

        assert inspected["status"] == "ok"
        assert inspected["texture_skip_writing"] is True
        assert preset.read_bytes() == before


def test_false_missing_and_copied_presets_are_fail_closed():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "Options_MA_Fbx.ini"
        copied = root / "_temporary" / "Options_MA_Fbx.ini"
        copied.parent.mkdir()
        _preset(source, "false")
        copied.write_bytes(source.read_bytes())

        for preset in (source, copied):
            before = preset.read_bytes()
            inspected = inspect_speedtree_export_options(preset)
            assert inspected["status"] == "texture_writing_enabled"
            with pytest.raises(
                SpeedTreeExportOptionsError,
                match="TextureSkipWriting=false",
            ):
                require_texture_skip_writing(preset)
            assert preset.read_bytes() == before

        missing = inspect_speedtree_export_options(root / "missing.ini")
        assert missing["status"] == "missing"
