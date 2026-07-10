"""SPM bone calibration + material prefix for the SK skeletal-vegetation pipeline.

Bone rule (checklist item 1, size-aware):
  SpeedTree's Relative bone style already scales bone count with spline length,
  but a single Relative value applied to every plant starves tiny weeds (rounds
  to 0 bones) and explodes big trees. So instead of one magic number we
  CALIBRATE per generator against ground truth:

  1. probe   — temporarily set every target generator to Absolute/1 and export
               the hierarchy XML once. Absolute/1 = exactly one bone per
               branch, so the per-generator bone count IS the branch count.
  2. target  — desired bones per generator = branches x target_bones_per_branch
               (acceptance window: branches x [min_per_branch, max_per_branch]).
  3. solve   — set Relative style and scale the value proportionally
               (bones scale ~linearly with the value) until the exported bone
               count lands inside the window. Generators already inside the
               window keep their current value untouched.

  Generators saved as Absolute with Bones=0 are treated as an intentional
  "no bones here" and skipped.

Materials (checklist item 2): every Assets/Material_v8 Name gets the M_ prefix
(attribute-only rename, verified safe — FBX picks up the new name, texture
paths untouched).

The patch is string-level and format-preserving. A timestamped backup goes to
<spm dir>/_spm_backups/ before the first write, and is restored automatically
if calibration dies midway.

Standalone:  python spm_audit.py <file.spm> [--dry-run] [--report out.json]
"""
import argparse
import gzip
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from sk_common import load_config

GEN_RE = re.compile(r"<Generator\b[^>]*>.*?</Generator>", re.DOTALL)
GEN_TYPE_RE = re.compile(r'<Generator\b[^>]*Type="([^"]+)"')
FIRST_NAME_RE = re.compile(r"<Name>([^<]*)</Name>")
MATERIAL_RE = re.compile(r'(<Material_v8\b[^>]*?Name=")([^"]*)(")')

BACKUP_SUBDIR = "_spm_backups"


def read_spm(path):
    return gzip.open(path, "rb").read().decode("utf-8")


def write_spm(path, text):
    with gzip.open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))


def prop_value(block, prop_name):
    m = re.search(
        r"<Name>" + re.escape(prop_name) + r"</Name>\s*<Value>([^<]*)</Value>",
        block,
        re.DOTALL,
    )
    return m.group(1) if m else None


def set_prop_value(block, prop_name, new_value):
    pat = re.compile(
        r"(<Name>" + re.escape(prop_name) + r"</Name>\s*<Value>)[^<]*(</Value>)",
        re.DOTALL,
    )
    return pat.sub(lambda m: m.group(1) + format_value(new_value) + m.group(2), block, count=1)


def format_value(value):
    value = float(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def audit_spm(path):
    text = read_spm(path)
    generators = []
    for m in GEN_RE.finditer(text):
        block = m.group(0)
        tm = GEN_TYPE_RE.match(block)
        gtype = tm.group(1) if tm else "?"
        nm = FIRST_NAME_RE.search(block)
        gname = nm.group(1) if nm else "?"
        if gtype != "Branch":
            continue
        style = prop_value(block, "Physics:Bone style")
        bones = prop_value(block, "Physics:Bones")
        hidden_match = re.search(r"<Hidden>([^<]*)</Hidden>", block)
        hidden = bool(hidden_match and hidden_match.group(1).strip().lower() in {"1", "true", "yes"})
        generators.append(
            {
                "name": gname,
                "type": gtype,
                "style": float(style) if style is not None else None,
                "bones": float(bones) if bones is not None else None,
                "hidden": hidden,
            }
        )
    materials = [
        {"name": m.group(2), "needs_prefix": not m.group(2).startswith("M_")}
        for m in MATERIAL_RE.finditer(text)
    ]
    return {"path": str(path), "generators": generators, "materials": materials}


def sk_readiness(audit):
    """Classify whether an SPM may enter the skeletal batch pipeline.

    BranchMesh-only assets legitimately have no SpeedTree bone generator and
    are handled later by Blender's rigid one-bone fallback.  A visible Branch
    generator whose bone properties are all Absolute/0 is different: the asset
    has not been authored for the SK path yet, so silently manufacturing a
    skeleton would hide bad source data.
    """
    visible = [
        gen
        for gen in audit.get("generators", [])
        if not gen.get("hidden", False)
        and gen.get("style") is not None
        and gen.get("bones") is not None
    ]
    enabled = [
        gen
        for gen in visible
        if not (gen["style"] == 0.0 and gen["bones"] == 0.0)
    ]
    disabled = [gen for gen in visible if gen not in enabled]
    details = [
        {
            "generator": gen.get("name", "?"),
            "style": gen.get("style"),
            "bones": gen.get("bones"),
        }
        for gen in disabled
    ]
    if visible and not enabled:
        settings = ", ".join(
            f"{item['generator']}(style={item['style']:g}, bones={item['bones']:g})"
            for item in details
        )
        error = (
            "SPM is not SK-ready: every visible Branch bone generator is Absolute/0 "
            f"(bones disabled): {settings}. Configure bones on at least one visible "
            "Branch generator before running the SK batch."
        )
        return {
            "ready": False,
            "mode": "all_bones_disabled",
            "visible_branch_generators": len(visible),
            "enabled_branch_generators": 0,
            "disabled_generators": details,
            "error": error,
        }
    return {
        "ready": True,
        "mode": "speedtree_bones" if enabled else "no_branch_generators",
        "visible_branch_generators": len(visible),
        "enabled_branch_generators": len(enabled),
        "disabled_generators": details,
    }


def plan_material_renames(audit):
    renames = {}
    skipped = []
    existing = {m["name"] for m in audit["materials"]}
    for mat in audit["materials"]:
        if not mat["needs_prefix"]:
            continue
        target = "M_" + mat["name"]
        if target in existing:
            skipped.append({"material": mat["name"], "reason": f"rename target exists: {target}"})
            continue
        renames[mat["name"]] = target
    return renames, skipped


def apply_branch_values(text, indices, style, bones):
    """Set style/bones on the Branch generators at the given positional indices.

    Indices are counted over Branch-type generators in document order (the same
    order audit_spm walks them). This sidesteps duplicate generator NAMES —
    elm_03 has three 'Bifurcating' and three 'Branch 4' — which a name-keyed
    patch would collide on.
    """
    index_set = set(indices)
    out = []
    pos = 0
    branch_i = -1
    for m in GEN_RE.finditer(text):
        block = m.group(0)
        tm = GEN_TYPE_RE.match(block)
        if tm and tm.group(1) == "Branch":
            branch_i += 1
            if branch_i in index_set:
                block = set_prop_value(block, "Physics:Bone style", style)
                block = set_prop_value(block, "Physics:Bones", bones)
        out.append(text[pos:m.start()])
        out.append(block)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def apply_material_renames(text, renames):
    applied = []

    def sub(m):
        old = m.group(2)
        new = renames.get(old)
        if not new:
            return m.group(0)
        applied.append((old, new))
        return m.group(1) + new + m.group(3)

    return MATERIAL_RE.sub(sub, text), applied


def target_generators_have_base_links(text, target_indices):
    """Whether selected Branch generators actually produce Branch<-Base nodes.

    Base/BaseRef nodes elsewhere in the SPM are irrelevant when their Branch
    generator has bones disabled (the birch failures had hundreds of those).
    """
    root = ET.fromstring(text)
    branch_guids = []
    for gen in root.findall(".//Generator"):
        if gen.attrib.get("Type") != "Branch":
            continue
        guid = gen.findtext("GUID")
        branch_guids.append(guid)
    target_guids = {
        branch_guids[index]
        for index in target_indices
        if 0 <= index < len(branch_guids) and branch_guids[index]
    }
    if not target_guids:
        return False
    node_types = {}
    nodes = []
    for node in root.findall(".//Node"):
        guid = node.findtext("GUID")
        if guid:
            node_types[guid] = node.attrib.get("Type")
        nodes.append(node)
    return any(
        node.attrib.get("Type") == "Branch"
        and node.findtext("GeneratorGUID") in target_guids
        and node_types.get(node.findtext("ParentGUID")) == "Base"
        for node in nodes
    )


def repair_external_frond_material_references(text):
    """Fallback Frond generators from external atlas cutouts to embedded ones.

    Some atlas-edited SPMs point procedural Frond generators at materials whose
    cutout meshes are external single-plate FBXs. SpeedTree accepts the SPM but
    exports an armature-only FBX. Keep the added material asset intact and only
    restore the generator reference to the closest embedded-cutout material.
    """
    root = ET.fromstring(text)
    meshes = {}
    for mesh in root.findall(".//Mesh"):
        mesh_id = mesh.get("ID")
        if not mesh_id:
            continue
        meshes[mesh_id] = (mesh.findtext("Embedded") or "false").strip().lower() in {"1", "true", "yes"}

    materials = {}
    for material in root.findall(".//Material_v8"):
        material_id = material.get("ID")
        if material_id:
            materials[material_id] = {
                "id": material_id,
                "name": material.get("Name", ""),
                "mesh_id": material.findtext("CutoutMeshID") or "",
            }

    safe_materials = [
        material
        for material in materials.values()
        if material["mesh_id"] in {"", "-1"} or meshes.get(material["mesh_id"], False)
    ]
    if not safe_materials:
        return text, []

    def name_tokens(name):
        return {
            token
            for token in re.split(r"[^a-z0-9]+", (name or "").lower())
            if token and token not in {"m", "atlas"}
        }

    replacements = {}
    for match in re.finditer(
        r"<Name>(Material:Frond:[^<]+:Material)</Name>\s*<Value>([^<]+)</Value>",
        text,
    ):
        property_name, material_id = match.groups()
        material = materials.get(material_id)
        if (
            not material
            or material["mesh_id"] in {"", "-1"}
            or meshes.get(material["mesh_id"], False)
        ):
            continue
        bad_tokens = name_tokens(material["name"])
        fallback = max(
            safe_materials,
            key=lambda candidate: (
                len(bad_tokens & name_tokens(candidate["name"])),
                -len(bad_tokens ^ name_tokens(candidate["name"])),
                candidate["name"],
            ),
        )
        replacements[material_id] = fallback["id"]

    if not replacements:
        return text, []

    changes = []

    def replace(match):
        old_id = match.group(2)
        new_id = replacements.get(old_id)
        if not new_id:
            return match.group(0)
        changes.append(
            {
                "property": match.group(1),
                "old_material_id": old_id,
                "old_material": materials[old_id]["name"],
                "new_material_id": new_id,
                "new_material": materials[new_id]["name"],
                "reason": "external Frond cutout produced geometry-less FBX",
            }
        )
        return f"<Name>{match.group(1)}</Name>\n<Value>{new_id}</Value>"

    patched = re.sub(
        r"<Name>(Material:Frond:[^<]+:Material)</Name>\s*<Value>([^<]+)</Value>",
        replace,
        text,
    )
    return patched, changes


def backup_spm(path):
    # Keep backups out of the working folder so the SPM list stays clean:
    # <spm dir>/_spm_backups/<stem>.skbatch_backup_<ts>.spm
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(path).parent / BACKUP_SUBDIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{Path(path).stem}.skbatch_backup_{ts}.spm"
    shutil.copy2(path, backup)
    return str(backup)


def export_verify_xml(spm_path, cfg, out_path):
    cmd = [
        cfg["speedtree_exe"],
        str(spm_path),
        "-export_options",
        cfg["xml_ini"],
        "-export",
        str(out_path),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(Path(spm_path).parent),
        capture_output=True,
        text=True,
        timeout=cfg.get("spm_verify_timeout", 900),
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    if result.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"SpeedTree XML verify export failed ({result.returncode}): {result.stderr[-500:]}")
    return out_path


def export_verify_fbx_geometry(spm_path, cfg, out_path):
    cmd = [
        cfg["speedtree_exe"],
        str(spm_path),
        "-export_options",
        cfg["fbx_ini"],
        "-export",
        str(out_path),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(Path(spm_path).parent),
        capture_output=True,
        text=True,
        timeout=cfg.get("spm_verify_timeout", 900),
        creationflags=0x08000000,
    )
    path = Path(out_path)
    if result.returncode != 0 or not path.exists():
        raise RuntimeError(f"SpeedTree FBX verify export failed ({result.returncode}): {result.stderr[-500:]}")
    # SpeedTree writes binary FBX. A real mesh contains the FBX property name
    # "Vertices" in clear text; armature-only/container-only exports do not.
    return b"Vertices" in path.read_bytes()


def bone_counts_from_xml(xml_path):
    root = ET.parse(xml_path).getroot()
    counts = {}
    for bone in root.findall(".//Bone"):
        gen = bone.get("Generator") or "?"
        counts[gen] = counts.get(gen, 0) + 1
    return counts


def calibrate_bones(spm_path, cfg, log=print):
    """Solve ONE Relative value (shared by every target Branch generator) so the
    tree's TOTAL bone count lands near a size-aware budget.

    Why total-budget instead of per-branch average:
      A big tree has thousands of tiny twigs. "N bones per branch" forces bones
      onto every twig and explodes (elm_03: 15,234 branches x 3 = 45k+ target ->
      80k+ bones). But SpeedTree's Relative style already gives short splines
      FEWER bones (0 when the value is low enough), so we instead pick a single
      value that hits a total budget = min(branches x per_branch, max_total).
      Small plants stay near per_branch; big trees hit the cap, which naturally
      leaves twigs at 0-1 bone and keeps bones on the big/long branches.
    This also dodges duplicate generator names (elm_03) and honours Size scalar
    automatically, because Relative bones track real spline length.

    Returns (generators_report, rounds, total_bones, meta, warnings, skipped, changed).
    """
    per_branch = float(cfg.get("target_bones_per_branch", 2.0))
    max_total = float(cfg.get("max_total_bones", 1500))
    lo_frac = float(cfg.get("total_window_low", 0.6))
    hi_frac = float(cfg.get("total_window_high", 1.5))
    value_cap = float(cfg.get("value_cap", 64.0))
    value_floor = float(cfg.get("value_floor", 0.02))
    seed = float(cfg.get("seed_relative_value", 0.5))
    max_rounds = int(cfg.get("max_calibration_rounds", 4))

    audit = audit_spm(spm_path)
    branch_gens = audit["generators"]  # Branch-type only, document order
    target_indices = []
    zero_bone_indices = []
    skipped = []
    for i, gen in enumerate(branch_gens):
        if gen.get("hidden", False):
            skipped.append({"generator": gen["name"], "reason": "hidden generator"})
            continue
        if gen["style"] is None or gen["bones"] is None:
            skipped.append({"generator": gen["name"], "reason": "no bone properties"})
            continue
        if gen["style"] == 0.0 and gen["bones"] == 0.0:
            if not gen.get("hidden", False):
                zero_bone_indices.append(i)
            skipped.append({"generator": gen["name"], "reason": "Absolute+0 = no-bones"})
            continue
        target_indices.append(i)

    rounds = []
    warnings = []
    activated_zero_bone_generators = False
    if not target_indices and zero_bone_indices:
        raise RuntimeError(sk_readiness(audit)["error"])
    if not target_indices:
        warnings.append("no bone-capable Branch generators; Blender will use a rigid one-bone fallback")
        return {}, rounds, None, {"mode": "no_branch_generators"}, warnings, skipped, False

    original_text = read_spm(spm_path)
    has_base_links = target_generators_have_base_links(original_text, target_indices)
    with tempfile.TemporaryDirectory(prefix="skbatch_cal_") as tmp:
        xml_out = Path(tmp) / f"{Path(spm_path).stem}_cal.xml"

        # -- probe: Absolute/1 == exactly 1 bone per branch -> total branch count
        write_spm(spm_path, apply_branch_values(original_text, target_indices, 0.0, 1.0))
        probe_counts = bone_counts_from_xml(export_verify_xml(spm_path, cfg, xml_out))
        total_branches = sum(probe_counts.values())
        rounds.append({"phase": "probe(absolute/1)", "total_branches": total_branches})
        log(f"  [probe] total branches = {total_branches}")
        if total_branches == 0:
            write_spm(spm_path, original_text)
            warnings.append("probe produced no branches (all hidden/empty?)")
            return {}, rounds, 0, None, warnings, skipped, False

        target_total = min(total_branches * per_branch, max_total)
        capped = total_branches * per_branch > max_total
        lo, hi = target_total * lo_frac, target_total * hi_frac

        # -- solve a single Relative value to hit target_total. Relative bones
        # grow super-linearly with the value, so damp the proportional step.
        r = seed
        final_counts = {}
        total = 0
        for round_index in range(max_rounds):
            r = max(value_floor, min(value_cap, r))
            write_spm(spm_path, apply_branch_values(original_text, target_indices, 1.0, r))
            final_counts = bone_counts_from_xml(export_verify_xml(spm_path, cfg, xml_out))
            total = sum(final_counts.values())
            rounds.append({"phase": f"relative round {round_index + 1}", "value": round(r, 4), "total_bones": total})
            log(f"  [calibrate] r={r:.3f} -> {total} bones (target {target_total:.0f}, window {lo:.0f}-{hi:.0f})")
            if lo <= total <= hi:
                break
            if total == 0:
                r *= 3
                continue
            new_r = max(value_floor, min(value_cap, r * (target_total / total) ** 0.6))
            if abs(new_r - r) < 1e-4:
                break  # hit floor/cap; can't get closer
            r = new_r

        final_r = max(value_floor, min(value_cap, r))

        calibration_mode = "base_ref_relative" if has_base_links else "root_only_relative"
        absolute_fallback = None
        material_reference_fallbacks = []
        if not has_base_links:
            fbx_out = Path(tmp) / f"{Path(spm_path).stem}_geometry_check.fbx"
            if not export_verify_fbx_geometry(spm_path, cfg, fbx_out):
                # Certain root/frond assets export an armature but silently drop
                # every mesh after Relative bone calibration. Absolute/1 is the
                # known-good SpeedTree representation for those assets.
                fallback_text = apply_branch_values(original_text, target_indices, 0.0, 1.0)
                write_spm(spm_path, fallback_text)
                final_counts = bone_counts_from_xml(export_verify_xml(spm_path, cfg, xml_out))
                total = sum(final_counts.values())
                if fbx_out.exists():
                    fbx_out.unlink()
                if not export_verify_fbx_geometry(spm_path, cfg, fbx_out):
                    fallback_text, material_reference_fallbacks = repair_external_frond_material_references(
                        fallback_text
                    )
                    if material_reference_fallbacks:
                        write_spm(spm_path, fallback_text)
                        if fbx_out.exists():
                            fbx_out.unlink()
                    if not material_reference_fallbacks or not export_verify_fbx_geometry(spm_path, cfg, fbx_out):
                        raise RuntimeError("SpeedTree FBX contains no mesh geometry after bone/material fallbacks")
                absolute_fallback = 1
                calibration_mode = (
                    "root_only_absolute_material_fallback"
                    if material_reference_fallbacks
                    else "root_only_absolute_fallback"
                )
                rounds.append(
                    {"phase": "geometry fallback", "bones_per_branch": 1, "total_bones": total}
                )
                warnings.append(
                    "Relative calibration produced an armature-only FBX; switched root-only generators to Absolute/1"
                )
                if material_reference_fallbacks:
                    warnings.append(
                        "external atlas cutouts produced no Frond geometry; restored embedded material references"
                    )
                log(f"  [geometry fallback] Absolute/1 -> {total} bones with valid FBX geometry")

    # generator report: names may repeat; XML aggregates bones by generator name
    generators_report = dict(sorted(final_counts.items(), key=lambda kv: -kv[1]))
    if not (lo <= total <= hi):
        if absolute_fallback is not None:
            warnings.append(
                f"total bones {total} outside target window {lo:.0f}-{hi:.0f}; "
                "kept Absolute/1 because it is the geometry-safe fallback"
            )
        else:
            warnings.append(
                f"total bones {total} outside target window {lo:.0f}-{hi:.0f} "
                f"(relative value at floor/cap {final_r:.3f})"
            )
    meta = {
        "mode": calibration_mode,
        "total_branches": total_branches,
        "target_total": round(target_total),
        "relative_value": round(final_r, 4),
        "capped": capped,
        "activated_zero_bone_generators": activated_zero_bone_generators,
    }
    if absolute_fallback is not None:
        meta["absolute_bones_per_branch"] = absolute_fallback
    if material_reference_fallbacks:
        meta["material_reference_fallbacks"] = material_reference_fallbacks
    return generators_report, rounds, total, meta, warnings, skipped, True


def process_spm(spm_path, cfg, log=print, dry_run=False):
    """Material prefix + bone calibration with backup/restore. Returns report."""
    spm_path = Path(spm_path)
    report = {
        "spm": str(spm_path),
        "status": "unchanged",
        "backup": None,
        "material_renames": [],
        "generators": {},
        "rounds": [],
        "total_bones": None,
        "skipped": [],
        "warnings": [],
    }

    audit = audit_spm(spm_path)
    readiness = sk_readiness(audit)
    report["sk_readiness"] = readiness
    renames, mat_skipped = plan_material_renames(audit)
    report["skipped"].extend(mat_skipped)

    if not readiness["ready"]:
        report["status"] = "not-sk-ready"
        report["error"] = readiness["error"]
        report["calibration"] = {
            "mode": readiness["mode"],
            "disabled_generators": readiness["disabled_generators"],
        }
        report["warnings"].append(
            "Skipped without modifying the SPM because its visible Branch generators have bones disabled."
        )
        return report

    if dry_run:
        report["status"] = "dry-run"
        report["planned_materials"] = renames
        report["current_generators"] = audit["generators"]
        return report

    backup = None
    if cfg.get("backup_spm", True):
        backup = backup_spm(spm_path)
        report["backup"] = backup

    try:
        if cfg.get("rename_materials", True) and renames:
            text = read_spm(spm_path)
            text, applied = apply_material_renames(text, renames)
            if applied:
                write_spm(spm_path, text)
                report["material_renames"] = applied

        generators, rounds, total, meta, warnings, skipped, changed = calibrate_bones(spm_path, cfg, log=log)
        report["generators"] = generators
        report["rounds"] = rounds
        report["total_bones"] = total
        report["calibration"] = meta
        report["warnings"].extend(warnings)
        report["skipped"].extend(skipped)
        report["status"] = "calibrated" if (changed or report["material_renames"]) else "already-ok"
    except Exception:
        if backup:
            shutil.copy2(backup, spm_path)
            report["warnings"].append("calibration failed; SPM restored from backup")
        report["status"] = "failed"
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spm", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    cfg = load_config()
    reports = []
    for spm in args.spm:
        print(f"== {spm}")
        try:
            rep = process_spm(spm, cfg, dry_run=args.dry_run)
        except Exception as exc:
            print(f"FAILED: {exc}")
            rep = {"spm": spm, "status": "failed", "error": str(exc)}
        reports.append(rep)
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    if args.report:
        Path(args.report).write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    if any(r.get("status") in {"failed", "not-sk-ready"} for r in reports):
        sys.exit(1)


if __name__ == "__main__":
    main()
