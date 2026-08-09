"""Blender-visible entry point for the SpeedTree integration gateway."""

bl_info = {
    "name": "SpeedTree Pipeline Bridge",
    "author": "PARK / Codex",
    "version": (1, 0, 0),
    "blender": (5, 1, 0),
    "location": "Headless integration API (no panel)",
    "description": (
        "Capability and source-identity boundary between SpeedTree Batch "
        "Tools and Blender add-ons"
    ),
    "category": "Pipeline",
}


def register():
    """The bridge is API-only and intentionally registers no UI state."""


def unregister():
    """The bridge is API-only and intentionally registers no UI state."""
