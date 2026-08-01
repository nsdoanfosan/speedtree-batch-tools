"""Build deterministic, structure-preserving sanitized real-pair fixtures.

The input SPM files may be plain XML or gzip containers.  Sanitization is a
raw-text substitution pass: it never parses and reserializes XML, so element
order, duplicate counts, namespaces, numeric spellings, whitespace, text, and
tail placement remain byte-for-byte unchanged outside the explicitly replaced
identity/path tokens.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "pcg_st9_texture_batch"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from speedtree_pipeline_contract import canonical_generator_guid  # noqa: E402
from stale_node_table_recovery import (  # noqa: E402
    _authoring_graph_core_projection,
    _default_modeler_atlas_maker,
    _default_modeler_material_map,
    _v4_default_modeler_atlas_maker,
    _v4_default_material_map,
)


WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/])[^<>\"\r\n]*"
)
UUID_RE = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
SCOPE_RE = re.compile(
    r'(?P<prefix>"scope"\s*:\s*")(?P<value>[0-9a-fA-F]{32})'
    r'(?P<suffix>")'
)
GUID_ELEMENT_RE = re.compile(
    r"(?P<open><(?:GUID|SourceGUID|TargetGUID)>)(?P<value>[^<]*)"
    r"(?P<close></(?:GUID|SourceGUID|TargetGUID)>)"
)
PRIVATE_USER_TOKEN_RE = re.compile(r"(?i)PARK")
TRUNCATED_GUID_RE = re.compile(r"^[A-Za-z0-9+/]{21}==$")
PADDED_GUID_RE = re.compile(r"^[A-Za-z0-9+/]{21}A==$")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_spm(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    decoded = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    return raw, decoded.decode("utf-8")


class Sanitizer:
    def __init__(self) -> None:
        self.paths: dict[str, str] = {}
        self.uuids: dict[str, str] = {}
        self.scopes: dict[str, str] = {}
        self.generator_guids: dict[str, str] = {}

    @staticmethod
    def _mapped(mapping: dict[str, str], value: str, template: str) -> str:
        if value not in mapping:
            mapping[value] = template.format(index=len(mapping) + 1)
        return mapping[value]

    def _path(self, match: re.Match[str]) -> str:
        value = match.group(0)
        suffix = Path(value.replace("\\", "/")).suffix
        return self._mapped(
            self.paths,
            value,
            "C:/sanitized/path_{index:04d}" + suffix,
        )

    def _uuid(self, match: re.Match[str]) -> str:
        return self._mapped(
            self.uuids,
            match.group(0),
            "00000000-0000-4000-8000-{index:012d}",
        )

    def _scope(self, match: re.Match[str]) -> str:
        value = self._mapped(
            self.scopes,
            match.group("value"),
            "{index:032x}",
        )
        return match.group("prefix") + value + match.group("suffix")

    def _generator_guid(self, match: re.Match[str]) -> str:
        value = match.group("value")
        canonical = canonical_generator_guid(value)
        if not (TRUNCATED_GUID_RE.fullmatch(value) or PADDED_GUID_RE.fullmatch(value)):
            sanitized = self._mapped(
                self.generator_guids,
                value,
                "sanitized-guid-{index:04d}",
            )
        else:
            body = self._mapped(
                self.generator_guids,
                canonical,
                "SG{index:019d}",
            )
            sanitized = body + ("==" if len(value) == 23 else "A==")
        return match.group("open") + sanitized + match.group("close")

    def sanitize(self, text: str) -> str:
        sanitized = WINDOWS_PATH_RE.sub(self._path, text)
        sanitized = PRIVATE_USER_TOKEN_RE.sub("SANITIZED_USER", sanitized)
        sanitized = UUID_RE.sub(self._uuid, sanitized)
        sanitized = SCOPE_RE.sub(self._scope, sanitized)
        sanitized = GUID_ELEMENT_RE.sub(self._generator_guid, sanitized)
        return sanitized


def parse_pair(value: str) -> tuple[str, Path, Path]:
    parts = value.split("=", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "--pair must be PAIR_ID=BEFORE_SPM=AFTER_SPM"
        )
    return parts[0], Path(parts[1]), Path(parts[2])


def first_differences(left, right, path="$", limit=5000):
    differences = []

    def node_label(value):
        if not isinstance(value, dict) or "tag" not in value:
            return ""
        label = f"<{value['tag']}>"
        for child in value.get("children", []):
            if child.get("tag") == "Name" and child.get("text"):
                label += f"[{child['text']}]"
                break
        return label

    def walk(a, b, current):
        if len(differences) >= limit:
            return
        if type(a) is not type(b):
            differences.append((current, repr(a)[:160], repr(b)[:160]))
        elif isinstance(a, dict):
            current += node_label(a)
            for key in sorted(set(a) | set(b)):
                if key not in a or key not in b:
                    differences.append(
                        (current + f".{key}", repr(a.get(key))[:160], repr(b.get(key))[:160])
                    )
                else:
                    walk(a[key], b[key], current + f".{key}")
        elif isinstance(a, list):
            if len(a) != len(b):
                differences.append((current + ".length", str(len(a)), str(len(b))))
            for index, (item_a, item_b) in enumerate(zip(a, b)):
                walk(item_a, item_b, current + f"[{index}]")
        elif a != b:
            differences.append((current, repr(a)[:160], repr(b)[:160]))

    walk(left, right, path)
    return differences


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pair",
        action="append",
        required=True,
        type=parse_pair,
        help="PAIR_ID=BEFORE_SPM=AFTER_SPM (repeatable)",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--allow-unequal", action="store_true")
    args = parser.parse_args()

    decoded_pairs = []
    for pair_id, before_path, after_path in args.pair:
        before_raw, before_text = decode_spm(before_path)
        after_raw, after_text = decode_spm(after_path)
        decoded_pairs.append(
            (
                pair_id,
                before_raw,
                before_text,
                after_raw,
                after_text,
            )
        )

    sanitizer = Sanitizer()
    sanitized_pairs = []
    for pair in decoded_pairs:
        pair_id, before_raw, before_text, after_raw, after_text = pair
        sanitized_pairs.append(
            (
                pair_id,
                before_raw,
                sanitizer.sanitize(before_text),
                after_raw,
                sanitizer.sanitize(after_text),
            )
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "issue_number": 42,
        "modeler_version": "10.1.0",
        "sanitization_contract": "raw_text_identity_and_path_substitution_v1",
        "sanitization_invariants": {
            "xml_reserialized": False,
            "element_order_preserved": True,
            "duplicate_count_preserved": True,
            "namespace_spelling_preserved": True,
            "numeric_lexemes_preserved_outside_replaced_identity_path_tokens": True,
            "text_and_tail_placement_preserved": True,
        },
        "pairs": [],
    }
    forbidden = ("PARK", "OneDrive", "Forestportfolio")
    for pair_id, before_raw, before_text, after_raw, after_text in sanitized_pairs:
        remaining = [
            token for token in forbidden
            if token.casefold() in (before_text + after_text).casefold()
        ]
        if remaining:
            raise RuntimeError(
                f"private path token remains in {pair_id}: {remaining}"
            )
        before_projection = _authoring_graph_core_projection(before_text)
        after_projection = _authoring_graph_core_projection(after_text)
        if args.diagnostics:
            before_root = ET.fromstring(before_text)
            after_root = ET.fromstring(after_text)
            before_assets = before_root.find("Assets")
            after_assets = after_root.find("Assets")
            legacy_default_maps = [
                ET.tostring(element, encoding="unicode")
                for element in after_root.iter("Map")
                if _default_modeler_material_map(element)
                and not _v4_default_material_map(element)
            ]
            legacy_atlas_makers = [
                ET.tostring(element, encoding="unicode")
                for element in after_root.iter("AtlasMaker")
                if _default_modeler_atlas_maker(element)
                and not _v4_default_modeler_atlas_maker(element)
            ]
            def material_summary(root):
                summary = []
                for material in root.iter("Material_v8"):
                    maps = list(material.findall("Map"))
                    name = material.findtext("Name")
                    summary.append({
                        "attributes": dict(material.attrib),
                        "name": name,
                        "maps": [item.attrib.get("Name") for item in maps],
                        "strict_defaults": [
                            item.attrib.get("Name")
                            for item in maps
                            if _v4_default_material_map(item)
                        ],
                    })
                return summary
            print(json.dumps({
                "pair_id": pair_id,
                "before_asset_tags": [child.tag for child in before_assets or ()],
                "after_asset_tags": [child.tag for child in after_assets or ()],
                "strict_default_map_misses": legacy_default_maps[:4],
                "strict_atlas_maker_misses": legacy_atlas_makers[:4],
                "before_materials": material_summary(before_root),
                "after_materials": material_summary(after_root),
            }, ensure_ascii=False))
        if before_projection["fingerprint"] != after_projection["fingerprint"]:
            differences = first_differences(
                before_projection["_rows"],
                after_projection["_rows"],
            )
            grouped = {}
            for path, _left, _right in differences:
                key = re.sub(r"\.children\[\d+\]", "/", path)
                grouped[key] = grouped.get(key, 0) + 1
            spline_names = sorted({
                match.group(1)
                for path, _left, _right in differences
                for match in [re.search(r"<SplineProperty>\[([^\]]+)\]", path)]
                if match
            })
            message = (
                f"sanitized pair does not compare equal: {pair_id}: "
                f"{before_projection['fingerprint']} != "
                f"{after_projection['fingerprint']}; "
                f"difference_count={len(differences)}; "
                f"spline_property_names={spline_names}; "
                f"groups={list(grouped.items())[:120]}"
            )
            if not args.allow_unequal:
                raise RuntimeError(message)
            print(message)
        before_bytes = before_text.encode("utf-8")
        after_bytes = after_text.encode("utf-8")
        before_name = f"{pair_id}.before.sanitized.xml.gz"
        after_name = f"{pair_id}.after.sanitized.xml.gz"
        (args.output_dir / before_name).write_bytes(
            gzip.compress(before_bytes, compresslevel=9, mtime=0)
        )
        (args.output_dir / after_name).write_bytes(
            gzip.compress(after_bytes, compresslevel=9, mtime=0)
        )
        manifest["pairs"].append(
            {
                "pair_id": pair_id,
                "before_fixture": before_name,
                "after_fixture": after_name,
                "private_before_raw_sha256": sha256(before_raw),
                "private_after_raw_sha256": sha256(after_raw),
                "sanitized_before_xml_sha256": sha256(before_bytes),
                "sanitized_after_xml_sha256": sha256(after_bytes),
                "expected_core_fingerprint": before_projection["fingerprint"],
            }
        )
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
