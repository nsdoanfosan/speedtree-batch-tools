"""SpeedTree SPM master/follower generator synchronization.

The tool treats each vegetation folder as a small family graph:

    master.spm
      - follower_a.spm
      - follower_b.spm
    independent.spm

Only generator settings and additive Base-subtree structure are synchronized.
Target identity and variation data (GUIDs, names, random seeds, materials,
BaseRef placement, node/freehand edits, and target-only generators) remain in
the follower.  The Generators/Links XML sections are patched without rewriting
the usually very large Nodes section.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET
import xml.parsers.expat
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from speedtree_legacy_cluster_contract import inspect_legacy_cluster_state
from speedtree_legacy_cluster_contract import FOREGROUND_TAGS
from cluster_blend_sync import discover_cluster_blend_relations

MANIFEST_NAME = "spm_generator_sync.json"
BACKUP_SUBDIR = "_spm_backups"
SKIP_DIRS = {
    BACKUP_SUBDIR,
    "_skbatch_backup",
    "_pcgtex_backups",
    "_codex_backups",
    "_atlas_cluster_normalization_backups",
}
BACKUP_NAME_RE = re.compile(
    r"(^~|\.sbk$|backup|codex_backup|skbatch_backup|pcgtex_backup)",
    re.IGNORECASE,
)
VARIANT_RE = re.compile(r"^(.*?)[_-](\d{2})$", re.IGNORECASE)

# Existing palette found in SK_tree_black_locast_01 BaseRef icons.
CATEGORY_COLORS = {
    "leaf": (0.0, 1.0, 0.0500038154, 1.0),
    "branch": (0.0, 0.366659045, 1.0, 1.0),
    "end": (1.0, 0.0, 0.0, 1.0),
}
CATEGORY_LABELS = {
    "leaf": "Leaf · 녹색",
    "branch": "Branch · 파란색",
    "end": "End · 빨간색",
}

# An absolute-distance generator authored for a small tree can multiply its
# node/triangle count when reused on a much larger tree.  Tree Shape:Radius is
# stored in the lightweight Generators section, so this guard runs before the
# expensive Modeler compute and before any original SPM is written.
SCALE_RISK_WARN_RATIO = 1.5
SCALE_RISK_BLOCK_RATIO = 2.5

SECTION_RE = {
    tag: re.compile(rf"<{tag}(?:\s[^>/]*)?>.*?</{tag}>", re.DOTALL)
    for tag in ("Generators", "Links")
}
SELF_CLOSING_SECTION_RE = {
    tag: re.compile(rf"<{tag}\b[^>]*/>") for tag in ("Generators", "Links")
}
ASSETS_SECTION_RE = re.compile(r"<Assets(?:\s[^>]*)?>.*?</Assets>", re.DOTALL)
ASSETS_SELF_CLOSING_RE = re.compile(r"<Assets\b[^>]*/>")
GENERATION_PASS_PROPERTY = "Generation:Shared:Pass"


class SyncError(RuntimeError):
    """Actionable SPM synchronization failure."""


def is_active_spm(path: Path) -> bool:
    path = Path(path)
    if path.suffix.lower() != ".spm" or not path.is_file():
        return False
    if any(part.lower() in {name.lower() for name in SKIP_DIRS} for part in path.parts):
        return False
    return not BACKUP_NAME_RE.search(path.name)


def read_spm_text(path: Path) -> tuple[str, bool]:
    path = Path(path)
    raw = path.read_bytes()
    compressed = raw.startswith(b"\x1f\x8b")
    if compressed:
        raw = gzip.decompress(raw)
    try:
        return raw.decode("utf-8"), compressed
    except UnicodeDecodeError as exc:
        raise SyncError(f"UTF-8 SPM이 아닙니다: {path}") from exc


def read_spm_prefix(path: Path) -> str:
    """Read only through </Links> for fast folder-board inspection."""
    path = Path(path)
    with path.open("rb") as probe:
        compressed = probe.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    chunks: list[str] = []
    with opener(path, "rt", encoding="utf-8", errors="strict", newline="") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            joined = "".join(chunks)
            end = joined.find("</Links>")
            if end >= 0:
                return joined[: end + len("</Links>")]
    if chunks and "</Generators>" in joined:
        # A valid empty SpeedTree model may contain only a Tree generator and
        # omit the Links section entirely.
        return joined
    raise SyncError(f"SPM에서 Links 섹션을 찾지 못했습니다: {path}")


def _spm_fingerprint(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _assert_spm_unchanged(fingerprints: dict[Path, str]) -> None:
    """Refuse to overwrite SPMs another tool rewrote while this run computed.

    Patches are built from the bytes read at plan time and the SpeedTree
    preflight between then and the write can run for minutes, so a Cluster
    relationship ON/OFF or a second sync finishing in that window would
    otherwise be silently reverted by this transaction's stale text.
    """
    drifted = [
        path for path, digest in fingerprints.items()
        if not path.is_file() or _spm_fingerprint(path) != digest
    ]
    if drifted:
        raise SyncError(
            "동기화를 계산하는 동안 다른 작업이 같은 SPM을 수정했습니다. "
            "원본을 덮어쓰지 않고 중단합니다: "
            + ", ".join(path.name for path in drifted)
            + "\nCluster 관계 ON/OFF 등 다른 도구가 끝난 뒤 다시 실행하세요."
        )


def write_spm_text(path: Path, text: str, compressed: bool) -> None:
    path = Path(path)
    payload = text.encode("utf-8")
    if compressed:
        payload = gzip.compress(payload, compresslevel=9, mtime=0)
    path.write_bytes(payload)


def _section_match(text: str, tag: str):
    if tag == "Generators":
        # Paired form deliberately ignores nested <Generators/> entries used
        # inside Force/Extra data before the real top-level section.
        return SECTION_RE[tag].search(text)
    generators = SECTION_RE["Generators"].search(text)
    start = generators.end() if generators else 0
    paired = SECTION_RE[tag].search(text, start)
    self_closing = SELF_CLOSING_SECTION_RE[tag].search(text, start)
    matches = [item for item in (paired, self_closing) if item is not None]
    return min(matches, key=lambda item: item.start()) if matches else None


def extract_section(text: str, tag: str) -> str:
    match = _section_match(text, tag)
    if not match:
        raise SyncError(f"SPM에서 {tag} 섹션을 찾지 못했습니다")
    return match.group(0)


def replace_section(text: str, tag: str, replacement: str) -> str:
    match = _section_match(text, tag)
    if not match:
        raise SyncError(f"SPM에서 {tag} 섹션을 찾지 못했습니다")
    return text[: match.start()] + replacement + text[match.end() :]


def insert_links_section(text: str, replacement: str) -> str:
    nodes = re.search(r"<Nodes\b", text)
    if nodes:
        index = nodes.start()
    else:
        index = text.rfind("</SpeedTree>")
        if index < 0:
            raise SyncError("SPM에서 Links 섹션을 삽입할 위치를 찾지 못했습니다")
    return text[:index] + replacement + "\n" + text[index:]


def append_assets_section(text: str, assets: list[ET.Element]) -> str:
    """Append cloned asset definitions without reserializing existing assets."""
    if not assets:
        return text
    serialized = "".join(
        "\n\t\t" + ET.tostring(asset, encoding="unicode", short_empty_elements=True)
        for asset in assets
    )
    section = ASSETS_SECTION_RE.search(text)
    if section is not None:
        close_at = section.end() - len("</Assets>")
        return text[:close_at] + serialized + "\n\t" + text[close_at:]
    self_closing = ASSETS_SELF_CLOSING_RE.search(text)
    if self_closing is not None:
        replacement = "<Assets>" + serialized + "\n\t</Assets>"
        return text[:self_closing.start()] + replacement + text[self_closing.end():]
    generators = _section_match(text, "Generators")
    if generators is None:
        raise SyncError("SPM에서 Assets 섹션을 삽입할 위치를 찾지 못했습니다")
    replacement = "<Assets>" + serialized + "\n\t</Assets>\n\t"
    return text[:generators.start()] + replacement + text[generators.start():]


def validate_xml_text(text: str) -> None:
    parser = xml.parsers.expat.ParserCreate()
    try:
        parser.Parse(text, True)
    except xml.parsers.expat.ExpatError as exc:
        raise SyncError(f"SPM XML 무결성 오류: {exc}") from exc


def canonical_base_name(name: str) -> str:
    value = re.sub(r"\s+", " ", str(name or "").strip().lower())
    value = re.sub(r"(?:[\s_-]+\d+)+$", "", value)
    return value.replace(" ", "_")


class SearchSyntaxError(ValueError):
    """Malformed SpeedTree search expression used by a Base filter."""


def _split_search_expression(expression: str, operator: str) -> list[str]:
    """Split one SpeedTree search operator outside quotes and parentheses."""
    parts: list[str] = []
    start = 0
    depth = 0
    quoted = False
    for index, character in enumerate(expression):
        if character == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise SearchSyntaxError("닫는 괄호가 더 많습니다")
        elif character == operator and depth == 0:
            parts.append(expression[start:index].strip())
            start = index + 1
    if quoted:
        raise SearchSyntaxError("닫히지 않은 따옴표가 있습니다")
    if depth != 0:
        raise SearchSyntaxError("닫히지 않은 괄호가 있습니다")
    parts.append(expression[start:].strip())
    if any(not part for part in parts):
        raise SearchSyntaxError(f"'{operator}' 연산자 양쪽에 검색식이 필요합니다")
    return parts


def _strip_search_parentheses(expression: str) -> str:
    value = expression.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        quoted = False
        closes_at_end = False
        for index, character in enumerate(value):
            if character == '"':
                quoted = not quoted
                continue
            if quoted:
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    closes_at_end = index == len(value) - 1
                    break
        if not closes_at_end:
            break
        value = value[1:-1].strip()
    return value


def speedtree_search_matches(value: str, expression: str) -> bool:
    """Evaluate documented SpeedTree search syntax against one Base name."""
    query = str(expression or "").strip()
    if not query:
        return True

    def evaluate(term: str) -> bool:
        term = _strip_search_parentheses(term)
        alternatives = _split_search_expression(term, "|")
        if len(alternatives) > 1:
            return any(evaluate(item) for item in alternatives)
        requirements = _split_search_expression(term, "&")
        if len(requirements) > 1:
            return all(evaluate(item) for item in requirements)
        if term.startswith("!"):
            remainder = term[1:].strip()
            if not remainder:
                raise SearchSyntaxError("'!' 뒤에 검색식이 필요합니다")
            return not evaluate(remainder)
        if term.startswith("(") or term.endswith(")"):
            raise SearchSyntaxError("괄호 위치가 올바르지 않습니다")

        case_sensitive = False
        exact = False
        if term.startswith("=="):
            case_sensitive = True
            exact = True
            term = term[2:].strip()
        elif term.startswith("="):
            exact = True
            term = term[1:].strip()
        quoted_term = len(term) >= 2 and term.startswith('"') and term.endswith('"')
        if quoted_term:
            term = term[1:-1]
        elif '"' in term:
            raise SearchSyntaxError("따옴표는 검색어 전체를 감싸야 합니다")
        if not term:
            raise SearchSyntaxError("빈 검색어입니다")

        candidate = str(value)
        if exact:
            return candidate == term if case_sensitive else candidate.casefold() == term.casefold()
        if quoted_term or not any(character in term for character in "*?"):
            return term.casefold() in candidate.casefold()
        wildcard = "".join(
            ".*" if character == "*" else "." if character == "?" else re.escape(character)
            for character in term
        )
        return re.search(wildcard, candidate, re.IGNORECASE) is not None

    return evaluate(query)


def classify_base_name(name: str) -> str | None:
    token = canonical_base_name(name)
    if "leaf" in token or "leaves" in token:
        return "leaf"
    if "end" in token or "tip" in token:
        return "end"
    if "branch" in token or "bough" in token or "twig" in token:
        return "branch"
    return None


def _child_text(element: ET.Element, name: str, default: str = "") -> str:
    child = element.find(name)
    return default if child is None or child.text is None else child.text


def _set_child_text(element: ET.Element, name: str, value: str) -> ET.Element:
    child = element.find(name)
    if child is None:
        child = ET.SubElement(element, name)
    child.text = str(value)
    return child


def _property_name(prop: ET.Element) -> str:
    return _child_text(prop, "Name")


def _property_value(prop: ET.Element) -> str:
    return _child_text(prop, "Value")


def _normalized_xml(element: ET.Element) -> tuple:
    """Canonical XML value without deepcopy/string serialization overhead."""
    text = element.text if element.text is not None and element.text.strip() else None
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        text,
        tuple(_normalized_xml(child) for child in list(element)),
    )


def _new_guid() -> str:
    return base64.b64encode(uuid.uuid4().bytes).decode("ascii")


def _format_color(value: float) -> str:
    if value == 0.0:
        return "0"
    if value == 1.0:
        return "1"
    return format(value, ".10g")


# A multi-property container holds its slot count in <MultiPropertyChildren>,
# while the indexed ``:N:Material`` / ``:Mesh`` / ``:Weight`` slot properties it
# counts are protected below so the follower keeps its own assets.  Copying the
# container on its own therefore makes the follower claim the master's slot
# count with nothing behind it, and SpeedTree draws the surplus slots as
# "Material: None / Mesh: Any" (a smaller count instead hides authored slots).
PROTECTED_MULTI_PROPERTY_CONTAINERS = (
    "leaves:type",
    "material:frond",
    "cap:material",
)


def is_protected_property(name: str) -> bool:
    """Properties kept from the follower to preserve identity and assets."""
    lowered = str(name or "").strip().lower()
    if lowered == "generation:shared:pass":
        return True
    if lowered in PROTECTED_MULTI_PROPERTY_CONTAINERS:
        return True
    protected_prefixes = (
        "random seeds:",
        "generation:collections:",
        "materials:",
        "material:",
        "cap:material",
        "leaves:type:",
        "dynamic lod:mesh",
        "generation:shared:force mesh containers:",
    )
    return lowered.startswith(protected_prefixes)


@dataclass
class BaseSyncResult:
    source_base: str
    target_base: str
    category: str
    matched_nodes: int = 0
    property_updates: int = 0
    added_nodes: int = 0
    target_only_nodes: int = 0
    color_updates: int = 0
    asset_reference_updates: int = 0
    copied_assets: list[dict[str, str]] = field(default_factory=list)
    created_base: bool = False
    added_node_details: list[dict[str, str]] = field(default_factory=list)
    target_only_details: list[dict[str, str]] = field(default_factory=list)
    changed_properties: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SyncPlan:
    master: str
    target: str
    base_results: list[BaseSyncResult] = field(default_factory=list)
    master_color_updates: int = 0
    target_color_updates: int = 0
    mapping_required: list[str] = field(default_factory=list)
    unmapped_source_bases: list[str] = field(default_factory=list)
    added_base_mappings: dict[str, str] = field(default_factory=dict)
    renamed_bases: dict[str, str] = field(default_factory=dict)
    base_ref_filter_updates: int = 0
    master_reference_renames: list[dict[str, str]] = field(default_factory=list)
    reference_renames: list[dict[str, str]] = field(default_factory=list)
    pass_adjustments: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scale_risk: dict[str, object] = field(default_factory=dict)
    changed: bool = False
    patched_text: str | None = field(default=None, repr=False)
    compressed: bool = field(default=True, repr=False)

    @property
    def property_updates(self) -> int:
        return sum(item.property_updates for item in self.base_results)

    @property
    def added_nodes(self) -> int:
        return sum(item.added_nodes for item in self.base_results)

    @property
    def target_only_nodes(self) -> int:
        return sum(item.target_only_nodes for item in self.base_results)

    def summary_lines(self) -> list[str]:
        lines = [
            f"마스터: {Path(self.master).name}",
            f"자식: {Path(self.target).name}",
            "",
        ]
        if self.scale_risk:
            ratio = float(self.scale_risk.get("ratio", 1.0))
            level = self.scale_risk.get("level", "safe")
            label = {"blocked": "차단", "warning": "주의", "safe": "안전"}.get(level, level)
            lines.append(
                f"크기 위험: {label} · 자식/마스터 반경 {ratio:.2f}배 "
                f"({self.scale_risk.get('master_radius', 0):g} → "
                f"{self.scale_risk.get('target_radius', 0):g})"
            )
            if level != "safe":
                lines.append("  거리 기반 생성 노드와 폴리곤이 크게 늘어날 수 있습니다.")
            lines.append("")
        for item in self.base_results:
            relation = (
                f"[새 Base] {item.target_base} ← {item.source_base}"
                if item.created_base
                else f"{item.target_base} ← {item.source_base}"
            )
            lines.append(
                f"{relation} [{item.category}] · "
                f"공통 {item.matched_nodes}, 속성 {item.property_updates}, "
                f"추가 예정 {item.added_nodes}, 자식 전용 {item.target_only_nodes}, "
                f"색 {item.color_updates}, 에셋 ID {item.asset_reference_updates}, "
                f"에셋 복사 {len(item.copied_assets)}"
            )
            lines.extend(
                f"  + 에셋 복사: {asset['kind']} · {asset['name']} (ID {asset['id']})"
                for asset in item.copied_assets
            )
            lines.extend(f"  ⚠ {warning}" for warning in item.warnings)
            for detail in item.added_node_details:
                lines.append(
                    f"  + 추가 예정: {detail['path']} ({detail['type']})"
                )
            for detail in item.target_only_details:
                lines.append(
                    f"  ◆ 자식 전용: {detail['path']} ({detail['type']})"
                )
        if self.mapping_required:
            lines.append("매핑 확인 필요: " + ", ".join(self.mapping_required))
        if self.unmapped_source_bases:
            lines.append("자식에 연결되지 않은 마스터 Base: " + ", ".join(self.unmapped_source_bases))
        if self.renamed_bases:
            lines.append("Base 이름 통일: " + ", ".join(
                f"{old} → {new}" for old, new in self.renamed_bases.items()
            ))
        if self.base_ref_filter_updates:
            lines.append(f"BaseRef 필터 이름 갱신: {self.base_ref_filter_updates}")
        if self.master_reference_renames:
            lines.append(f"마스터 Reference 이름 정리: {len(self.master_reference_renames)}개")
            lines.extend(
                f"  Ref: {item['old']} → {item['new']}"
                for item in self.master_reference_renames
            )
        if self.reference_renames:
            lines.append(f"자식 Reference 이름 정리: {len(self.reference_renames)}개")
            lines.extend(
                f"  Ref: {item['old']} → {item['new']}"
                for item in self.reference_renames
            )
        if self.pass_adjustments:
            lines.append(f"Generation Pass 안전 보정: {len(self.pass_adjustments)}개")
            lines.extend(
                f"  {item['type']} {item['name']}: "
                f"{item['old_pass']} → {item['new_pass']} ({item['reason']})"
                for item in self.pass_adjustments
            )
        lines.extend(f"⚠ {warning}" for warning in self.warnings)
        lines.append("")
        lines.append("변경 예정" if self.changed else "현재 동기화 상태가 최신입니다")
        return lines


class SPMDocument:
    def __init__(self, path: Path, text: str, compressed: bool, full: bool = True):
        self.path = Path(path)
        self.text = text if full else ""
        self.compressed = compressed
        self.full = full
        self.links_missing = _section_match(text, "Links") is None
        self.asset_names_by_id: dict[str, dict[str, str]] = {
            "Material_v8": {}, "Mesh": {},
        }
        self.asset_ids_by_name: dict[str, dict[str, str]] = {
            "Material_v8": {}, "Mesh": {},
        }
        self.asset_elements_by_id: dict[str, dict[str, ET.Element]] = {
            "Material_v8": {}, "Mesh": {},
        }
        self.pending_asset_elements: list[ET.Element] = []
        if full:
            self._index_assets(text)
        try:
            self.generators_element = ET.fromstring(extract_section(text, "Generators"))
            self.links_element = ET.fromstring(
                "<Links />" if self.links_missing else extract_section(text, "Links")
            )
        except ET.ParseError as exc:
            raise SyncError(f"Generator/Link XML 파싱 실패: {self.path.name}: {exc}") from exc
        self.reindex()

    def _index_assets(self, text: str) -> None:
        section = ASSETS_SECTION_RE.search(text)
        if section is None:
            return
        try:
            assets = ET.fromstring(section.group(0))
        except ET.ParseError as exc:
            raise SyncError(f"Assets XML 파싱 실패: {self.path.name}: {exc}") from exc
        for asset in list(assets):
            kind = asset.tag
            if kind not in self.asset_names_by_id:
                continue
            asset_id = str(asset.attrib.get("ID", ""))
            asset_name = str(asset.attrib.get("Name", ""))
            if not asset_id or not asset_name:
                continue
            self.asset_names_by_id[kind][asset_id] = asset_name
            self.asset_ids_by_name[kind][asset_name.casefold()] = asset_id
            self.asset_elements_by_id[kind][asset_id] = asset

    def asset_name(self, kind: str, asset_id: str) -> str | None:
        return self.asset_names_by_id.get(kind, {}).get(str(asset_id))

    def asset_id(self, kind: str, asset_name: str) -> str | None:
        return self.asset_ids_by_name.get(kind, {}).get(str(asset_name).casefold())

    def _next_asset_id(self, kind: str) -> str:
        used = set(self.asset_names_by_id.get(kind, {}))
        numeric = [int(value) for value in used if value.isdigit()]
        candidate = max(numeric, default=-1) + 1
        while str(candidate) in used:
            candidate += 1
        return str(candidate)

    def copy_asset_from(
        self,
        source_document: "SPMDocument",
        kind: str,
        source_asset_id: str,
        copied_assets: list[dict[str, str]],
        warnings: list[str],
    ) -> str | None:
        """Copy one missing asset and recursively remap its dependencies."""
        source_asset_id = str(source_asset_id)
        source_name = source_document.asset_name(kind, source_asset_id)
        if source_name is None:
            warnings.append(f"마스터 에셋 정의 없음: {kind} ID {source_asset_id}")
            return None
        existing_id = self.asset_id(kind, source_name)
        if existing_id is not None:
            return existing_id
        source_asset = source_document.asset_elements_by_id.get(kind, {}).get(source_asset_id)
        if source_asset is None:
            warnings.append(f"마스터 에셋 XML 없음: {kind} · {source_name}")
            return None

        clone = copy.deepcopy(source_asset)
        clone.tail = None
        target_asset_id = self._next_asset_id(kind)
        clone.set("ID", target_asset_id)

        # Register before recursion so cyclic BackMaterialID references cannot
        # clone the same asset repeatedly.
        self.asset_names_by_id[kind][target_asset_id] = source_name
        self.asset_ids_by_name[kind][source_name.casefold()] = target_asset_id
        self.asset_elements_by_id[kind][target_asset_id] = clone
        self.pending_asset_elements.append(clone)
        copied_assets.append({"kind": kind, "name": source_name, "id": target_asset_id})

        if kind == "Material_v8":
            cutout = clone.find("CutoutMeshID")
            if cutout is not None and cutout.text not in (None, "", "-1"):
                remapped = self.copy_asset_from(
                    source_document, "Mesh", cutout.text, copied_assets, warnings
                )
                if remapped is not None:
                    cutout.text = remapped
            for item in clone.findall("./SupplementalCutoutMeshIDs/CutoutMesh"):
                source_mesh_id = item.attrib.get("ID")
                if source_mesh_id in (None, "", "-1"):
                    continue
                remapped = self.copy_asset_from(
                    source_document, "Mesh", source_mesh_id, copied_assets, warnings
                )
                if remapped is not None:
                    item.set("ID", remapped)
            back = clone.find("BackMaterialID")
            if back is not None and back.text not in (None, "", "-1"):
                remapped = self.copy_asset_from(
                    source_document, "Material_v8", back.text, copied_assets, warnings
                )
                if remapped is not None:
                    back.text = remapped
        return target_asset_id

    @classmethod
    def from_path(cls, path: Path, full: bool = True) -> "SPMDocument":
        path = Path(path)
        if full:
            text, compressed = read_spm_text(path)
        else:
            text = read_spm_prefix(path)
            with path.open("rb") as handle:
                compressed = handle.read(2) == b"\x1f\x8b"
        return cls(path, text, compressed, full=full)

    def reindex(self) -> None:
        self.generators = list(self.generators_element.findall("Generator"))
        self.links = list(self.links_element.findall("Link"))
        self.by_guid = {_child_text(item, "GUID"): item for item in self.generators}
        if "" in self.by_guid:
            raise SyncError(f"GUID가 없는 Generator가 있습니다: {self.path.name}")
        if len(self.by_guid) != len(self.generators):
            raise SyncError(f"중복 Generator GUID가 있습니다: {self.path.name}")
        self.generator_index = {
            _child_text(item, "GUID"): index for index, item in enumerate(self.generators)
        }
        self.children: dict[str, list[str]] = defaultdict(list)
        self.parent: dict[str, str] = {}
        self.link_by_pair: dict[tuple[str, str], ET.Element] = {}
        for link in self.links:
            source = _child_text(link, "SourceGUID")
            target = _child_text(link, "TargetGUID")
            self.children[source].append(target)
            self.parent[target] = source
            self.link_by_pair[(source, target)] = link
        for source in list(self.children):
            self.children[source].sort(key=self._sort_key)

    def _sort_key(self, guid: str) -> tuple[int, int]:
        generator = self.by_guid.get(guid)
        if generator is None:
            return (2**31 - 1, 2**31 - 1)
        try:
            order = int(_child_text(generator.find("Extra") or ET.Element("Extra"), "m_nOrderValue", "0"))
        except ValueError:
            order = 0
        return (order, self.generator_index.get(guid, 2**31 - 1))

    @staticmethod
    def generator_name(generator: ET.Element) -> str:
        return _child_text(generator, "Name")

    @staticmethod
    def generator_type(generator: ET.Element) -> str:
        return str(generator.attrib.get("Type", ""))

    @staticmethod
    def generator_guid(generator: ET.Element) -> str:
        return _child_text(generator, "GUID")

    def base_nodes(self) -> list[ET.Element]:
        return [item for item in self.generators if self.generator_type(item) == "Base"]

    def base_refs(self) -> list[ET.Element]:
        return [item for item in self.generators if self.generator_type(item) == "BaseRef"]

    def generator_pass(self, generator: ET.Element) -> int:
        properties = generator.find("Properties")
        raw_value = "1"
        if properties is not None:
            for prop in list(properties):
                if _property_name(prop) == GENERATION_PASS_PROPERTY:
                    raw_value = _property_value(prop).strip() or "1"
                    break
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise SyncError(
                f"{self.generator_type(generator)} '{self.generator_name(generator)}'의 "
                f"Generation Pass가 정수가 아닙니다: {raw_value}"
            ) from exc
        if value < 1:
            raise SyncError(
                f"{self.generator_type(generator)} '{self.generator_name(generator)}'의 "
                f"Generation Pass는 1 이상이어야 합니다: {value}"
            )
        return value

    def set_generator_pass(self, generator: ET.Element, value: int) -> bool:
        value = int(value)
        current = self.generator_pass(generator)
        if current == value:
            return False
        properties = generator.find("Properties")
        if properties is None:
            properties = ET.SubElement(generator, "Properties")
        prop = next(
            (item for item in list(properties) if _property_name(item) == GENERATION_PASS_PROPERTY),
            None,
        )
        if prop is None:
            prop = ET.SubElement(properties, "Property")
            _set_child_text(prop, "Name", GENERATION_PASS_PROPERTY)
        _set_child_text(prop, "Value", str(value))
        return True

    def resolve_base(self, name: str) -> ET.Element | None:
        exact = [item for item in self.base_nodes() if self.generator_name(item).casefold() == str(name).casefold()]
        if len(exact) == 1:
            return exact[0]
        canonical = canonical_base_name(name)
        matches = [
            item for item in self.base_nodes()
            if canonical_base_name(self.generator_name(item)) == canonical
        ]
        return matches[0] if len(matches) == 1 else None

    def child_guids(self, guid: str) -> list[str]:
        return list(self.children.get(guid, ()))

    def descendants(self, guid: str) -> list[str]:
        found: list[str] = []
        stack = list(reversed(self.child_guids(guid)))
        seen: set[str] = set()
        while stack:
            item = stack.pop()
            if item in seen:
                continue
            seen.add(item)
            found.append(item)
            stack.extend(reversed(self.child_guids(item)))
        return found

    def subtree_count(self, guid: str) -> int:
        return 1 + len(self.descendants(guid))

    def base_ref_filter(self, generator: ET.Element) -> str:
        properties = generator.find("Properties")
        if properties is None:
            return ""
        for prop in list(properties):
            if _property_name(prop) == "Settings:Base filter":
                return _property_value(prop)
        return ""

    def refs_for_base(self, base: ET.Element) -> list[ET.Element]:
        base_name = self.generator_name(base)
        return [ref for ref in self.base_refs() if speedtree_search_matches(
            base_name, self.base_ref_filter(ref)
        )]

    def bases_for_ref(self, ref: ET.Element) -> list[ET.Element]:
        expression = self.base_ref_filter(ref)
        return [base for base in self.base_nodes() if speedtree_search_matches(
            self.generator_name(base), expression
        )]

    def base_type_signature(self, base: ET.Element) -> tuple[tuple[str, int], ...]:
        counter = Counter(
            self.generator_type(self.by_guid[guid])
            for guid in self.descendants(self.generator_guid(base))
            if guid in self.by_guid
        )
        return tuple(sorted(counter.items()))

    def integrity_errors(self) -> list[str]:
        errors: list[str] = []
        link_guids: set[str] = set()
        passes: dict[str, int] = {}
        for generator in self.generators:
            guid = self.generator_guid(generator)
            try:
                passes[guid] = self.generator_pass(generator)
            except SyncError as exc:
                errors.append(str(exc))
        for link in self.links:
            source = _child_text(link, "SourceGUID")
            target = _child_text(link, "TargetGUID")
            link_guid = _child_text(link, "GUID")
            if source not in self.by_guid:
                errors.append(f"Link source GUID 없음: {source}")
            if target not in self.by_guid:
                errors.append(f"Link target GUID 없음: {target}")
            if not link_guid:
                errors.append("GUID 없는 Link")
            elif link_guid in link_guids:
                errors.append(f"중복 Link GUID: {link_guid}")
            link_guids.add(link_guid)
            source_generator = self.by_guid.get(source)
            target_generator = self.by_guid.get(target)
            # Base generators are special template boundaries. Their own pass
            # schedules Reference consumption and does not constrain the pass
            # of generators authored inside the reusable Base template.
            if (
                source_generator is not None
                and target_generator is not None
                and self.generator_type(source_generator) != "Base"
                and source in passes
                and target in passes
                and passes[target] < passes[source]
            ):
                errors.append(
                    f"Generation Pass 조상 순서 오류: "
                    f"{self.generator_name(source_generator)}({passes[source]}) > "
                    f"{self.generator_name(target_generator)}({passes[target]})"
                )
        for ref in self.base_refs():
            filter_name = self.base_ref_filter(ref)
            try:
                bases = self.bases_for_ref(ref)
            except SearchSyntaxError as exc:
                errors.append(
                    f"BaseRef '{self.generator_name(ref)}'의 Base filter 검색식 오류: "
                    f"{filter_name!r} ({exc})"
                )
                continue
            if not bases:
                errors.append(
                    f"BaseRef '{self.generator_name(ref)}'의 Base filter를 찾지 못함: "
                    f"{filter_name or '<비어 있음>'}"
                )
                continue
            ref_guid = self.generator_guid(ref)
            if ref_guid not in passes:
                continue
            for base in bases:
                base_guid = self.generator_guid(base)
                if base_guid in passes and passes[ref_guid] >= passes[base_guid]:
                    errors.append(
                        f"Reference/Base Pass 순서 오류: {self.generator_name(ref)}"
                        f"({passes[ref_guid]}) < {self.generator_name(base)}"
                        f"({passes[base_guid]}) 이어야 합니다"
                    )
        return errors

    def validate(self, rendered_text: str | None = None) -> None:
        errors = self.integrity_errors()
        if errors:
            raise SyncError(f"{self.path.name} 무결성 오류: " + " | ".join(errors))
        if rendered_text is not None:
            validate_xml_text(rendered_text)

    def render(self) -> str:
        if not self.full:
            raise SyncError("전체 SPM을 읽지 않은 문서는 저장할 수 없습니다")
        generators = ET.tostring(
            self.generators_element, encoding="unicode", short_empty_elements=True
        )
        links = ET.tostring(self.links_element, encoding="unicode", short_empty_elements=True)
        text = replace_section(self.text, "Generators", generators)
        text = insert_links_section(text, links) if self.links_missing else replace_section(
            text, "Links", links
        )
        text = append_assets_section(text, self.pending_asset_elements)
        return text


def repair_generation_passes(document: SPMDocument) -> list[dict[str, object]]:
    """Increase only the passes needed by SpeedTree hierarchy/reference rules.

    Existing target scheduling remains intact whenever it is valid. Base
    template children are excluded from ancestor propagation because the Base
    pass schedules Reference consumption, not the reusable template subtree.
    """
    original = {
        document.generator_guid(generator): document.generator_pass(generator)
        for generator in document.generators
    }
    reasons: dict[str, set[str]] = defaultdict(set)
    limit = max(1, len(document.generators) + 1)
    for _round in range(limit):
        changed = False
        for parent_guid, child_guids in document.children.items():
            parent = document.by_guid.get(parent_guid)
            if parent is None or document.generator_type(parent) == "Base":
                continue
            parent_pass = document.generator_pass(parent)
            for child_guid in child_guids:
                child = document.by_guid.get(child_guid)
                if child is None or document.generator_pass(child) >= parent_pass:
                    continue
                document.set_generator_pass(child, parent_pass)
                reasons[child_guid].add(
                    f"조상 {document.generator_name(parent)} pass {parent_pass}"
                )
                changed = True

        for ref in document.base_refs():
            try:
                bases = document.bases_for_ref(ref)
            except SearchSyntaxError as exc:
                raise SyncError(
                    f"BaseRef '{document.generator_name(ref)}'의 Base filter 검색식 오류: "
                    f"{document.base_ref_filter(ref)!r} ({exc})"
                ) from exc
            ref_pass = document.generator_pass(ref)
            for base in bases:
                required = ref_pass + 1
                if document.generator_pass(base) >= required:
                    continue
                document.set_generator_pass(base, required)
                reasons[document.generator_guid(base)].add(
                    f"Reference {document.generator_name(ref)} pass {ref_pass}보다 이후"
                )
                changed = True
        if not changed:
            break
    else:
        raise SyncError(
            f"{document.path.name} Generation Pass 제약에 순환이 있어 자동 보정할 수 없습니다"
        )

    adjustments: list[dict[str, object]] = []
    for generator in document.generators:
        guid = document.generator_guid(generator)
        old_pass = original[guid]
        new_pass = document.generator_pass(generator)
        if old_pass == new_pass:
            continue
        adjustments.append({
            "guid": guid,
            "name": document.generator_name(generator),
            "type": document.generator_type(generator),
            "old_pass": old_pass,
            "new_pass": new_pass,
            "reason": "; ".join(sorted(reasons.get(guid, {"의존성 제약"}))),
        })
    return adjustments


def _scale_color(color: tuple[float, float, float, float], factor: float):
    return tuple(max(0.0, min(1.0, channel * factor)) for channel in color[:3]) + (color[3],)


def base_role_color(category: str, base_name: str) -> tuple[float, float, float, float] | None:
    """Return a stable shade so same-role Bases remain visually distinct."""
    color = CATEGORY_COLORS.get(category)
    if color is None:
        return None
    token = canonical_base_name(base_name)
    if any(word in token for word in ("small", "minor", "twig")):
        factor = 0.58
    elif any(word in token for word in ("big", "large", "major", "main")):
        factor = 1.0
    elif token in {category, "leaves" if category == "leaf" else category}:
        factor = 1.0
    else:
        # Stable fallback for arbitrary names. Four separated brightness bands
        # are enough to distinguish neighboring Bases without changing role hue.
        digest = hashlib.sha256(token.encode("utf-8")).digest()[0]
        factor = (1.0, 0.86, 0.72, 0.62)[digest % 4]
    return _scale_color(color, factor)


def unique_role_color(category: str, base_name: str):
    color = base_role_color(category, base_name)
    return _scale_color(color, 0.42) if color is not None else None


def set_icon_palette(
    generator: ET.Element,
    background: tuple[float, float, float, float] | None,
    unique: bool = False,
    preserve_foreground: bool = False,
) -> bool:
    if background is None:
        return False
    extra = generator.find("Extra")
    if extra is None:
        extra = ET.Element("Extra")
        properties = generator.find("Properties")
        index = list(generator).index(properties) if properties is not None else len(generator)
        generator.insert(index, extra)
    expected = {
        "m_bSetBackgroundIconColor": "true",
        "m_vecBackgroundIconColor_r": _format_color(background[0]),
        "m_vecBackgroundIconColor_g": _format_color(background[1]),
        "m_vecBackgroundIconColor_b": _format_color(background[2]),
        "m_vecBackgroundIconColor_a": _format_color(background[3]),
    }
    if not preserve_foreground:
        expected.update({
            "m_bSetForegroundIconColor": "true",
            "m_vecForegroundIconColor_r": "1" if unique else "0",
            "m_vecForegroundIconColor_g": "1" if unique else "0",
            "m_vecForegroundIconColor_b": "1" if unique else "0",
            "m_vecForegroundIconColor_a": "1",
        })
    changed = False
    for name, value in expected.items():
        child = extra.find(name)
        if child is None:
            child = ET.SubElement(extra, name)
            child.text = value
            changed = True
        elif (child.text or "") != value:
            child.text = value
            changed = True
    return changed


def _icon_foreground_values(generator: ET.Element):
    extra = generator.find("Extra")
    if extra is None:
        return None
    values = {
        tag: extra.findtext(tag) for tag in FOREGROUND_TAGS
    }
    return values if all(value is not None for value in values.values()) else None


def _restore_icon_foreground(
    generator: ET.Element, values: dict[str, str] | None
) -> None:
    if not values:
        return
    extra = generator.find("Extra")
    if extra is None:
        extra = ET.Element("Extra")
        properties = generator.find("Properties")
        index = (
            list(generator).index(properties)
            if properties is not None else len(generator)
        )
        generator.insert(index, extra)
    for tag, value in values.items():
        child = extra.find(tag)
        if child is None:
            child = ET.SubElement(extra, tag)
        child.text = value


def set_icon_background(generator: ET.Element, category: str) -> bool:
    """Compatibility wrapper for callers that only know the role."""
    return set_icon_palette(generator, CATEGORY_COLORS.get(category), unique=False)


def standardize_base_colors(
    document: SPMDocument,
    categories: dict[str, str | None],
    unique_guids: set[str] | None = None,
    preserve_foreground_guids: set[str] | None = None,
) -> tuple[int, list[str]]:
    updates = 0
    warnings: list[str] = []
    unique_guids = unique_guids or set()
    preserve_foreground_guids = preserve_foreground_guids or set()
    processed: set[str] = set()
    for base in document.base_nodes():
        name = document.generator_name(base)
        category = categories.get(name) or categories.get(canonical_base_name(name))
        category = category or classify_base_name(name)
        if category not in CATEGORY_COLORS:
            warnings.append(f"색 분류가 없는 Base: {name}")
            continue
        regular_color = base_role_color(category, name)
        unique_color = unique_role_color(category, name)
        member_guids = [document.generator_guid(base), *document.descendants(document.generator_guid(base))]
        for guid in member_guids:
            node = document.by_guid.get(guid)
            is_unique = guid in unique_guids
            color = unique_color if is_unique else regular_color
            if node is not None and set_icon_palette(
                    node, color, unique=is_unique,
                    preserve_foreground=guid in preserve_foreground_guids):
                updates += 1
            processed.add(guid)
        for ref in document.refs_for_base(base):
            guid = document.generator_guid(ref)
            if guid in processed:
                continue
            is_unique = guid in unique_guids
            color = unique_color if is_unique else regular_color
            if set_icon_palette(
                    ref, color, unique=is_unique,
                    preserve_foreground=guid in preserve_foreground_guids):
                updates += 1
            processed.add(guid)
    return updates, warnings


def sync_generator_properties(source: ET.Element, target: ET.Element) -> tuple[int, list[str]]:
    source_props = source.find("Properties")
    target_props = target.find("Properties")
    if source_props is None:
        return 0, []
    if target_props is None:
        target_props = ET.SubElement(target, "Properties")
    target_by_name = {_property_name(item): item for item in list(target_props)}
    changes = 0
    names: list[str] = []
    for source_prop in list(source_props):
        name = _property_name(source_prop)
        if not name or is_protected_property(name):
            continue
        old = target_by_name.get(name)
        if old is not None and _normalized_xml(old) == _normalized_xml(source_prop):
            continue
        clone = copy.deepcopy(source_prop)
        if old is None:
            target_props.append(clone)
        else:
            index = list(target_props).index(old)
            clone.tail = old.tail
            target_props.remove(old)
            target_props.insert(index, clone)
        target_by_name[name] = clone
        changes += 1
        names.append(name)
    return changes, names


def _reroll_random_seeds(generator: ET.Element, salt: str) -> None:
    properties = generator.find("Properties")
    if properties is None:
        return
    for prop in list(properties):
        name = _property_name(prop)
        if not name.lower().startswith("random seeds:"):
            continue
        digest = hashlib.sha256(f"{salt}|{name}".encode("utf-8")).digest()
        value = int.from_bytes(digest[:4], "little") & 0x7FFFFFFF
        _set_child_text(prop, "Value", str(value))


def _clone_source_link(
    source_document: SPMDocument,
    source_parent: str,
    source_child: str,
    target_parent: str,
    target_child: str,
    target_parent_name: str,
    target_child_name: str,
) -> ET.Element:
    source_link = source_document.link_by_pair.get((source_parent, source_child))
    if source_link is None:
        link = ET.Element("Link")
        for tag, value in (
            ("SourceGUID", target_parent),
            ("TargetGUID", target_child),
            ("Name", f"{target_parent_name}->{target_child_name}"),
            ("GUID", _new_guid()),
            ("Hidden", "false"),
        ):
            ET.SubElement(link, tag).text = value
        ET.SubElement(link, "Extra")
        ET.SubElement(link, "Properties")
        return link
    link = copy.deepcopy(source_link)
    _set_child_text(link, "SourceGUID", target_parent)
    _set_child_text(link, "TargetGUID", target_child)
    _set_child_text(link, "Name", f"{target_parent_name}->{target_child_name}")
    _set_child_text(link, "GUID", _new_guid())
    return link


def _asset_kind_for_property(property_name: str) -> str | None:
    lowered = str(property_name or "").strip().casefold()
    if lowered.endswith(":material"):
        return "Material_v8"
    if lowered.endswith(":mesh"):
        return "Mesh"
    return None


def _asset_role(asset_name: str | None) -> str | None:
    token = str(asset_name or "").casefold()
    if "leaf" in token:
        return "leaf"
    if "cluster" in token:
        return "cluster"
    if "branch" in token or "bark" in token:
        return "branch"
    if "frond" in token:
        return "frond"
    return None


def remap_generator_asset_references(
    source_document: SPMDocument,
    target_document: SPMDocument,
    source_generator: ET.Element,
    target_generator: ET.Element,
    force: bool = False,
) -> tuple[int, list[dict[str, str]], list[str]]:
    """Translate source-local asset IDs into target-local IDs by asset name.

    Existing target asset choices remain untouched unless their current local
    ID is invalid or resolves to a conflicting semantic role (for example a
    Leaf property resolving to a cluster material).  Fresh clones use force so
    every resolvable material/mesh ID is translated before insertion.
    """
    source_properties = source_generator.find("Properties")
    target_properties = target_generator.find("Properties")
    if source_properties is None or target_properties is None:
        return 0, [], []
    target_by_name = {_property_name(item): item for item in list(target_properties)}
    updates = 0
    copied_assets: list[dict[str, str]] = []
    warnings: list[str] = []
    for source_prop in list(source_properties):
        property_name = _property_name(source_prop)
        kind = _asset_kind_for_property(property_name)
        if kind is None:
            continue
        target_prop = target_by_name.get(property_name)
        if target_prop is None:
            continue
        source_asset_name = source_document.asset_name(kind, _property_value(source_prop))
        if source_asset_name is None:
            continue
        current_id = _property_value(target_prop)
        current_asset_name = target_document.asset_name(kind, current_id)
        source_role = _asset_role(source_asset_name)
        current_role = _asset_role(current_asset_name)
        role_conflict = (
            source_role is not None and current_role is not None
            and source_role != current_role
        )
        should_update = force or current_asset_name is None or role_conflict
        target_asset_id = target_document.asset_id(kind, source_asset_name)
        if should_update and target_asset_id is None:
            target_asset_id = target_document.copy_asset_from(
                source_document,
                kind,
                _property_value(source_prop),
                copied_assets,
                warnings,
            )
        if target_asset_id is None:
            continue
        if should_update and current_id != target_asset_id:
            _set_child_text(target_prop, "Value", target_asset_id)
            updates += 1
    return updates, copied_assets, warnings


def clone_subtree(
    source_document: SPMDocument,
    target_document: SPMDocument,
    source_guid: str,
    source_parent_guid: str,
    target_parent_guid: str,
    target_parent_level: int,
    category: str,
    salt: str,
    result: BaseSyncResult,
    detail_path: str = "",
) -> str | None:
    source = source_document.by_guid[source_guid]
    if source_document.generator_type(source) == "BaseRef":
        result.warnings.append(
            f"마스터 전용 BaseRef는 자동 추가하지 않음: {source_document.generator_name(source)}"
        )
        return None
    clone = copy.deepcopy(source)
    new_guid = _new_guid()
    _set_child_text(clone, "GUID", new_guid)
    _set_child_text(clone, "Level", str(target_parent_level + 1))
    asset_updates, copied_assets, asset_warnings = remap_generator_asset_references(
        source_document, target_document, source, clone, force=True
    )
    result.asset_reference_updates += asset_updates
    result.copied_assets.extend(copied_assets)
    for warning in asset_warnings:
        if warning not in result.warnings:
            result.warnings.append(warning)
    _reroll_random_seeds(clone, f"{salt}|{source_guid}|{new_guid}")
    set_icon_background(clone, category)
    target_document.generators_element.append(clone)

    target_parent = target_document.by_guid[target_parent_guid]
    link = _clone_source_link(
        source_document,
        source_parent_guid,
        source_guid,
        target_parent_guid,
        new_guid,
        target_document.generator_name(target_parent),
        target_document.generator_name(clone),
    )
    target_document.links_element.append(link)
    result.added_nodes += 1
    source_name = source_document.generator_name(source)
    current_path = f"{detail_path} > {source_name}" if detail_path else source_name
    result.added_node_details.append({
        "base": result.target_base,
        "name": source_name,
        "type": source_document.generator_type(source),
        "path": current_path,
        "guid": new_guid,
    })

    # Make the new node visible to recursive calls without a full rebuild.
    target_document.by_guid[new_guid] = clone
    target_document.children[target_parent_guid].append(new_guid)
    for child_guid in source_document.child_guids(source_guid):
        clone_subtree(
            source_document,
            target_document,
            child_guid,
            source_guid,
            new_guid,
            target_parent_level + 1,
            category,
            salt,
            result,
            current_path,
        )
    return new_guid


def _children_by_type(document: SPMDocument, guid: str) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for child_guid in document.child_guids(guid):
        child = document.by_guid.get(child_guid)
        if child is not None:
            grouped[document.generator_type(child)].append(child_guid)
    return grouped


def _collect_subtree_details(
    document: SPMDocument,
    guid: str,
    base_name: str,
    path_prefix: str = "",
    exclude_source_baseref: bool = False,
) -> list[dict[str, str]]:
    generator = document.by_guid.get(guid)
    if generator is None:
        return []
    generator_type = document.generator_type(generator)
    if exclude_source_baseref and generator_type == "BaseRef":
        return []
    name = document.generator_name(generator)
    current_path = f"{path_prefix} > {name}" if path_prefix else name
    details = [{
        "base": base_name,
        "name": name,
        "type": generator_type,
        "path": current_path,
        "guid": guid,
    }]
    for child_guid in document.child_guids(guid):
        details.extend(_collect_subtree_details(
            document,
            child_guid,
            base_name,
            current_path,
            exclude_source_baseref=exclude_source_baseref,
        ))
    return details


def _matching_target_parent(
    source_document: SPMDocument,
    target_document: SPMDocument,
    source_guid: str,
) -> ET.Element | None:
    """Find the follower parent corresponding to a source Base parent."""
    source_parent_guid = source_document.parent.get(source_guid)
    source_parent = source_document.by_guid.get(source_parent_guid or "")
    if source_parent is None:
        return None
    parent_type = source_document.generator_type(source_parent)
    candidates = [
        item for item in target_document.generators
        if target_document.generator_type(item) == parent_type
    ]
    source_name = source_document.generator_name(source_parent)
    exact = [
        item for item in candidates
        if target_document.generator_name(item).casefold() == source_name.casefold()
    ]
    if len(exact) == 1:
        return exact[0]
    return candidates[0] if len(candidates) == 1 else None


def sync_subtree(
    source_document: SPMDocument,
    target_document: SPMDocument,
    source_guid: str,
    target_guid: str,
    category: str,
    salt: str,
    result: BaseSyncResult,
) -> None:
    source = source_document.by_guid[source_guid]
    target = target_document.by_guid[target_guid]
    result.matched_nodes += 1
    if source_document.generator_type(source) != "BaseRef":
        count, names = sync_generator_properties(source, target)
        result.property_updates += count
        result.changed_properties.extend(names)
        asset_updates, copied_assets, asset_warnings = remap_generator_asset_references(
            source_document, target_document, source, target, force=False
        )
        result.asset_reference_updates += asset_updates
        result.copied_assets.extend(copied_assets)
        for warning in asset_warnings:
            if warning not in result.warnings:
                result.warnings.append(warning)
    if set_icon_background(target, category):
        result.color_updates += 1

    source_groups = _children_by_type(source_document, source_guid)
    target_groups = _children_by_type(target_document, target_guid)
    for generator_type in sorted(set(source_groups) | set(target_groups)):
        source_children = source_groups.get(generator_type, [])
        target_children = target_groups.get(generator_type, [])
        paired = min(len(source_children), len(target_children))
        for index in range(paired):
            sync_subtree(
                source_document,
                target_document,
                source_children[index],
                target_children[index],
                category,
                salt,
                result,
            )
        for source_child in source_children[paired:]:
            try:
                parent_level = int(_child_text(target, "Level", "0"))
            except ValueError:
                parent_level = 0
            clone_subtree(
                source_document,
                target_document,
                source_child,
                source_guid,
                target_guid,
                parent_level,
                category,
                salt,
                result,
            )
        for target_child in target_children[paired:]:
            details = _collect_subtree_details(
                target_document, target_child, result.target_base
            )
            result.target_only_details.extend(details)
            result.target_only_nodes += len(details)


def _set_base_ref_filter(generator: ET.Element, value: str) -> bool:
    properties = generator.find("Properties")
    if properties is None:
        return False
    for prop in list(properties):
        if _property_name(prop) == "Settings:Base filter":
            if _property_value(prop) == value:
                return False
            _set_child_text(prop, "Value", value)
            return True
    return False


def _refresh_links_for_generators(document: SPMDocument, guids: set[str]) -> None:
    for link in document.links:
        source_guid = _child_text(link, "SourceGUID")
        target_guid = _child_text(link, "TargetGUID")
        if source_guid not in guids and target_guid not in guids:
            continue
        source = document.by_guid.get(source_guid)
        target = document.by_guid.get(target_guid)
        if source is not None and target is not None:
            _set_child_text(
                link,
                "Name",
                f"{document.generator_name(source)}->{document.generator_name(target)}",
            )


def _export_safe_name_token(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", str(value or "")).encode(
        "ascii", "ignore"
    ).decode("ascii")
    token = re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_")
    if not token:
        token = "Base"
    if token[0].isdigit():
        token = "Base_" + token
    return token


def _hierarchy_sort_key(document: SPMDocument, guid: str) -> tuple:
    path: list[int] = []
    current = guid
    seen: set[str] = set()
    while current in document.parent and current not in seen:
        seen.add(current)
        parent = document.parent[current]
        siblings = document.child_guids(parent)
        try:
            path.append(siblings.index(current))
        except ValueError:
            path.append(document.generator_index.get(current, 2**31 - 1))
        current = parent
    return tuple(reversed(path)) + (document.generator_index.get(guid, 2**31 - 1),)


def standardize_base_ref_names(
    document: SPMDocument,
) -> list[dict[str, str]]:
    """Give BaseRefs deterministic, export-safe names without touching Bases.

    The Base filter is read only.  Base names, Base GUIDs, and BaseRef filter
    values remain byte-for-byte unchanged; only BaseRef Name and link display
    names are updated.
    """
    refs = document.base_refs()
    occupied = {
        document.generator_name(item).casefold()
        for item in document.generators
        if document.generator_type(item) != "BaseRef"
    }
    groups: dict[str, list[ET.Element]] = defaultdict(list)
    labels: dict[str, str] = {}
    for ref in refs:
        filter_name = document.base_ref_filter(ref)
        base = document.resolve_base(filter_name)
        base_name = document.generator_name(base) if base is not None else filter_name
        token = _export_safe_name_token(base_name or "Unmapped")
        key = token.casefold()
        groups[key].append(ref)
        labels[key] = token

    renames: list[dict[str, str]] = []
    renamed_guids: set[str] = set()
    for key in sorted(groups):
        token = labels[key]
        refs_in_group = sorted(
            groups[key],
            key=lambda item: _hierarchy_sort_key(document, document.generator_guid(item)),
        )
        number = 1
        for ref in refs_in_group:
            while True:
                candidate = f"Ref_{token}_{number:03d}"
                number += 1
                if candidate.casefold() not in occupied:
                    break
            occupied.add(candidate.casefold())
            old_name = document.generator_name(ref)
            if old_name == candidate:
                continue
            _set_child_text(ref, "Name", candidate)
            guid = document.generator_guid(ref)
            renamed_guids.add(guid)
            renames.append({
                "guid": guid,
                "base": document.generator_name(document.resolve_base(document.base_ref_filter(ref)))
                if document.resolve_base(document.base_ref_filter(ref)) is not None
                else document.base_ref_filter(ref),
                "old": old_name,
                "new": candidate,
            })
    _refresh_links_for_generators(document, renamed_guids)
    return renames


def suggest_base_map(
    source_document: SPMDocument,
    target_document: SPMDocument,
    source_categories: dict[str, str | None] | None = None,
) -> dict[str, str | None]:
    source_categories = source_categories or {}
    source_bases = source_document.base_nodes()
    result: dict[str, str | None] = {}
    for target_base in target_document.base_nodes():
        target_name = target_document.generator_name(target_base)
        exact = source_document.resolve_base(target_name)
        if exact is not None:
            result[target_name] = source_document.generator_name(exact)
            continue
        category = classify_base_name(target_name)
        category_candidates = []
        for source_base in source_bases:
            source_name = source_document.generator_name(source_base)
            source_category = (
                source_categories.get(source_name)
                or source_categories.get(canonical_base_name(source_name))
                or classify_base_name(source_name)
            )
            if source_category == category and category is not None:
                category_candidates.append(source_base)
        if len(category_candidates) == 1:
            result[target_name] = source_document.generator_name(category_candidates[0])
            continue
        signature = target_document.base_type_signature(target_base)
        signature_candidates = [
            source_base for source_base in source_bases
            if source_document.base_type_signature(source_base) == signature
        ]
        result[target_name] = (
            source_document.generator_name(signature_candidates[0])
            if len(signature_candidates) == 1
            else None
        )
    return result


def source_base_categories(
    source_document: SPMDocument,
    overrides: dict[str, str | None] | None = None,
) -> dict[str, str | None]:
    overrides = overrides or {}
    result: dict[str, str | None] = {}
    for base in source_document.base_nodes():
        name = source_document.generator_name(base)
        result[name] = (
            overrides.get(name)
            or overrides.get(canonical_base_name(name))
            or classify_base_name(name)
        )
    return result


def tree_shape_radius(document: SPMDocument) -> float | None:
    """Return the authoring radius used by SpeedTree's root generator."""
    tree = next(
        (item for item in document.generators if document.generator_type(item) == "Tree"),
        None,
    )
    if tree is None:
        return None
    for prop in tree.findall("./Properties/Property"):
        if _property_name(prop).strip().casefold() != "shape:radius":
            continue
        try:
            value = float(_property_value(prop))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return None


def assess_scale_risk(
    master_document: SPMDocument,
    target_document: SPMDocument,
) -> dict[str, object]:
    """Classify small-master → large-target generator multiplication risk."""
    master_radius = tree_shape_radius(master_document)
    target_radius = tree_shape_radius(target_document)
    if master_radius is None or target_radius is None:
        return {}
    ratio = target_radius / master_radius
    if ratio >= SCALE_RISK_BLOCK_RATIO:
        level = "blocked"
    elif ratio >= SCALE_RISK_WARN_RATIO:
        level = "warning"
    else:
        level = "safe"
    return {
        "level": level,
        "ratio": ratio,
        "master_radius": master_radius,
        "target_radius": target_radius,
        "warn_ratio": SCALE_RISK_WARN_RATIO,
        "block_ratio": SCALE_RISK_BLOCK_RATIO,
    }


def build_sync_plan(
    master_path: Path,
    target_path: Path,
    base_map: dict[str, str | None],
    base_categories: dict[str, str | None] | None = None,
    master_document: SPMDocument | None = None,
) -> SyncPlan:
    master_path = Path(master_path)
    target_path = Path(target_path)
    source = master_document or SPMDocument.from_path(master_path, full=True)
    target = SPMDocument.from_path(target_path, full=True)
    original_target_text = target.text
    legacy_cluster_guids = set(
        inspect_legacy_cluster_state(target_path)[
            "classified_generator_guids"
        ]
    )
    legacy_foregrounds = {
        guid: _icon_foreground_values(target.by_guid[guid])
        for guid in legacy_cluster_guids if guid in target.by_guid
    }
    categories = source_base_categories(source, base_categories)
    plan = SyncPlan(master=str(master_path), target=str(target_path), compressed=target.compressed)
    plan.scale_risk = assess_scale_risk(source, target)

    target_names = {target.generator_name(base) for base in target.base_nodes()}
    for target_name in sorted(target_names, key=str.casefold):
        if target_name not in base_map:
            plan.mapping_required.append(target_name)

    used_source: set[str] = set()
    for target_name, source_name in base_map.items():
        if source_name in (None, "", "__independent__"):
            continue
        target_base = target.resolve_base(target_name)
        source_base = source.resolve_base(source_name)
        if target_base is None:
            plan.mapping_required.append(f"대상 Base 없음: {target_name}")
            continue
        if source_base is None:
            plan.mapping_required.append(f"마스터 Base 없음: {source_name}")
            continue
        resolved_source_name = source.generator_name(source_base)
        category = categories.get(resolved_source_name)
        if category not in CATEGORY_COLORS:
            plan.mapping_required.append(f"색 분류 없음: {resolved_source_name}")
            continue
        used_source.add(resolved_source_name)
        result = BaseSyncResult(
            source_base=resolved_source_name,
            target_base=target.generator_name(target_base),
            category=category,
        )
        sync_subtree(
            source,
            target,
            source.generator_guid(source_base),
            target.generator_guid(target_base),
            category,
            f"{target_path}|{target.generator_name(target_base)}",
            result,
        )
        plan.base_results.append(result)

    # A master Base with no governed counterpart is additive: create the Base
    # itself and its subtree under the matching follower Tree/root generator.
    for source_base in source.base_nodes():
        source_name = source.generator_name(source_base)
        if source_name in used_source:
            continue
        category = categories.get(source_name)
        if category not in CATEGORY_COLORS:
            plan.mapping_required.append(f"색 분류 없음: {source_name}")
            continue
        existing_name = [
            base for base in target.base_nodes()
            if canonical_base_name(target.generator_name(base)) == canonical_base_name(source_name)
        ]
        if existing_name:
            plan.mapping_required.append(f"새 Base 이름 충돌: {source_name}")
            continue
        target_parent = _matching_target_parent(
            source, target, source.generator_guid(source_base)
        )
        if target_parent is None:
            plan.mapping_required.append(f"새 Base를 연결할 부모 없음: {source_name}")
            continue
        try:
            parent_level = int(_child_text(target_parent, "Level", "0"))
        except ValueError:
            parent_level = 0
        result = BaseSyncResult(
            source_base=source_name,
            target_base=source_name,
            category=category,
            created_base=True,
        )
        new_guid = clone_subtree(
            source,
            target,
            source.generator_guid(source_base),
            source.parent[source.generator_guid(source_base)],
            target.generator_guid(target_parent),
            parent_level,
            category,
            f"{target_path}|new-base|{source_name}",
            result,
        )
        if new_guid is None:
            plan.mapping_required.append(f"새 Base 추가 실패: {source_name}")
            continue
        plan.base_results.append(result)
        plan.added_base_mappings[source_name] = source_name
        used_source.add(source_name)

    target.reindex()
    plan.pass_adjustments = repair_generation_passes(target)
    plan.reference_renames = standardize_base_ref_names(target)
    target_color_categories: dict[str, str | None] = {}
    unique_guids: set[str] = set()
    for item in plan.base_results:
        target_color_categories[item.target_base] = item.category
        unique_guids.update(
            detail["guid"] for detail in item.target_only_details
            if detail.get("guid")
        )
    # sync_subtree applies the role palette while copying settings. Restore
    # the exact receipt-owned foreground observed at transaction start before
    # the final background-only palette pass. Drifted/user-edited foregrounds
    # are preserved as-is; this is not an automatic marker repair.
    for guid, values in legacy_foregrounds.items():
        node = target.by_guid.get(guid)
        if node is not None:
            _restore_icon_foreground(node, values)
    target_color_updates, color_warnings = standardize_base_colors(
        target,
        target_color_categories,
        unique_guids=unique_guids,
        preserve_foreground_guids=legacy_cluster_guids,
    )
    plan.target_color_updates = target_color_updates
    plan.warnings.extend(color_warnings)
    plan.unmapped_source_bases = [
        source.generator_name(base) for base in source.base_nodes()
        if source.generator_name(base) not in used_source
    ]
    target.validate()
    rendered = target.render()
    target.validate(rendered)
    plan.patched_text = rendered
    plan.changed = rendered != original_target_text
    return plan


def standardize_master_document(
    master_path: Path,
    base_categories: dict[str, str | None] | None = None,
) -> tuple[SPMDocument, str, int, list[dict[str, str]], list[str]]:
    document = SPMDocument.from_path(master_path, full=True)
    categories = source_base_categories(document, base_categories)
    reference_renames = standardize_base_ref_names(document)
    legacy_cluster_guids = set(
        inspect_legacy_cluster_state(master_path)[
            "classified_generator_guids"
        ]
    )
    updates, warnings = standardize_base_colors(
        document,
        categories,
        preserve_foreground_guids=legacy_cluster_guids,
    )
    document.reindex()
    document.validate()
    rendered = document.render()
    document.validate(rendered)
    # Followers should read the standardized source in the same transaction.
    standardized = SPMDocument(Path(master_path), rendered, document.compressed, full=True)
    return standardized, rendered, updates, reference_renames, warnings


def base_sync_signature(
    document: SPMDocument,
    base_categories: dict[str, str | None] | None = None,
) -> str:
    categories = source_base_categories(document, base_categories)
    payload: list[object] = []
    for base in sorted(document.base_nodes(), key=lambda item: document.generator_name(item).casefold()):
        base_name = document.generator_name(base)
        category = categories.get(base_name)

        def visit(guid: str) -> dict[str, object]:
            generator = document.by_guid[guid]
            props = []
            properties = generator.find("Properties")
            if properties is not None:
                for prop in list(properties):
                    if not is_protected_property(_property_name(prop)):
                        props.append(_normalized_xml(prop))
            return {
                "type": document.generator_type(generator),
                "properties": props,
                "children": [visit(child) for child in document.child_guids(guid)],
            }

        payload.append({"base": canonical_base_name(base_name), "category": category,
                        "tree": visit(document.generator_guid(base))})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _common_target_payload(
    source_document: SPMDocument,
    target_document: SPMDocument,
    source_guid: str,
    target_guid: str,
) -> dict[str, object]:
    target = target_document.by_guid[target_guid]
    properties = []
    target_properties = target.find("Properties")
    if target_properties is not None:
        for prop in list(target_properties):
            if not is_protected_property(_property_name(prop)):
                properties.append(_normalized_xml(prop))
    children = []
    source_groups = _children_by_type(source_document, source_guid)
    target_groups = _children_by_type(target_document, target_guid)
    for generator_type in sorted(source_groups):
        source_children = source_groups[generator_type]
        target_children = target_groups.get(generator_type, [])
        for index, source_child in enumerate(source_children):
            if index < len(target_children):
                children.append(_common_target_payload(
                    source_document, target_document,
                    source_child, target_children[index],
                ))
            else:
                children.append({"type": generator_type, "missing": True})
    return {
        "type": target_document.generator_type(target),
        "properties": properties,
        "children": children,
    }


def target_sync_signature(
    source_document: SPMDocument,
    target_document: SPMDocument,
    base_map: dict[str, str | None],
) -> str:
    """Hash only the follower data governed by the master.

    Target-only generators are intentionally excluded. Protected identity and
    asset properties are excluded for the same reason.
    """
    payload = []
    for target_name, source_name in sorted(base_map.items(), key=lambda item: item[0].casefold()):
        if source_name in (None, "", "__independent__"):
            continue
        source_base = source_document.resolve_base(source_name)
        target_base = target_document.resolve_base(target_name)
        if source_base is None or target_base is None:
            payload.append({"target": target_name, "source": source_name, "missing_base": True})
            continue
        payload.append({
            "target": canonical_base_name(target_name),
            "source": canonical_base_name(source_name),
            "tree": _common_target_payload(
                source_document, target_document,
                source_document.generator_guid(source_base),
                target_document.generator_guid(target_base),
            ),
        })
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _structure_delta(
    source_document: SPMDocument,
    target_document: SPMDocument,
    source_guid: str,
    target_guid: str,
    source_base_name: str = "",
    target_base_name: str = "",
    missing_details: list[dict[str, str]] | None = None,
    target_only_details: list[dict[str, str]] | None = None,
) -> tuple[int, int, int]:
    common = 1
    missing = 0
    target_only = 0
    source_groups = _children_by_type(source_document, source_guid)
    target_groups = _children_by_type(target_document, target_guid)
    for generator_type in sorted(set(source_groups) | set(target_groups)):
        source_children = source_groups.get(generator_type, [])
        target_children = target_groups.get(generator_type, [])
        paired = min(len(source_children), len(target_children))
        for index in range(paired):
            child_common, child_missing, child_only = _structure_delta(
                source_document, target_document,
                source_children[index], target_children[index],
                source_base_name, target_base_name,
                missing_details, target_only_details,
            )
            common += child_common
            missing += child_missing
            target_only += child_only
        for source_child in source_children[paired:]:
            details = _collect_subtree_details(
                source_document,
                source_child,
                source_base_name,
                exclude_source_baseref=True,
            )
            missing += len(details)
            if missing_details is not None:
                missing_details.extend(details)
        for target_child in target_children[paired:]:
            details = _collect_subtree_details(
                target_document, target_child, target_base_name
            )
            target_only += len(details)
            if target_only_details is not None:
                target_only_details.extend(details)
    return common, missing, target_only


def compare_base_structure(
    source_document: SPMDocument,
    target_document: SPMDocument,
    base_map: dict[str, str | None],
    include_details: bool = False,
) -> dict[str, object]:
    totals = {
        "common": 0, "missing": 0, "target_only": 0,
        "mapping_errors": 0, "missing_bases": 0,
    }
    if include_details:
        totals["missing_details"] = []
        totals["target_only_details"] = []
    used_source: set[str] = set()
    for target_name, source_name in base_map.items():
        if source_name in (None, "", "__independent__"):
            continue
        source_base = source_document.resolve_base(source_name)
        target_base = target_document.resolve_base(target_name)
        if source_base is None or target_base is None:
            totals["mapping_errors"] += 1
            continue
        used_source.add(source_document.generator_name(source_base))
        common, missing, target_only = _structure_delta(
            source_document, target_document,
            source_document.generator_guid(source_base),
            target_document.generator_guid(target_base),
            source_document.generator_name(source_base),
            target_document.generator_name(target_base),
            totals.get("missing_details") if include_details else None,
            totals.get("target_only_details") if include_details else None,
        )
        totals["common"] += common
        totals["missing"] += missing
        totals["target_only"] += target_only
    for source_base in source_document.base_nodes():
        source_name = source_document.generator_name(source_base)
        if source_name not in used_source:
            totals["missing_bases"] += 1
            details = _collect_subtree_details(
                source_document,
                source_document.generator_guid(source_base),
                source_name,
                exclude_source_baseref=True,
            )
            totals["missing"] += len(details)
            if include_details:
                totals["missing_details"].extend(details)
    return totals


def default_manifest() -> dict:
    return {"version": 1, "groups": [], "independent": []}


def load_manifest(folder: Path) -> dict:
    folder = Path(folder)
    path = folder / MANIFEST_NAME
    if not path.exists():
        return default_manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"관계 설정 파일을 읽을 수 없습니다: {path}: {exc}") from exc
    data.setdefault("version", 1)
    data.setdefault("groups", [])
    data.setdefault("independent", [])
    return data


def save_manifest(folder: Path, manifest: dict) -> Path:
    folder = Path(folder)
    path = folder / MANIFEST_NAME
    temporary = folder / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def relation_index(manifest: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for group in manifest.get("groups", []):
        master = group.get("master", "")
        if master:
            result[master] = {"role": "master", "master": master, "group": group}
        for follower in group.get("followers", []):
            name = follower.get("file", "")
            if name:
                result[name] = {
                    "role": "follower", "master": master,
                    "group": group, "follower": follower,
                }
    for name in manifest.get("independent", []):
        result[name] = {"role": "independent", "master": None}
    return result


def _remove_relation(manifest: dict, filename: str) -> None:
    manifest["independent"] = [item for item in manifest.get("independent", []) if item != filename]
    new_groups = []
    for group in manifest.get("groups", []):
        if group.get("master") == filename:
            # Existing followers become unassigned instead of silently independent.
            continue
        group["followers"] = [
            item for item in group.get("followers", []) if item.get("file") != filename
        ]
        new_groups.append(group)
    manifest["groups"] = new_groups


def set_master(manifest: dict, filename: str) -> dict:
    _remove_relation(manifest, filename)
    manifest.setdefault("groups", []).append({
        "master": filename,
        "base_categories": {},
        "followers": [],
    })
    return manifest


def promote_master(
    folder: Path,
    filename: str,
    verify_callback=None,
) -> dict:
    """Promote one SPM and standardize its master-side Base data immediately."""

    folder = Path(folder)
    master_path = folder / filename
    if not master_path.is_file():
        raise SyncError(f"마스터 SPM이 없습니다: {master_path}")

    manifest = load_manifest(folder)
    set_master(manifest, filename)
    group = find_group(manifest, filename)
    source = SPMDocument.from_path(master_path, full=False)
    categories = source_base_categories(source)
    group["base_categories"] = categories

    _document, rendered, color_updates, reference_renames, warnings = (
        standardize_master_document(master_path, categories)
    )
    original_text, compressed = read_spm_text(master_path)
    changed = rendered != original_text

    validate_xml_text(rendered)
    check = SPMDocument(master_path, rendered, compressed, full=True)
    check.validate()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = folder / BACKUP_SUBDIR / f"master_promotion_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    master_backup = backup_dir / f"01_{filename}"
    shutil.copy2(master_path, master_backup)

    manifest_path = folder / MANIFEST_NAME
    manifest_existed = manifest_path.is_file()
    manifest_backup = backup_dir / f"00_{MANIFEST_NAME}"
    if manifest_existed:
        shutil.copy2(manifest_path, manifest_backup)

    try:
        if changed:
            write_spm_text(master_path, rendered, compressed)
            written, written_compressed = read_spm_text(master_path)
            if written != rendered or written_compressed != compressed:
                raise SyncError(f"저장 후 바이트 검증 실패: {filename}")
            SPMDocument(master_path, written, compressed, full=True).validate()
            if verify_callback is not None:
                verify_callback(master_path)
        save_manifest(folder, manifest)
    except Exception:
        shutil.copy2(master_backup, master_path)
        if manifest_existed and manifest_backup.is_file():
            shutil.copy2(manifest_backup, manifest_path)
        elif not manifest_existed:
            manifest_path.unlink(missing_ok=True)
        raise

    return {
        "manifest": manifest,
        "master": str(master_path),
        "changed": changed,
        "color_updates": color_updates,
        "reference_renames": reference_renames,
        "warnings": warnings,
        "backup_dir": str(backup_dir),
    }


def set_independent(manifest: dict, filenames: list[str]) -> dict:
    for filename in filenames:
        _remove_relation(manifest, filename)
        manifest.setdefault("independent", []).append(filename)
    manifest["independent"] = sorted(set(manifest["independent"]), key=str.casefold)
    return manifest


def assign_follower(
    manifest: dict,
    master: str,
    filename: str,
    base_map: dict[str, str | None],
    confirmed: bool = True,
) -> dict:
    _remove_relation(manifest, filename)
    group = next((item for item in manifest.get("groups", []) if item.get("master") == master), None)
    if group is None:
        raise SyncError(f"마스터가 설정에 없습니다: {master}")
    group.setdefault("followers", []).append({
        "file": filename,
        "base_map": base_map,
        "base_map_confirmed": bool(confirmed),
        "last_sync": None,
        "last_master_hash": None,
        "last_target_hash": None,
    })
    group["followers"].sort(key=lambda item: item.get("file", "").casefold())
    return manifest


def scan_tree_folders(root: Path, sk_only: bool = False) -> list[dict]:
    root = Path(root)
    if not root.is_dir():
        raise SyncError(f"나무 루트 폴더가 없습니다: {root}")
    folders: list[dict] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [
            name for name in dirs
            if name.lower() not in {item.lower() for item in SKIP_DIRS}
            and name.casefold() != "cluster"
        ]
        folder = Path(current)
        spms = sorted(
            [
                folder / name
                for name in files
                if is_active_spm(folder / name)
                and (not sk_only or name.lower().startswith("sk_"))
            ],
            key=lambda item: item.name.casefold(),
        )
        cluster_blends = json.loads(json.dumps(
            discover_cluster_blend_relations(folder),
            default=str,
        ))
        if not spms and not cluster_blends:
            continue
        manifest = load_manifest(folder)
        relations = relation_index(manifest)
        candidates = [
            path.name for path in spms
            if path.stem.lower().endswith("_01") and path.name not in relations
        ]
        folders.append({
            "folder": str(folder),
            "spms": [path.name for path in spms],
            "manifest": manifest,
            "relations": relations,
            "master_candidates": candidates,
            "cluster_blends": cluster_blends,
        })
    return folders


def find_group(manifest: dict, master: str) -> dict:
    group = next((item for item in manifest.get("groups", []) if item.get("master") == master), None)
    if group is None:
        raise SyncError(f"마스터 그룹이 없습니다: {master}")
    return group


def verify_speedtree_export(
    spm_path: Path,
    speedtree_exe: Path,
    xml_ini: Path,
    timeout: int = 300,
) -> None:
    spm_path = Path(spm_path)
    speedtree_exe = Path(speedtree_exe)
    xml_ini = Path(xml_ini)
    if not speedtree_exe.is_file():
        raise SyncError(f"SpeedTree 실행 파일이 없습니다: {speedtree_exe}")
    if not xml_ini.is_file():
        raise SyncError(f"SpeedTree XML export 설정이 없습니다: {xml_ini}")
    with tempfile.TemporaryDirectory(prefix="spm_generator_sync_verify_") as temp:
        output = Path(temp) / f"{spm_path.stem}_verify.xml"
        cmd = [
            str(speedtree_exe), str(spm_path),
            "-export_options", str(xml_ini),
            "-export", str(output),
        ]
        result = subprocess.run(
            cmd,
            cwd=str(spm_path.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode != 0 or not output.is_file():
            detail = (result.stderr or result.stdout or "")[-1000:]
            raise SyncError(
                f"SpeedTree 10.1 XML 검증 실패: {spm_path.name} "
                f"(code {result.returncode}) {detail}"
            )
        try:
            ET.parse(output)
        except ET.ParseError as exc:
            raise SyncError(f"SpeedTree export XML 파싱 실패: {spm_path.name}: {exc}") from exc


def verify_temporary_patches(
    patches: list[tuple[Path, str, bool]],
    callback,
    progress_callback=None,
) -> None:
    """Compute patched sibling copies before originals are ever overwritten."""
    token = uuid.uuid4().hex[:10]
    temporary_paths: list[Path] = []
    try:
        total = len(patches)
        for index, (path, text, compressed) in enumerate(patches, start=1):
            temporary = path.parent / f"__spm_sync_preflight_{token}_{path.name}"
            write_spm_text(temporary, text, compressed)
            temporary_paths.append(temporary)
            if progress_callback is not None:
                progress_callback(path, index, total)
            callback(temporary)
    except subprocess.TimeoutExpired as exc:
        raise SyncError(
            "SpeedTree 사전검사가 5분을 넘겨 중단되었습니다. 노드·폴리곤 폭증 "
            "가능성이 있어 원본은 수정하지 않았습니다."
        ) from exc
    except Exception as exc:
        if isinstance(exc, SyncError):
            raise SyncError(f"SpeedTree 사전검사 실패 · 원본 미수정: {exc}") from exc
        raise
    finally:
        for path in temporary_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for path, _text, _compressed in patches:
            for leftover in path.parent.glob(f"*{token}*"):
                if leftover.is_file() and leftover.name.startswith(("~", "__spm_sync_preflight_")):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass


def apply_pass_repair_transaction(
    spm_path: Path,
    verify_callback=None,
) -> dict:
    """Repair only Generation Pass dependencies with backup and rollback."""
    spm_path = Path(spm_path)
    if not spm_path.is_file():
        raise SyncError(f"SPM이 없습니다: {spm_path}")
    original_bytes = spm_path.read_bytes()
    document = SPMDocument.from_path(spm_path, full=True)
    adjustments = repair_generation_passes(document)
    if not adjustments:
        document.validate()
        return {
            "status": "unchanged",
            "file": str(spm_path),
            "backup_dir": None,
            "pass_adjustments": [],
        }

    rendered = document.render()
    document.validate(rendered)
    if verify_callback is not None:
        verify_temporary_patches(
            [(spm_path, rendered, document.compressed)],
            verify_callback,
        )
    if spm_path.read_bytes() != original_bytes:
        raise SyncError(
            f"사전검사 중 파일이 변경되어 Pass 복구를 중단했습니다: {spm_path.name}"
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = spm_path.parent / BACKUP_SUBDIR / f"pass_repair_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    spm_backup = backup_dir / f"01_{spm_path.name}"
    temporary = spm_path.with_name(
        f".__spm_pass_repair_{uuid.uuid4().hex[:10]}_{spm_path.name}"
    )
    replaced_original = False
    try:
        write_spm_text(temporary, rendered, document.compressed)
        candidate_text, candidate_compressed = read_spm_text(temporary)
        candidate = SPMDocument(
            temporary, candidate_text, candidate_compressed, full=True
        )
        candidate.validate(candidate_text)

        shutil.copy2(spm_path, spm_backup)
        if spm_backup.read_bytes() != original_bytes:
            raise SyncError(
                f"백업 직전 파일이 변경되어 Pass 복구를 중단했습니다: {spm_path.name}"
            )
        autosave_path = spm_path.with_name(f"~{spm_path.stem}.sbk")
        if autosave_path.is_file():
            shutil.copy2(autosave_path, backup_dir / f"02_{autosave_path.name}")
        # Keep the final comparison adjacent to the atomic replacement so an
        # open Modeler or OneDrive update is far less likely to be overwritten.
        if spm_path.read_bytes() != original_bytes:
            raise SyncError(
                f"저장 직전 파일이 변경되어 Pass 복구를 중단했습니다: {spm_path.name}"
            )
        os.replace(temporary, spm_path)
        replaced_original = True
        written_text, written_compressed = read_spm_text(spm_path)
        written = SPMDocument(spm_path, written_text, written_compressed, full=True)
        written.validate(written_text)
    except Exception:
        if replaced_original:
            shutil.copy2(spm_backup, spm_path)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "applied",
        "file": str(spm_path),
        "backup_dir": str(backup_dir),
        "pass_adjustments": adjustments,
    }


def verify_auto_copies(
    folder: Path,
    master_name: str,
    target_names: list[str],
    speedtree_exe: Path,
    xml_ini: Path,
) -> list[SyncPlan]:
    """Apply auto suggestions to temporary sibling copies and verify in Modeler.

    Sibling copies keep all relative texture/mesh references valid. Originals are
    never written. Every temporary file is removed in a finally block.
    """
    folder = Path(folder)
    master_path = folder / master_name
    source_prefix = SPMDocument.from_path(master_path, full=False)
    categories = source_base_categories(source_prefix)
    master_doc, master_text, color_updates, master_ref_renames, color_warnings = standardize_master_document(
        master_path, categories
    )
    token = uuid.uuid4().hex[:10]
    temporary_paths: list[Path] = []
    plans: list[SyncPlan] = []
    try:
        temp_master = folder / f"__spm_sync_verify_{token}_{master_name}"
        write_spm_text(temp_master, master_text, master_doc.compressed)
        temporary_paths.append(temp_master)
        verify_speedtree_export(temp_master, speedtree_exe, xml_ini)
        for target_name in target_names:
            target_path = folder / target_name
            target_prefix = SPMDocument.from_path(target_path, full=False)
            mapping = suggest_base_map(source_prefix, target_prefix, categories)
            plan = build_sync_plan(
                master_path, target_path, mapping, categories,
                master_document=master_doc,
            )
            plan.master_color_updates = color_updates
            plan.master_reference_renames = master_ref_renames
            plan.warnings.extend(color_warnings)
            if plan.mapping_required:
                raise SyncError(
                    f"{target_name} 자동 매핑 확인 필요: " + ", ".join(plan.mapping_required)
                )
            temp_target = folder / f"__spm_sync_verify_{token}_{target_name}"
            write_spm_text(temp_target, plan.patched_text, plan.compressed)
            temporary_paths.append(temp_target)
            verify_speedtree_export(temp_target, speedtree_exe, xml_ini)
            plans.append(plan)
        return plans
    finally:
        for path in temporary_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in folder.glob(f"*{token}*"):
            if path.is_file() and path.name.startswith(("~", "__spm_sync_verify_")):
                try:
                    path.unlink()
                except OSError:
                    pass


def apply_group_transaction(
    folder: Path,
    master_name: str,
    follower_names: list[str] | None = None,
    verify_speedtree: bool = True,
    speedtree_exe: Path | None = None,
    xml_ini: Path | None = None,
    verify_callback=None,
    progress_callback=None,
) -> dict:
    """Preflight, backup, write, verify, and rollback a whole master group."""
    folder = Path(folder)
    if progress_callback is not None:
        progress_callback("설정과 마스터 읽는 중", 2)
    manifest = load_manifest(folder)
    group = find_group(manifest, master_name)
    configured = {item.get("file"): item for item in group.get("followers", [])}
    selected = follower_names or list(configured)
    if not selected:
        raise SyncError("동기화할 자식 SPM이 없습니다")
    unknown = [name for name in selected if name not in configured]
    if unknown:
        raise SyncError("마스터의 자식이 아닌 파일: " + ", ".join(unknown))

    master_path = folder / master_name
    if not master_path.is_file():
        raise SyncError(f"마스터 SPM이 없습니다: {master_path}")
    categories = group.get("base_categories") or {}
    master_doc, master_text, master_color_updates, master_ref_renames, master_warnings = standardize_master_document(
        master_path, categories
    )
    master_hash = base_sync_signature(master_doc, categories)

    plans: list[SyncPlan] = []
    for index, name in enumerate(selected, start=1):
        if progress_callback is not None:
            percent = 5 + round(30 * (index - 1) / max(1, len(selected)))
            progress_callback(f"패치 계산 중 · {name} ({index}/{len(selected)})", percent)
        entry = configured[name]
        if not entry.get("base_map_confirmed"):
            raise SyncError(f"Base 매핑을 먼저 확인해야 합니다: {name}")
        target_path = folder / name
        if not target_path.is_file():
            raise SyncError(f"자식 SPM이 없습니다: {target_path}")
        plan = build_sync_plan(
            master_path,
            target_path,
            entry.get("base_map") or {},
            categories,
            master_document=master_doc,
        )
        plan.master_color_updates = master_color_updates
        plan.master_reference_renames = master_ref_renames
        plan.warnings.extend(master_warnings)
        if plan.mapping_required:
            raise SyncError(f"{name} Base 매핑 확인 필요: " + ", ".join(plan.mapping_required))
        plans.append(plan)

    # Every patch below is built from the bytes just read, so remember them and
    # re-check right before the write.
    plan_fingerprints = {
        path: _spm_fingerprint(path)
        for path in dict.fromkeys([master_path, *(folder / name for name in selected)])
        if path.is_file()
    }

    if progress_callback is not None:
        progress_callback("크기 위험과 XML 무결성 확인 중", 38)

    blocked = [plan for plan in plans if plan.scale_risk.get("level") == "blocked"]
    if blocked:
        details = []
        for plan in blocked:
            risk = plan.scale_risk
            details.append(
                f"{Path(plan.target).name}: 반경 {risk['master_radius']:g} → "
                f"{risk['target_radius']:g} ({risk['ratio']:.2f}배)"
            )
        raise SyncError(
            "크기 차이로 노드·폴리곤 폭증 위험이 있어 원본을 수정하지 않았습니다. "
            "큰 나무용 Base를 별도 마스터로 사용하세요.\n" + "\n".join(details)
        )

    patches: list[tuple[Path, str, bool]] = []
    original_master_text, master_compressed = read_spm_text(master_path)
    if master_text != original_master_text:
        patches.append((master_path, master_text, master_compressed))
    for plan in plans:
        if plan.changed and plan.patched_text is not None:
            patches.append((Path(plan.target), plan.patched_text, plan.compressed))
    if not patches:
        now = datetime.now().isoformat(timespec="seconds")
        for plan in plans:
            entry = configured[Path(plan.target).name]
            updated_map = {
                plan.renamed_bases.get(target_name, target_name): source_name
                for target_name, source_name in (entry.get("base_map") or {}).items()
            }
            updated_map.update(plan.added_base_mappings)
            entry["base_map"] = updated_map
            entry["last_sync"] = now
            entry["last_master_hash"] = master_hash
            target_doc = SPMDocument(Path(plan.target), plan.patched_text, plan.compressed, full=True)
            entry["last_target_hash"] = target_sync_signature(
                master_doc, target_doc, entry.get("base_map") or {}
            )
        save_manifest(folder, manifest)
        if progress_callback is not None:
            progress_callback("이미 최신 · 관계 정보 확인 완료", 100)
        return {
            "status": "up_to_date", "changed_files": [], "backup_dir": None,
            "plans": plans, "master_hash": master_hash,
        }

    # All in-memory patches must be valid before the first filesystem mutation.
    for path, text, _compressed in patches:
        validate_xml_text(text)
        check = SPMDocument(path, text, _compressed, full=True)
        check.validate()

    if progress_callback is not None:
        progress_callback("메모리 패치 검증 완료", 44)

    # Built-in Modeler validation is a true preflight: temporary sibling SPMs
    # are computed first and originals remain byte-identical on any failure.
    callback = verify_callback
    if callback is None and verify_speedtree:
        if speedtree_exe is None or xml_ini is None:
            raise SyncError("SpeedTree 검증 경로가 설정되지 않았습니다")
        verify_temporary_patches(
            patches,
            lambda path: verify_speedtree_export(path, speedtree_exe, xml_ini),
            progress_callback=lambda path, index, total: progress_callback(
                f"SpeedTree 사전검사 · {path.name} ({index}/{total})",
                45 + round(30 * (index - 1) / max(1, total)),
            ) if progress_callback is not None else None,
        )

    if progress_callback is not None:
        progress_callback("사전검사 완료 · 동시 수정 확인 중", 76)

    _assert_spm_unchanged(plan_fingerprints)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = folder / BACKUP_SUBDIR / f"generator_sync_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backups: list[tuple[Path, Path]] = []
    try:
        transaction_paths = list(dict.fromkeys(
            [master_path, *(folder / name for name in selected)]
        ))
        for index, path in enumerate(transaction_paths, start=1):
            if progress_callback is not None:
                progress_callback(
                    f"원본 백업 중 · {path.name} ({index}/{len(transaction_paths)})",
                    78 + round(8 * index / max(1, len(transaction_paths))),
                )
            backup = backup_dir / f"{index:02d}_{path.name}"
            shutil.copy2(path, backup)
            backups.append((path, backup))
        manifest_path = folder / MANIFEST_NAME
        if manifest_path.is_file():
            shutil.copy2(manifest_path, backup_dir / f"00_{MANIFEST_NAME}")
        for index, (path, text, compressed) in enumerate(patches, start=1):
            if progress_callback is not None:
                progress_callback(
                    f"검증된 SPM 저장 중 · {path.name} ({index}/{len(patches)})",
                    86 + round(8 * index / max(1, len(patches))),
                )
            write_spm_text(path, text, compressed)
        for path, text, _compressed in patches:
            written, _ = read_spm_text(path)
            if written != text:
                raise SyncError(f"저장 후 바이트 검증 실패: {path.name}")
            validate_xml_text(written)
            SPMDocument(path, written, path.read_bytes().startswith(b"\x1f\x8b"), full=True).validate()

        # Custom callbacks are retained as post-write transaction tests.  The
        # built-in SpeedTree path has already verified identical temp bytes.
        if callback is not None:
            for path, _text, _compressed in patches:
                callback(path)

        now = datetime.now().isoformat(timespec="seconds")
        for plan in plans:
            entry = configured[Path(plan.target).name]
            updated_map = {
                plan.renamed_bases.get(target_name, target_name): source_name
                for target_name, source_name in (entry.get("base_map") or {}).items()
            }
            updated_map.update(plan.added_base_mappings)
            entry["base_map"] = updated_map
            entry["last_sync"] = now
            entry["last_master_hash"] = master_hash
            target_doc = SPMDocument(
                Path(plan.target), plan.patched_text, plan.compressed, full=True
            )
            entry["last_target_hash"] = target_sync_signature(
                master_doc, target_doc, entry.get("base_map") or {}
            )
        save_manifest(folder, manifest)
        if progress_callback is not None:
            progress_callback("관계 정보 저장 완료", 100)
    except Exception:
        for path, backup in backups:
            if backup.exists():
                shutil.copy2(backup, path)
        raise
    return {
        "status": "applied",
        "changed_files": [str(path) for path, _text, _compressed in patches],
        "backup_dir": str(backup_dir),
        "plans": plans,
        "master_hash": master_hash,
    }


def inspect_file(path: Path) -> dict:
    document = SPMDocument.from_path(path, full=False)
    bases = []
    for base in document.base_nodes():
        name = document.generator_name(base)
        try:
            reference_count = len(document.refs_for_base(base))
        except SearchSyntaxError:
            reference_count = 0
        bases.append({
            "name": name,
            "category": classify_base_name(name),
            "signature": dict(document.base_type_signature(base)),
            "refs": reference_count,
        })
    return {"file": Path(path).name, "bases": bases, "errors": document.integrity_errors()}


def plan_to_json(plan: SyncPlan) -> dict:
    data = asdict(plan)
    data.pop("patched_text", None)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SpeedTree SPM master/follower generator sync")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("root")
    scan_parser.add_argument(
        "--sk-only",
        action="store_true",
        help="SK_로 시작하는 SPM만 표시합니다.",
    )

    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("spm", nargs="+")

    repair_pass_parser = sub.add_parser("repair-passes")
    repair_pass_parser.add_argument("spm")
    repair_pass_parser.add_argument("--no-speedtree-verify", action="store_true")
    repair_pass_parser.add_argument("--speedtree-exe")
    repair_pass_parser.add_argument("--xml-ini")

    preview_parser = sub.add_parser("preview")
    preview_parser.add_argument("folder")
    preview_parser.add_argument("master")
    preview_parser.add_argument("target")

    preview_auto_parser = sub.add_parser("preview-auto")
    preview_auto_parser.add_argument("folder")
    preview_auto_parser.add_argument("master")
    preview_auto_parser.add_argument("target", nargs="+")

    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("folder")
    apply_parser.add_argument("master")
    apply_parser.add_argument("target", nargs="*")
    apply_parser.add_argument("--no-speedtree-verify", action="store_true")
    apply_parser.add_argument("--speedtree-exe")
    apply_parser.add_argument("--xml-ini")

    verify_auto_parser = sub.add_parser("verify-auto-copy")
    verify_auto_parser.add_argument("folder")
    verify_auto_parser.add_argument("master")
    verify_auto_parser.add_argument("target", nargs="+")
    verify_auto_parser.add_argument("--speedtree-exe", required=True)
    verify_auto_parser.add_argument("--xml-ini", required=True)

    args = parser.parse_args(argv)
    if args.command == "scan":
        print(json.dumps(
            scan_tree_folders(Path(args.root), sk_only=args.sk_only),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    if args.command == "inspect":
        print(json.dumps([inspect_file(Path(path)) for path in args.spm], ensure_ascii=False, indent=2))
        return 0
    if args.command == "repair-passes":
        callback = None
        if not args.no_speedtree_verify:
            if not args.speedtree_exe or not args.xml_ini:
                raise SyncError("SpeedTree 검증 경로가 설정되지 않았습니다")
            speedtree_exe = Path(args.speedtree_exe)
            xml_ini = Path(args.xml_ini)
            callback = lambda path: verify_speedtree_export(path, speedtree_exe, xml_ini)
        result = apply_pass_repair_transaction(Path(args.spm), verify_callback=callback)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "preview":
        folder = Path(args.folder)
        manifest = load_manifest(folder)
        group = find_group(manifest, args.master)
        follower = next(
            (item for item in group.get("followers", []) if item.get("file") == args.target),
            None,
        )
        if follower is None:
            raise SyncError(f"자식 설정이 없습니다: {args.target}")
        plan = build_sync_plan(
            folder / args.master,
            folder / args.target,
            follower.get("base_map") or {},
            group.get("base_categories") or {},
        )
        print(json.dumps(plan_to_json(plan), ensure_ascii=False, indent=2))
        return 0
    if args.command == "preview-auto":
        folder = Path(args.folder)
        master_path = folder / args.master
        source_prefix = SPMDocument.from_path(master_path, full=False)
        categories = source_base_categories(source_prefix)
        source_full, _master_text, color_updates, master_ref_renames, color_warnings = standardize_master_document(
            master_path, categories
        )
        output = []
        for target_name in args.target:
            target_path = folder / target_name
            target_prefix = SPMDocument.from_path(target_path, full=False)
            mapping = suggest_base_map(source_prefix, target_prefix, categories)
            plan = build_sync_plan(
                master_path, target_path, mapping, categories,
                master_document=source_full,
            )
            plan.master_color_updates = color_updates
            plan.master_reference_renames = master_ref_renames
            plan.warnings.extend(color_warnings)
            output.append({"base_map_suggestion": mapping, "plan": plan_to_json(plan)})
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    if args.command == "apply":
        result = apply_group_transaction(
            Path(args.folder), args.master, args.target or None,
            verify_speedtree=not args.no_speedtree_verify,
            speedtree_exe=Path(args.speedtree_exe) if args.speedtree_exe else None,
            xml_ini=Path(args.xml_ini) if args.xml_ini else None,
        )
        result["plans"] = [plan_to_json(plan) for plan in result["plans"]]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "verify-auto-copy":
        plans = verify_auto_copies(
            Path(args.folder), args.master, args.target,
            Path(args.speedtree_exe), Path(args.xml_ini),
        )
        print(json.dumps([plan_to_json(plan) for plan in plans], ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2)
