"""Runtime gateway for every external Blender add-on used by the batch.

Blender worker scripts request named capabilities and resolve only operations
granted by those capabilities.  Direct imports of add-on implementation
modules outside this file are an integration-boundary violation.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from pathlib import Path

from blender_addon_contract import (
    ADDONS,
    CONTRACT_SCHEMA_VERSION,
    GATEWAY_API_NAME,
    GATEWAY_API_VERSION,
    AddonContractError,
    build_runtime_request,
    integration_manifest,
    native_capabilities_for,
    operations_for_requirements,
    source_expectations_from_environment,
    validate_runtime_receipt,
)


class BlenderAddonGatewayError(RuntimeError):
    """A Blender add-on failed capability or source negotiation."""

    def __init__(self, message, *, failure_contract=None):
        super().__init__(message)
        self.failure_contract = failure_contract or {}


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolved(path):
    return Path(path).expanduser().resolve()


def _path_within(path, root):
    try:
        _resolved(path).relative_to(_resolved(root))
        return True
    except (OSError, ValueError):
        return False


def _module_identity(package):
    module_file = Path(package.__file__).resolve()
    version = None
    bl_info = getattr(package, "bl_info", None)
    if isinstance(bl_info, dict) and bl_info.get("version") is not None:
        raw_version = bl_info["version"]
        if isinstance(raw_version, (list, tuple)):
            version = ".".join(str(value) for value in raw_version)
        else:
            version = str(raw_version)
    return {
        "module_file": str(module_file),
        "source_root": str(module_file.parent),
        "module_sha256": _sha256_file(module_file),
        "addon_version": version,
    }


def _resolve_symbol(specification):
    module_name, separator, attribute_path = specification.partition(":")
    if not separator or not module_name or not attribute_path:
        raise BlenderAddonGatewayError(
            f"invalid gateway operation specification: {specification}"
        )
    value = importlib.import_module(module_name)
    for attribute in attribute_path.split("."):
        try:
            value = getattr(value, attribute)
        except AttributeError as exc:
            raise BlenderAddonGatewayError(
                f"missing Blender add-on operation symbol: {specification}"
            ) from exc
    return value


def _replace_symbol(specification, replacement):
    module_name, separator, attribute_path = specification.partition(":")
    if not separator or not module_name or not attribute_path:
        raise BlenderAddonGatewayError(
            f"invalid gateway operation specification: {specification}"
        )
    owner = importlib.import_module(module_name)
    attributes = attribute_path.split(".")
    for attribute in attributes[:-1]:
        owner = getattr(owner, attribute)
    previous = getattr(owner, attributes[-1])
    setattr(owner, attributes[-1], replacement)
    return previous


def _enable_addon(addon_id, expected_source=None):
    specification = ADDONS[addon_id]
    module_name = specification["module"]
    if expected_source and module_name not in sys.modules:
        expected = _resolved(expected_source)
        package_dir = expected.parent if expected.is_file() else expected
        if not (package_dir / "__init__.py").is_file():
            candidate = package_dir / module_name
            if candidate.is_dir():
                package_dir = candidate
        if not (package_dir / "__init__.py").is_file():
            raise BlenderAddonGatewayError(
                f"configured {addon_id} source is not an importable add-on "
                f"package: {expected}",
                failure_contract={
                    "code": "BLENDER_ADDON_SOURCE_INVALID",
                    "addon": addon_id,
                    "expected": str(expected),
                },
            )
        package_parent = str(package_dir.parent)
        if package_parent not in sys.path:
            sys.path.insert(0, package_parent)
    try:
        import addon_utils  # type: ignore
    except ImportError as exc:
        raise BlenderAddonGatewayError(
            "Blender addon_utils is unavailable; gateway must run inside Blender"
        ) from exc

    _default_enabled, loaded = addon_utils.check(module_name)
    if not loaded:
        addon_utils.enable(
            module_name,
            default_set=False,
            persistent=False,
        )
    _default_enabled, loaded = addon_utils.check(module_name)
    if not loaded:
        raise BlenderAddonGatewayError(
            f"Blender add-on did not enable: {module_name}",
            failure_contract={
                "code": "BLENDER_ADDON_ENABLE_FAILED",
                "addon": addon_id,
            },
        )
    return importlib.import_module(module_name)


def _negotiate_native_contract(addon_id, requirements):
    specification = ADDONS[addon_id]
    module_name = specification.get("native_api_module")
    if not module_name:
        return {
            "mode": "gateway_adapter",
            "api_module": None,
            "api_contract": None,
        }
    try:
        api_module = importlib.import_module(module_name)
        require_contract = getattr(api_module, "require_integration_contract")
    except (ImportError, AttributeError) as exc:
        raise BlenderAddonGatewayError(
            f"{addon_id} does not expose its required public integration API",
            failure_contract={
                "code": "BLENDER_ADDON_PUBLIC_API_MISSING",
                "addon": addon_id,
                "api_module": module_name,
            },
        ) from exc
    native_capabilities = native_capabilities_for(requirements, addon_id)
    contract = require_contract(
        minimum_version=specification["minimum_native_api_version"],
        capabilities=native_capabilities,
    )
    if not isinstance(contract, dict):
        raise BlenderAddonGatewayError(
            f"{addon_id} returned an invalid public integration contract"
        )
    return {
        "mode": "native_api",
        "api_module": module_name,
        "api_contract": contract,
    }


def _validate_expected_source(addon_id, module_file, expected_source):
    if not expected_source:
        return None
    expected = _resolved(expected_source)
    actual = _resolved(module_file)
    if expected.is_file():
        matches = actual == expected
    else:
        matches = _path_within(actual, expected)
    if not matches:
        raise BlenderAddonGatewayError(
            f"loaded {addon_id} source differs from the configured source: "
            f"loaded={actual}, expected={expected}",
            failure_contract={
                "code": "BLENDER_ADDON_SOURCE_MISMATCH",
                "addon": addon_id,
                "loaded": str(actual),
                "expected": str(expected),
            },
        )
    return str(expected)


class RuntimeSession:
    """One negotiated Blender worker runtime."""

    def __init__(self, request, receipt):
        self.request = request
        self.receipt = validate_runtime_receipt(request, receipt)
        self.requirements = request["requirements"]

    def operation(self, addon_id, operation_name):
        allowed = operations_for_requirements(self.requirements, addon_id)
        if operation_name not in allowed:
            raise BlenderAddonGatewayError(
                f"operation {operation_name!r} was not granted for {addon_id}",
                failure_contract={
                    "code": "BLENDER_ADDON_OPERATION_NOT_GRANTED",
                    "addon": addon_id,
                    "operation": operation_name,
                    "granted": sorted(allowed),
                },
            )
        operation = ADDONS[addon_id]["operations"].get(operation_name)
        if not operation:
            raise BlenderAddonGatewayError(
                f"gateway has no operation mapping for {addon_id}.{operation_name}"
            )
        return _resolve_symbol(operation)

    def replace_operation(self, addon_id, operation_name, replacement):
        """Replace one explicitly granted hook for this short-lived worker."""
        if not callable(replacement):
            raise BlenderAddonGatewayError("replacement operation must be callable")
        allowed = operations_for_requirements(self.requirements, addon_id)
        if operation_name not in allowed:
            raise BlenderAddonGatewayError(
                f"operation {operation_name!r} was not granted for {addon_id}"
            )
        operation = ADDONS[addon_id]["operations"].get(operation_name)
        if not operation:
            raise BlenderAddonGatewayError(
                f"gateway has no operation mapping for {addon_id}.{operation_name}"
            )
        return _replace_symbol(operation, replacement)

    def detach_timer(self, addon_id, callback_name):
        """Remove a requested add-on UI timer from an isolated worker."""
        if addon_id not in self.requirements:
            raise BlenderAddonGatewayError(
                f"runtime did not negotiate add-on: {addon_id}"
            )
        try:
            import bpy  # type: ignore
        except ImportError as exc:
            raise BlenderAddonGatewayError("bpy is unavailable") from exc
        package = importlib.import_module(ADDONS[addon_id]["module"])
        callback = getattr(package, callback_name, None)
        if callback is not None and bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
            return True
        return False

    def disable(self, addon_id):
        if addon_id not in self.requirements:
            raise BlenderAddonGatewayError(
                f"runtime did not negotiate add-on: {addon_id}"
            )
        import addon_utils  # type: ignore

        addon_utils.disable(ADDONS[addon_id]["module"], default_set=False)


def prepare_runtime(job, requirements, *, expected_sources=None):
    """Enable and negotiate the exact add-ons required by one Blender job."""
    environment_sources = source_expectations_from_environment()
    merged_sources = dict(environment_sources)
    if expected_sources:
        merged_sources.update(expected_sources)
    request = build_runtime_request(
        job,
        requirements,
        expected_sources=merged_sources,
    )
    rows = []
    try:
        for addon_id, capabilities in request["requirements"].items():
            package = _enable_addon(
                addon_id,
                request["expected_sources"].get(addon_id),
            )
            identity = _module_identity(package)
            expected = _validate_expected_source(
                addon_id,
                identity["module_file"],
                request["expected_sources"].get(addon_id),
            )
            allowed_operations = operations_for_requirements(
                request["requirements"], addon_id
            )
            # Resolve every granted symbol before any job-level mutation.  A
            # partially upgraded add-on therefore fails at startup, not in the
            # middle of an SPM/Blend transaction.
            for operation_name in sorted(allowed_operations):
                _resolve_symbol(ADDONS[addon_id]["operations"][operation_name])
            native = _negotiate_native_contract(
                addon_id,
                request["requirements"],
            )
            rows.append(
                {
                    "id": addon_id,
                    "status": "ready",
                    "capabilities": capabilities,
                    "expected_source": expected,
                    **identity,
                    **native,
                }
            )
    except (AddonContractError, BlenderAddonGatewayError) as exc:
        if isinstance(exc, BlenderAddonGatewayError):
            raise
        raise BlenderAddonGatewayError(str(exc)) from exc
    except Exception as exc:
        raise BlenderAddonGatewayError(
            f"Blender add-on negotiation failed for {job}: "
            f"{type(exc).__name__}: {exc}",
            failure_contract={
                "code": "BLENDER_ADDON_NEGOTIATION_FAILED",
                "job": job,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        ) from exc

    receipt = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "status": "ready",
        "gateway": {
            "name": GATEWAY_API_NAME,
            "version": GATEWAY_API_VERSION,
            "module_file": str(Path(__file__).resolve()),
            "module_sha256": _sha256_file(__file__),
        },
        "job": request["job"],
        "request_sha256": request["request_sha256"],
        "addons": rows,
        "process_id": os.getpid(),
    }
    return RuntimeSession(request, receipt)


def get_integration_contract():
    return integration_manifest()


__all__ = [
    "BlenderAddonGatewayError",
    "RuntimeSession",
    "get_integration_contract",
    "prepare_runtime",
]
