"""Render every texture set used by current SK SPMs, then normalize the SPMs.

The operation is resumable: a complete six-map T_ set is skipped on the next
run.  No SPM is modified unless every required render succeeds.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import sbs_auto
from export_texture_plan import build_texture_plan_from_report, bucket_refs
from pcg_texture_audit import make_report
from pcg_texture_common import REPORT_DIR, load_config
from spm_texture_normalize import (
    build_spm_patch,
    cleanup_preserved_cluster_outputs,
    jobs_from_texture_plan,
    normalize_spms_transactionally,
    output_paths,
)


def row_uses_current_sk(row):
    return any(Path(value).name.lower().startswith("sk_")
               for value in row.get("material_spms") or [])


def current_sk_owner_rows(plan):
    """Return one render owner per texture base used by any current SK SPM."""
    groups = {}
    for row in plan.get("items", []):
        key = str(row.get("texture_base") or row.get("atlas_base") or "").lower()
        if key:
            groups.setdefault(key, []).append(row)
    owners = []
    for rows in groups.values():
        if not any(row_uses_current_sk(row) for row in rows):
            continue
        owner = next((row for row in rows if not row.get("shared_from")), rows[0])
        owners.append(owner)
    return sorted(owners, key=lambda row: str(row.get("texture_base", "")).lower())


def complete_output_set(row, expected_pixels=None, roles=None):
    paths = output_paths(row["texture_dir"], row["texture_base"])
    selected_roles = tuple(roles or paths)
    selected = [paths[role] for role in selected_roles]
    if not all(path.is_file() and path.stat().st_size > 0 for path in selected):
        return False
    if expected_pixels:
        try:
            if not all(sbs_auto.image_pixel_size(path) == tuple(expected_pixels)
                       for path in selected):
                return False
        except Exception:
            return False
    return not any(
        sbs_auto.rendered_map_content_error(paths[role], role)
        for role in selected_roles
    )


def verify_complete_output_set(texture_dir, texture_base, expected_pixels=None):
    """Raise unless the complete six-map contract exists at one resolution."""
    paths = output_paths(texture_dir, texture_base)
    errors = []
    for role in sbs_auto.RENDER_MAPS:
        path = paths[role]
        if not path.is_file() or path.stat().st_size <= 0:
            errors.append(f"{role}=missing")
            continue
        if expected_pixels:
            try:
                size = sbs_auto.image_pixel_size(path)
            except Exception as exc:
                errors.append(f"{role}=unreadable ({exc})")
                continue
            if tuple(size) != tuple(expected_pixels):
                errors.append(f"{role}={size}, expected {tuple(expected_pixels)}")
                continue
        content_error = sbs_auto.rendered_map_content_error(path, role)
        if content_error:
            errors.append(f"{role}={content_error}")
    if errors:
        raise RuntimeError(
            f"{texture_base}: incomplete six-map output: " + "; ".join(errors))
    return [paths[role] for role in sbs_auto.RENDER_MAPS]


def expected_job_size(job):
    if job.get("mode") == "direct":
        return sbs_auto.graph_render_size_log2(job["sbs"], job["graph"])
    if job.get("inputs"):
        return sbs_auto.render_size_log2(job["inputs"])
    if job.get("mode") == "render":
        info = sbs_auto.parse_m_graph(job["sbs"], job["graph"])
        return sbs_auto.render_size_log2(info["inputs"])
    return sbs_auto.MAX_OUTPUT_LOG2, sbs_auto.MAX_OUTPUT_LOG2


def job_needs_source_repair(job):
    if job.get("mode") == "direct":
        return bool(_direct_subsurface_repair(job))
    if job.get("mode") != "render" or not job["row"].get("canonical_source_provenance"):
        return False
    try:
        current = sbs_auto.parse_m_graph(job["sbs"], job["graph"])["inputs"]
        planned, _notes = sbs_auto.plan_inputs_from_row(
            job["row"], require_alpha=False)
    except Exception:
        return True
    return any(
        not current.get(slot) or not _same_path(current[slot], desired)
        for slot, desired in planned.items()
    )


def _is_neutral_input(path):
    return bool(path) and Path(path).stem.lower().startswith("neutral_")


def _direct_subsurface_repair(job):
    """Recover only an authoritative real Subsurface source from a neutral slot.

    Direct procedural graphs may intentionally author Subsurface Amount, so this
    deliberately does not reconcile every bitmap or force Amount to white.
    """
    if job.get("mode") != "direct" \
            or not job.get("row", {}).get("canonical_source_provenance"):
        return {}
    try:
        current = sbs_auto.parse_m_graph(job["sbs"], job["graph"])["inputs"]
        planned, _notes = sbs_auto.plan_inputs_from_row(
            job["row"], require_alpha=False)
    except Exception:
        return {}
    desired = planned.get("Subsurface")
    existing = current.get("Subsurface")
    if not desired or _is_neutral_input(desired) or not Path(desired).is_file():
        return {}
    if not _is_neutral_input(existing):
        return {}
    return {"Subsurface": Path(desired)}


def _packaged_resource_replacement(sbs_path, missing_path):
    """Find a moved embedded resource beside its SBS by the recorded filename."""
    sbs_path = Path(sbs_path)
    raw_stem = Path(missing_path).stem.lower()
    if raw_stem in {"neutral_black", "neutral_white", "neutral_normal"}:
        return sbs_auto.neutral_image(raw_stem.removeprefix("neutral_"))
    target_stems = [raw_stem]
    if raw_stem.startswith("xxxxxx-"):
        target_stems.append(raw_stem[7:])
    roots = [sbs_path.parent / f"{sbs_path.stem}.resources"]
    roots.extend(sorted((sbs_path.parent / ".autosave").glob(f"{sbs_path.stem}*.resources")))
    for root in roots:
        if not root.is_dir():
            continue
        exact = [path for path in root.iterdir()
                 if path.is_file() and path.stem.lower() in target_stems]
        if len(exact) == 1:
            return exact[0]
    return None


def _inputs_from_prefixed_graph_resources(sbs_path, graph_name):
    """Recover bitmap inputs from an old inserted graph with stale connRefs."""
    source = sbs_auto.inspect_graph_sources(sbs_path, graph_name)
    prefix = graph_name.lower() + "_"
    suffix_to_slot = {value: key for key, value in sbs_auto.SLOT_SUFFIX.items()}
    inputs = {}
    for bitmap in source["bitmaps"]:
        resource = bitmap["resource"]
        low = resource.lower()
        if not low.startswith(prefix):
            continue
        suffix = low[len(prefix):]
        slot = suffix_to_slot.get(suffix)
        path = Path(bitmap["path"])
        if slot and path.is_file():
            inputs[slot] = path
    if "Base_Color" in inputs:
        inputs["Subsurface_Amount"] = sbs_auto.neutral_image("white")
        return inputs

    # Older hand-authored graphs do not prefix resource IDs with the graph
    # name.  Accept only a coherent, sufficiently complete bitmap family.
    refs = [item["path"] for item in source["bitmaps"]
            if item.get("path") and Path(item["path"]).is_file()]
    buckets = bucket_refs(refs)
    if not buckets.get("albedo") or not buckets.get("normal"):
        return None
    fake_row = {
        "folder_name": graph_name,
        "cluster_name": graph_name,
        "atlas_base": graph_name,
        "folder": str(Path(sbs_path).parent),
        "texture_dir": str(Path(sbs_path).parent),
        **{f"source_{kind}": values for kind, values in buckets.items()
           if kind != "unknown"},
    }
    try:
        recovered, _notes = sbs_auto.plan_inputs_from_row(fake_row, require_alpha=False)
    except Exception:
        return None
    real_roles = sum(1 for slot in (
        "Base_Color", "Normal", "Height", "Roughness", "Opacity", "Subsurface")
        if recovered.get(slot) and "neutral_" not in Path(recovered[slot]).name.lower())
    return recovered if real_roles >= 3 else None


def _unique_graph_opacity_source(graph_sources):
    candidates = []
    for row in graph_sources.get("bitmaps") or []:
        path = Path(row.get("path") or "")
        low = path.name.lower()
        if path.is_file() and any(word in low for word in ("opacity", "alpha", "mask")):
            if path not in candidates:
                candidates.append(path)
    return candidates[0] if len(candidates) == 1 else None


def build_job(row):
    base = row["atlas_base"]
    texture_base = row.get("texture_base") or base
    sbs_files = row.get("sbs_files") or []
    graph_name = row.get("m_graph")
    graph_sbs = row.get("m_graph_sbs")
    if not graph_name:
        for sbs in sbs_files:
            name = (sbs_auto.find_m_graph_name(sbs, texture_base)
                    or sbs_auto.find_m_graph_name(sbs, base))
            if name:
                graph_name, graph_sbs = name, sbs
                break
    job = {
        "base": base,
        "texture_base": texture_base,
        "row": row,
        "out_dir": row.get("texture_dir") or str(Path(row["folder"]) / "texture"),
        "normal_opengl": row.get("normal_convention") != "DirectX",
    }
    if graph_name:
        promotion = sbs_auto.authoring_graph_promotion_candidate(
            graph_sbs, base, texture_base)
        if promotion:
            authoring_sources = sbs_auto.inspect_graph_sources(
                graph_sbs, promotion["authoring"])
            job.update(
                mode="direct",
                graph=promotion["authoring"],
                sbs=graph_sbs,
                direct_maps=list(sbs_auto.RENDER_MAPS),
                normalize_cluster=True,
                direct_fallback_opacity=_unique_graph_opacity_source(authoring_sources),
                promote_authoring=promotion,
                rename_graph_to=(
                    texture_base
                    if promotion["managed"].lower() != texture_base.lower()
                    else None
                ),
                notes=[
                    f"promote procedural/direct {promotion['authoring']} over generated clone "
                    f"{promotion['managed']}"
                ],
            )
            return job
        # A previous resumable pass may already have renamed legacy M_ to T_.
        current_name = sbs_auto.find_m_graph_name(graph_sbs, texture_base)
        if not current_name:
            current_name = sbs_auto.find_m_graph_name(graph_sbs, graph_name)
        if current_name:
            graph_name = current_name
        cluster_state = sbs_auto.graph_cluster_normalization_state(
            graph_sbs, graph_name)
        if cluster_state["cluster_count"] > 1:
            raise RuntimeError(
                f"{graph_name}: multiple Cluster_System wrappers")
        # A procedural graph already wrapped by Cluster_System must be cooked
        # and rendered as that graph.  Parsing its bitmap ancestry and calling
        # Cluster_System.sbsar directly would bypass the authoring nodes again.
        if cluster_state["wrapped_direct"]:
            source = sbs_auto.inspect_graph_sources(graph_sbs, graph_name)
            job.update(
                mode="direct", graph=graph_name, sbs=graph_sbs,
                direct_maps=list(sbs_auto.RENDER_MAPS),
                normalize_cluster=False,
                direct_fallback_opacity=_unique_graph_opacity_source(source),
                rename_graph_to=(
                    texture_base
                    if graph_name.lower() != texture_base.lower() else None
                ),
            )
            return job
        try:
            info = sbs_auto.parse_m_graph(graph_sbs, graph_name)
            graph_error = None
        except Exception as exc:
            info = {"inputs": {}}
            graph_error = str(exc)
        missing_files = [
            f"{slot}={path}" for slot, path in info["inputs"].items()
            if not Path(path).is_file()
        ]
        repairs = {}
        if not graph_error and info["inputs"].get("Base_Color") and missing_files:
            for slot, path in info["inputs"].items():
                if Path(path).is_file():
                    continue
                replacement = _packaged_resource_replacement(graph_sbs, path)
                if replacement:
                    repairs[slot] = replacement
            if len(repairs) == len(missing_files):
                missing_files = []
        graph_is_usable = not graph_error and bool(info["inputs"].get("Base_Color")) and not missing_files
        if not graph_is_usable:
            # Some legacy M_ graphs only happen to share the material name;
            # they are not Cluster_System management graphs at all.  Keep the
            # legacy graph untouched and insert a clean T_ graph from the
            # original SPM texture set.
            source = sbs_auto.inspect_graph_sources(graph_sbs, graph_name)
            direct_maps = [name for name in sbs_auto.RENDER_MAPS if name in source["outputs"]]
            if all(name in direct_maps for name in ("color", "normal", "extra", "height")):
                job.update(
                    mode="direct", graph=graph_name, sbs=graph_sbs,
                    direct_maps=list(sbs_auto.RENDER_MAPS),
                    normalize_cluster=True,
                    direct_fallback_opacity=_unique_graph_opacity_source(source),
                    rename_graph_to=(
                        texture_base
                        if graph_name.lower() != texture_base.lower() else None
                    ),
                )
                return job
            inputs = _inputs_from_prefixed_graph_resources(graph_sbs, graph_name)
            if inputs:
                existing_t = sbs_auto.find_m_graph_name(graph_sbs, texture_base)
                if existing_t and existing_t.lower() == texture_base.lower():
                    raise RuntimeError(f"{texture_base}: an unusable T_ graph already exists")
                job.update(
                    mode="insert", graph=texture_base, sbs=graph_sbs,
                    inputs=inputs,
                    notes=[f"rebuilt from SBS resources of {graph_name}"],
                )
                return job
            if not graph_name.lower().startswith("m_"):
                reason = graph_error or ("Base_Color resource is missing" if not info["inputs"].get("Base_Color") \
                    else "broken resources: " + "; ".join(missing_files)
                )
                raise RuntimeError(f"{graph_name}: {reason}")
            inputs, notes = sbs_auto.plan_inputs_from_row(row, require_alpha=False)
            job.update(mode="insert", graph=texture_base, sbs=graph_sbs,
                       inputs=inputs, notes=notes)
            return job
        rename_to = (
            texture_base
            if graph_name.lower() != texture_base.lower()
            else None
        )
        job.update(
            mode="render", graph=graph_name, sbs=graph_sbs,
            rename_graph_to=rename_to, repair_resources=repairs,
        )
    else:
        inputs, notes = sbs_auto.plan_inputs_from_row(row, require_alpha=False)
        job.update(
            mode="insert" if sbs_files else "render_only",
            graph=texture_base,
            sbs=sbs_files[0] if sbs_files else None,
            inputs=inputs,
            notes=notes,
        )
    return job


def preflight(plan, force=False):
    jobs = []
    skipped_complete = []
    errors = []
    for row in current_sk_owner_rows(plan):
        try:
            job = build_job(row)
            if force:
                job["force_cluster_recook"] = True
            job["size_log2"] = expected_job_size(job)
            expected_pixels = sbs_auto.size_log2_pixels(job["size_log2"])
            graph_needs_update = False
            if job.get("mode") == "render":
                graph_needs_update = sbs_auto.managed_graph_resolution_state(
                    job["sbs"], job["graph"])["needs_update"]
            source_needs_repair = job_needs_source_repair(job)
            structural_needs_update = bool(
                job.get("promote_authoring")
                or job.get("normalize_cluster")
                or job.get("rename_graph_to")
            )
            if (complete_output_set(row, expected_pixels=expected_pixels)
                    and not force and not graph_needs_update
                    and not source_needs_repair
                    and not structural_needs_update):
                skipped_complete.append(row)
                continue
            jobs.append(job)
        except Exception as exc:
            errors.append({
                "texture_base": row.get("texture_base"),
                "folder": row.get("folder"),
                "error": str(exc),
            })
    return jobs, skipped_complete, errors


def validate_spm_job_coverage(spm_jobs):
    """Prove every active material has a managed output or preserved source mapping."""
    errors = []
    for job in spm_jobs:
        try:
            build_spm_patch(job["spm"], job["materials"], require_outputs=False)
        except Exception as exc:
            errors.append({
                "texture_base": None,
                "folder": str(Path(job["spm"]).parent),
                "error": f"SPM mapping coverage: {exc}",
            })
    return errors


def _same_path(left, right):
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, ValueError):
        return str(left).lower() == str(right).lower()


def _merge_render_output_info(render_results):
    """Merge changed/unchanged partitions from direct and fallback renders."""
    merged = {
        "changed_files": [],
        "unchanged_files": [],
        "created_files": [],
        "backup_dirs": [],
    }
    seen = {key: set() for key in merged}

    def append_unique(key, value):
        path = Path(value)
        marker = str(path).lower()
        if marker not in seen[key]:
            seen[key].add(marker)
            merged[key].append(path)

    for info in render_results:
        for path in info.get("changed_files", info.get("files", [])):
            append_unique("changed_files", path)
        for path in info.get("unchanged_files", []):
            append_unique("unchanged_files", path)
        for path in info.get("created_files", []):
            append_unique("created_files", path)
        if info.get("backup_dir"):
            append_unique("backup_dirs", info["backup_dir"])

    changed_keys = {str(path).lower() for path in merged["changed_files"]}
    merged["unchanged_files"] = [
        path for path in merged["unchanged_files"]
        if str(path).lower() not in changed_keys
    ]
    merged["backup_dir"] = (
        merged["backup_dirs"][0] if merged["backup_dirs"] else None)
    return merged


def run_job(job, cfg, timeout):
    base = job["base"]
    texture_base = job["texture_base"]
    promotion_update = None
    if job.get("promote_authoring"):
        promotion = job["promote_authoring"]
        promotion_update = sbs_auto.promote_authoring_graph(
            job["sbs"], promotion["authoring"], promotion["managed"])
        job["graph"] = promotion["managed"]
        job["promote_authoring"] = None
    if job.get("rename_graph_to"):
        sbs_auto.rename_managed_graph(job["sbs"], job["graph"], job["rename_graph_to"])
        job["graph"] = job["rename_graph_to"]
        job["rename_graph_to"] = None
    if job["mode"] == "direct":
        normalization_update = None
        resolution_update = None
        source_repair_updates = []
        try:
            for slot, replacement in _direct_subsurface_repair(job).items():
                source_repair_updates.append(
                    sbs_auto.patch_m_graph_input_resource(
                        job["sbs"], job["graph"], slot, replacement))
            if job.get("normalize_cluster"):
                normalization_update = sbs_auto.normalize_graph_through_cluster(
                    job["sbs"], job["graph"],
                    normal_opengl=job["normal_opengl"], cfg=cfg)
                job["normalize_cluster"] = False
                job["direct_maps"] = list(sbs_auto.RENDER_MAPS)
            direct_maps = tuple(
                job.get("direct_maps", sbs_auto.RENDER_MAPS))
            desired_size = tuple(
                job.get("size_log2")
                or sbs_auto.graph_render_size_log2(
                    job["sbs"], job["graph"]))
            # Normalize the graph, bitmap readers, and final Cluster_System
            # wrapper before cooking.  A relative -2 wrapper override would
            # otherwise turn an explicitly requested 4K render into 1K.
            resolution_update = sbs_auto.set_managed_graph_resolution(
                job["sbs"], job["graph"], size_log2=desired_size)
            job["size_log2"] = tuple(resolution_update["size_log2"])
            rendered = sbs_auto.render_sbs_graph_maps(
                job["sbs"], job["graph"], texture_base, job["out_dir"],
                cache_root=REPORT_DIR / "_cooked_sbs_cache",
                cfg=cfg, maps=direct_maps,
                size_log2=job.get("size_log2"),
                timeout=timeout, return_info=True,
                normal_opengl=job["normal_opengl"],
                force_recook=bool(job.get("force_cluster_recook")))
            render_results = [rendered]
            actual_size = tuple(rendered["size_log2"])
            if actual_size != job["size_log2"]:
                raise RuntimeError(
                    f"direct graph ignored normalized output size: "
                    f"expected {job['size_log2']}, got {actual_size}")
            missing_roles = tuple(
                role for role in sbs_auto.RENDER_MAPS if role not in direct_maps)
            if missing_roles:
                try:
                    planned_inputs, _notes = sbs_auto.plan_inputs_from_row(
                        job["row"], require_alpha=False)
                except Exception:
                    planned_inputs = {}
                if "opacity" in missing_roles:
                    planned_inputs["Opacity"] = (
                        job.get("direct_fallback_opacity")
                        or planned_inputs.get("Opacity")
                        or sbs_auto.neutral_image("white")
                    )
                fallback_rendered = sbs_auto.render_maps(
                    texture_base,
                    planned_inputs,
                    sbs_auto.default_params(job["normal_opengl"]),
                    job["out_dir"], cfg=cfg, maps=missing_roles,
                    size_log2=job.get("size_log2"),
                    timeout=timeout,
                    return_info=True,
                )
                render_results.append(fallback_rendered)
            files = verify_complete_output_set(
                job["out_dir"], texture_base,
                expected_pixels=sbs_auto.size_log2_pixels(job["size_log2"]),
            )
            output_changes = _merge_render_output_info(render_results)
        except Exception:
            # Graph structure and the six rendered maps are one transaction.
            # render_sbs_graph_maps restores textures; restore the SBS too.
            if resolution_update and resolution_update.get("changed") \
                    and resolution_update.get("backup"):
                shutil.copy2(resolution_update["backup"], job["sbs"])
            if normalization_update and normalization_update.get("changed") \
                    and normalization_update.get("backup"):
                shutil.copy2(normalization_update["backup"], job["sbs"])
            if source_repair_updates:
                shutil.copy2(source_repair_updates[0]["backup"], job["sbs"])
            raise
        deleted = sbs_auto.delete_legacy_m_outputs(
            base, job["out_dir"], legacy_maps=job["row"].get("legacy_export_maps"))
        return {
            "texture_base": texture_base,
            "mode": "direct_sbs_graph",
            "files": [str(path) for path in files],
            "changed_files": [str(path) for path in output_changes["changed_files"]],
            "unchanged_files": [str(path) for path in output_changes["unchanged_files"]],
            "created_files": [str(path) for path in output_changes["created_files"]],
            "backup_dir": (
                str(output_changes["backup_dir"])
                if output_changes["backup_dir"] else None),
            "backup_dirs": [str(path) for path in output_changes["backup_dirs"]],
            "resolution_update": resolution_update,
            "promotion_update": promotion_update,
            "cluster_normalization": normalization_update,
            "source_repairs": source_repair_updates,
            "deleted_legacy": [str(path) for path in deleted],
        }
    if job["mode"] == "render":
        for slot, replacement in job.get("repair_resources", {}).items():
            sbs_auto.patch_m_graph_input_resource(
                job["sbs"], job["graph"], slot, replacement)
        info = sbs_auto.parse_m_graph(job["sbs"], job["graph"])
        inputs, params = info["inputs"], info["params"]
        try:
            planned, _notes = sbs_auto.plan_inputs_from_row(job["row"], require_alpha=False)
        except Exception:
            planned = {}
        if job["row"].get("canonical_source_provenance"):
            for slot, desired in planned.items():
                current = inputs.get(slot)
                if not current or _same_path(current, desired):
                    continue
                sbs_auto.patch_m_graph_input_resource(
                    job["sbs"], job["graph"], slot, desired)
                inputs[slot] = desired
        desired_subsurface = planned.get("Subsurface")
        current_subsurface = inputs.get("Subsurface")
        if desired_subsurface and (
                not current_subsurface
                or _is_neutral_input(current_subsurface)):
            sbs_auto.patch_m_graph_input_resource(
                job["sbs"], job["graph"], "Subsurface", desired_subsurface)
            inputs["Subsurface"] = desired_subsurface
        white = sbs_auto.neutral_image("white")
        current_amount = inputs.get("Subsurface_Amount")
        if current_amount and not _same_path(current_amount, white):
            sbs_auto.patch_m_graph_input_resource(
                job["sbs"], job["graph"], "Subsurface_Amount", white)
        inputs["Subsurface_Amount"] = white
    else:
        inputs = dict(job["inputs"])
        inputs["Subsurface_Amount"] = sbs_auto.neutral_image("white")
        params = sbs_auto.default_params(job["normal_opengl"])

    had_graph_ao = bool(inputs.get("Ambient_Occlusion"))
    inputs, hbao_path = sbs_auto.ensure_hbao_input(
        texture_base, inputs, job["out_dir"], cfg=cfg,
        size_log2=job.get("size_log2"), timeout=timeout)
    if hbao_path and job["mode"] == "render" and had_graph_ao:
        sbs_auto.patch_m_graph_input_resource(
            job["sbs"], job["graph"], "Ambient_Occlusion", hbao_path)
    if job["mode"] == "insert":
        sbs_auto.insert_m_graph(
            job["sbs"], texture_base, inputs,
            normal_opengl=job["normal_opengl"], cfg=cfg)
    resolution_update = None
    if job["mode"] == "render":
        job["size_log2"] = sbs_auto.render_size_log2(inputs)
        resolution_update = sbs_auto.set_managed_graph_resolution(
            job["sbs"], job["graph"], inputs=inputs)

    rendered = sbs_auto.render_maps(
        texture_base, inputs, params, job["out_dir"],
        cfg=cfg, size_log2=job.get("size_log2"), timeout=timeout, return_info=True)
    files = verify_complete_output_set(
        job["out_dir"], texture_base,
        expected_pixels=rendered.get("pixel_size"),
    )
    output_changes = _merge_render_output_info([rendered])
    deleted = sbs_auto.delete_legacy_m_outputs(
        base, job["out_dir"], legacy_maps=job["row"].get("legacy_export_maps"))
    return {
        "texture_base": texture_base,
        "files": [str(path) for path in files],
        "changed_files": [str(path) for path in output_changes["changed_files"]],
        "unchanged_files": [str(path) for path in output_changes["unchanged_files"]],
        "created_files": [str(path) for path in output_changes["created_files"]],
        "backup_dir": (
            str(output_changes["backup_dir"])
            if output_changes["backup_dir"] else None),
        "resolution_update": resolution_update,
        "deleted_legacy": [str(path) for path in deleted],
    }


def write_progress(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="rerender complete six-map sets too")
    parser.add_argument("--audit-json")
    parser.add_argument("--reuse-audit", action="store_true",
                        help="load --audit-json instead of rescanning all SBS files")
    parser.add_argument("--progress-json")
    args = parser.parse_args()

    cfg = load_config()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = Path(args.audit_json or REPORT_DIR / f"current_sk_texture_audit_{stamp}.json")
    progress_path = Path(args.progress_json or REPORT_DIR / "current_sk_texture_migration_progress.json")

    print("[preflight] loading audit", flush=True)
    if args.reuse_audit and audit_path.is_file():
        report = json.loads(audit_path.read_text(encoding="utf-8"))
    else:
        report = make_report(cfg)
        audit_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    plan = build_texture_plan_from_report(report, audit_path)
    # Capture the intended SPM mappings before M_ graphs are renamed.  This
    # also records which existing graphs have real Subsurface/Translucency.
    spm_jobs = jobs_from_texture_plan(plan)
    print("[preflight] checking render jobs", flush=True)
    jobs, complete, errors = preflight(plan, force=args.force)
    errors.extend(validate_spm_job_coverage(spm_jobs))
    summary = {
        "audit": str(audit_path),
        "current_sk_spms": len({
            spm.lower() for row in plan.get("items", [])
            for spm in row.get("material_spms") or []
            if Path(spm).name.lower().startswith("sk_")
        } | {
            str(row.get("spm", "")).lower()
            for row in plan.get("preserved_cluster_materials") or []
            if Path(row.get("spm", "")).name.lower().startswith("sk_")
        }),
        "render_sets": len(current_sk_owner_rows(plan)),
        "jobs": len(jobs),
        "already_complete": len(complete),
        "preflight_errors": errors,
    }
    if args.dry_run or errors:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if errors:
            raise SystemExit(2)
        return

    progress = {**summary, "started_at": datetime.now().isoformat(timespec="seconds"),
                "completed": [], "failed": []}
    write_progress(progress_path, progress)
    timeout = cfg.get("sbsrender_timeout", 1800)
    total = len(jobs)
    for index, job in enumerate(jobs, 1):
        print(f"[{index}/{total}] {job['texture_base']}", flush=True)
        try:
            result = run_job(job, cfg, timeout)
            progress["completed"].append(result)
            print(f"  OK {len(result['files'])} maps", flush=True)
        except Exception as exc:
            failure = {"texture_base": job["texture_base"], "error": str(exc)}
            progress["failed"].append(failure)
            print(f"  FAILED {exc}", flush=True)
        write_progress(progress_path, progress)

    if progress["failed"]:
        print(json.dumps({"rendered": len(progress["completed"]),
                          "failed": progress["failed"],
                          "spm_normalized": False}, indent=2, ensure_ascii=False))
        raise SystemExit(3)

    # Patch all current SK SPMs as one transaction.  A preflight failure
    # changes none of them.
    normalized = normalize_spms_transactionally(
        spm_jobs, backup_root=Path(cfg["tree_root"]) / "_spm_backups")
    cleanup = cleanup_preserved_cluster_outputs(plan)
    progress["finished_at"] = datetime.now().isoformat(timespec="seconds")
    progress["normalized"] = normalized
    progress["preserved_cluster_cleanup"] = cleanup
    write_progress(progress_path, progress)
    print(json.dumps({
        "rendered": len(progress["completed"]),
        "already_complete": len(complete),
        "normalized": normalized,
        "preserved_cluster_cleanup": cleanup,
        "progress": str(progress_path),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
