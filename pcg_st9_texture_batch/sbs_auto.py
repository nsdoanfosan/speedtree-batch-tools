"""③ Substance 자동화.

두 가지를 한다:
1. 렌더: Cluster_System_01.sbsar 를 sbsrender 로 직접 돌려서
   아틀라스 텍스처 5장(color/normal/extra/height/opacity)을 뽑는다.
   - .sbs 안에 M_ 그래프가 이미 있으면: 그 그래프의 비트맵 연결과 인스턴스
     파라미터를 XML에서 읽어 그대로 사용한다 (Designer에서 만든 세팅 존중).
     M_ 그래프는 "비트맵 → Cluster_System_01 인스턴스 → 출력" 순수 통과
     구조라서 직접 렌더와 결과가 같다 (elm에서 픽셀 비교로 검증).
   - 없으면: 검사 보드의 원본 텍스처 후보를 슬롯에 매핑해서 렌더한다.
2. .sbs에 M_ 그래프 삽입: 사용자가 Designer에서 계속 관리할 수 있도록,
   elm의 M_Leaf_elm_atlas_01 을 템플릿으로 새 그래프+리소스를 .sbs에 넣는다.
   수정 전 pcgtex_backup 백업이 남는다.

세트 .sbs 전체를 sbscooker로 쿡하는 방식은 쓰지 않는다: 레거시 그래프들의
깨진 참조와 Cluster_System_01 이중 의존성 때문에 쿡이 실패한다(Error 13).
"""
import copy
import json
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

RENDER_MAPS = ("color", "normal", "extra", "height", "opacity")

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
# 참고: 'normal'(OpenGL 플래그) 파라미터는 M_ 그래프들에 저장돼 있지만
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
                            size_log2=12, timeout=1800):
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
    newest_source = max(height_path.stat().st_mtime, hbao_sbsar.stat().st_mtime)
    if target.exists() and target.stat().st_mtime >= newest_source:
        return target
    with tempfile.TemporaryDirectory(prefix="hbao_", dir=str(generated_dir)) as temp_dir:
        name = f"{atlas_base}_ao_from_height_{{outputNodeName}}"
        cmd = [
            str(exe), "render", str(hbao_sbsar),
            "--set-entry", f"input@{height_path}",
            "--set-value", f"$outputsize@{size_log2},{size_log2}",
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


def _find_graph(root, graph_name):
    for graph in root.iter("graph"):
        ident = graph.find("identifier")
        if ident is not None and ident.get("v") == graph_name:
            return graph
    return None


def list_m_graphs(sbs_path):
    try:
        root = ET.parse(sbs_path).getroot()
    except Exception:
        return []
    names = []
    for graph in root.iter("graph"):
        ident = graph.find("identifier")
        if ident is not None and ident.get("v", "").lower().startswith("m_"):
            names.append(ident.get("v"))
    return names


def find_m_graph_name(sbs_path, atlas_base):
    """atlas_base(M_...)와 대소문자 무시 일치하는 그래프 이름을 찾는다."""
    for name in list_m_graphs(sbs_path):
        if name.lower() == str(atlas_base).lower():
            return name
    return None


def parse_m_graph(sbs_path, graph_name):
    """M_ 그래프의 비트맵 연결과 인스턴스 파라미터를 읽는다."""
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

    bitmap_by_uid = {}
    instance = None          # compInstance 요소 (파라미터가 여기에)
    instance_node = None     # 상위 compNode 요소 (connection이 여기에)
    comps = graph.find("compNodes")
    for node in comps or []:
        uid = node.find("uid").get("v")
        imp_wrap = node.find("compImplementation")
        if imp_wrap is None or not len(imp_wrap):
            continue
        imp = list(imp_wrap)[0]
        if imp.tag == "compFilter" and imp.find("filter") is not None \
                and imp.find("filter").get("v") == "bitmap":
            for param in imp.iter("parameter"):
                if param.find("name").get("v") == "bitmapresourcepath":
                    value = _param_value(param)
                    if value:
                        res_name = value[1].split("/")[-1].split("?")[0]
                        bitmap_by_uid[uid] = res_name
        elif imp.tag == "compInstance":
            instance = imp
            instance_node = node

    if instance is None:
        raise RuntimeError(f"{graph_name}: Cluster_System 인스턴스가 없음")

    inputs = {}
    for conn in instance_node.iter("connection"):
        slot = conn.find("identifier").get("v")
        ref = conn.find("connRef").get("v")
        res_name = bitmap_by_uid.get(ref)
        if res_name and res_name in resource_files:
            inputs[slot] = resource_files[res_name]

    params = {}
    for param in instance.iter("parameter"):
        name = param.find("name").get("v")
        value = _param_value(param)
        if value:
            params[name] = value
    return {"inputs": inputs, "params": params}


# ------------------------------------------------------------------ 렌더
def neutral_image(kind):
    """없는 입력용 중립 이미지 (black / flat normal)를 assets에 만들어 재사용."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    path = ASSETS_DIR / f"neutral_{kind}.png"
    if not path.exists():
        from PIL import Image
        color = (128, 128, 255) if kind == "normal" else (0, 0, 0)
        Image.new("RGB", (16, 16), color).save(path)
    return path


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
                maps=RENDER_MAPS, size_log2=12, timeout=1800,
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

    cmd = [str(exe), "render", str(sbsar)]
    for slot in SLOT_ORDER:
        path = inputs.get(slot)
        if path is None:
            path = neutral_image("normal" if slot == "Normal" else "black")
        cmd += ["--set-entry", f"{slot}@{Path(path)}"]
    cmd += ["--set-value", f"$outputsize@{size_log2},{size_log2}"]
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
    }
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
    r"^m_.*_(color|normal|extra|height|opacity)\.(tga|png|tif|tiff)$", re.IGNORECASE)

SOURCE_BUCKETS = {
    "albedo": "source_albedo",
    "alpha": "source_alpha",
    "normal": "source_normal",
    "height": "source_height",
    "ao": "source_ao",
    "roughness": "source_roughness",
}
FAMILY_SUFFIX_RE = re.compile(
    r"(?:[_-](?:base[_-]?color|basecolor|albedo|diffuse|colour|color|opacity|alpha|"
    r"transparency|mask|normal|nor[_-]?gl|nrm|height|displacement|depth|"
    r"ambient[_-]?occlusion|ao|occlusion|roughness|rough|gloss))$",
    re.IGNORECASE,
)
RESOLUTION_SUFFIX_RE = re.compile(r"(?:[_-](?:1k|2k|4k|8k|16k|\d+x\d+))$", re.IGNORECASE)
GENERIC_TARGET_TOKENS = {
    "m", "sk", "sm", "atlas", "cluster", "material", "tree", "bush", "weed",
    "leaf", "leaves", "branch", "bark", "color", "normal", "extra", "height",
    "opacity", "basecolor", "albedo", "01", "02", "03", "04", "05",
}


def source_family_key(path):
    stem = Path(path).stem.lower()
    stem = FAMILY_SUFFIX_RE.sub("", stem)
    stem = RESOLUTION_SUFFIX_RE.sub("", stem)
    stem = FAMILY_SUFFIX_RE.sub("", stem)
    return stem.strip("_-")


def _source_target_tokens(row):
    values = [row.get("folder_name", ""), row.get("cluster_name", ""), row.get("atlas_base", "")]
    values.extend(row.get("material_names") or [])
    tokens = re.findall(r"[a-z]+|\d+", " ".join(map(str, values)).lower())
    return sorted({token for token in tokens if token not in GENERIC_TARGET_TOKENS and len(token) > 2})


class SourceSetAmbiguity(RuntimeError):
    def __init__(self, message, candidates):
        super().__init__(message)
        self.candidates = candidates


def source_set_candidates(row):
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
            if OWN_EXPORT_RE.match(Path(str(ref).replace("\\", "/")).name):
                continue
            path = resolve_ref(ref, bases)
            if not path:
                continue
            key = (str(path.parent).lower(), source_family_key(path))
            group = groups.setdefault(key, {"paths": {}, "parent": path.parent, "family": key[1]})
            group["paths"].setdefault(kind, path)
    complete = [group for group in groups.values()
                if group["paths"].get("albedo") and group["paths"].get("alpha")]
    if not complete:
        found = ", ".join(sorted({group["family"] for group in groups.values()})) or "없음"
        raise RuntimeError(f"같은 원본 세트에서 albedo+alpha를 찾지 못함 (후보: {found})")
    target_tokens = _source_target_tokens(row)
    for group in complete:
        haystack = f"{group['parent']} {group['family']}".lower()
        group["score"] = sum(len(token) for token in target_tokens if token in haystack)
        group["label"] = f"{group['parent']}\\{group['family']}"
    complete.sort(key=lambda group: (-group["score"], str(group["parent"]).lower(), group["family"]))
    return complete


def select_source_set(row, preferred=None):
    """Choose one coherent texture family; never mix albedo/alpha from different sets."""
    complete = source_set_candidates(row)
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


def plan_inputs_from_row(row, preferred=None):
    """검사 보드 texture-plan 행에서 렌더 입력을 만든다. (M_ 그래프가 없을 때)"""
    selected = select_source_set(row, preferred=preferred)
    paths = selected["paths"]
    albedo = paths["albedo"]
    alpha = paths["alpha"]
    normal = paths.get("normal")
    height = paths.get("height")
    ao = paths.get("ao")
    roughness = paths.get("roughness")

    notes = [f"원본 세트: {selected['parent']}\\{selected['family']}"]
    inputs = {"Base_Color": albedo, "Opacity": alpha}
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
    # Subsurface/Vertex_Color 는 기본 검정 (render_maps 에서 채움)
    return inputs, notes


def ensure_hbao_input(atlas_base, inputs, out_dir, cfg=None, size_log2=12, timeout=1800):
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
        "Depth_Blend": ("constantValueFloat1", "0"),
        "normal": ("constantValueBool", "1" if normal_opengl else "0"),
        "step_01": ("constantValueBool", "1"),
        "switch": ("constantValueBool", "0"),
        "distance": ("constantValueFloat1", "0"),
    }


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


def patch_m_graph_input_resource(sbs_path, graph_name, slot, input_path):
    """Point one existing M_ graph bitmap slot at a new file, with rollback."""
    sbs_path = Path(sbs_path)
    input_path = Path(input_path)
    tree = ET.parse(sbs_path)
    root = tree.getroot()
    graph = _find_graph(root, graph_name)
    if graph is None:
        raise RuntimeError(f"graph not found: {graph_name} in {sbs_path.name}")
    bitmap_by_uid = {}
    instance_node = None
    for node in graph.find("compNodes") or []:
        uid_el = node.find("uid")
        imp_wrap = node.find("compImplementation")
        if uid_el is None or imp_wrap is None or not len(imp_wrap):
            continue
        imp = list(imp_wrap)[0]
        if imp.tag == "compFilter" and imp.find("filter") is not None \
                and imp.find("filter").get("v") == "bitmap":
            for param in imp.iter("parameter"):
                name_el = param.find("name")
                if name_el is None or name_el.get("v") != "bitmapresourcepath":
                    continue
                value = _param_value(param)
                if value:
                    bitmap_by_uid[uid_el.get("v")] = value[1].split("/")[-1].split("?")[0]
        elif imp.tag == "compInstance":
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
    resource_name = bitmap_by_uid.get(ref_uid)
    if not resource_name:
        raise RuntimeError(f"{graph_name}: {slot} 비트맵 리소스를 찾지 못함")
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


def insert_m_graph(sbs_path, graph_name, inputs, normal_opengl=True, cfg=None):
    """elm 템플릿을 복제해 sbs에 M_ 그래프 + 비트맵 리소스를 넣는다.

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
        path = inputs.get(slot)
        if path is None:
            path = neutral_image("normal" if slot == "Normal" else "black")
        filled[slot] = Path(path)

    resources = []
    for el in graph.iter():
        if el.tag != "compImplementation" or not len(el):
            continue
        imp = list(el)[0]
        if imp.tag == "compInstance":
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
            "inputs": {k: str(v) for k, v in parsed["inputs"].items()}}


# ------------------------------------------------------------------ CLI
def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_render = sub.add_parser("render", help="sbs의 M_ 그래프를 읽어 5장 렌더")
    p_render.add_argument("sbs")
    p_render.add_argument("graph")
    p_render.add_argument("--out-dir", required=True)
    p_render.add_argument("--size", type=int, default=12, help="log2 크기 (12=4096)")

    p_list = sub.add_parser("list", help="sbs 안의 M_ 그래프 나열")
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
