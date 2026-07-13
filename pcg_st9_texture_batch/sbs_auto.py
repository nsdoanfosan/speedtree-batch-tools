"""③ Substance 자동화.

두 가지를 한다:
1. 렌더: Cluster_System_01.sbsar 를 sbsrender 로 직접 돌려서
   사용 머티리얼별 텍스처 6장
   (color/normal/extra/height/opacity/subsurface)을 뽑는다.
   - .sbs 안에 T_ 그래프가 이미 있으면: 그 그래프의 비트맵 연결과 인스턴스
     파라미터를 XML에서 읽어 그대로 사용한다 (Designer에서 만든 세팅 존중).
     T_ 그래프는 "비트맵 → Cluster_System_01 인스턴스 → 출력" 순수 통과
     구조라서 직접 렌더와 결과가 같다 (elm에서 픽셀 비교로 검증).
   - 레거시 M_ 그래프는 T_로 이름을 바꾼 뒤 사용한다.
   - 없으면: 검사 보드의 SpeedTree 원본 텍스처를 슬롯에 매핑해서 렌더한다.
2. .sbs에 T_ 그래프 삽입: 사용자가 Designer에서 계속 관리할 수 있도록,
   elm 템플릿을 바탕으로 새 그래프+리소스를 .sbs에 넣는다.
   수정 전 pcgtex_backup 백업이 남는다.

세트 .sbs 전체를 sbscooker로 쿡하는 방식은 쓰지 않는다: 레거시 그래프들의
깨진 참조와 Cluster_System_01 이중 의존성 때문에 쿡이 실패한다(Error 13).
"""
import copy
import hashlib
import json
import math
import random
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

from pcg_texture_common import load_config

TOOL_DIR = Path(__file__).resolve().parent
ASSETS_DIR = TOOL_DIR / "assets"
TEMPLATE_PATH = ASSETS_DIR / "m_graph_template.xml"
TEMPLATE_GRAPH_NAME = "M_Leaf_elm_atlas_01"

DEFAULT_DESIGNER_DIR = r"C:\Program Files\Adobe\Adobe Substance 3D Designer"
DEFAULT_CLUSTER_SBSAR = r"D:\OneDrive\Forestportfolio\substanceDesigner\Cluster_System_01.sbsar"

RENDER_MAPS = ("color", "normal", "extra", "height", "opacity", "subsurface")
MAX_OUTPUT_LOG2 = 12

# Cluster_System_01.sbsar 이미지 입력 슬롯 (sbsrender info 로 확인)
SLOT_ORDER = [
    "Base_Color", "Opacity", "Normal", "Height", "Roughness", "Depth",
    "Subsurface", "Subsurface_Amount", "Vertex_Color", "Ambient_Occlusion",
]
# 슬롯 -> 리소스 이름 접미사 ({그래프이름}_{접미사})
SLOT_SUFFIX = {
    "Base_Color": "albedo",
    "Opacity": "opacity",
    "Normal": "normal",
    "Height": "height",
    "Roughness": "roughness",
    "Depth": "depth",
    "Subsurface": "subsurface",
    "Subsurface_Amount": "subsurface_amount",
    "Vertex_Color": "vertex_color",
    "Ambient_Occlusion": "ao",
}
# 템플릿(elm) 리소스 접미사 -> 슬롯. ao_from_height 같은 변형 포함.
TEMPLATE_SUFFIX_TO_SLOT = {
    "albedo": "Base_Color",
    "opacity": "Opacity",
    "normal": "Normal",
    "height": "Height",
    "roughness": "Roughness",
    "depth": "Depth",
    "subsurface": "Subsurface",
    "subsurface_amount": "Subsurface_Amount",
    "vertex_color": "Vertex_Color",
    "ao": "Ambient_Occlusion",
    "ao_from_height": "Ambient_Occlusion",
}
# sbsrender 로 넘겨도 되는 값 파라미터 (sbsar가 실제로 노출하는 것만)
SAFE_VALUE_PARAMS = {
    "Height_blend", "AO_blend", "Depth_Blend", "Leaf_hue", "saturation",
    "Leaf_luminosity", "Branch_hue", "Branch_saturation", "Branch_luminosity",
    "switch", "step_01", "Detail", "detail", "switch_depth", "Branch_leaf",
    "Roughness_overlay", "opacitymult", "distance", "Step_02", "Step_0",
    "Roughness_invert", "Roughness_VertexRed",
}
COLOR_PASSTHROUGH_PARAMS = (
    "Height_blend", "AO_blend", "Depth_Blend", "Detail", "detail",
)
# 참고: 'normal'(OpenGL 플래그) 파라미터는 관리 그래프에 저장돼 있지만
# 현재 Cluster_System_01.sbsar 는 이 이름을 노출하지 않아 무시된다
# (sbscooker 로그: ERROR_UNKNOWN_INSTANCE_PARAMETER_NAME). 현재 sbsar는 항상
# OpenGL→DirectX 변환하므로 DirectX 원본은 render_maps에서 출력 G를 보정한다.

IMAGE_EXT_FORMAT = {".png": "png", ".tga": "tga", ".tif": "tif", ".tiff": "tif",
                    ".jpg": "jpg", ".jpeg": "jpg", ".exr": "exr", ".bmp": "bmp"}


def sbsrender_exe(cfg=None):
    cfg = cfg or load_config()
    base = Path(cfg.get("designer_dir", DEFAULT_DESIGNER_DIR))
    return base / "sbsrender.exe"


def sbscooker_exe(cfg=None):
    cfg = cfg or load_config()
    base = Path(cfg.get("designer_dir", DEFAULT_DESIGNER_DIR))
    return base / "sbscooker.exe"


def cluster_sbsar(cfg=None):
    cfg = cfg or load_config()
    return Path(cfg.get("cluster_sbsar", DEFAULT_CLUSTER_SBSAR))


def hbao_source_sbs(cfg=None):
    cfg = cfg or load_config()
    base = Path(cfg.get("designer_dir", DEFAULT_DESIGNER_DIR))
    return base / "resources" / "packages" / "hbao_2.sbs"


def _hidden_creationflags():
    return 0x08000000 if sys.platform == "win32" else 0


def normalize_size_log2(size_log2):
    """Return an (x, y) Substance output-size pair."""
    if isinstance(size_log2, (tuple, list)) and len(size_log2) == 2:
        return int(size_log2[0]), int(size_log2[1])
    if isinstance(size_log2, str):
        values = [value for value in re.split(r"[,xX ]+", size_log2.strip()) if value]
        if len(values) == 2:
            return int(values[0]), int(values[1])
        if len(values) == 1:
            value = int(values[0])
            return value, value
    value = int(size_log2)
    return value, value


def cap_size_log2(size_log2, max_log2=MAX_OUTPUT_LOG2):
    """Preserve aspect ratio while moving the longest edge down to max_log2."""
    x_log2, y_log2 = normalize_size_log2(size_log2)
    shift = max(0, x_log2 - max_log2, y_log2 - max_log2)
    return max(0, x_log2 - shift), max(0, y_log2 - shift)


def image_size_log2(path, max_log2=MAX_OUTPUT_LOG2):
    """Read an image's native ratio and return a capped Substance size pair."""
    from PIL import Image
    with Image.open(path) as image:
        width, height = image.size
    if width < 1 or height < 1:
        raise RuntimeError(f"invalid image size: {path}: {width}x{height}")
    raw = (int(round(math.log2(width))), int(round(math.log2(height))))
    return cap_size_log2(raw, max_log2=max_log2)


def render_size_log2(inputs=None, size_log2=None, max_log2=MAX_OUTPUT_LOG2):
    """Choose Base Color's native aspect ratio unless a size is explicitly set."""
    if size_log2 is not None:
        return cap_size_log2(normalize_size_log2(size_log2), max_log2=max_log2)
    inputs = inputs or {}
    candidates = [inputs.get("Base_Color")]
    candidates.extend(path for path in inputs.values() if path not in candidates)
    for path in candidates:
        if not path or "neutral_" in Path(path).name.lower():
            continue
        try:
            return image_size_log2(path, max_log2=max_log2)
        except Exception:
            continue
    return max_log2, max_log2


def size_log2_pixels(size_log2):
    x_log2, y_log2 = normalize_size_log2(size_log2)
    return 1 << x_log2, 1 << y_log2


def image_pixel_size(path):
    from PIL import Image
    with Image.open(path) as image:
        return tuple(image.size)


def _size_value(size_log2):
    x_log2, y_log2 = normalize_size_log2(size_log2)
    return f"{x_log2},{y_log2}"


def ensure_hbao_sbsar(cfg=None, timeout=1800):
    """Cook Designer's installed HBAO package into a disposable local cache."""
    cfg = cfg or load_config()
    cooker = sbscooker_exe(cfg)
    source = hbao_source_sbs(cfg)
    if not cooker.exists():
        raise RuntimeError(f"sbscooker.exe 없음: {cooker}")
    if not source.exists():
        raise RuntimeError(f"Designer HBAO 패키지 없음: {source}")
    cache_dir = Path(tempfile.gettempdir()) / "pcg_st9_texture_batch"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "hbao_2.sbsar"
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    result = subprocess.run(
        [str(cooker), "--inputs", str(source), "--output-path", str(cache_dir)],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_hidden_creationflags(),
    )
    if result.returncode != 0 or not target.exists():
        tail = (result.stderr or result.stdout or "")[-1500:]
        raise RuntimeError(f"HBAO 패키지 cook 실패: {tail}")
    return target


def render_hbao_from_height(atlas_base, height_path, out_dir, cfg=None,
                            size_log2=None, timeout=1800):
    """Render Designer's official HBAO from height and keep it as an SBS source."""
    cfg = cfg or load_config()
    exe = sbsrender_exe(cfg)
    hbao_sbsar = ensure_hbao_sbsar(cfg, timeout=timeout)
    height_path = Path(height_path)
    if not height_path.exists():
        raise RuntimeError(f"HBAO용 height 없음: {height_path}")
    generated_dir = Path(out_dir) / "_pcgtex_generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    target = generated_dir / f"{atlas_base}_ao_from_height.png"
    resolved_size = render_size_log2(
        {"Base_Color": height_path}, size_log2=size_log2)
    expected_pixels = size_log2_pixels(resolved_size)
    newest_source = max(height_path.stat().st_mtime, hbao_sbsar.stat().st_mtime)
    if target.exists() and target.stat().st_mtime >= newest_source:
        try:
            if image_pixel_size(target) == expected_pixels:
                return target
        except Exception:
            pass
    with tempfile.TemporaryDirectory(prefix="hbao_", dir=str(generated_dir)) as temp_dir:
        name = f"{atlas_base}_ao_from_height_{{outputNodeName}}"
        cmd = [
            str(exe), "render", str(hbao_sbsar),
            "--set-entry", f"input@{height_path}",
            "--set-value", f"$outputsize@{_size_value(resolved_size)}",
            "--input-graph-output", "output",
            "--output-name", name,
            "--output-format", "png",
            "--output-path", temp_dir,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=_hidden_creationflags(),
        )
        rendered = Path(temp_dir) / f"{atlas_base}_ao_from_height_output.png"
        if result.returncode != 0 or not rendered.exists() or rendered.stat().st_size == 0:
            tail = (result.stderr or result.stdout or "")[-1500:]
            raise RuntimeError(f"height→HBAO 렌더 실패: {tail}")
        staged = generated_dir / f".{target.name}.tmp"
        try:
            shutil.copy2(rendered, staged)
            staged.replace(target)
        finally:
            if staged.exists():
                staged.unlink()
    return target


# ------------------------------------------------------------------ 공용 XML
def _param_value(param):
    pv = param.find("paramValue")
    if pv is None:
        return None
    for child in pv:
        return child.tag, child.get("v")
    return None


def _is_cluster_instance(instance):
    if instance is None or instance.tag != "compInstance":
        return False
    path = instance.find("path")
    value = path.get("v", "") if path is not None else ""
    return "cluster_system_01" in value.lower()


def _graph_bitmap_nodes(graph):
    """Return graph node and bitmap-resource maps keyed by compNode uid."""
    nodes = {}
    bitmaps = {}
    for node in graph.find("compNodes") or []:
        uid_el = node.find("uid")
        implementation = node.find("compImplementation")
        if uid_el is None or implementation is None or not len(implementation):
            continue
        uid = uid_el.get("v")
        nodes[uid] = node
        imp = list(implementation)[0]
        if imp.tag != "compFilter" or imp.find("filter") is None \
                or imp.find("filter").get("v") != "bitmap":
            continue
        for param in imp.iter("parameter"):
            name = param.find("name")
            if name is None or name.get("v") != "bitmapresourcepath":
                continue
            value = _param_value(param)
            if value:
                bitmaps[uid] = value[1].split("/")[-1].split("?")[0]
    return nodes, bitmaps


def _upstream_bitmap_resources(uid, nodes, bitmaps, visited=None):
    """Trace a Cluster input through utility nodes to its source bitmaps."""
    if not uid:
        return []
    if uid in bitmaps:
        return [bitmaps[uid]]
    visited = set(visited or ())
    if uid in visited:
        return []
    visited.add(uid)
    node = nodes.get(uid)
    if node is None:
        return []
    resources = []
    for connection in node.iter("connection"):
        ref = connection.find("connRef")
        if ref is not None:
            resources.extend(_upstream_bitmap_resources(
                ref.get("v"), nodes, bitmaps, visited))
    # Preserve graph order but remove duplicates.
    return list(dict.fromkeys(resources))


def _find_graph(root, graph_name):
    for graph in root.iter("graph"):
        ident = graph.find("identifier")
        if ident is not None and ident.get("v") == graph_name:
            return graph
    return None


def list_m_graphs(sbs_path):
    """List managed texture-export graphs (current T_ and legacy M_ names)."""
    try:
        root = ET.parse(sbs_path).getroot()
    except Exception:
        return []
    names = []
    for graph in root.iter("graph"):
        ident = graph.find("identifier")
        if ident is not None and ident.get("v", "").lower().startswith(("m_", "t_")):
            names.append(ident.get("v"))
    return names


def exact_graph_name(sbs_path, graph_name):
    """Return the graph's stored spelling only for an exact case-insensitive ID."""
    wanted = str(graph_name).lower()
    return next((name for name in list_m_graphs(sbs_path) if name.lower() == wanted), None)


def authoring_graph_promotion_candidate(sbs_path, material_name, texture_name):
    """Detect a procedural/direct M_ graph shadowed by a generated T_ clone."""
    authoring = exact_graph_name(sbs_path, material_name)
    managed = exact_graph_name(sbs_path, texture_name)
    if not authoring or not managed or authoring.lower() == managed.lower():
        return None
    authoring_info = inspect_graph_sources(sbs_path, authoring)
    managed_info = inspect_graph_sources(sbs_path, managed)
    required = {"color", "normal", "extra", "height"}
    if not required.issubset(set(authoring_info.get("outputs") or [])):
        return None
    managed_instances = managed_info.get("instances") or []
    if not managed_instances or any(
            "cluster_system_01" not in row.get("path", "").lower()
            for row in managed_instances):
        return None
    # A non-Cluster instance is direct evidence that rebuilding from the
    # bitmap resources would bypass authoring logic (noise, blends, HBAO...).
    # A graph with no instances is also a direct node graph and must be kept.
    authoring_instances = authoring_info.get("instances") or []
    has_authoring_logic = not authoring_instances or any(
        "cluster_system_01" not in row.get("path", "").lower()
        for row in authoring_instances)
    if not has_authoring_logic:
        return None
    return {
        "authoring": authoring,
        "managed": managed,
        "direct_maps": [
            role for role in RENDER_MAPS
            if role in set(authoring_info.get("outputs") or [])
        ],
    }


def promote_authoring_graph(sbs_path, authoring_name, managed_name):
    """Delete a generated T_ clone and promote the original M_ graph to T_."""
    sbs_path = Path(sbs_path)
    tree = ET.parse(sbs_path)
    root = tree.getroot()
    authoring = _find_graph(root, authoring_name)
    managed = _find_graph(root, managed_name)
    if authoring is None or managed is None:
        raise RuntimeError(
            f"authoring promotion graphs missing: {authoring_name}, {managed_name}")

    managed_prefix = managed_name.lower() + "_"
    managed_resources = []
    for resource in root.iter("resource"):
        ident = resource.find("identifier")
        if ident is not None and ident.get("v", "").lower().startswith(managed_prefix):
            managed_resources.append((resource, ident.get("v")))

    managed_descendants = set(managed.iter())
    for _resource, identifier in managed_resources:
        needle = f"pkg:///resources/{identifier}".lower()
        for element in root.iter():
            if element in managed_descendants or element.tag == "resource":
                continue
            value = element.get("v", "").lower()
            if needle in value:
                raise RuntimeError(
                    f"cannot remove shared managed resource {identifier}: referenced outside {managed_name}")

    parent_map = {child: parent for parent in root.iter() for child in parent}
    graph_parent = parent_map.get(managed)
    if graph_parent is None:
        raise RuntimeError(f"cannot locate graph parent: {managed_name}")
    graph_parent.remove(managed)
    for resource, _identifier in managed_resources:
        parent = parent_map.get(resource)
        if parent is not None:
            parent.remove(resource)

    old_low = authoring_name.lower()
    for element in root.iter():
        if element.tag == "filepath":
            continue
        value = element.get("v")
        if not value:
            continue
        value_low = value.lower()
        if value_low == old_low:
            element.set("v", managed_name)
        elif value_low.startswith(old_low + "_"):
            element.set("v", managed_name + value[len(authoring_name):])
        else:
            element.set("v", re.sub(
                rf"(?i)(pkg:///resources/){re.escape(authoring_name)}_",
                rf"\1{managed_name}_", value))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = sbs_path.with_name(
        f"{sbs_path.stem}.pcgtex_backup_before_promote_{authoring_name}_{stamp}.sbs")
    shutil.copy2(sbs_path, backup)
    try:
        tree.write(sbs_path, encoding="utf-8", xml_declaration=True)
        verify_root = ET.parse(sbs_path).getroot()
        if (_find_graph(verify_root, managed_name) is None
                or _find_graph(verify_root, authoring_name) is not None):
            raise RuntimeError("authoring graph promotion verification failed")
        graph_ids = [
            graph.find("identifier").get("v") for graph in verify_root.iter("graph")
            if graph.find("identifier") is not None
        ]
        resource_ids = [
            resource.find("identifier").get("v") for resource in verify_root.iter("resource")
            if resource.find("identifier") is not None
        ]
        duplicates = {
            value for values in (graph_ids, resource_ids) for value in values
            if values.count(value) > 1
        }
        if duplicates:
            raise RuntimeError(f"duplicate identifiers after graph promotion: {sorted(duplicates)}")
    except Exception:
        shutil.copy2(backup, sbs_path)
        raise
    return {
        "sbs": str(sbs_path), "old": authoring_name, "new": managed_name,
        "removed_clone": managed_name,
        "removed_resources": [identifier for _resource, identifier in managed_resources],
        "backup": str(backup),
    }


def find_m_graph_name(sbs_path, atlas_base):
    """Find a managed graph by case-insensitive M_/T_ base name."""
    for name in list_m_graphs(sbs_path):
        if name.lower() == str(atlas_base).lower():
            return name
    return None


def rename_managed_graph(sbs_path, old_name, new_name):
    """Rename a legacy M_ export graph and its internal resource IDs to T_."""
    sbs_path = Path(sbs_path)
    tree = ET.parse(sbs_path)
    root = tree.getroot()
    graph = _find_graph(root, old_name)
    if graph is None:
        raise RuntimeError(f"SBS 그래프 없음: {old_name}")
    if _find_graph(root, new_name) is not None:
        raise RuntimeError(f"SBS 그래프 이름 충돌: {new_name}")
    old_low = old_name.lower()
    for element in root.iter():
        if element.tag == "filepath":
            continue
        value = element.get("v")
        if not value:
            continue
        value_low = value.lower()
        if value_low == old_low:
            element.set("v", new_name)
        elif value_low.startswith(old_low + "_"):
            element.set("v", new_name + value[len(old_name):])
        else:
            element.set("v", re.sub(
                rf"(?i)(pkg:///resources/){re.escape(old_name)}_",
                rf"\1{new_name}_", value))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = sbs_path.with_name(
        f"{sbs_path.stem}.pcgtex_backup_before_graph_rename_{old_name}_{stamp}.sbs")
    shutil.copy2(sbs_path, backup)
    try:
        tree.write(sbs_path, encoding="utf-8", xml_declaration=True)
        verify_root = ET.parse(sbs_path).getroot()
        if _find_graph(verify_root, new_name) is None or _find_graph(verify_root, old_name) is not None:
            raise RuntimeError("renamed graph verification failed")
    except Exception:
        shutil.copy2(backup, sbs_path)
        raise
    return {"sbs": str(sbs_path), "old": old_name, "new": new_name,
            "backup": str(backup)}


def parse_m_graph(sbs_path, graph_name):
    """T_ 관리 그래프의 비트맵 연결과 인스턴스 파라미터를 읽는다."""
    sbs_path = Path(sbs_path)
    root = ET.parse(sbs_path).getroot()
    graph = _find_graph(root, graph_name)
    if graph is None:
        raise RuntimeError(f"graph not found: {graph_name} in {sbs_path.name}")

    resource_files = {}
    for res in root.iter("resource"):
        ident = res.find("identifier")
        fp = res.find("filepath")
        if ident is None or fp is None:
            continue
        path = (sbs_path.parent / fp.get("v").replace("\\", "/")).resolve()
        resource_files[ident.get("v")] = path

    nodes, bitmap_by_uid = _graph_bitmap_nodes(graph)
    instance = None          # compInstance 요소 (파라미터가 여기에)
    instance_node = None     # 상위 compNode 요소 (connection이 여기에)
    comps = graph.find("compNodes")
    for node in comps or []:
        imp_wrap = node.find("compImplementation")
        if imp_wrap is None or not len(imp_wrap):
            continue
        imp = list(imp_wrap)[0]
        if _is_cluster_instance(imp):
            instance = imp
            instance_node = node

    if instance is None:
        raise RuntimeError(f"{graph_name}: Cluster_System 인스턴스가 없음")

    inputs = {}
    for conn in instance_node.iter("connection"):
        slot = conn.find("identifier").get("v")
        ref = conn.find("connRef").get("v")
        resources = _upstream_bitmap_resources(ref, nodes, bitmap_by_uid)
        paths = list(dict.fromkeys(resource_files[name] for name in resources if name in resource_files))
        if len(paths) == 1:
            inputs[slot] = paths[0]

    params = {}
    for param in instance.iter("parameter"):
        name = param.find("name").get("v")
        value = _param_value(param)
        if value:
            params[name] = value
    return {"inputs": inputs, "params": params}


def inspect_graph_sources(sbs_path, graph_name):
    """Inspect every bitmap and instance in one SBS graph without guessing roles."""
    sbs_path = Path(sbs_path)
    root = ET.parse(sbs_path).getroot()
    graph = _find_graph(root, graph_name)
    if graph is None:
        raise RuntimeError(f"graph not found: {graph_name} in {sbs_path.name}")
    resource_files = {}
    for resource in root.iter("resource"):
        ident = resource.find("identifier")
        filepath = resource.find("filepath")
        if ident is None or filepath is None:
            continue
        resource_files[ident.get("v")] = (
            sbs_path.parent / filepath.get("v", "").replace("\\", "/")).resolve()
    bitmaps = []
    instances = []
    for node in graph.find("compNodes") or []:
        implementation = node.find("compImplementation")
        if implementation is None or not len(implementation):
            continue
        imp = list(implementation)[0]
        if imp.tag == "compFilter" and imp.find("filter") is not None \
                and imp.find("filter").get("v") == "bitmap":
            for param in imp.iter("parameter"):
                name = param.find("name")
                if name is None or name.get("v") != "bitmapresourcepath":
                    continue
                value = _param_value(param)
                resource_name = value[1].split("/")[-1].split("?")[0] if value else ""
                bitmaps.append({
                    "resource": resource_name,
                    "path": str(resource_files.get(resource_name, "")),
                })
        elif imp.tag == "compInstance":
            path = imp.find("path")
            instances.append({
                "path": path.get("v", "") if path is not None else "",
                "connections": [
                    conn.find("identifier").get("v", "")
                    for conn in node.iter("connection")
                    if conn.find("identifier") is not None
                ],
            })
    outputs = [
        ident.get("v", "")
        for ident in graph.findall("graphOutputs/graphoutput/identifier")
    ]
    return {"bitmaps": bitmaps, "instances": instances, "outputs": outputs}


def graph_render_size_log2(sbs_path, graph_name, max_log2=MAX_OUTPUT_LOG2):
    """Infer a graph's intended aspect ratio from Base Color, then graph options."""
    try:
        parsed = parse_m_graph(sbs_path, graph_name)
        if parsed["inputs"].get("Base_Color"):
            return render_size_log2(parsed["inputs"], max_log2=max_log2)
    except Exception:
        pass
    root = ET.parse(sbs_path).getroot()
    graph = _find_graph(root, graph_name)
    if graph is not None:
        for option in graph.findall("options/option"):
            name = option.find("name")
            value = option.find("value")
            if name is not None and name.get("v") == "defaultParentSize" and value is not None:
                try:
                    return cap_size_log2(value.get("v", ""), max_log2=max_log2)
                except Exception:
                    pass
    try:
        source = inspect_graph_sources(sbs_path, graph_name)
        preferred = [
            item for item in source["bitmaps"]
            if any(token in item["resource"].lower()
                   for token in ("albedo", "basecolor", "base_color", "diffuse"))
        ]
        for item in preferred + source["bitmaps"]:
            path = Path(item.get("path", ""))
            if path.is_file() and "neutral_" not in path.name.lower():
                return image_size_log2(path, max_log2=max_log2)
    except Exception:
        pass
    return max_log2, max_log2


def set_graph_default_size(sbs_path, graph_name, size_log2):
    """Record a direct/procedural graph's effective output size without changing nodes."""
    sbs_path = Path(sbs_path)
    tree = ET.parse(sbs_path)
    graph = _find_graph(tree.getroot(), graph_name)
    if graph is None:
        raise RuntimeError(f"graph not found: {graph_name} in {sbs_path.name}")
    x_log2, y_log2 = normalize_size_log2(size_log2)
    desired = f"{x_log2}x{y_log2}"
    changed = False
    for option in graph.findall("options/option"):
        name = option.find("name")
        value = option.find("value")
        if name is not None and name.get("v") == "defaultParentSize" and value is not None:
            if value.get("v") != desired:
                value.set("v", desired)
                changed = True
            break
    if not changed:
        return {"changed": False, "backup": None, "size_log2": (x_log2, y_log2)}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = sbs_path.with_name(
        f"{sbs_path.stem}.pcgtex_backup_before_default_resolution_{graph_name}_{stamp}.sbs")
    shutil.copy2(sbs_path, backup)
    try:
        tree.write(sbs_path, encoding="utf-8", xml_declaration=True)
        if graph_render_size_log2(sbs_path, graph_name) != (x_log2, y_log2):
            raise RuntimeError("direct graph default resolution verification failed")
    except Exception:
        shutil.copy2(backup, sbs_path)
        raise
    return {"changed": True, "backup": str(backup), "size_log2": (x_log2, y_log2)}


def _set_graph_resolution(graph, size_log2):
    """Set graph inheritance and every bitmap read to one aspect-preserving size."""
    x_log2, y_log2 = normalize_size_log2(size_log2)
    int2_value = f"{x_log2} {y_log2}"
    option_value = f"{x_log2}x{y_log2}"
    changed = False
    for option in graph.findall("options/option"):
        name = option.find("name")
        value = option.find("value")
        if name is None or name.get("v") != "defaultParentSize" or value is None:
            continue
        if value.get("v") != option_value:
            value.set("v", option_value)
            changed = True
    for node in graph.findall("compNodes/compNode"):
        implementation = node.find("compImplementation")
        if implementation is None or not len(implementation):
            continue
        imp = list(implementation)[0]
        filter_el = imp.find("filter") if imp.tag == "compFilter" else None
        if filter_el is None or filter_el.get("v") != "bitmap":
            continue
        for param in imp.iter("parameter"):
            name = param.find("name")
            if name is None or name.get("v") != "outputsize":
                continue
            value = _param_value(param)
            if not value:
                continue
            value_el = param.find("paramValue")
            value_el = list(value_el)[0] if value_el is not None and len(value_el) else None
            if value_el is not None and value_el.get("v") != int2_value:
                value_el.set("v", int2_value)
                changed = True
            relative = param.find("relativeTo")
            if relative is not None and relative.get("v") != "0":
                relative.set("v", "0")
                changed = True
    return changed


def managed_graph_resolution_state(sbs_path, graph_name, inputs=None,
                                   max_log2=MAX_OUTPUT_LOG2):
    desired = render_size_log2(inputs, max_log2=max_log2) if inputs else \
        graph_render_size_log2(sbs_path, graph_name, max_log2=max_log2)
    root = ET.parse(sbs_path).getroot()
    graph = _find_graph(root, graph_name)
    if graph is None:
        raise RuntimeError(f"graph not found: {graph_name} in {Path(sbs_path).name}")
    probe = copy.deepcopy(graph)
    changed = _set_graph_resolution(probe, desired)
    return {"size_log2": desired, "pixel_size": size_log2_pixels(desired),
            "needs_update": changed}


def set_managed_graph_resolution(sbs_path, graph_name, inputs=None,
                                 max_log2=MAX_OUTPUT_LOG2):
    """Persist capped native aspect ratio in the SBS graph with rollback."""
    sbs_path = Path(sbs_path)
    tree = ET.parse(sbs_path)
    root = tree.getroot()
    graph = _find_graph(root, graph_name)
    if graph is None:
        raise RuntimeError(f"graph not found: {graph_name} in {sbs_path.name}")
    desired = render_size_log2(inputs, max_log2=max_log2) if inputs else \
        graph_render_size_log2(sbs_path, graph_name, max_log2=max_log2)
    if not _set_graph_resolution(graph, desired):
        return {"changed": False, "backup": None, "size_log2": desired,
                "pixel_size": size_log2_pixels(desired)}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = sbs_path.with_name(
        f"{sbs_path.stem}.pcgtex_backup_before_resolution_{graph_name}_{stamp}.sbs")
    shutil.copy2(sbs_path, backup)
    try:
        tree.write(sbs_path, encoding="utf-8", xml_declaration=True)
        verify = managed_graph_resolution_state(
            sbs_path, graph_name, inputs=inputs, max_log2=max_log2)
        if verify["needs_update"]:
            raise RuntimeError("managed graph resolution verification failed")
    except Exception:
        shutil.copy2(backup, sbs_path)
        raise
    return {"changed": True, "backup": str(backup), "size_log2": desired,
            "pixel_size": size_log2_pixels(desired)}


# ------------------------------------------------------------------ 렌더
def neutral_image(kind):
    """없는 입력용 중립 이미지 (black / white / flat normal)를 재사용."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    path = ASSETS_DIR / f"neutral_{kind}.png"
    if not path.exists():
        from PIL import Image
        colors = {
            "normal": (128, 128, 255),
            "white": (255, 255, 255),
            "black": (0, 0, 0),
        }
        color = colors.get(kind, colors["black"])
        Image.new("RGB", (16, 16), color).save(path)
    return path


def neutral_kind_for_slot(slot):
    if slot == "Normal":
        return "normal"
    if slot in ("Opacity", "Subsurface_Amount"):
        return "white"
    return "black"


def format_param_for_render(name, tag_value):
    tag, value = tag_value
    if tag == "constantValueBool":
        return f"{name}@{1 if value in ('1', 'true', 'True') else 0}"
    if tag.startswith("constantValueInt"):
        return f"{name}@{value.replace(' ', ',')}"
    if tag.startswith("constantValueFloat"):
        return f"{name}@{value.replace(' ', ',')}"
    return None


def _param_bool(params, name):
    value = (params or {}).get(name)
    if not value:
        return None
    tag, raw = value
    if tag != "constantValueBool":
        return None
    return raw in ("1", "true", "True")


def _invert_normal_green(path):
    """Invert only G in a rendered 8-bit normal map, preserving alpha if present."""
    from PIL import Image
    path = Path(path)
    with Image.open(path) as image:
        mode = "RGBA" if "A" in image.getbands() else "RGB"
        converted = image.convert(mode)
        channels = list(converted.split())
        channels[1] = channels[1].point(lambda value: 255 - value)
        Image.merge(mode, channels).save(path)


def _prepare_output_transaction(produced, atlas_base):
    existing = {path: path.exists() for path in produced}
    backup_dir = None
    if any(existing.values()):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = produced[0].parent / "_pcgtex_backups" / f"{atlas_base}_{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for path, was_present in existing.items():
            if was_present:
                shutil.copy2(path, backup_dir / path.name)
    # Remove exact TGA targets so an old file cannot make a failed render look successful.
    try:
        for path, was_present in existing.items():
            if was_present:
                path.unlink()
    except Exception:
        _restore_output_transaction(produced, existing, backup_dir)
        raise
    return existing, backup_dir


def _restore_output_transaction(produced, existing, backup_dir):
    for path in produced:
        if path.exists():
            path.unlink()
        if existing.get(path) and backup_dir:
            shutil.copy2(backup_dir / path.name, path)


def render_maps(atlas_base, inputs, params, out_dir, cfg=None,
                maps=RENDER_MAPS, size_log2=None, timeout=1800,
                return_info=False):
    """Cluster_System_01.sbsar 를 직접 렌더해서 {atlas_base}_{map}.tga 생성."""
    cfg = cfg or load_config()
    exe = sbsrender_exe(cfg)
    sbsar = cluster_sbsar(cfg)
    if not exe.exists():
        raise RuntimeError(f"sbsrender.exe 없음: {exe} (설정 designer_dir 확인)")
    if not sbsar.exists():
        raise RuntimeError(f"Cluster_System_01.sbsar 없음: {sbsar}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # A white Subsurface_Amount is the identity multiplier.  Always force it
    # to 1 so the source translucency/subsurface image passes through without
    # an unintended second attenuation.
    inputs = dict(inputs or {})
    inputs["Subsurface_Amount"] = neutral_image("white")
    params = normalized_export_params(params)

    resolved_size = render_size_log2(inputs, size_log2=size_log2)
    cmd = [str(exe), "render", str(sbsar)]
    for slot in SLOT_ORDER:
        path = inputs.get(slot)
        if path is None:
            path = neutral_image(neutral_kind_for_slot(slot))
        cmd += ["--set-entry", f"{slot}@{Path(path)}"]
    cmd += ["--set-value", f"$outputsize@{_size_value(resolved_size)}"]
    for name, tag_value in (params or {}).items():
        if name not in SAFE_VALUE_PARAMS:
            continue
        arg = format_param_for_render(name, tag_value)
        if arg:
            cmd += ["--set-value", arg]
    for map_name in maps:
        cmd += ["--input-graph-output", map_name]
    cmd += [
        "--output-name", f"{atlas_base}_{{outputNodeName}}",
        "--output-format", "tga",
        "--output-path", str(out_dir),
    ]
    produced = [out_dir / f"{atlas_base}_{m}.tga" for m in maps]
    existing, backup_dir = _prepare_output_transaction(produced, atlas_base)
    normal_corrected = False
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=_hidden_creationflags(),
        )
        missing = [p.name for p in produced if not p.exists() or p.stat().st_size == 0]
        if result.returncode != 0 or missing:
            tail = (result.stderr or result.stdout or "")[-1500:]
            raise RuntimeError(f"sbsrender 실패 (누락: {missing}): {tail}")
        wrong_size = [
            f"{path.name}={image_pixel_size(path)}"
            for path in produced
            if image_pixel_size(path) != size_log2_pixels(resolved_size)
        ]
        if wrong_size:
            raise RuntimeError(
                f"sbsrender output size mismatch; expected "
                f"{size_log2_pixels(resolved_size)}: {wrong_size}")
        # The current Cluster_System_01.sbsar always performs OpenGL -> DirectX.
        # A DirectX source therefore needs one compensating G inversion afterward.
        normal_opengl = _param_bool(params, "normal")
        behavior = cfg.get("cluster_sbsar_normal_behavior", "opengl_to_directx")
        if normal_opengl is False and behavior == "opengl_to_directx" and "normal" in maps:
            _invert_normal_green(out_dir / f"{atlas_base}_normal.tga")
            normal_corrected = True
    except Exception:
        _restore_output_transaction(produced, existing, backup_dir)
        raise
    info = {
        "files": produced,
        "backup_dir": backup_dir,
        "normal_green_corrected": normal_corrected,
        "size_log2": resolved_size,
        "pixel_size": size_log2_pixels(resolved_size),
    }
    return info if return_info else produced


def cook_sbs_package(sbs_path, cache_root, cfg=None, timeout=1800):
    """Cook an existing SBS package into a reusable cache without editing it."""
    cfg = cfg or load_config()
    sbs_path = Path(sbs_path)
    stat = sbs_path.stat()
    key = hashlib.sha1(
        f"{sbs_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    cache_dir = Path(cache_root) / f"{sbs_path.stem}_{key}"
    sbsar = cache_dir / f"{sbs_path.stem}.sbsar"
    if sbsar.is_file() and sbsar.stat().st_size > 0:
        return sbsar
    cache_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(sbscooker_exe(cfg)), "--inputs", str(sbs_path),
         "--output-path", str(cache_dir)],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_hidden_creationflags(),
    )
    if not sbsar.is_file() or sbsar.stat().st_size == 0:
        tail = (result.stderr or result.stdout or "")[-2000:]
        raise RuntimeError(f"SBS cook failed: {sbs_path.name}: {tail}")
    return sbsar


def render_sbs_graph_maps(sbs_path, graph_name, texture_base, out_dir,
                          cache_root, cfg=None, maps=RENDER_MAPS,
                          size_log2=None, timeout=1800, return_info=False):
    """Render final maps directly from a procedural/composite SBS graph."""
    cfg = cfg or load_config()
    sbsar = cook_sbs_package(sbs_path, cache_root, cfg=cfg, timeout=timeout)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    produced = [out_dir / f"{texture_base}_{name}.tga" for name in maps]
    existing, backup_dir = _prepare_output_transaction(produced, texture_base)
    resolved_size = (
        render_size_log2(size_log2=size_log2)
        if size_log2 is not None
        else graph_render_size_log2(sbs_path, graph_name)
    )
    cmd = [
        str(sbsrender_exe(cfg)), "render", str(sbsar),
        "--input-graph", graph_name,
        "--set-value", f"$outputsize@{_size_value(resolved_size)}",
    ]
    for name in maps:
        cmd += ["--input-graph-output", name]
    cmd += [
        "--output-name", f"{texture_base}_{{outputNodeName}}",
        "--output-format", "tga",
        "--output-path", str(out_dir),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=_hidden_creationflags(),
        )
        missing = [path.name for path in produced if not path.is_file() or path.stat().st_size == 0]
        if result.returncode != 0 or missing:
            tail = (result.stderr or result.stdout or "")[-2000:]
            raise RuntimeError(
                f"SBS graph render failed: {graph_name} (missing: {missing}): {tail}")
        actual_sizes = {image_pixel_size(path) for path in produced}
        if len(actual_sizes) != 1:
            raise RuntimeError(f"SBS graph outputs have inconsistent sizes: {actual_sizes}")
        actual_pixels = next(iter(actual_sizes))
        actual_size = image_size_log2(produced[0])
        if size_log2_pixels(actual_size) != actual_pixels or max(actual_pixels) > (1 << MAX_OUTPUT_LOG2):
            raise RuntimeError(
                f"SBS graph output size is not a capped power-of-two size: {actual_pixels}")
    except Exception:
        _restore_output_transaction(produced, existing, backup_dir)
        raise
    info = {"files": produced, "backup_dir": backup_dir, "cooked_sbsar": sbsar,
            "requested_size_log2": resolved_size,
            "size_log2": actual_size, "pixel_size": actual_pixels,
            "size_overridden_by_graph": actual_size != resolved_size}
    return info if return_info else produced


# ------------------------------------------------------------------ 원본 추정
def resolve_ref(ref, base_dirs):
    """SPM/SBS에서 뽑은 (상대)경로 후보를 실제 파일로 해석한다."""
    if not ref:
        return None
    text = str(ref).replace("\\", "/")
    path = Path(text)
    if path.is_absolute():
        return path if path.exists() else None
    for base in base_dirs:
        candidate = (Path(base) / text).resolve()
        if candidate.exists():
            return candidate
    # 마지막 수단: 파일 이름만으로 base 폴더들에서 찾기
    name = Path(text).name
    for base in base_dirs:
        candidate = Path(base) / name
        if candidate.exists():
            return candidate
    return None


OWN_EXPORT_RE = re.compile(
    r"^[mt]_.*_(color|normal|extra|height|opacity|subsurface)\.(tga|png|tif|tiff)$",
    re.IGNORECASE)
LEGACY_M_OUTPUT_RE = re.compile(
    r"^m_.*_(color|normal|extra|height|opacity|subsurface)\.(tga|png|tif|tiff|exr)$",
    re.IGNORECASE,
)

SOURCE_BUCKETS = {
    "albedo": "source_albedo",
    "alpha": "source_alpha",
    "normal": "source_normal",
    "height": "source_height",
    "ao": "source_ao",
    "roughness": "source_roughness",
    "subsurface": "source_subsurface",
}
FAMILY_SUFFIX_RE = re.compile(
    r"(?:[_-](?:base[_-]?color|basecolor|albedo|diffuse|colour|color|opacity|alpha|"
    r"transparency|mask|normal|nor[_-]?gl|nrm|height|displacement|depth|"
    r"ambient[_-]?occlusion|ao|occlusion|roughness|rough|gloss|"
    r"subsurface|translucency|translucent))$",
    re.IGNORECASE,
)
RESOLUTION_SUFFIX_RE = re.compile(r"(?:[_-](?:1k|2k|4k|8k|16k|\d+x\d+))$", re.IGNORECASE)
GENERIC_TARGET_TOKENS = {
    "m", "sk", "sm", "atlas", "cluster", "material", "tree", "bush", "weed",
    "leaf", "leaves", "branch", "bark", "color", "normal", "extra", "height",
    "opacity", "subsurface", "translucency", "basecolor", "albedo",
    "01", "02", "03", "04", "05",
}


def source_family_key(path):
    stem = Path(path).stem.lower()
    stem = FAMILY_SUFFIX_RE.sub("", stem)
    stem = RESOLUTION_SUFFIX_RE.sub("", stem)
    stem = FAMILY_SUFFIX_RE.sub("", stem)
    return stem.strip("_-")


def is_managed_output_ref(path, row):
    """Exclude current/legacy generated outputs from original-source inference."""
    path = Path(path)
    if OWN_EXPORT_RE.match(path.name):
        return True
    texture_dir = row.get("texture_dir")
    if not texture_dir:
        return False
    try:
        path.resolve().relative_to(Path(texture_dir).resolve())
    except (OSError, ValueError):
        return False
    stem = path.stem.lower()
    bases = []
    for value in (row.get("atlas_base"), row.get("texture_base")):
        value = str(value or "").lower()
        if not value:
            continue
        bases.append(value)
        if value.startswith(("m_", "t_")):
            bases.append(value[2:])
    return any(
        stem == f"{base}_{map_name}"
        for base in bases
        for map_name in RENDER_MAPS
    )


def delete_legacy_m_outputs(material_base, out_dir, legacy_maps=None, maps=RENDER_MAPS):
    """Delete exact legacy M_ Unreal outputs after the matching T_ render succeeds."""
    material_base = str(material_base)
    if not material_base.lower().startswith("m_"):
        return []
    candidates = []
    out_dir = Path(out_dir)
    for map_name in maps:
        for extension in (".tga", ".png", ".tif", ".tiff", ".exr"):
            candidates.append(out_dir / f"{material_base}_{map_name}{extension}")
    candidates.extend(
        Path(path) for path in (legacy_maps or {}).values() if path
    )
    deleted = []
    seen = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file() or not LEGACY_M_OUTPUT_RE.match(path.name):
            continue
        path.unlink()
        deleted.append(path)
    return deleted


def _source_target_tokens(row):
    values = [row.get("folder_name", ""), row.get("cluster_name", ""), row.get("atlas_base", "")]
    values.extend(row.get("material_names") or [])
    tokens = re.findall(r"[a-z]+|\d+", " ".join(map(str, values)).lower())
    return sorted({token for token in tokens if token not in GENERIC_TARGET_TOKENS and len(token) > 2})


class SourceSetAmbiguity(RuntimeError):
    def __init__(self, message, candidates):
        super().__init__(message)
        self.candidates = candidates


def source_set_candidates(row, require_alpha=True):
    """Return complete, coherent source sets sorted by target-name relevance."""
    bases = []
    if row.get("cluster_spm"):
        bases.append(Path(row["cluster_spm"]).parent)
    if row.get("folder"):
        bases.append(Path(row["folder"]))
    if row.get("texture_dir"):
        bases.append(Path(row["texture_dir"]))
    groups = {}
    for kind, bucket in SOURCE_BUCKETS.items():
        for ref in row.get(bucket) or []:
            path = resolve_ref(ref, bases)
            if not path:
                continue
            if is_managed_output_ref(path, row):
                continue
            key = (str(path.parent).lower(), source_family_key(path))
            group = groups.setdefault(key, {"paths": {}, "parent": path.parent, "family": key[1]})
            group["paths"].setdefault(kind, path)
    complete = [
        group for group in groups.values()
        if group["paths"].get("albedo")
        and (group["paths"].get("alpha") or not require_alpha)
    ]
    if not complete:
        found = ", ".join(sorted({group["family"] for group in groups.values()})) or "없음"
        required = "albedo+alpha" if require_alpha else "albedo"
        raise RuntimeError(f"같은 원본 세트에서 {required}를 찾지 못함 (후보: {found})")
    target_tokens = _source_target_tokens(row)
    for group in complete:
        haystack = f"{group['parent']} {group['family']}".lower()
        group["score"] = sum(len(token) for token in target_tokens if token in haystack)
        group["label"] = f"{group['parent']}\\{group['family']}"
    complete.sort(key=lambda group: (-group["score"], str(group["parent"]).lower(), group["family"]))
    return complete


def select_source_set(row, preferred=None, require_alpha=True):
    """Choose one coherent texture family; never mix albedo/alpha from different sets."""
    complete = source_set_candidates(row, require_alpha=require_alpha)
    if preferred:
        preferred_low = str(preferred).lower()
        for group in complete:
            if group["label"].lower() == preferred_low:
                return group
        raise RuntimeError(f"저장된 원본 세트 선택을 현재 파일에서 찾지 못함: {preferred}")
    top_score = complete[0]["score"]
    tied = [group for group in complete if group["score"] == top_score]
    if len(tied) > 1:
        labels = [group["label"] for group in tied[:5]]
        raise SourceSetAmbiguity(
            "원본 텍스처 세트가 여러 개라 자동 선택 불가: " + " | ".join(labels), tied)
    return complete[0]


def plan_inputs_from_row(row, preferred=None, require_alpha=True):
    """검사 보드 texture-plan 행에서 렌더 입력을 만든다. (T_ 그래프가 없을 때)"""
    selected = select_source_set(
        row, preferred=preferred, require_alpha=require_alpha)
    paths = selected["paths"]
    albedo = paths["albedo"]
    alpha = paths.get("alpha")
    normal = paths.get("normal")
    height = paths.get("height")
    ao = paths.get("ao")
    roughness = paths.get("roughness")
    subsurface = paths.get("subsurface")

    notes = [f"원본 세트: {selected['parent']}\\{selected['family']}"]
    inputs = {"Base_Color": albedo}
    if alpha:
        inputs["Opacity"] = alpha
    else:
        notes.append("opacity 없음 → 흰색(불투명) 사용")
    if normal:
        inputs["Normal"] = normal
    else:
        notes.append("normal 없음 → 평면 노멀 사용")
    if height:
        inputs["Height"] = height
        inputs["Depth"] = height
    else:
        notes.append("height 없음 → 검정 사용")
    # AO 규칙: 원본 AO가 없으면 실행 단계에서 Designer HBAO를 height로 생성한다.
    if ao:
        inputs["Ambient_Occlusion"] = ao
    elif height:
        notes.append("AO 없음 → height에서 Designer HBAO 생성 예정")
    else:
        notes.append("AO/height 없음 → 검정 사용")
    if roughness:
        inputs["Roughness"] = roughness
    if subsurface:
        inputs["Subsurface"] = subsurface
    else:
        notes.append("subsurface/translucency 없음 → 검정 사용")
    # Amount=1은 출력의 항등값이며 render_maps/그래프 삽입에서 강제한다.
    inputs["Subsurface_Amount"] = neutral_image("white")
    # Vertex_Color 는 기본 검정 (render_maps 에서 채움)
    return inputs, notes


def ensure_hbao_input(atlas_base, inputs, out_dir, cfg=None, size_log2=None, timeout=1800):
    """Replace missing/raw-height AO with a real Designer HBAO render."""
    height = inputs.get("Height")
    ao = inputs.get("Ambient_Occlusion")
    if not height:
        return dict(inputs), None
    same_as_height = False
    neutral_ao = bool(ao and "neutral" in Path(ao).name.lower())
    if ao:
        try:
            same_as_height = Path(ao).resolve() == Path(height).resolve()
        except OSError:
            same_as_height = str(ao).lower() == str(height).lower()
    if ao and not same_as_height and not neutral_ao:
        return dict(inputs), None
    generated = render_hbao_from_height(
        atlas_base, height, out_dir, cfg=cfg, size_log2=size_log2, timeout=timeout)
    updated = dict(inputs)
    updated["Ambient_Occlusion"] = generated
    return updated, generated


def default_params(normal_opengl=True):
    """elm 템플릿과 같은 기본 파라미터. SDF(distance)=0."""
    return {
        "detail": ("constantValueFloat1", "0"),
        "Detail": ("constantValueFloat1", "0"),
        "Height_blend": ("constantValueFloat1", "0"),
        "AO_blend": ("constantValueFloat1", "0"),
        "Depth_Blend": ("constantValueFloat1", "0"),
        "normal": ("constantValueBool", "1" if normal_opengl else "0"),
        "step_01": ("constantValueBool", "1"),
        "switch": ("constantValueBool", "0"),
        "distance": ("constantValueFloat1", "0"),
    }


def normalized_export_params(params):
    """Keep Unreal export channels separate instead of baking masks into Color."""
    normalized = dict(params or {})
    for name in COLOR_PASSTHROUGH_PARAMS:
        normalized[name] = ("constantValueFloat1", "0")
    return normalized


# ------------------------------------------------------------------ 그래프 삽입
def _new_uid(used):
    while True:
        uid = str(random.randint(1200000000, 1999999999))
        if uid not in used:
            used.add(uid)
            return uid


def _collect_uids(root):
    return {el.get("v") for el in root.iter("uid") if el.get("v")}


def _relpath_posix(target, base_dir):
    import os
    try:
        return os.path.relpath(str(target), str(base_dir)).replace("\\", "/")
    except ValueError:
        # 다른 드라이브(예: 중립 이미지가 C:, sbs가 D:)면 절대경로 사용
        return str(Path(target)).replace("\\", "/")


def _find_dependency_uid(root, predicate):
    for dep in root.iter("dependency"):
        fn = dep.find("filename")
        uid = dep.find("uid")
        if fn is not None and uid is not None and predicate(fn.get("v", "")):
            return uid.get("v")
    return None


def _ensure_cluster_dependency(root, sbs_path, cfg, used_uids):
    uid = _find_dependency_uid(
        root, lambda fn: fn.split("/")[-1].lower() == "cluster_system_01.sbsar")
    if uid:
        return uid
    deps = root.find("dependencies")
    if deps is None:
        raise RuntimeError("dependencies 요소가 없음")
    uid = _new_uid(used_uids)
    dep = ET.SubElement(deps, "dependency")
    ET.SubElement(dep, "filename").set("v", _relpath_posix(cluster_sbsar(cfg), Path(sbs_path).parent))
    ET.SubElement(dep, "uid").set("v", uid)
    ET.SubElement(dep, "type").set("v", "package")
    ET.SubElement(dep, "fileUID").set("v", "0")
    ET.SubElement(dep, "versionUID").set("v", "0")
    return uid


def _load_template():
    if not TEMPLATE_PATH.exists():
        raise RuntimeError(f"템플릿 없음: {TEMPLATE_PATH}")
    wrap = ET.parse(TEMPLATE_PATH).getroot()
    graph = wrap.find("graph")
    resource = wrap.find("resource")
    if graph is None or resource is None:
        raise RuntimeError("템플릿 파일이 손상됨 (graph/resource 누락)")
    return graph, resource


def m_graph_backup(sbs_path, graph_name):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = Path(sbs_path).with_name(
        f"{Path(sbs_path).stem}.pcgtex_backup_before_add_{graph_name}_{ts}.sbs")
    shutil.copy2(sbs_path, backup)
    return backup


def _patch_m_graph_input_resource_legacy(sbs_path, graph_name, slot, input_path):
    """Point one existing M_ graph bitmap slot at a new file, with rollback."""
    sbs_path = Path(sbs_path)
    input_path = Path(input_path)
    tree = ET.parse(sbs_path)
    root = tree.getroot()
    graph = _find_graph(root, graph_name)
    if graph is None:
        raise RuntimeError(f"graph not found: {graph_name} in {sbs_path.name}")
    nodes, bitmap_by_uid = _graph_bitmap_nodes(graph)
    instance_node = None
    for node in graph.find("compNodes") or []:
        imp_wrap = node.find("compImplementation")
        if imp_wrap is None or not len(imp_wrap):
            continue
        imp = list(imp_wrap)[0]
        if _is_cluster_instance(imp):
            instance_node = node
    if instance_node is None:
        raise RuntimeError(f"{graph_name}: Cluster_System 인스턴스가 없음")
    ref_uid = None
    for conn in instance_node.iter("connection"):
        ident = conn.find("identifier")
        if ident is not None and ident.get("v") == slot:
            ref = conn.find("connRef")
            ref_uid = ref.get("v") if ref is not None else None
            break
    resources = _upstream_bitmap_resources(ref_uid, nodes, bitmap_by_uid)
    if len(resources) != 1:
        raise RuntimeError(
            f"{graph_name}: {slot} 원본 비트맵을 하나로 추적하지 못함: {resources}")
    resource_name = resources[0]
    resource = None
    for candidate in root.iter("resource"):
        ident = candidate.find("identifier")
        if ident is not None and ident.get("v") == resource_name:
            resource = candidate
            break
    if resource is None or resource.find("filepath") is None:
        raise RuntimeError(f"{graph_name}: 리소스 요소 없음: {resource_name}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_slot = re.sub(r"[^A-Za-z0-9_]+", "_", slot)
    backup = sbs_path.with_name(
        f"{sbs_path.stem}.pcgtex_backup_before_set_{graph_name}_{safe_slot}_{stamp}.sbs")
    shutil.copy2(sbs_path, backup)
    try:
        resource.find("filepath").set("v", _relpath_posix(input_path, sbs_path.parent))
        fmt = IMAGE_EXT_FORMAT.get(input_path.suffix.lower(), "png")
        format_el = resource.find("format")
        if format_el is not None:
            format_el.set("v", fmt)
        tree.write(sbs_path, encoding="utf-8", xml_declaration=True)
        parsed_path = parse_m_graph(sbs_path, graph_name)["inputs"].get(slot)
        if not parsed_path or Path(parsed_path).resolve() != input_path.resolve():
            raise RuntimeError(f"{slot} 입력 변경 검증 실패")
    except Exception:
        shutil.copy2(backup, sbs_path)
        raise
    return {"backup": str(backup), "resource": resource_name, "path": str(input_path)}


def patch_m_graph_input_resource(sbs_path, graph_name, slot, input_path):
    """Point one managed graph slot at a file without mutating shared resources.

    SBS package resources are global. The first patch clones the complete upstream
    node branch and its bitmap resource for this slot; subsequent patches update
    that explicitly isolated resource in place.
    """
    sbs_path = Path(sbs_path)
    input_path = Path(input_path)
    tree = ET.parse(sbs_path)
    root = tree.getroot()
    graph = _find_graph(root, graph_name)
    if graph is None:
        raise RuntimeError(f"graph not found: {graph_name} in {sbs_path.name}")

    nodes, bitmap_by_uid = _graph_bitmap_nodes(graph)
    instance_node = None
    for node in graph.find("compNodes") or []:
        imp_wrap = node.find("compImplementation")
        if imp_wrap is None or not len(imp_wrap):
            continue
        implementation = list(imp_wrap)[0]
        if _is_cluster_instance(implementation):
            instance_node = node
            break
    if instance_node is None:
        raise RuntimeError(f"{graph_name}: Cluster_System instance not found")

    target_connection = None
    ref_uid = None
    for connection in instance_node.iter("connection"):
        identifier = connection.find("identifier")
        if identifier is not None and identifier.get("v") == slot:
            target_connection = connection
            ref = connection.find("connRef")
            ref_uid = ref.get("v") if ref is not None else None
            break
    if target_connection is None or not ref_uid:
        raise RuntimeError(f"{graph_name}: input connection not found: {slot}")

    resources = _upstream_bitmap_resources(ref_uid, nodes, bitmap_by_uid)
    if len(resources) != 1:
        raise RuntimeError(
            f"{graph_name}: expected one source bitmap for {slot}, got {resources}")
    resource_name = resources[0]
    resource = next((
        candidate for candidate in root.iter("resource")
        if candidate.find("identifier") is not None
        and candidate.find("identifier").get("v") == resource_name
    ), None)
    if resource is None or resource.find("filepath") is None:
        raise RuntimeError(f"{graph_name}: resource element not found: {resource_name}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_slot = re.sub(r"[^A-Za-z0-9_]+", "_", slot)
    isolated_base = f"{graph_name}_{safe_slot}__pcgtex_isolated"
    backup = sbs_path.with_name(
        f"{sbs_path.stem}.pcgtex_backup_before_set_{graph_name}_{safe_slot}_{stamp}.sbs")
    shutil.copy2(sbs_path, backup)
    try:
        isolated = resource_name.lower().startswith(isolated_base.lower())
        if not isolated:
            used_uids = _collect_uids(root)
            branch_uids = set()

            def collect_branch(uid):
                if not uid or uid in branch_uids or uid not in nodes:
                    return
                branch_uids.add(uid)
                for upstream_connection in nodes[uid].iter("connection"):
                    upstream_ref = upstream_connection.find("connRef")
                    if upstream_ref is not None:
                        collect_branch(upstream_ref.get("v"))

            collect_branch(ref_uid)
            source_nodes = [
                node for node in (graph.find("compNodes") or [])
                if node.find("uid") is not None
                and node.find("uid").get("v") in branch_uids
            ]
            if not source_nodes:
                raise RuntimeError(f"{graph_name}: empty upstream branch for {slot}")

            clones = [copy.deepcopy(node) for node in source_nodes]
            uid_map = {}
            for clone in clones:
                for uid_element in clone.iter("uid"):
                    old_uid = uid_element.get("v")
                    if old_uid and old_uid not in uid_map:
                        uid_map[old_uid] = _new_uid(used_uids)
            for clone in clones:
                for element in clone.iter():
                    if element.tag in ("uid", "connRef", "connRefOutput", "output"):
                        old_uid = element.get("v")
                        if old_uid in uid_map:
                            element.set("v", uid_map[old_uid])

            existing_names = {
                identifier.get("v")
                for candidate in root.iter("resource")
                for identifier in [candidate.find("identifier")]
                if identifier is not None and identifier.get("v")
            }
            isolated_name = isolated_base
            suffix = 2
            while isolated_name in existing_names:
                isolated_name = f"{isolated_base}_{suffix}"
                suffix += 1

            resource_clone = copy.deepcopy(resource)
            resource_clone.find("identifier").set("v", isolated_name)
            for uid_element in resource_clone.iter("uid"):
                uid_element.set("v", _new_uid(used_uids))
            parent_map = {child: parent for parent in root.iter() for child in parent}
            resource_parent = parent_map.get(resource)
            if resource_parent is None:
                raise RuntimeError(f"{graph_name}: resource parent not found: {resource_name}")
            resource_parent.append(resource_clone)

            replaced_bitmap = False
            for clone in clones:
                for parameter in clone.iter("parameter"):
                    name = parameter.find("name")
                    if name is None or name.get("v") != "bitmapresourcepath":
                        continue
                    value = _param_value(parameter)
                    if not value or value[1].split("/")[-1].split("?")[0] != resource_name:
                        continue
                    param_value = parameter.find("paramValue")
                    value_element = next(iter(param_value), None) if param_value is not None else None
                    if value_element is None:
                        continue
                    query = "?" + value[1].split("?", 1)[1] if "?" in value[1] else ""
                    value_element.set("v", f"pkg:///Resources/{isolated_name}{query}")
                    replaced_bitmap = True
            if not replaced_bitmap:
                raise RuntimeError(
                    f"{graph_name}: bitmap reference missing in cloned branch: {resource_name}")

            comp_nodes = graph.find("compNodes")
            for clone in clones:
                comp_nodes.append(clone)
            target_connection.find("connRef").set("v", uid_map[ref_uid])
            output_ref = target_connection.find("connRefOutput")
            if output_ref is not None and output_ref.get("v") in uid_map:
                output_ref.set("v", uid_map[output_ref.get("v")])
            resource = resource_clone
            resource_name = isolated_name
            isolated = True

        resource.find("filepath").set("v", _relpath_posix(input_path, sbs_path.parent))
        format_element = resource.find("format")
        if format_element is not None:
            format_element.set(
                "v", IMAGE_EXT_FORMAT.get(input_path.suffix.lower(), "png"))
        tree.write(sbs_path, encoding="utf-8", xml_declaration=True)
        parsed_path = parse_m_graph(sbs_path, graph_name)["inputs"].get(slot)
        if not parsed_path or Path(parsed_path).resolve() != input_path.resolve():
            raise RuntimeError(f"{graph_name}: failed to verify patched input {slot}")
    except Exception:
        shutil.copy2(backup, sbs_path)
        raise
    return {
        "backup": str(backup),
        "resource": resource_name,
        "path": str(input_path),
        "isolated": isolated,
    }


def insert_m_graph(sbs_path, graph_name, inputs, normal_opengl=True, cfg=None):
    """elm 템플릿을 복제해 sbs에 T_ 그래프 + 비트맵 리소스를 넣는다.

    inputs: {슬롯: 절대경로}. 없는 슬롯은 중립 이미지로 채운다.
    반환: {"backup": ..., "resources": [...], "graph": graph_name}
    """
    cfg = cfg or load_config()
    sbs_path = Path(sbs_path)
    tree = ET.parse(sbs_path)
    root = tree.getroot()
    if _find_graph(root, graph_name) is not None:
        raise RuntimeError(f"{graph_name} 그래프가 이미 있음")

    himself_uid = _find_dependency_uid(root, lambda fn: fn == "?himself")
    if not himself_uid:
        raise RuntimeError("?himself 의존성이 없음 — 파일이 비정상")
    used_uids = _collect_uids(root)
    cluster_uid = _ensure_cluster_dependency(root, sbs_path, cfg, used_uids)

    template_graph, template_resource = _load_template()
    graph = copy.deepcopy(template_graph)
    graph.find("identifier").set("v", graph_name)

    # 1) 템플릿 내부 uid 전부 새로 발급 (connRef/connRefOutput/output 참조 유지)
    uid_map = {}
    for el in graph.iter("uid"):
        old = el.get("v")
        if old and old not in uid_map:
            uid_map[old] = _new_uid(used_uids)
    for el in graph.iter():
        if el.tag in ("uid", "connRef", "connRefOutput", "output"):
            old = el.get("v")
            if old in uid_map:
                el.set("v", uid_map[old])

    # 2) 비트맵 리소스 경로/인스턴스 경로 재작성
    filled = {}
    for slot in SLOT_ORDER:
        path = neutral_image("white") if slot == "Subsurface_Amount" else inputs.get(slot)
        if path is None:
            path = neutral_image(neutral_kind_for_slot(slot))
        filled[slot] = Path(path)

    resources = []
    for el in graph.iter():
        if el.tag != "compImplementation" or not len(el):
            continue
        imp = list(el)[0]
        if _is_cluster_instance(imp):
            imp.find("path").set("v", f"pkg:///Cluster_System_01?dependency={cluster_uid}")
            for param in imp.iter("parameter"):
                if param.find("name").get("v") == "normal":
                    pv = param.find("paramValue/constantValueBool")
                    if pv is not None:
                        pv.set("v", "1" if normal_opengl else "0")
        elif imp.tag == "compFilter" and imp.find("filter") is not None \
                and imp.find("filter").get("v") == "bitmap":
            for param in imp.iter("parameter"):
                if param.find("name").get("v") != "bitmapresourcepath":
                    continue
                value_el = param.find("paramValue/constantValueString")
                old_res = value_el.get("v").split("/")[-1].split("?")[0]
                suffix = old_res[len(TEMPLATE_GRAPH_NAME) + 1:].lower()
                slot = TEMPLATE_SUFFIX_TO_SLOT.get(suffix)
                if slot is None:
                    raise RuntimeError(f"템플릿 리소스 접미사 해석 실패: {old_res}")
                new_res = f"{graph_name}_{SLOT_SUFFIX[slot]}"
                value_el.set("v", f"pkg:///Resources/{new_res}?dependency={himself_uid}")
                resources.append((new_res, filled[slot]))

    # 3) 리소스 요소 생성 (Resources 그룹의 content 안)
    content = root.find("content")
    group = None
    for grp in content.findall("group"):
        ident = grp.find("identifier")
        if ident is not None and ident.get("v") == "Resources":
            group = grp
            break
    if group is None:
        group = ET.SubElement(content, "group")
        ET.SubElement(group, "identifier").set("v", "Resources")
        ET.SubElement(group, "uid").set("v", _new_uid(used_uids))
        ET.SubElement(group, "content")
    group_content = group.find("content")
    if group_content is None:
        group_content = ET.SubElement(group, "content")

    existing_res = {r.find("identifier").get("v")
                    for r in group_content.findall("resource")
                    if r.find("identifier") is not None}
    added_resources = []
    for res_name, path in dict(resources).items():
        if res_name in existing_res:
            continue
        res = copy.deepcopy(template_resource)
        res.find("identifier").set("v", res_name)
        res.find("uid").set("v", _new_uid(used_uids))
        res.find("filepath").set("v", _relpath_posix(path, sbs_path.parent))
        fmt = IMAGE_EXT_FORMAT.get(Path(path).suffix.lower(), "png")
        res.find("format").set("v", fmt)
        group_content.append(res)
        added_resources.append(res_name)

    resolution = render_size_log2(filled)
    _set_graph_resolution(graph, resolution)
    content.append(graph)

    backup = m_graph_backup(sbs_path, graph_name)
    tree.write(sbs_path, encoding="utf-8", xml_declaration=True)

    # 4) 검증: 다시 파싱해서 입력이 읽히는지 확인
    parsed = parse_m_graph(sbs_path, graph_name)
    missing = [s for s in ("Base_Color", "Opacity") if s not in parsed["inputs"]]
    if missing:
        shutil.copy2(backup, sbs_path)
        raise RuntimeError(f"삽입 검증 실패({missing}) — 백업에서 복원함")
    return {"backup": str(backup), "graph": graph_name,
            "resources": added_resources,
            "inputs": {k: str(v) for k, v in parsed["inputs"].items()},
            "size_log2": resolution,
            "pixel_size": size_log2_pixels(resolution)}


# ------------------------------------------------------------------ CLI
def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_render = sub.add_parser("render", help="sbs의 T_ 관리 그래프를 읽어 6장 렌더")
    p_render.add_argument("sbs")
    p_render.add_argument("graph")
    p_render.add_argument("--out-dir", required=True)
    p_render.add_argument(
        "--size", type=int, default=None,
        help="optional square log2 override; default keeps Base Color ratio with a 4K cap")

    p_list = sub.add_parser("list", help="sbs 안의 T_/레거시 M_ 그래프 나열")
    p_list.add_argument("sbs")

    args = parser.parse_args()
    if args.cmd == "list":
        print(json.dumps(list_m_graphs(args.sbs), ensure_ascii=False))
        return
    if args.cmd == "render":
        info = parse_m_graph(args.sbs, args.graph)
        files = render_maps(args.graph, info["inputs"], info["params"],
                            args.out_dir, size_log2=args.size)
        print(json.dumps([str(f) for f in files], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
