"""Blender-side validation and packing for the SpeedTree vertex payload."""

from array import array
import math

try:
    import numpy as _np
except ImportError:  # Blender normally bundles NumPy; keep a portable fallback.
    _np = None


BYTE_EPSILON = 0.5 / 255.0
SPARSE_GREEN_ZERO_RATIO = 0.90
SPEEDTREE_AO_UV_NAME = "blend_ao"
NANITE_VERTEX_PAYLOAD_UV_NAME = "vertex_color_ga"
NANITE_VERTEX_PAYLOAD_UV_INDEX = 2
NANITE_VERTEX_PAYLOAD_GREEN_TAG = 2.0


def _color_property_name(attribute):
    if not len(attribute.data):
        return "color"
    sample = attribute.data[0]
    if (
        getattr(attribute, "data_type", "") == "BYTE_COLOR"
        and hasattr(sample, "color_srgb")
    ):
        return "color_srgb"
    return "color"


def _color_attribute(mesh):
    attributes = getattr(mesh, "color_attributes", None)
    if attributes is None:
        return None

    getter = getattr(attributes, "get", None)
    if callable(getter):
        named = getter("color")
        if named is not None:
            # ``color`` is the SpeedTree contract source of truth.  Returning
            # it even when malformed lets the caller block the export instead
            # of silently packing a different active color attribute.
            return named

    active = getattr(attributes, "active_color", None)
    candidates = []
    if active is not None and active not in candidates:
        candidates.append(active)
    for attribute in attributes:
        if attribute not in candidates:
            candidates.append(attribute)

    for attribute in candidates:
        if (
            getattr(attribute, "domain", "") == "CORNER"
            and getattr(attribute, "data_type", "") in {"BYTE_COLOR", "FLOAT_COLOR"}
        ):
            return attribute
    return candidates[0] if candidates else None


def _read_rgba(attribute):
    count = len(attribute.data)
    values = array("f", [0.0]) * (count * 4)
    if count == 0:
        return values

    property_name = _color_property_name(attribute)
    try:
        attribute.data.foreach_get(property_name, values)
    except (AttributeError, TypeError, ValueError):
        property_name = "color"
        for index, item in enumerate(attribute.data):
            color = getattr(item, property_name)
            base = index * 4
            values[base : base + 4] = array("f", color)
    return values


def _write_rgba(attribute, values):
    property_name = _color_property_name(attribute)
    try:
        attribute.data.foreach_set(property_name, values)
    except (AttributeError, TypeError, ValueError):
        for index, item in enumerate(attribute.data):
            base = index * 4
            setattr(item, property_name, tuple(values[base : base + 4]))


def _read_uv(layer):
    count = len(layer.data)
    values = array("f", [0.0]) * (count * 2)
    if count:
        layer.data.foreach_get("uv", values)
    return values


def _write_uv(layer, values):
    try:
        layer.data.foreach_set("uv", values)
    except (AttributeError, TypeError, ValueError):
        for index, item in enumerate(layer.data):
            base = index * 2
            item.uv = tuple(values[base : base + 2])


def _numpy_values(values):
    if _np is None:
        return None
    if isinstance(values, _np.ndarray):
        return values
    if isinstance(values, array) and values.typecode == "f":
        return _np.frombuffer(values, dtype=_np.float32)
    try:
        return _np.asarray(values, dtype=_np.float64)
    except (TypeError, ValueError):
        return None


def _all_finite(values):
    numeric = _numpy_values(values)
    if numeric is not None:
        return bool(_np.isfinite(numeric).all())
    return all(math.isfinite(float(value)) for value in values)


def _outside_unit_interval(values):
    numeric = _numpy_values(values)
    if numeric is not None:
        return bool(
            (numeric < -BYTE_EPSILON).any()
            or (numeric > 1.0 + BYTE_EPSILON).any()
        )
    return any(
        float(value) < -BYTE_EPSILON
        or float(value) > 1.0 + BYTE_EPSILON
        for value in values
    )


def _max_delta(left, right):
    left_numeric = _numpy_values(left)
    right_numeric = _numpy_values(right)
    if left_numeric is not None and right_numeric is not None:
        if left_numeric.size != right_numeric.size:
            raise ValueError("delta buffers must contain the same value count")
        if left_numeric.size == 0:
            return 0.0
        return float(
            _np.max(
                _np.abs(
                    left_numeric.reshape(-1) - right_numeric.reshape(-1)
                )
            )
        )
    return max((abs(float(a) - float(b)) for a, b in zip(left, right)), default=0.0)


def _blocked_payload_report(obj, issue):
    return {
        "object": getattr(obj, "name", "<missing>"),
        "status": "blocked",
        "issues": [issue],
    }


def pack_speedtree_vertex_payload(
    obj,
    ao_uv_name=SPEEDTREE_AO_UV_NAME,
    payload_uv_name=NANITE_VERTEX_PAYLOAD_UV_NAME,
    payload_uv_index=NANITE_VERTEX_PAYLOAD_UV_INDEX,
    mirror_to_nanite_uv=False,
):
    """Copy SpeedTree ``blend_ao.V`` to Color.A without changing RGB.

    The optional UV mirror is deliberately off by default.  When explicitly
    enabled it writes a presence-tagged UV2 ``(2 + G, A)`` for engines whose
    Skeletal Nanite path cannot read vertex colors.
    """
    if obj is None or getattr(obj, "type", None) != "MESH" or not getattr(obj, "data", None):
        return _blocked_payload_report(obj, "missing_export_mesh")

    mesh = obj.data
    attribute = _color_attribute(mesh)
    if attribute is None:
        return _blocked_payload_report(obj, "missing_color_attribute")
    if getattr(attribute, "domain", "") != "CORNER":
        return _blocked_payload_report(obj, "color_attribute_domain_must_be_corner")
    if getattr(attribute, "data_type", "") not in {"BYTE_COLOR", "FLOAT_COLOR"}:
        return _blocked_payload_report(obj, "unsupported_color_attribute_type")

    loop_count = len(getattr(mesh, "loops", []))
    if len(attribute.data) != loop_count:
        return _blocked_payload_report(obj, "color_attribute_loop_count_mismatch")

    rgba_before = _read_rgba(attribute)
    if not _all_finite(rgba_before):
        return _blocked_payload_report(obj, "color_attribute_contains_non_finite_values")
    if _outside_unit_interval(rgba_before):
        return _blocked_payload_report(obj, "color_attribute_outside_zero_one")

    uv_layers = getattr(mesh, "uv_layers", None)
    if uv_layers is None:
        return _blocked_payload_report(obj, "missing_uv_layers")
    source = uv_layers.get(ao_uv_name) if hasattr(uv_layers, "get") else None
    if source is None:
        return _blocked_payload_report(obj, f"missing_ao_uv_layer:{ao_uv_name}")
    if len(uv_layers) < 2:
        return _blocked_payload_report(obj, "speedtree_uv_contract_requires_uv0_and_blend_ao")
    if getattr(uv_layers[0], "name", "") != "uv0":
        return _blocked_payload_report(obj, "speedtree_uv0_must_be_index_0")
    source_index = next(
        (index for index, layer in enumerate(uv_layers) if layer == source),
        -1,
    )
    if source_index != 1:
        return _blocked_payload_report(obj, "speedtree_blend_ao_must_be_index_1")
    if len(source.data) != loop_count:
        return _blocked_payload_report(obj, "ao_uv_loop_count_mismatch")

    target = None
    target_created = False
    target_index = None
    payload_before = array("f")
    if mirror_to_nanite_uv:
        target = uv_layers.get(payload_uv_name) if hasattr(uv_layers, "get") else None
        if target is None:
            if len(uv_layers) > payload_uv_index:
                occupied = getattr(uv_layers[payload_uv_index], "name", "<unnamed>")
                return _blocked_payload_report(
                    obj,
                    f"nanite_payload_uv_index_{payload_uv_index}_occupied:{occupied}",
                )
            if len(uv_layers) != payload_uv_index:
                return _blocked_payload_report(
                    obj,
                    f"nanite_payload_uv_requires_index_{payload_uv_index}:current_count={len(uv_layers)}",
                )
            target = uv_layers.new(name=payload_uv_name)
            target_created = True

        target_index = next(
            (index for index, layer in enumerate(uv_layers) if layer == target),
            -1,
        )
        if target_index != payload_uv_index:
            if target_created:
                uv_layers.remove(target)
            return _blocked_payload_report(
                obj,
                f"nanite_payload_uv_wrong_index:{target_index}",
            )
        if len(target.data) != loop_count:
            if target_created:
                uv_layers.remove(target)
            return _blocked_payload_report(obj, "nanite_payload_uv_loop_count_mismatch")
        payload_before = _read_uv(target)

    ao_uv = _read_uv(source)
    if _np is not None:
        ao_matrix = _np.frombuffer(ao_uv, dtype=_np.float32).reshape(
            loop_count, 2
        )
        blend_values = ao_matrix[:, 0]
        ao_values = ao_matrix[:, 1]
    else:
        blend_values = array("f", ao_uv[0::2])
        ao_values = array("f", ao_uv[1::2])
    if not _all_finite(blend_values):
        if target_created:
            uv_layers.remove(target)
        return _blocked_payload_report(obj, "blend_ao_u_contains_non_finite_values")
    if _outside_unit_interval(blend_values):
        if target_created:
            uv_layers.remove(target)
        return _blocked_payload_report(
            obj,
            "blend_ao_u_outside_zero_one_nanite_fallback_unsafe",
        )
    if not _all_finite(ao_values):
        if target_created:
            uv_layers.remove(target)
        return _blocked_payload_report(obj, "ao_uv_contains_non_finite_values")
    if _outside_unit_interval(ao_values):
        if target_created:
            uv_layers.remove(target)
        return _blocked_payload_report(obj, "ao_uv_outside_zero_one")

    rgba_after = array("f", rgba_before)
    payload = array("f", [0.0]) * (loop_count * 2) if mirror_to_nanite_uv else array("f")
    if _np is not None:
        rgba_before_matrix = _np.frombuffer(
            rgba_before, dtype=_np.float32
        ).reshape(loop_count, 4)
        rgba_after_matrix = _np.frombuffer(
            rgba_after, dtype=_np.float32
        ).reshape(loop_count, 4)
        clamped_ao = _np.clip(ao_values, 0.0, 1.0)
        rgba_after_matrix[:, 3] = clamped_ao
        if mirror_to_nanite_uv:
            payload_matrix = _np.frombuffer(
                payload, dtype=_np.float32
            ).reshape(loop_count, 2)
            payload_matrix[:, 0] = (
                NANITE_VERTEX_PAYLOAD_GREEN_TAG
                + rgba_before_matrix[:, 1]
            )
            # Unreal's FBX skeletal importer applies V = 1 - V. Store the
            # transport inverse so UE UV2.V resolves back to the same AO held
            # in VertexColor.A.
            payload_matrix[:, 1] = 1.0 - clamped_ao
    else:
        for index, ao in enumerate(ao_values):
            clamped_ao = min(1.0, max(0.0, float(ao)))
            rgba_after[index * 4 + 3] = clamped_ao
            if mirror_to_nanite_uv:
                payload[index * 2] = (
                    NANITE_VERTEX_PAYLOAD_GREEN_TAG
                    + rgba_before[index * 4 + 1]
                )
                payload[index * 2 + 1] = 1.0 - clamped_ao

    try:
        _write_rgba(attribute, rgba_after)
        if mirror_to_nanite_uv:
            _write_uv(target, payload)
    except Exception:
        try:
            _write_rgba(attribute, rgba_before)
        finally:
            if mirror_to_nanite_uv:
                if target_created:
                    uv_layers.remove(target)
                else:
                    _write_uv(target, payload_before)
        raise

    verified_rgba = _read_rgba(attribute)
    if _np is not None:
        verified_rgba_matrix = _np.frombuffer(
            verified_rgba, dtype=_np.float32
        ).reshape(loop_count, 4)
        rgb_before = rgba_before_matrix[:, :3]
        rgb_after = verified_rgba_matrix[:, :3]
        before_alpha = rgba_before_matrix[:, 3]
        verified_alpha = verified_rgba_matrix[:, 3]
    else:
        rgb_before = array(
            "f",
            [
                value
                for base in range(0, len(rgba_before), 4)
                for value in rgba_before[base : base + 3]
            ],
        )
        rgb_after = array(
            "f",
            [
                value
                for base in range(0, len(verified_rgba), 4)
                for value in verified_rgba[base : base + 3]
            ],
        )
        before_alpha = rgba_before[3::4]
        verified_alpha = verified_rgba[3::4]
    tolerance = BYTE_EPSILON + 1.0e-6
    changed = (
        _max_delta(before_alpha, ao_values) > tolerance
        or (mirror_to_nanite_uv and (
            target_created or _max_delta(payload_before, payload) > 1.0e-6
        ))
    )
    deltas = {
        "preserved_rgb_max_delta": _max_delta(rgb_before, rgb_after),
        "alpha_vs_blend_ao_v_max_delta": _max_delta(verified_alpha, ao_values),
    }
    if mirror_to_nanite_uv:
        verified_payload = _read_uv(target)
        if _np is not None:
            verified_payload_matrix = _np.frombuffer(
                verified_payload, dtype=_np.float32
            ).reshape(loop_count, 2)
            verified_g = verified_rgba_matrix[:, 1]
            payload_g = (
                verified_payload_matrix[:, 0]
                - NANITE_VERTEX_PAYLOAD_GREEN_TAG
            )
            payload_a = 1.0 - verified_payload_matrix[:, 1]
        else:
            verified_g = verified_rgba[1::4]
            payload_g = array(
                "f",
                [
                    value - NANITE_VERTEX_PAYLOAD_GREEN_TAG
                    for value in verified_payload[0::2]
                ],
            )
            payload_a = array(
                "f", [1.0 - value for value in verified_payload[1::2]]
            )
        deltas.update(
            {
                "payload_u_vs_green_max_delta": _max_delta(payload_g, verified_g),
                "one_minus_payload_v_vs_alpha_max_delta": _max_delta(
                    payload_a,
                    verified_alpha,
                ),
            }
        )
    issues = [name for name, value in deltas.items() if value > tolerance]
    return {
        "object": getattr(obj, "name", "<missing>"),
        "status": "blocked" if issues else "ok",
        "issues": issues,
        "color_attribute": getattr(attribute, "name", ""),
        "ao_source_uv": ao_uv_name,
        "ao_source_component": "V",
        "nanite_uv_mirror_enabled": bool(mirror_to_nanite_uv),
        "payload_uv": payload_uv_name if mirror_to_nanite_uv else None,
        "payload_uv_index": target_index,
        "payload_semantics": ({
            "u": "2 + VertexColor.G height attenuation (presence-tagged)",
            "v": "1 - VertexColor.A transport; UE FBX import resolves it to AO",
        } if mirror_to_nanite_uv else None),
        "green_tag": NANITE_VERTEX_PAYLOAD_GREEN_TAG if mirror_to_nanite_uv else None,
        "changed": changed,
        "uv_layers_after": [getattr(layer, "name", "") for layer in uv_layers],
        "loop_count": loop_count,
        "speedtree_blend": summarize_scalar(blend_values),
        "ao": summarize_scalar(ao_values),
        "before_channels": summarize_rgba(rgba_before),
        "after_channels": summarize_rgba(verified_rgba),
        "verification": deltas,
    }


def summarize_scalar(values, epsilon=BYTE_EPSILON):
    numeric = _numpy_values(values)
    if numeric is not None:
        numeric = numeric.reshape(-1)
        count = int(numeric.size)
        finite_mask = _np.isfinite(numeric)
        finite_count = int(_np.count_nonzero(finite_mask))
        finite = numeric if finite_count == count else numeric[finite_mask]
        return {
            "count": count,
            "finite_count": finite_count,
            "min": float(_np.min(finite)) if finite_count else 0.0,
            "max": float(_np.max(finite)) if finite_count else 0.0,
            "mean": (
                float(_np.sum(finite, dtype=_np.float64) / finite_count)
                if finite_count
                else 0.0
            ),
            "zero_count": int(_np.count_nonzero(finite <= epsilon)),
            "one_count": int(
                _np.count_nonzero(finite >= 1.0 - epsilon)
            ),
        }
    values = [float(value) for value in values]
    count = len(values)
    finite = [value for value in values if math.isfinite(value)]
    return {
        "count": count,
        "finite_count": len(finite),
        "min": min(finite) if finite else 0.0,
        "max": max(finite) if finite else 0.0,
        "mean": sum(finite) / len(finite) if finite else 0.0,
        "zero_count": sum(value <= epsilon for value in finite),
        "one_count": sum(value >= 1.0 - epsilon for value in finite),
    }


def summarize_rgba(values, epsilon=BYTE_EPSILON):
    if len(values) % 4:
        raise ValueError("RGBA buffer length must be divisible by four")

    count = len(values) // 4
    numeric = _numpy_values(values)
    if numeric is not None:
        matrix = numeric.reshape(count, 4)
        result = {}
        for channel, name in enumerate("rgba"):
            column = matrix[:, channel]
            zero_count = int(_np.count_nonzero(column <= epsilon))
            one_count = int(
                _np.count_nonzero(column >= 1.0 - epsilon)
            )
            result[name] = {
                "count": count,
                "min": float(_np.min(column)) if count else 0.0,
                "max": float(_np.max(column)) if count else 0.0,
                "mean": (
                    float(_np.sum(column, dtype=_np.float64) / count)
                    if count
                    else 0.0
                ),
                "zero_count": zero_count,
                "zero_ratio": zero_count / count if count else 0.0,
                "one_count": one_count,
                "one_ratio": one_count / count if count else 0.0,
            }
        return result
    mins = [1.0, 1.0, 1.0, 1.0]
    maxs = [0.0, 0.0, 0.0, 0.0]
    sums = [0.0, 0.0, 0.0, 0.0]
    zero_counts = [0, 0, 0, 0]
    one_counts = [0, 0, 0, 0]
    for base in range(0, len(values), 4):
        for channel in range(4):
            value = float(values[base + channel])
            mins[channel] = min(mins[channel], value)
            maxs[channel] = max(maxs[channel], value)
            sums[channel] += value
            zero_counts[channel] += int(value <= epsilon)
            one_counts[channel] += int(value >= 1.0 - epsilon)

    result = {}
    for channel, name in enumerate("rgba"):
        result[name] = {
            "count": count,
            "min": mins[channel] if count else 0.0,
            "max": maxs[channel] if count else 0.0,
            "mean": sums[channel] / count if count else 0.0,
            "zero_count": zero_counts[channel],
            "zero_ratio": zero_counts[channel] / count if count else 0.0,
            "one_count": one_counts[channel],
            "one_ratio": one_counts[channel] / count if count else 0.0,
        }
    return result


def inspect_object_vertex_colors(obj, require_green_signal=False):
    report = {
        "object": getattr(obj, "name", "<missing>"),
        "status": "ok",
        "require_green_signal": bool(require_green_signal),
        "issues": [],
        "warnings": [],
    }
    if obj is None or getattr(obj, "type", None) != "MESH" or not getattr(obj, "data", None):
        report["status"] = "blocked"
        report["issues"].append("missing_export_mesh")
        return report

    attribute = _color_attribute(obj.data)
    if attribute is None:
        report["status"] = "blocked"
        report["issues"].append("missing_color_attribute")
        return report

    values = _read_rgba(attribute)
    channels = summarize_rgba(values)
    loop_count = len(getattr(obj.data, "loops", []))
    report.update(
        {
            "attribute": getattr(attribute, "name", ""),
            "domain": getattr(attribute, "domain", ""),
            "data_type": getattr(attribute, "data_type", ""),
            "element_count": len(values) // 4,
            "loop_count": loop_count,
            "channels": channels,
        }
    )
    if not values:
        report["status"] = "blocked"
        report["issues"].append("empty_color_attribute")
        return report

    if getattr(attribute, "domain", "") != "CORNER":
        report["status"] = "blocked"
        report["issues"].append("color_attribute_domain_must_be_corner")
    if getattr(attribute, "data_type", "") not in {"BYTE_COLOR", "FLOAT_COLOR"}:
        report["status"] = "blocked"
        report["issues"].append("unsupported_color_attribute_type")
    if len(values) // 4 != loop_count:
        report["status"] = "blocked"
        report["issues"].append("color_attribute_loop_count_mismatch")
    if any(not math.isfinite(float(value)) for value in values):
        report["status"] = "blocked"
        report["issues"].append("color_attribute_contains_non_finite_values")
    if any(float(value) < -BYTE_EPSILON or float(value) > 1.0 + BYTE_EPSILON for value in values):
        report["status"] = "blocked"
        report["issues"].append("color_attribute_outside_zero_one")

    green = channels["g"]
    if require_green_signal and green["max"] <= BYTE_EPSILON:
        # An all-zero G channel disables the optional height attenuation but
        # does not corrupt RGB/AO/UV payload transport. Freshly rebuilt trees
        # may intentionally have no authored height mask, so report it without
        # discarding an otherwise valid Blender repair.
        report["warnings"].append("green_channel_has_no_signal")
    elif green["zero_ratio"] >= SPARSE_GREEN_ZERO_RATIO:
        report["warnings"].append("green_channel_sparse_by_contract")
    return report
