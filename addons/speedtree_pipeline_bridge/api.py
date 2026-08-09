"""Stable Blender-side import path for the batch integration gateway."""

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from blender_addon_gateway import (  # noqa: E402,F401
    BlenderAddonGatewayError,
    RuntimeSession,
    get_integration_contract,
    prepare_runtime,
)


__all__ = [
    "BlenderAddonGatewayError",
    "RuntimeSession",
    "get_integration_contract",
    "prepare_runtime",
]
