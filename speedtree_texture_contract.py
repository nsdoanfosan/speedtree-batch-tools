"""Shared SpeedTree 10.1 managed-texture readiness contract.

The contract deliberately binds an exported STMAT material to the managed
``T_<set>_<role>`` paths referenced by that material.  Material display names
are not used to guess a texture set: differently named materials such as Green
and Dead may intentionally reference the same managed set.

All public return values contain only JSON-serializable standard-library types.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path


REQUIRED_TEXTURE_ROLES = (
    "color",
    "normal",
    "extra",
    "height",
    "opacity",
    "subsurface",
)
TEXTURE_EXTENSIONS = (".tga", ".png", ".tif", ".tiff", ".exr")

_DUPLICATE_SUFFIX_RE = re.compile(r"\.\d{3}$")
_ROLE_SUFFIX_RE = re.compile(
    r"^(?P<base>T_.+)_(?P<role>" + "|".join(REQUIRED_TEXTURE_ROLES) + r")$",
    re.IGNORECASE,
)


def _absolute_path(value, relative_to=None):
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute() and relative_to is not None:
        path = Path(relative_to) / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path.absolute()


def _path_key(value):
    return os.path.normcase(str(_absolute_path(value))).casefold()


def _local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1]


def _attribute(node, name):
    expected = str(name).casefold()
    for key, value in node.attrib.items():
        if _local_name(key).casefold() == expected:
            return str(value or "").strip()
    return ""


def normalize_material_key(value):
    """Return a stable comparison key for a SpeedTree material name."""
    name = _DUPLICATE_SUFFIX_RE.sub("", str(value or "").strip())
    if name.casefold().endswith("_mat"):
        name = name[:-4]
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def normalize_texture_set_key(value):
    """Return a stable set key for a material, texture base, or texture path."""
    text = str(value or "").strip()
    if not text:
        return ""
    name = Path(text).stem if Path(text).suffix else Path(text).name
    parsed = parse_managed_texture_path(name)
    if parsed is not None:
        name = parsed["texture_base"]
    name = _DUPLICATE_SUFFIX_RE.sub("", name)
    if name[:2].casefold() in {"m_", "t_"}:
        name = name[2:]
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def parse_managed_texture_path(value):
    """Parse a managed ``T_<set>_<role>`` filename or path.

    ``None`` is returned for non-managed names.  Existence is intentionally not
    required because an STMAT reference can identify a set even when the
    referenced candidate directory is incomplete.
    """
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.suffix and path.suffix.casefold() not in TEXTURE_EXTENSIONS:
        return None
    match = _ROLE_SUFFIX_RE.match(path.stem)
    if match is None:
        return None
    texture_base = match.group("base")
    return {
        "path": text,
        "texture_base": texture_base,
        "set_key": normalize_texture_set_key(texture_base),
        "role": match.group("role").casefold(),
    }


def _canonical_texture_base(spelling_counts):
    """Pick one canonical spelling among case-variant texture base names.

    Windows filesystems treat differently cased spellings as one managed set,
    so the spelling used by the most role files wins; a single stray file such
    as ``T_leaf_x_atlas_02_extra`` cannot rename or split the set.
    """
    return min(spelling_counts, key=lambda name: (-spelling_counts[name], name))


def _coerce_directories(texture_dirs):
    if texture_dirs is None:
        return []
    if isinstance(texture_dirs, (str, os.PathLike)):
        texture_dirs = [texture_dirs]
    by_key = {}
    for value in texture_dirs:
        directory = _absolute_path(value)
        by_key.setdefault(_path_key(directory), directory)
    return [by_key[key] for key in sorted(by_key)]


def index_texture_sets(texture_dirs):
    """Index managed texture files in one or more directories.

    The result maps each normalized set key to a sorted list of directory-local
    candidates.  Keeping candidates separate prevents same-named sets in two
    folders from being silently merged.  Base names that differ only by case
    are one set; ``texture_bases`` reports one canonical spelling per set.
    """
    extension_rank = {
        extension: rank for rank, extension in enumerate(TEXTURE_EXTENSIONS)
    }
    indexed = {}
    for directory in _coerce_directories(texture_dirs):
        try:
            files = sorted(
                (path for path in directory.iterdir() if path.is_file()),
                key=lambda path: (path.name.casefold(), path.name),
            )
        except OSError:
            continue
        local_rows = {}
        for path in files:
            parsed = parse_managed_texture_path(path)
            if parsed is None:
                continue
            set_key = parsed["set_key"]
            role = parsed["role"]
            row = local_rows.setdefault(
                set_key,
                {
                    "directory": str(directory),
                    "set_key": set_key,
                    "texture_bases": {},
                    "files": {},
                    "file_ranks": {},
                },
            )
            base = parsed["texture_base"]
            spelling_counts = row["texture_bases"].setdefault(base.casefold(), {})
            spelling_counts[base] = spelling_counts.get(base, 0) + 1
            rank = extension_rank[path.suffix.casefold()]
            previous = row["file_ranks"].get(role)
            candidate_rank = (rank, path.name.casefold(), path.name)
            if previous is None or candidate_rank < previous:
                row["files"][role] = str(_absolute_path(path))
                row["file_ranks"][role] = candidate_rank

        for set_key, mutable in local_rows.items():
            bases = sorted(
                (
                    _canonical_texture_base(spelling_counts)
                    for spelling_counts in mutable["texture_bases"].values()
                ),
                key=lambda item: (item.casefold(), item),
            )
            files_by_role = {
                role: mutable["files"][role]
                for role in REQUIRED_TEXTURE_ROLES
                if role in mutable["files"]
            }
            missing_roles = [
                role for role in REQUIRED_TEXTURE_ROLES if role not in files_by_role
            ]
            row = {
                "directory": mutable["directory"],
                "set_key": set_key,
                "texture_base": bases[0] if len(bases) == 1 else "",
                "texture_bases": bases,
                "files": files_by_role,
                "missing_roles": missing_roles,
                "ambiguous_bases": len(bases) != 1,
                "complete": len(bases) == 1 and not missing_roles,
            }
            indexed.setdefault(set_key, []).append(row)

    result = {}
    for set_key in sorted(indexed):
        result[set_key] = sorted(
            indexed[set_key],
            key=lambda row: (
                _path_key(row["directory"]),
                row["texture_base"].casefold(),
                row["texture_base"],
            ),
        )
    return result


def read_stmat_material_sources(stmat_path):
    """Read material/map ``Source`` rows from a SpeedTree 10.1 STMAT file."""
    stmat = _absolute_path(stmat_path)
    result = {
        "status": "missing_stmat",
        "stmat": str(stmat),
        "materials": [],
        "error": "",
    }
    if not stmat.is_file():
        return result
    try:
        root = ET.parse(stmat).getroot()
    except (OSError, ET.ParseError) as exc:
        result["status"] = "invalid_stmat"
        result["error"] = str(exc)
        return result

    materials = []
    for node in root.iter():
        if _local_name(node.tag).casefold() != "material":
            continue
        material_name = _attribute(node, "Name")
        sources = []
        for map_node in node.iter():
            if map_node is node or _local_name(map_node.tag).casefold() != "map":
                continue
            source = _attribute(map_node, "Source")
            if not source:
                continue
            resolved = _absolute_path(source, stmat.parent)
            sources.append(
                {
                    "map": _attribute(map_node, "Name"),
                    "source": source,
                    "resolved_source": str(resolved),
                }
            )
        materials.append(
            {
                "material": material_name,
                "material_key": normalize_material_key(material_name),
                "material_index": len(materials),
                "sources": sources,
            }
        )
    result["materials"] = materials
    result["status"] = "ok" if materials else "empty_stmat"
    return result


def _flatten_index(texture_index):
    return [
        row
        for set_key in sorted(texture_index)
        for row in texture_index[set_key]
    ]


def _referenced_sets(material):
    referenced = {}
    for source in material["sources"]:
        parsed = parse_managed_texture_path(source["resolved_source"])
        if parsed is None:
            continue
        row = referenced.setdefault(
            parsed["set_key"],
            {"roles": set(), "directories": {}, "texture_bases": set()},
        )
        row["roles"].add(parsed["role"])
        directory = str(Path(source["resolved_source"]).parent)
        directory_key = _path_key(directory)
        directory_row = row["directories"].setdefault(
            directory_key, {"directory": directory, "roles": set()}
        )
        directory_row["roles"].add(parsed["role"])
        row["texture_bases"].add(parsed["texture_base"])
    return referenced


def _required_roles(required_roles):
    values = REQUIRED_TEXTURE_ROLES if required_roles is None else required_roles
    roles = []
    seen = set()
    for value in values:
        role = str(value or "").strip().casefold()
        if role and role not in seen:
            seen.add(role)
            roles.append(role)
    return roles


def _directory_preference(texture_dirs):
    if texture_dirs is None:
        return {}
    if isinstance(texture_dirs, (str, os.PathLike)):
        texture_dirs = [texture_dirs]
    result = {}
    for value in texture_dirs:
        key = _path_key(value)
        if key not in result:
            result[key] = len(result)
    return result


def _texture_base_name(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = parse_managed_texture_path(text)
    return parsed["texture_base"] if parsed is not None else Path(text).name


def _resolve_indexed_texture_set(
    texture_index,
    texture_base,
    required_roles=None,
    directory_preference=None,
):
    roles = _required_roles(required_roles)
    requested_base = _texture_base_name(texture_base)
    set_key = normalize_texture_set_key(requested_base)
    empty = {
        "status": "invalid_texture_base",
        "set_key": set_key,
        "texture_base": requested_base,
        "texture_dir": "",
        "files": {},
        "missing_roles": list(roles),
    }
    # This API resolves a literal managed T_ base.  Refusing M_/material names
    # prevents Green/Yellow or other authoring labels from becoming implicit
    # texture/instance rules.
    if not requested_base.casefold().startswith("t_") or not set_key:
        return empty

    candidates = []
    for candidate in texture_index.get(set_key, []):
        exact_bases = [
            base for base in candidate["texture_bases"]
            if base.casefold() == requested_base.casefold()
        ]
        if not exact_bases:
            continue
        missing_roles = [
            role for role in roles if role not in candidate["files"]
        ]
        candidates.append((candidate, missing_roles))
    if not candidates:
        empty["status"] = "missing_texture_set"
        return empty

    preference = directory_preference or {}

    def candidate_rank(row):
        candidate, missing_roles = row
        directory_key = _path_key(candidate["directory"])
        return (
            len(missing_roles),
            candidate["ambiguous_bases"],
            preference.get(directory_key, len(preference)),
            directory_key,
            candidate["texture_base"].casefold(),
            candidate["texture_base"],
        )

    candidate, missing_roles = min(candidates, key=candidate_rank)
    complete = not missing_roles and not candidate["ambiguous_bases"]
    return {
        "status": "ok" if complete else "incomplete_texture_set",
        "set_key": set_key,
        # Prefer the on-disk canonical spelling so a differently cased STMAT
        # reference cannot leak into downstream asset naming.
        "texture_base": candidate["texture_base"] or requested_base,
        "texture_dir": candidate["directory"],
        "files": {
            role: candidate["files"][role]
            for role in roles
            if role in candidate["files"]
        },
        "missing_roles": list(missing_roles),
    }


def resolve_texture_set(texture_dirs, texture_base, required_roles=None):
    """Resolve one literal ``T_`` texture base across candidate directories.

    A directory containing every configured role always wins over an earlier
    partial directory.  The selected files and missing roles use the same
    deterministic lower-case role keys as :func:`resolve_texture_bindings`.
    Material names are intentionally rejected; callers must provide the actual
    managed texture base.
    """
    texture_index = index_texture_sets(texture_dirs)
    return _resolve_indexed_texture_set(
        texture_index,
        texture_base,
        required_roles,
        _directory_preference(texture_dirs),
    )


def _missing_binding(material, reason, set_keys, missing_roles):
    return {
        "material": material["material"],
        "material_key": material["material_key"],
        "material_index": material["material_index"],
        "status": reason,
        "set_key": "",
        "texture_base": "",
        "texture_dir": "",
        "stmat_roles": [],
        "files": {},
        "referenced_set_keys": list(set_keys),
        "missing_roles": list(missing_roles),
    }


def resolve_texture_bindings(stmat_path, texture_dirs=None):
    """Resolve every STMAT material to one complete managed texture set.

    Source paths supply the authoritative set keys.  ``texture_dirs`` may add
    alternate managed-output locations; a complete candidate is selected even
    if an earlier/directly referenced candidate is partial.  Bindings remain
    one row per STMAT material, so two slots sharing a set are never collapsed.
    """
    parsed_stmat = read_stmat_material_sources(stmat_path)
    result = {
        "status": parsed_stmat["status"],
        "stmat": parsed_stmat["stmat"],
        "required_roles": list(REQUIRED_TEXTURE_ROLES),
        "sets": [],
        "bindings": [],
        "missing": [],
    }
    if parsed_stmat.get("error"):
        result["error"] = parsed_stmat["error"]
    if parsed_stmat["status"] not in {"ok", "empty_stmat"}:
        return result

    source_directories = []
    for material in parsed_stmat["materials"]:
        for source in material["sources"]:
            if parse_managed_texture_path(source["resolved_source"]) is not None:
                source_directories.append(Path(source["resolved_source"]).parent)
    source_directories.extend(_coerce_directories(texture_dirs))
    texture_index = index_texture_sets(source_directories)
    result["sets"] = _flatten_index(texture_index)

    for material in parsed_stmat["materials"]:
        references = _referenced_sets(material)
        set_keys = sorted(references)
        if not set_keys:
            binding = _missing_binding(
                material,
                "not_managed",
                [],
                [],
            )
            result["bindings"].append(binding)
            continue

        complete_resolutions = []
        partial_resolutions = []
        for set_key in set_keys:
            reference = references[set_key]
            preferred_directories = [
                row["directory"] for row in reference["directories"].values()
            ] + source_directories
            seen_bases = set()
            for texture_base in sorted(
                reference["texture_bases"], key=lambda value: (value.casefold(), value)
            ):
                base_key = texture_base.casefold()
                if base_key in seen_bases:
                    continue
                seen_bases.add(base_key)
                resolution = _resolve_indexed_texture_set(
                    texture_index,
                    texture_base,
                    REQUIRED_TEXTURE_ROLES,
                    _directory_preference(preferred_directories),
                )
                if resolution["status"] == "ok":
                    complete_resolutions.append((set_key, resolution))
                else:
                    partial_resolutions.append(resolution)

        if len(complete_resolutions) == 1:
            set_key, resolution = complete_resolutions[0]
            reference = references[set_key]
            binding = {
                "material": material["material"],
                "material_key": material["material_key"],
                "material_index": material["material_index"],
                "status": "ok",
                "set_key": set_key,
                "texture_base": resolution["texture_base"],
                "texture_dir": resolution["texture_dir"],
                "stmat_roles": sorted(reference["roles"]),
                "files": dict(resolution["files"]),
                "referenced_set_keys": set_keys,
                "missing_roles": [],
            }
            result["bindings"].append(binding)
            continue

        if len(complete_resolutions) > 1:
            reason = "ambiguous_complete_sets"
            missing_roles = []
        else:
            reason = "incomplete_texture_set"
            if partial_resolutions:
                best_partial = min(
                    partial_resolutions,
                    key=lambda resolution: (
                        len(resolution["missing_roles"]),
                        _path_key(resolution["texture_dir"])
                        if resolution["texture_dir"] else "",
                    ),
                )
                missing_roles = list(best_partial["missing_roles"])
            else:
                missing_roles = list(REQUIRED_TEXTURE_ROLES)

        binding = _missing_binding(material, reason, set_keys, missing_roles)
        result["bindings"].append(binding)
        result["missing"].append(
            {
                "material": material["material"],
                "material_index": material["material_index"],
                "reason": reason,
                "set_keys": set_keys,
                "missing_roles": missing_roles,
            }
        )

    managed_count = sum(
        1 for binding in result["bindings"]
        if binding["status"] != "not_managed"
    )
    result["managed_material_count"] = managed_count
    result["status"] = (
        "empty_stmat"
        if parsed_stmat["status"] == "empty_stmat"
        else "not_applicable"
        if not managed_count
        else "incomplete"
        if result["missing"]
        else "ok"
    )
    return result


__all__ = [
    "normalize_material_key",
    "normalize_texture_set_key",
    "parse_managed_texture_path",
    "index_texture_sets",
    "read_stmat_material_sources",
    "resolve_texture_set",
    "resolve_texture_bindings",
]
