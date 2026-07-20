"""SK Vegetation Batch — SPM 본 캘리브레이션 + Blender Repair + Unreal Push GUI.

단계 (왼쪽부터, 빠른 것 → 느린 것):
  🔍 검사        : 아무것도 수정하지 않고 상태만 표에 채움 (SPM 본 세팅 상태,
                   M_ 머티리얼, blend 최신 여부, 핸드오프 JSON 준비 여부)
  ① SPM 본 세팅 : 가지 수를 실측(프로브 익스포트)해서 "가지당 목표 본 수"에
                   맞게 Relative 값을 자동 계산. 파일당 수십초~수분. 실패해도
                   백업에서 자동 복원되므로 여기서 전부 끝내고 ②로 넘어가면 됨.
  ② Blender Repair : 헤드리스 Blender로 import/repair 후 SPM 옆에 .blend 저장.
                   파일당 수분~수십분(느림). 이미 최신인 blend는 건너뜀.
  ③ Unreal Push : 보내기 전에 준비 검사(blend/JSON 존재, 언리얼 실행 여부)를
                   먼저 전부 통과시킨 뒤에만 실제 push 시작.

모든 무거운 작업은 낮은 우선순위 + CPU 코어 제한이 걸린 백그라운드 프로세스로
실행된다 (자식 SpeedTree CLI에 상속. 헤드리스 Blender는 GPU를 쓰지 않음).
"""
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

from batch_ui_common import CheckedRowController, copy_selected_row_paths

from sk_common import (
    CALIBRATION_CACHE_VERSION,
    LOG_DIR,
    PUSH_ABORT_KINDS,
    PUSH_MANIFEST_SCHEMA_VERSION,
    PUSH_SOURCE_FINGERPRINT_CACHE_VERSION,
    atomic_write_bytes,
    atomic_write_json,
    blend_path_for,
    cached_file_content_snapshot,
    cached_push_source_fingerprint,
    calibration_cache_matches,
    calibration_settings_signature,
    classify_push_failure,
    close_process_kill_job,
    compact_error_message,
    file_content_snapshot,
    is_manual_bones_locked,
    legacy_calibration_settings_signature,
    launch_limited,
    load_config,
    load_job_report,
    load_state,
    manifest_item_files_match,
    manual_bones_marker_path,
    save_config,
    save_state,
    scan_sk_spms,
    set_manual_bones_marker,
    push_source_snapshot,
    summarize_job_failure,
    terminate_process_tree,
    wind_preset_for,
)
from spm_leaf_handoff_contract import (
    inspect_all_speedtree_material_export,
    inspect_speedtree_material_export,
    inspect_spm_leaf_contract,
    leaf_contract_user_message,
    save_leaf_contract_cache,
    speedtree_stmat_path,
)
from speedtree_pipeline_contract import validate_preflight_envelope

WIND_OPTIONS = (
    ("자동 (파일명 기준)", "auto"),
    ("TREE", "TREE"),
    ("BUSH", "BUSH"),
    ("GRASS", "GRASS"),
    ("NONE", "NONE"),
)
BONE_MODE_OPTIONS = (("자동 계산", "auto"), ("수동 본 유지", "manual"))
CHECK_ON = "☑"
CHECK_OFF = "☐"
STATUS_COLUMNS = ("spm_status", "blend_status", "push_status")


def compact_table_status(value, max_chars=28):
    """Keep the overview table compact; the full value stays in row details."""
    text = " ".join(str(value or "-").split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars - 1].rstrip()}…"


def _normalized_path(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _sha256_snapshot(path):
    """Read a stable SHA-256 snapshot without changing the source file."""
    candidate = Path(path)
    for _attempt in range(2):
        before = candidate.stat()
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = candidate.stat()
        if (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        ):
            return {
                "sha256": digest.hexdigest(),
                "size": after.st_size,
            }
    raise OSError(f"File changed while reading: {candidate}")


def current_speedtree_bone_measurement(spm):
    """Return a verified exported bone count for the current SPM, or None.

    The XML is accepted only when its artifact hash matches the export receipt.
    The receipt's SPM input hash marks the count as current or last-measured.
    This is deliberately read-only and content-based: a timestamp-only change
    cannot schedule or invalidate work.
    """
    spm = Path(spm)
    xml_path = spm.parent / "xml" / f"{spm.stem}.xml"
    receipt_path = (
        xml_path.parent / ".speedtree_export_cache" / f"{xml_path.name}.json"
    )
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        source = receipt["inputs"]["spm"]
        if _normalized_path(source["path"]) != _normalized_path(spm):
            return None
        source_snapshot = _sha256_snapshot(spm)
        source_current = not (
            source_snapshot["size"] != int(source["size"])
            or source_snapshot["sha256"].lower()
            != str(source["sha256"]).lower()
        )

        artifact = next(
            item
            for item in receipt["artifacts"]
            if item.get("relative_path") == xml_path.name
        )
        xml_snapshot = _sha256_snapshot(xml_path)
        if (
            xml_snapshot["size"] != int(artifact["size"])
            or xml_snapshot["sha256"].lower()
            != str(artifact["sha256"]).lower()
        ):
            return None

        root = ET.parse(xml_path).getroot()
        xml_source = root.get("Source")
        if xml_source and _normalized_path(xml_source) != _normalized_path(spm):
            return None
        bones = root.find(".//Bones")
        if bones is None:
            return None
        actual_count = len(bones.findall("Bone"))
        declared_count = int(bones.get("Count", actual_count))
        if declared_count != actual_count:
            return None
        return {
            "count": actual_count,
            "current": source_current,
            "xml_path": xml_path,
            "receipt_path": receipt_path,
        }
    except (ET.ParseError, OSError, TypeError, ValueError, KeyError, StopIteration):
        return None


def manual_bone_status_text(spm):
    measurement = current_speedtree_bone_measurement(spm)
    if measurement is None:
        return "수동 본 유지 🔒 · 본 수 미측정 (검증된 SpeedTree XML 없음)"
    if not measurement["current"]:
        return (
            f"수동 본 유지 🔒 · 최근 실측 본 {measurement['count']}개 "
            "(SPM 변경 후 미재검증)"
        )
    return (
        f"수동 본 유지 🔒 · SpeedTree 본 {measurement['count']}개 "
        "(현재 SPM과 일치하는 XML)"
    )


def selected_row_detail_text(spm, statuses):
    return "\n".join(
        (
            f"경로  {spm}",
            f"① SPM  {statuses.get('spm_status', '-')}",
            f"② Blender  {statuses.get('blend_status', '-')}",
            f"③ Unreal  {statuses.get('push_status', '-')}",
        )
    )


def spm_check_status_parts(audit):
    """Return human-readable SPM settings without implying a failure."""
    generators = audit.get("generators") or []
    fixed = sum(
        1
        for generator in generators
        if generator.get("style") == 0.0
        and (generator.get("bones") or 0) > 0
    )
    relative = sum(
        1 for generator in generators if generator.get("style") == 1.0
    )
    disabled = sum(
        1
        for generator in generators
        if generator.get("style") == 0.0
        and (generator.get("bones") or 0) == 0
    )
    material_renames = sum(
        1 for material in (audit.get("materials") or [])
        if material.get("needs_prefix")
    )
    parts = []
    if fixed:
        parts.append(f"고정 본(Absolute) {fixed}개")
    if relative:
        parts.append(f"자동 본(Relative) {relative}개")
    if disabled:
        parts.append(f"본 꺼짐 {disabled}개")
    if material_renames:
        parts.append(f"M_ 필요 {material_renames}개")
    graph = audit.get("bone_graph") or {}
    if graph.get("root_target_generator_count"):
        parts.append(
            f"자동 대상 {graph['root_target_generator_count']} / "
            f"Base 제외 {graph['base_excluded_generator_count']}"
        )
    if graph.get("unknown_base_generators"):
        parts.append(f"Base 미분류 {len(graph['unknown_base_generators'])}")
    return parts


class BatchItemError(RuntimeError):
    """One item failed, with a machine-readable queue-impact classification."""

    def __init__(
        self, reason, kind="data_error", report=None, log_file=None, report_file=None
    ):
        super().__init__(reason)
        self.kind = kind
        self.report = report or {}
        self.log_file = log_file
        self.report_file = report_file


class Tooltip:
    """말풍선 도움말: 위젯에 마우스를 올리면 설명이 뜬다."""

    def __init__(self, widget, text, wrap=380):
        self.widget = widget
        self.text = text
        self.wrap = wrap
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip, text=self.text, justify="left", wraplength=self.wrap,
            background="#ffffe0", relief="solid", borderwidth=1, padx=6, pady=4,
        ).pack()

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.state = load_state()
        self.items = {}  # iid -> {"spm": Path, "checked": bool, "wind_override": str}
        self._table_full_values = {}
        self.checked_rows = CheckedRowController(self.items, self._redraw_checked_row)
        self.ui_queue = queue.Queue()
        self.worker = None
        self.cell_editor = None
        self.stop_flag = threading.Event()
        self.active_procs = set()          # all running child procs (serial or parallel)
        self.procs_lock = threading.Lock()
        self.state_lock = threading.RLock()  # guards self.state writes across worker threads
        self._scan_generation = 0
        self.scan_worker = None
        self._live_poll_active = False
        self._live_poll_after_id = None
        self.spm_calibration_signature = calibration_settings_signature(self.cfg)
        self.legacy_spm_calibration_signature = (
            legacy_calibration_settings_signature(self.cfg)
        )

        root.title("SK Vegetation Batch — 검사 → 본 세팅 → Blender → Unreal")
        root.geometry("1460x840")
        self._build_ui()
        self.root.after(100, self._drain_ui_queue)
        self.scan()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="루트:").pack(side="left")
        self.root_var = tk.StringVar(value=self.cfg["root"])
        ttk.Entry(top, textvariable=self.root_var, width=66).pack(side="left", padx=4)
        self.btn_pick_root = ttk.Button(top, text="...", width=3, command=self._pick_root)
        self.btn_pick_root.pack(side="left")
        self.btn_scan = ttk.Button(top, text="스캔", command=self.scan)
        self.btn_scan.pack(side="left", padx=6)
        self.btn_select_all = ttk.Button(top, text="전체 선택", command=lambda: self._set_all(True))
        self.btn_select_all.pack(side="left")
        self.btn_clear_all = ttk.Button(top, text="전체 해제", command=lambda: self._set_all(False))
        self.btn_clear_all.pack(side="left", padx=4)
        ttk.Button(
            top, text="선택 SPM 경로 복사", command=self.copy_selected_paths
        ).pack(side="left", padx=(4, 0))

        opts = ttk.LabelFrame(self.root, text="옵션 (각 항목에 마우스를 올리면 설명이 뜹니다)", padding=6)
        opts.pack(fill="x", padx=6)

        transport_opts = ttk.LabelFrame(
            self.root,
            text="Unreal Push transport",
            padding=6,
        )
        transport_opts.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(transport_opts, text="③ Unreal Push:").pack(side="left")
        self.transport_var = tk.StringVar(
            value=self.cfg.get("push_transport", "rpc")
        )
        transport_combo = ttk.Combobox(
            transport_opts,
            textvariable=self.transport_var,
            values=("rpc", "headless"),
            width=12,
            state="readonly",
        )
        transport_combo.pack(side="left", padx=6)
        Tooltip(
            transport_combo,
            "rpc = 기존 열린 Unreal Editor 경로\n"
            "headless = Blender 전체 export 후 UnrealEditor-Cmd 1회 배치 import",
        )
        self.night_headless_var = tk.BooleanVar(
            value=bool(self.cfg.get("night_headless", True))
        )
        night_check = ttk.Checkbutton(
            transport_opts,
            text="목록 전체 자동(야간)은 headless",
            variable=self.night_headless_var,
        )
        night_check.pack(side="left", padx=(12, 0))
        Tooltip(
            night_check,
            "켜면 목록 전체 자동의 마지막 Push는 열린 Unreal 없이 headless로 실행합니다. "
            "단독 ③ Push는 왼쪽 transport 선택을 따릅니다.",
        )
        ttk.Label(transport_opts, text="② Repair·③ export 동시:").pack(
            side="left", padx=(18, 0)
        )
        self.blender_parallel_var = tk.IntVar(
            value=int(self.cfg.get("blender_parallel_jobs", 2))
        )
        blender_parallel_spin = ttk.Spinbox(
            transport_opts,
            from_=1,
            to=4,
            textvariable=self.blender_parallel_var,
            width=4,
        )
        blender_parallel_spin.pack(side="left", padx=4)
        Tooltip(
            blender_parallel_spin,
            "Blender Repair와 headless Send2UE export를 동시에 처리할 개수입니다. "
            "기본값 2는 시작 비용을 겹치되 메모리 사용량을 제한합니다.",
        )

        lbl = ttk.Label(opts, text="가지당 목표 본 수:")
        lbl.pack(side="left")
        self.target_var = tk.DoubleVar(value=float(self.cfg.get("target_bones_per_branch", 2.0)))
        spin = ttk.Spinbox(opts, from_=1, to=6, increment=0.5, textvariable=self.target_var, width=5)
        spin.pack(side="left", padx=4)
        tip = ("① SPM 본 세팅에서 '작은 식물'의 목표입니다.\n"
               "가지(spline) 수를 실제 익스포트로 세고, 총 본 수가 대략 '가지 수 × 이 값'이 "
               "되도록 SpeedTree의 Relative 값을 자동 계산합니다.\n"
               "· 작은 풀: 가지 하나당 이 개수 근처의 본이 들어감\n"
               "· 큰 나무: '가지 수 × 이 값'이 아래 '최대 총 본 수'를 넘으면 "
               "Base 소속 본을 먼저 끄고, 그래도 넘을 때만 Tree 밀도를 낮춤\n"
               "Relative 스타일이라 가지 길이(=Size scalar 포함)에 비례해 본이 배분됩니다.")
        Tooltip(lbl, tip); Tooltip(spin, tip)

        lbl2 = ttk.Label(opts, text="최대 총 본 수:")
        lbl2.pack(side="left", padx=(10, 0))
        self.maxtotal_var = tk.IntVar(value=int(self.cfg.get("max_total_bones", 1500)))
        spin2 = ttk.Spinbox(opts, from_=200, to=8000, increment=100, textvariable=self.maxtotal_var, width=6)
        spin2.pack(side="left", padx=4)
        tip2 = ("한 나무의 총 본 수 상한입니다 (본 폭증 방지의 핵심).\n"
                "큰 나무는 가지가 수천~수만 개라 '가지당 목표'를 그대로 적용하면 본이 폭발합니다"
                "(예: elm_03은 가지 15,234개 → 예전 방식 80,000본).\n"
                "이 상한이 걸리면 Base 소속 자동 대상부터 Absolute/0으로 끕니다. "
                "그래도 초과할 때만 Tree/트렁크의 Relative 밀도를 낮춥니다.")
        Tooltip(lbl2, tip2); Tooltip(spin2, tip2)

        lbl4 = ttk.Label(opts, text="우선순위:")
        lbl4.pack(side="left", padx=(14, 0))
        self.priority_var = tk.StringVar(value=self.cfg.get("priority", "belownormal"))
        combo = ttk.Combobox(opts, textvariable=self.priority_var, values=["idle", "belownormal", "normal"],
                             width=11, state="readonly")
        combo.pack(side="left", padx=4)
        tip4 = ("백그라운드 작업의 CPU 우선순위입니다.\n"
                "idle = 다른 작업에 거의 영향 없음(가장 느림)\n"
                "belownormal = 권장 (다른 작업 우선, 놀 때만 전력)\n"
                "normal = 최고 속도 (컴퓨터가 무거워질 수 있음)")
        Tooltip(lbl4, tip4); Tooltip(combo, tip4)

        lbl5 = ttk.Label(opts, text="CPU 코어:")
        lbl5.pack(side="left", padx=(10, 0))
        self.cores_var = tk.IntVar(value=int(self.cfg.get("cpu_cores", 4)))
        spin5 = ttk.Spinbox(opts, from_=1, to=64, textvariable=self.cores_var, width=5)
        spin5.pack(side="left", padx=4)
        tip5 = ("백그라운드 작업이 사용할 수 있는 CPU 코어 수 제한입니다 (순차 실행 시). "
                "동시 실행이 2 이상이면 이 제한은 무시하고 모든 코어에 분산합니다.")
        Tooltip(lbl5, tip5); Tooltip(spin5, tip5)

        lbl6 = ttk.Label(opts, text="동시 실행:")
        lbl6.pack(side="left", padx=(10, 0))
        self.parallel_var = tk.IntVar(value=int(self.cfg.get("spm_parallel_jobs", 4)))
        spin6 = ttk.Spinbox(opts, from_=1, to=16, textvariable=self.parallel_var, width=4)
        spin6.pack(side="left", padx=4)
        tip6 = ("① SPM 본 세팅을 몇 개 파일 동시에 처리할지.\n"
                "기본값 4로 독립 SPM을 병렬 처리합니다. 한 번의 SpeedTree 익스포트가 "
                "2분을 넘는 파일은 원본을 복원하고 수동 처리로 넘깁니다.")
        Tooltip(lbl6, tip6); Tooltip(spin6, tip6)

        self.force_var = tk.BooleanVar(value=False)
        chk = ttk.Checkbutton(opts, text="완료된 항목도 다시 실행", variable=self.force_var)
        chk.pack(side="left", padx=12)
        Tooltip(chk, ("② Blender Repair에서, 이미 SPM보다 최신인 .blend가 있는 항목은 기본적으로 "
                      "건너뜁니다. ① SPM 본 세팅도 동일 SPM/옵션 캐시를 기본 사용합니다. "
                      "이 옵션을 켜면 ①② 모두 강제로 다시 실행합니다."))

        actions = ttk.Frame(self.root, padding=6)
        actions.pack(fill="x")
        self.btn_check = ttk.Button(actions, text="🔍 검사 (수정 없음)",
                                    command=lambda: self.start_batch("check"))
        self.btn_check.pack(side="left")
        Tooltip(self.btn_check, ("아무것도 수정하지 않습니다.\n"
                                 "SPM 본 세팅 상태 / M_ 머티리얼 / blend 최신 여부 / 핸드오프 JSON "
                                 "준비 여부를 빠르게 확인해서 표에 채웁니다.\n"
                                 "'고정 본(Absolute)'은 자동 보정 실패가 아니라 가지마다 고정된 "
                                 "본 수를 쓰는 Generator이고, '자동 본(Relative)'은 가지 길이에 따라 "
                                 "본 밀도가 계산되는 Generator입니다.\n"
                                 "수동 본 유지 항목은 검증된 최근 XML의 실제 SpeedTree 본 수를 "
                                 "표시하고, 이후 SPM이 바뀌었으면 미재검증으로 구분합니다. "
                                 "검사 결과는 파일이나 실행 캐시에 저장하지 않습니다.\n"
                                 "오래 걸리는 ①②③을 돌리기 전에 먼저 눌러보세요."))
        self.btn_spm = ttk.Button(actions, text="① SPM 본 세팅 (빠름)",
                                  command=lambda: self.start_batch("spm"))
        self.btn_spm.pack(side="left", padx=6)
        Tooltip(self.btn_spm, ("SPM만 수정합니다 (blend/언리얼은 건드리지 않음).\n"
                               "가지 수를 실측해서 '가지당 목표 본 수'에 맞게 본 값을 자동 계산하고, "
                               "머티리얼에 M_ 프리픽스를 붙입니다.\n"
                               "수정 전 _spm_backups\\ 에 백업이 남고, 실패하면 자동 복원됩니다.\n"
                               "느린 ②로 넘어가기 전에 여기서 전부 끝내고 결과를 확인하세요."))
        self.btn_blender = ttk.Button(actions, text="② Blender Repair (느림)",
                                      command=lambda: self.start_batch("blender"))
        self.btn_blender.pack(side="left", padx=6)
        Tooltip(self.btn_blender, ("헤드리스 Blender로 SpeedTree 익스포트→임포트→본/웨이트 수리를 돌리고 "
                                   "SPM 옆에 같은 이름의 .blend와 wind JSON을 저장합니다.\n"
                                   "완료 전에 T_ 6종 또는 보존 Cluster 텍스처, 빈 머티리얼 슬롯까지 검사합니다.\n"
                                   "파일당 수분~수십분. 이미 최신인 blend는 건너뜁니다("
                                   "'완료된 항목도 다시 실행'으로 강제 가능)."))
        self.btn_push = ttk.Button(actions, text="③ Unreal Push",
                                   command=lambda: self.start_batch("push"))
        self.btn_push.pack(side="left", padx=6)
        Tooltip(self.btn_push, ("push 전에 준비 검사를 먼저 전부 통과시킵니다:\n"
                                "· .blend 존재 + SPM보다 최신인지\n"
                                "· 텍스처 세트와 Repair 사전검사(빈 머티리얼 슬롯 포함)\n"
                                "· wind JSON(핸드오프 산출물) 존재\n"
                                "· 언리얼 에디터 실행 여부\n"
                                "준비 안 된 항목은 이유를 표시하고 건너뛴 뒤, 준비된 것만 push합니다."))
        self.btn_all = ttk.Button(
            actions,
            text="🌙 목록 전체 자동 ①→②→③",
            command=self.start_full_pipeline,
        )
        self.btn_all.pack(side="left", padx=(10, 4))
        Tooltip(
            self.btn_all,
            "체크 상태와 무관하게 현재 목록의 모든 항목을 밤새 순서대로 처리합니다.\n"
            "① SPM 본 세팅 전체 완료 → ② Blender Repair 전체 완료 → "
            "③ Unreal Push 순서입니다.\n"
            "개별 ①/②/③ 버튼만 체크된 항목을 대상으로 합니다.\n"
            "개별 실패·수동 처리 항목은 기록하고 나머지 파일은 계속 진행합니다.",
        )
        self.btn_stop = ttk.Button(actions, text="중지", command=self.stop_batch, state="disabled")
        self.btn_stop.pack(side="left", padx=10)
        self.progress_var = tk.StringVar(value="대기")
        ttk.Label(actions, textvariable=self.progress_var).pack(side="left", padx=14)

        meters = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        meters.pack(fill="x")
        ttk.Label(meters, text="전체:").pack(side="left")
        self.batch_progress = ttk.Progressbar(
            meters, mode="determinate", maximum=100, length=220
        )
        self.batch_progress.pack(side="left", padx=(4, 6))
        self.batch_progress_var = tk.StringVar(value="0/0 (0%)")
        ttk.Label(meters, textvariable=self.batch_progress_var, width=15).pack(side="left")
        ttk.Label(
            meters,
            text="단계·경과 시간·수동 전환까지 남은 시간은 각 파일 행에 표시됩니다.",
        ).pack(side="left", padx=(14, 0))

        cols = ("bone_mode", "wind", "spm_status", "blend_status", "push_status", "folder")
        visible_cols = ("bone_mode", "wind", "spm_status", "blend_status", "push_status")
        tablef = ttk.LabelFrame(
            self.root,
            text="파일 목록 (표는 요약 · 행을 선택하면 아래에 전체 내용 표시)",
            padding=4,
        )
        tablef.pack(fill="both", expand=True, padx=6)
        tablef.columnconfigure(0, weight=1)
        tablef.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tablef,
            columns=cols,
            displaycolumns=visible_cols,
            show="tree headings",
            height=12,
        )
        self.tree.heading("#0", text="파일 (첫 클릭=이 행만 활성 · Ctrl+C=SPM 경로)")
        self.tree.column("#0", width=300, minwidth=220, anchor="w")
        headers = {
            "bone_mode": ("본 모드 (▼)", 125),
            "wind": ("Wind (▼)", 110),
            "spm_status": ("① SPM", 205),
            "blend_status": ("② Blender", 245),
            "push_status": ("③ Unreal", 190),
            "folder": ("폴더", 0),
        }
        for key, (label, width) in headers.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        tree_y = ttk.Scrollbar(tablef, orient="vertical", command=self.tree.yview)
        tree_x = ttk.Scrollbar(tablef, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Control-c>", self.copy_selected_paths, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._refresh_selected_detail, add="+")

        detailf = ttk.LabelFrame(
            self.root,
            text="선택 항목 상세 (읽기 전용)",
            padding=4,
        )
        detailf.pack(fill="x", padx=6, pady=(4, 0))
        detailf.columnconfigure(0, weight=1)
        detailf.rowconfigure(0, weight=1)
        self.detail_text = tk.Text(
            detailf,
            height=5,
            wrap="word",
            state="disabled",
            borderwidth=0,
        )
        detail_y = ttk.Scrollbar(
            detailf, orient="vertical", command=self.detail_text.yview
        )
        self.detail_text.configure(yscrollcommand=detail_y.set)
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        detail_y.grid(row=0, column=1, sticky="ns")

        logf = ttk.LabelFrame(self.root, text="로그", padding=4)
        logf.pack(fill="both", padx=6, pady=4)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log_text = tk.Text(logf, height=7, wrap="none", state="disabled")
        log_y = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        log_x = ttk.Scrollbar(logf, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_y.grid(row=0, column=1, sticky="ns")
        log_x.grid(row=1, column=0, sticky="ew")

    def _pick_root(self):
        path = filedialog.askdirectory(initialdir=self.root_var.get())
        if path:
            self.root_var.set(path)

    def log(self, msg):
        self.ui_queue.put(("log", msg))

    def _table_display_value(self, iid, column, value):
        if column not in STATUS_COLUMNS:
            return value
        if not hasattr(self, "_table_full_values"):
            self._table_full_values = {}
        full_value = str(value or "-")
        self._table_full_values[(iid, column)] = full_value
        return compact_table_status(full_value)

    def _set_table_cell(self, iid, column, value):
        self.tree.set(iid, column, self._table_display_value(iid, column, value))
        self._refresh_selected_detail()

    def _refresh_selected_detail(self, _event=None):
        detail = getattr(self, "detail_text", None)
        if detail is None:
            return
        selected = self.tree.selection()
        if not selected or selected[0] not in self.items:
            text = "행을 선택하면 전체 경로와 ①②③ 상태가 여기에 표시됩니다."
        else:
            iid = selected[0]
            statuses = {
                column: getattr(self, "_table_full_values", {}).get(
                    (iid, column), self.tree.set(iid, column) or "-"
                )
                for column in STATUS_COLUMNS
            }
            text = selected_row_detail_text(self.items[iid]["spm"], statuses)
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", text)
        detail.configure(state="disabled")

    def _drain_ui_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    stamp = datetime.now().strftime("%H:%M:%S")
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", f"[{stamp}] {payload}\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "cell":
                    iid, column, value = payload
                    if iid in self.items:
                        self._set_table_cell(iid, column, value)
                elif kind == "progress":
                    self.progress_var.set(payload)
                elif kind == "batch_progress":
                    done, total = payload
                    percent = (done / total * 100.0) if total else 0.0
                    self.batch_progress.configure(value=percent)
                    self.batch_progress_var.set(f"{done}/{total} ({percent:.0f}%)")
                elif kind == "scan_done":
                    generation, result, error = payload
                    if error is not None:
                        self.scan_worker = None
                        self.log(f"스캔 실패: {error}")
                        self.progress_var.set("스캔 실패")
                        self._set_scan_controls(False)
                    else:
                        self.scan(prepared=result, generation=generation)
                elif kind == "done":
                    for btn in (
                        self.btn_check, self.btn_spm, self.btn_blender, self.btn_push,
                        self.btn_all,
                        self.btn_pick_root, self.btn_scan, self.btn_select_all, self.btn_clear_all,
                    ):
                        btn.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_ui_queue)

    # ------------------------------------------------------------------ scan
    @staticmethod
    def _snapshot_spm(task):
        spm, cached_snapshot = task
        try:
            snapshot, cache_hit = cached_file_content_snapshot(
                spm, cached_snapshot
            )
            return str(spm), snapshot, None, cache_hit
        except OSError as exc:
            return str(spm), None, str(exc), False

    @classmethod
    def _collect_scan_result(cls, root, snapshot_caches):
        spms = scan_sk_spms(root)
        snapshots = {}
        errors = []
        cache_hits = 0
        if spms:
            tasks = [
                (spm, snapshot_caches.get(str(spm))) for spm in spms
            ]
            with ThreadPoolExecutor(max_workers=min(8, len(spms))) as pool:
                for iid, snapshot, error, cache_hit in pool.map(
                    cls._snapshot_spm, tasks
                ):
                    if snapshot:
                        snapshots[iid] = snapshot
                        cache_hits += int(cache_hit)
                    elif error:
                        errors.append((iid, error))
        return {
            "spms": spms,
            "snapshots": snapshots,
            "errors": errors,
            "snapshot_cache_hits": cache_hits,
        }

    @staticmethod
    def _migrate_restored_marker_calibration_cache(
        spm, calibration_cache, current_snapshot
    ):
        """Retarget a cache produced from this tool's former cosmetic marker.

        The cleanup receipt must prove that the current SPM is the exact
        pre-marker backup and the cached SPM fingerprint is the exact marked
        recovery backup. Ordinary user edits cannot pass both checks.
        """
        if (
            not isinstance(calibration_cache, dict)
            or calibration_cache.get("version") != CALIBRATION_CACHE_VERSION
            or calibration_cache.get("status") not in {"calibrated", "already-ok"}
            or not isinstance(current_snapshot, dict)
        ):
            return False
        spm = Path(spm)
        receipt_path = (
            spm.parent
            / "reports"
            / f"{spm.stem}_material_problem_node_markers.json"
        )
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_spm = os.path.normcase(
                os.path.abspath(str(receipt.get("spm") or ""))
            )
            if (
                receipt.get("status") != "restored"
                or receipt.get("restore_preserved_original_timestamp") is not True
                or receipt_spm
                != os.path.normcase(os.path.abspath(str(spm)))
            ):
                return False
            restore_source = Path(receipt.get("restore_source") or "")
            recovery_rows = [
                row
                for row in (receipt.get("backups") or [])
                if isinstance(row, dict)
                and row.get("reason") == "before_exact_marker_cleanup_restore"
            ]
            if not restore_source.is_file() or not recovery_rows:
                return False
            original_snapshot = file_content_snapshot(restore_source)
            marked_snapshot = file_content_snapshot(recovery_rows[-1]["path"])
        except (OSError, ValueError, TypeError, KeyError):
            return False
        if (
            original_snapshot.get("fingerprint")
            != current_snapshot.get("fingerprint")
            or marked_snapshot.get("fingerprint")
            != calibration_cache.get("spm_fingerprint")
        ):
            return False
        calibration_cache["spm_fingerprint"] = current_snapshot["fingerprint"]
        calibration_cache["marker_restore_cache_migrated_at"] = (
            datetime.now().isoformat(timespec="seconds")
        )
        return True

    @staticmethod
    def _quick_blend_status_text(spm):
        """Paint the row cheaply; full texture validation follows in worker."""
        blend = blend_path_for(spm)
        if not blend.exists():
            return "생성 필요 — blend 없음 → ② Blender Repair"
        return "검증 중…"

    def _set_scan_controls(self, scanning):
        state = "disabled" if scanning else "normal"
        for name in (
            "btn_check", "btn_spm", "btn_blender", "btn_push", "btn_all",
            "btn_pick_root", "btn_scan", "btn_select_all", "btn_clear_all",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=state)

    def scan(self, prepared=None, generation=None):
        if prepared is None:
            self._scan_generation = getattr(self, "_scan_generation", 0) + 1
            generation = self._scan_generation
            root = self.root_var.get()
            self.cfg = self._collect_cfg()
            self.cfg["root"] = root
            self.spm_calibration_signature = calibration_settings_signature(self.cfg)
            self.legacy_spm_calibration_signature = (
                legacy_calibration_settings_signature(self.cfg)
            )
            save_config(self.cfg)
            with self.state_lock:
                snapshot_caches = {
                    iid: entry.get("spm_snapshot_cache")
                    for iid, entry in self.state.items()
                    if isinstance(entry, dict)
                }

            if hasattr(self, "root") and hasattr(self.root, "after"):
                self._set_scan_controls(True)
                if hasattr(self, "progress_var"):
                    self.progress_var.set("SK SPM 스캔 중…")

                def worker():
                    try:
                        result = self._collect_scan_result(root, snapshot_caches)
                        error = None
                    except Exception as exc:
                        result, error = None, exc
                    self.ui_queue.put((
                        "scan_done", (generation, result, error)
                    ))

                self.scan_worker = threading.Thread(target=worker, daemon=True)
                self.scan_worker.start()
                return
            prepared = self._collect_scan_result(root, snapshot_caches)
        elif generation != getattr(self, "_scan_generation", 0):
            return
        else:
            root = self.root_var.get()
            self.scan_worker = None

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        if not hasattr(self, "_table_full_values"):
            self._table_full_values = {}
        else:
            self._table_full_values.clear()
        spms = prepared["spms"]
        snapshots = prepared["snapshots"]
        for iid, snapshot_error in prepared.get("errors", []):
            self.log(
                f"[경고] SPM 지문 계산 실패: {Path(iid).name}: "
                f"{snapshot_error}"
            )
        for spm in spms:
            iid = str(spm)
            entry = self.state.setdefault(iid, {})
            # The file system is the source of truth.  A saved "완료" label may
            # have become stale after the SPM was edited since the last run.
            entry["blend_status"] = self._quick_blend_status_text(spm)
            if snapshots.get(iid):
                entry["spm_snapshot_cache"] = snapshots[iid]
                calibration_cache = entry.get("calibration_cache")
                self._migrate_restored_marker_calibration_cache(
                    spm, calibration_cache, snapshots[iid]
                )
                if (
                    isinstance(calibration_cache, dict)
                    and calibration_cache.get("settings_signature")
                    == getattr(self, "legacy_spm_calibration_signature", None)
                    and calibration_cache_matches(
                        calibration_cache,
                        snapshots[iid]["fingerprint"],
                        self.spm_calibration_signature,
                        getattr(
                            self, "legacy_spm_calibration_signature", None
                        ),
                    )
                ):
                    # Migrate a still-current legacy stat-bearing signature at
                    # scan time.  From now on a touch-only INI/audit timestamp
                    # change cannot schedule this SPM again.
                    calibration_cache["settings_signature"] = (
                        self.spm_calibration_signature
                    )
            wind_override = entry.get("wind_override", "auto")
            if wind_override not in {value for _label, value in WIND_OPTIONS}:
                wind_override = "auto"
                entry["wind_override"] = "auto"
            manual_bones_locked = is_manual_bones_locked(spm, entry)
            if manual_bones_locked:
                entry["manual_bones_locked"] = True
                self.state[iid] = entry
                marker = manual_bones_marker_path(spm)
                if not marker.exists():
                    try:
                        set_manual_bones_marker(spm, True)
                    except OSError as exc:
                        self.log(f"[경고] 수동 본 marker 저장 실패: {spm.name}: {exc}")
            self.items[iid] = {
                "spm": spm,
                "checked": True,
                "wind_override": wind_override,
                "bone_mode": "manual" if manual_bones_locked else "auto",
                "manual_bones_locked": manual_bones_locked,
                "spm_snapshot": snapshots.get(iid),
                "live_texture_paths": (),
                "live_status_signature": None,
            }
            spm_status = (
                manual_bone_status_text(spm)
                if manual_bones_locked
                else entry.get("spm_status", "-")
            )
            blend_status = entry.get("blend_status", "-")
            push_status = self._current_push_status_text(iid, spm)
            self.tree.insert(
                "", "end", iid=iid,
                text=self._item_label(iid),
                values=(
                    self._bone_mode_label(iid),
                    self._wind_label(iid),
                    self._table_display_value(iid, "spm_status", spm_status),
                    self._table_display_value(iid, "blend_status", blend_status),
                    self._table_display_value(iid, "push_status", push_status),
                    str(spm.parent),
                ),
            )
        self.checked_rows.sync_after_reload()
        save_state(self.state)
        cache_count = sum(
            1
            for iid, item in self.items.items()
            if item.get("spm_snapshot")
            and calibration_cache_matches(
                self.state.get(iid, {}).get("calibration_cache"),
                item["spm_snapshot"]["fingerprint"],
                self.spm_calibration_signature,
                getattr(self, "legacy_spm_calibration_signature", None),
            )
        )
        self.log(
            f"스캔 완료: SK SPM {len(spms)}개 · 본 세팅 캐시 {cache_count}개. "
            "먼저 [🔍 검사]로 상태를 확인해보세요."
        )

        if hasattr(self, "progress_var"):
            self.progress_var.set(
                f"스캔 완료 · SPM {len(spms)}개 · "
                f"지문 재사용 {prepared.get('snapshot_cache_hits', 0)}개"
            )
        self._set_scan_controls(False)
        if hasattr(self, "root") and hasattr(self.root, "after"):
            self._schedule_live_status_poll(50)

    @staticmethod
    def _reported_texture_paths(spm):
        report = (
            Path(spm).parent / "reports" /
            f"{Path(spm).stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ()
        paths = []
        for material in (data.get("texture_normalization") or {}).get(
            "materials", []
        ):
            files = (
                material.get("files")
                or material.get("preserved_files")
                or material.get("source_maps")
                or {}
            )
            for value in files.values():
                if value:
                    paths.append(str(Path(value)))
        return tuple(sorted(set(paths), key=os.path.normcase))

    @staticmethod
    def _live_status_signature(spm, texture_paths=()):
        """Cheap cross-tool change detector for PCG -> SK handoff files."""
        spm = Path(spm)
        blend = blend_path_for(spm)
        report = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        stmat = speedtree_stmat_path(spm)

        def stat_key(path):
            try:
                stat = Path(path).stat()
                return stat.st_size, stat.st_mtime_ns
            except OSError:
                return None

        texture_stats = tuple(
            (os.path.normcase(str(path)), stat_key(path))
            for path in texture_paths
        )
        return (
            stat_key(spm), stat_key(blend), stat_key(report), stat_key(stmat),
            texture_stats,
        )

    def _schedule_live_status_poll(self, delay_ms=10000):
        try:
            pending = getattr(self, "_live_poll_after_id", None)
            if pending is not None:
                self.root.after_cancel(pending)
            self._live_poll_after_id = self.root.after(
                delay_ms, self._poll_live_file_statuses
            )
        except (AttributeError, tk.TclError):
            self._live_poll_after_id = None

    def _poll_live_file_statuses(self):
        """Start a non-blocking cross-tool status refresh."""
        self._live_poll_after_id = None
        try:
            if self._live_poll_active:
                return
            generation = self._scan_generation
            snapshot = [
                (
                    iid,
                    item["spm"],
                    tuple(item.get("live_texture_paths") or ()),
                    item.get("live_status_signature"),
                )
                for iid, item in list(self.items.items())
            ]
            self._live_poll_active = True
            threading.Thread(
                target=self._poll_live_file_status_worker,
                args=(generation, snapshot),
                daemon=True,
            ).start()
        finally:
            self._schedule_live_status_poll(10000)

    def _poll_live_file_status_worker(self, generation, snapshot):
        changed = False
        try:
            def inspect_row(row):
                iid, spm, texture_paths, previous_signature = row
                signature = self._live_status_signature(spm, texture_paths)
                if signature == previous_signature:
                    return None
                # A changed report can point at a new set of T_/Cluster files.
                texture_paths = self._reported_texture_paths(spm)
                signature = self._live_status_signature(spm, texture_paths)
                status = self._blend_status_text(spm)
                push_status = self._current_push_status_text(iid, spm)
                return iid, texture_paths, signature, status, push_status

            rows = []
            if snapshot:
                # Four workers keep the large embedded-geometry SPMs bounded
                # in memory while gzip/regex parsing proceeds in parallel.
                with ThreadPoolExecutor(max_workers=min(4, len(snapshot))) as pool:
                    rows = [row for row in pool.map(inspect_row, snapshot) if row]
            for iid, texture_paths, signature, status, push_status in rows:
                with self.state_lock:
                    if generation != self._scan_generation or iid not in self.items:
                        continue
                    item = self.items[iid]
                    item["live_texture_paths"] = texture_paths
                    item["live_status_signature"] = signature
                    self.state.setdefault(iid, {})["blend_status"] = status
                self.ui_queue.put(("cell", (iid, "blend_status", status)))
                self.ui_queue.put(("cell", (iid, "push_status", push_status)))
                changed = True
            if changed:
                with self.state_lock:
                    if generation == self._scan_generation:
                        save_state(self.state)
                try:
                    save_leaf_contract_cache()
                except OSError as exc:
                    self.log(f"[캐시 경고] leaf 상태 캐시 저장 실패: {exc}")
        finally:
            self._live_poll_active = False

    def _item_label(self, iid):
        item = self.items[iid]
        mark = CHECK_ON if item["checked"] else CHECK_OFF
        lock = "🔒 " if item.get("manual_bones_locked", False) else ""
        return f"{mark} {lock}{item['spm'].name}"

    def _redraw_checked_row(self, iid, _item):
        self.tree.item(iid, text=self._item_label(iid))

    def _bone_mode_label(self, iid):
        if self.items[iid].get("manual_bones_locked", False):
            return "수동 본 유지 🔒  ▼"
        return "자동 계산  ▼"

    def _wind_label(self, iid):
        item = self.items[iid]
        auto = wind_preset_for(item["spm"].stem)
        if item["wind_override"] == "auto":
            return f"{auto} (자동)  ▼"
        return f"{item['wind_override']} (수동)  ▼"

    def _close_cell_editor(self):
        if self.cell_editor is not None:
            try:
                self.cell_editor.destroy()
            except tk.TclError:
                pass
            self.cell_editor = None

    def _open_cell_dropdown(self, iid, column, options, current_value, callback):
        self._close_cell_editor()
        bbox = self.tree.bbox(iid, column)
        if not bbox:
            return
        x, y, width, height = bbox
        labels = [label for label, _value in options]
        value_by_label = {label: value for label, value in options}
        label_by_value = {value: label for label, value in options}
        editor = ttk.Combobox(self.tree, state="readonly", values=labels)
        editor.set(label_by_value.get(current_value, labels[0]))
        editor.place(x=x, y=y, width=width, height=height)
        self.cell_editor = editor

        def commit(_event=None):
            value = value_by_label.get(editor.get())
            self._close_cell_editor()
            if value is not None:
                callback(value)

        editor.bind("<<ComboboxSelected>>", commit)
        editor.bind("<Escape>", lambda _event: self._close_cell_editor())
        editor.focus_set()

        def post_dropdown():
            if self.cell_editor is editor and editor.winfo_exists():
                editor.tk.call("ttk::combobox::Post", editor._w)

        self.root.after_idle(post_dropdown)

    def _on_click(self, event):
        if self.worker and self.worker.is_alive():
            return "break"
        self._close_cell_editor()
        region = self.tree.identify_region(event.x, event.y)
        iid = self.tree.identify_row(event.y)
        if iid not in self.items:
            return
        if region == "tree":
            # Returning "break" suppresses Treeview's native selection, so
            # select explicitly.  Ctrl+C must copy the row the user just hit,
            # not every checked execution target or a stale prior selection.
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.focus_set()
            self.checked_rows.click(iid)
            return "break"
        if region != "cell":
            return
        column = self.tree.identify_column(event.x)
        if column == "#1":
            current = self.items[iid].get("bone_mode", "auto")
            self.root.after_idle(
                lambda: self._open_cell_dropdown(
                    iid,
                    "bone_mode",
                    BONE_MODE_OPTIONS,
                    current,
                    lambda value: self._set_bone_mode(iid, value),
                )
            )
            return "break"
        if column == "#2":
            current = self.items[iid].get("wind_override", "auto")
            self.root.after_idle(
                lambda: self._open_cell_dropdown(
                    iid,
                    "wind",
                    WIND_OPTIONS,
                    current,
                    lambda value: self._set_wind_override(iid, value),
                )
            )
            return "break"

    def _set_all(self, checked):
        if self.worker and self.worker.is_alive():
            return
        self.checked_rows.set_all(checked)

    def copy_selected_paths(self, _event=None):
        count = copy_selected_row_paths(
            self.root,
            self.tree,
            self.items,
            lambda item: item.get("spm"),
        )
        if count:
            self.progress_var.set(f"SPM 경로 복사 완료 · {count}개")
        else:
            self.progress_var.set("복사할 SPM 행을 먼저 클릭하세요")
        return "break"

    def _set_bone_mode(self, iid, mode):
        if iid not in self.items or mode not in {"auto", "manual"}:
            return
        item = self.items[iid]
        locked = mode == "manual"
        marker = set_manual_bones_marker(item["spm"], locked)
        item["bone_mode"] = mode
        item["manual_bones_locked"] = locked
        entry = self.state.setdefault(iid, {})
        if locked:
            entry["manual_bones_locked"] = True
            entry["spm_status"] = manual_bone_status_text(item["spm"])
        else:
            entry.pop("manual_bones_locked", None)
            entry["spm_status"] = "자동 계산 대상"
            entry.pop("spm_summary", None)
        self.tree.item(iid, text=self._item_label(iid))
        self.tree.set(iid, "bone_mode", self._bone_mode_label(iid))
        self._set_table_cell(iid, "spm_status", entry["spm_status"])
        save_state(self.state)
        self.log(
            f"{'수동 본 유지 설정' if locked else '자동 계산 복귀'}: "
            f"{item['spm'].name} ({marker})"
        )

    def _set_wind_override(self, iid, value):
        valid_values = {option_value for _label, option_value in WIND_OPTIONS}
        if iid not in self.items or value not in valid_values:
            return
        item = self.items[iid]
        item["wind_override"] = value
        self.state.setdefault(iid, {})["wind_override"] = value
        save_state(self.state)
        self.tree.set(iid, "wind", self._wind_label(iid))
        self.log(f"Wind 설정: {item['spm'].name} → {self._wind_label(iid).replace('  ▼', '')}")

    # ------------------------------------------------------------------ batch
    def _collect_cfg(self):
        cfg = dict(self.cfg)
        try:
            cfg["priority"] = self.priority_var.get()
            cfg["cpu_cores"] = int(self.cores_var.get())
            cfg["target_bones_per_branch"] = float(self.target_var.get())
            cfg["max_total_bones"] = int(self.maxtotal_var.get())
            cfg["spm_parallel_jobs"] = max(1, int(self.parallel_var.get()))
            cfg["blender_parallel_jobs"] = max(
                1, min(4, int(self.blender_parallel_var.get()))
            )
            transport = self.transport_var.get()
            cfg["push_transport"] = transport if transport in {"rpc", "headless"} else "rpc"
            cfg["night_headless"] = bool(self.night_headless_var.get())
        except (AttributeError, tk.TclError):
            pass
        return cfg

    @staticmethod
    def _snapshot_still_current(spm, snapshot):
        if not snapshot:
            return False
        try:
            stat = Path(spm).stat()
        except OSError:
            return False
        return (
            snapshot.get("size") == stat.st_size
            and snapshot.get("mtime_ns") == stat.st_mtime_ns
        )

    def _current_spm_snapshot(self, item):
        snapshot = item.get("spm_snapshot")
        if not self._snapshot_still_current(item["spm"], snapshot):
            snapshot = file_content_snapshot(item["spm"])
            item["spm_snapshot"] = snapshot
            with self.state_lock:
                self.state.setdefault(str(item["spm"]), {})[
                    "spm_snapshot_cache"
                ] = snapshot
        return snapshot

    def _spm_cache_matches(self, item):
        snapshot = self._current_spm_snapshot(item)
        entry = self.state.get(str(item["spm"]), {})
        return calibration_cache_matches(
            entry.get("calibration_cache"),
            snapshot["fingerprint"],
            self.spm_calibration_signature,
            getattr(self, "legacy_spm_calibration_signature", None),
        )

    def _spm_schedule_key(self, item):
        if item.get("manual_bones_locked", False):
            return (0, 0.0, item["spm"].name.lower())
        if not self.force_rerun and self._spm_cache_matches(item):
            return (1, 0.0, item["spm"].name.lower())
        entry = self.state.get(str(item["spm"]), {})
        duration = float(entry.get("spm_last_duration_seconds", 0.0) or 0.0)
        # Longest predicted jobs start first after all zero-cost work has been
        # resolved, which avoids a single slow tree becoming the final tail.
        return (2, -duration, item["spm"].name.lower())

    def start_batch(self, phase):
        self._close_cell_editor()
        targets = [item for item in self.items.values() if item["checked"]]
        if not targets:
            messagebox.showinfo("SK Batch", "선택된 항목이 없습니다.")
            return
        self.cfg = self._collect_cfg()
        self.spm_calibration_signature = calibration_settings_signature(self.cfg)
        self.legacy_spm_calibration_signature = (
            legacy_calibration_settings_signature(self.cfg)
        )
        if phase != "check":
            save_config(self.cfg)
        self.active_push_transport = self.cfg.get("push_transport", "rpc")
        # snapshot tk vars on the main thread; the worker must not touch them
        self.force_rerun = bool(self.force_var.get())
        self.stop_flag.clear()
        self.batch_progress.configure(value=0)
        self.batch_progress_var.set(f"0/{len(targets)} (0%)")
        for btn in (
            self.btn_check, self.btn_spm, self.btn_blender, self.btn_push,
            self.btn_all,
            self.btn_pick_root, self.btn_scan, self.btn_select_all, self.btn_clear_all,
        ):
            btn.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.worker = threading.Thread(target=self._run_batch, args=(phase, targets), daemon=True)
        self.worker.start()

    def start_full_pipeline(self):
        self._close_cell_editor()
        targets = list(self.items.values())
        if not targets:
            messagebox.showinfo("SK Batch", "현재 목록에 항목이 없습니다.")
            return
        self.cfg = self._collect_cfg()
        self.spm_calibration_signature = calibration_settings_signature(self.cfg)
        self.legacy_spm_calibration_signature = (
            legacy_calibration_settings_signature(self.cfg)
        )
        save_config(self.cfg)
        self.active_push_transport = (
            "headless"
            if self.cfg.get("night_headless", True)
            else self.cfg.get("push_transport", "rpc")
        )
        self.force_rerun = bool(self.force_var.get())
        self.stop_flag.clear()
        self.batch_progress.configure(value=0)
        self.batch_progress_var.set(f"0/{len(targets)} (0%)")
        for btn in (
            self.btn_check, self.btn_spm, self.btn_blender, self.btn_push,
            self.btn_all,
            self.btn_pick_root, self.btn_scan, self.btn_select_all, self.btn_clear_all,
        ):
            btn.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.log(
            f"🌙 목록 전체 자동 시작: {len(targets)}개 · "
            "① SPM → ② Blender → ③ Unreal"
        )
        self.worker = threading.Thread(
            target=self._run_full_pipeline,
            args=(targets,),
            daemon=True,
        )
        self.worker.start()

    def _run_full_pipeline(self, targets):
        phases = (
            ("spm", "① SPM 본 세팅"),
            ("blender", "② Blender Repair"),
            ("push", "③ Unreal Push"),
        )
        pipeline_abort = None
        eligible = list(targets)
        failed_items = set()
        for phase, label in phases:
            if self.stop_flag.is_set() or not eligible:
                break
            self.log(f"🌙 {label} 시작")
            phase_ok = self._run_batch(phase, eligible, emit_done=False)
            phase_failed = set(getattr(self, "_phase_failed_items", set()))
            if phase_failed:
                failed_items.update(phase_failed)
                eligible = [
                    item for item in eligible
                    if str(item["spm"]) not in phase_failed
                ]
                self.log(
                    f"🌙 {label}: 실패/준비 제외 {len(phase_failed)}개는 "
                    "다음 단계로 넘기지 않습니다."
                )
            self.log(f"🌙 {label} 종료")
            if not phase_ok:
                pipeline_abort = getattr(self, "_phase_abort_reason", None)
                break
        if self.stop_flag.is_set():
            final_text = "중지됨"
        elif pipeline_abort:
            final_text = f"전체 자동 중단 — {pipeline_abort}"
        elif failed_items:
            final_text = (
                f"전체 자동 종료 — 성공 {len(eligible)}개 · "
                f"실패/준비 제외 {len(failed_items)}개"
            )
        else:
            final_text = "전체 자동 완료"
        self.ui_queue.put(("progress", final_text))
        self.ui_queue.put(("done", None))
        self.log(f"🌙 {final_text}")

    def stop_batch(self):
        self.stop_flag.set()
        # Worker polling performs the tree kill. Keeping it in one place avoids
        # racing a direct parent-only kill that would orphan SpeedTree children.
        self.log("중지 요청됨 — 실행 중인 작업과 SpeedTree 자식을 종료합니다.")

    def _record_phase_status(
        self, iid, column, status_text, kind, reason, details=None, persist=True
    ):
        """Write the same structured item outcome to GUI and persistent state."""
        self.ui_queue.put(("cell", (iid, column, status_text)))
        with self.state_lock:
            state_entry = self.state.setdefault(iid, {})
            state_entry[column] = status_text
            state_entry[f"{column}_kind"] = kind
            error_entry = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "kind": kind,
                "message": reason,
            }
            if details:
                error_entry.update(details)
            state_entry[f"{column}_error"] = error_entry
            if persist:
                save_state(self.state)

    @staticmethod
    def _failure_status_text(reason, kind):
        if kind == "manual_required":
            return reason
        if kind in PUSH_ABORT_KINDS:
            return f"중단: {reason}"
        return f"실패: {reason}"

    def _run_batch(self, phase, targets, emit_done=True):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._phase_abort_reason = None
        self._phase_failed_items = set()
        requested_targets = list(targets)
        preflight_skipped = set()
        if phase == "spm":
            targets = sorted(targets, key=self._spm_schedule_key)
        if phase == "push":
            requested_ids = {str(item["spm"]) for item in requested_targets}
            targets, preflight_abort = self._push_preflight(targets)
            ready_ids = {str(item["spm"]) for item in targets}
            preflight_skipped = requested_ids - ready_ids
            self._phase_failed_items.update(preflight_skipped)
            if preflight_abort:
                self._phase_failed_items.update(requested_ids)
                self._phase_abort_reason = preflight_abort
                if emit_done:
                    self.ui_queue.put(("progress", f"Unreal Push 중단 — {preflight_abort}"))
                    self.ui_queue.put(("done", None))
                return False
            if not targets:
                excluded = len(preflight_skipped)
                reason = (
                    f"준비 검사 통과 항목 없음 · {excluded}개 제외"
                    if excluded else "준비 검사 통과 항목 없음"
                )
                self._phase_abort_reason = reason
                self.log(f"Unreal Push 중단 — {reason}. 표의 준비 검사 결과를 확인하세요.")
                if emit_done:
                    self.ui_queue.put(("progress", f"Unreal Push 중단 — {reason}"))
                    self.ui_queue.put(("done", None))
                return False
        titles = {"check": "검사", "spm": "SPM 본 세팅", "blender": "Blender Repair", "push": "Unreal Push"}
        column_by_phase = {"check": "spm_status", "spm": "spm_status",
                           "blender": "blend_status", "push": "push_status"}
        if (
            phase == "push"
            and getattr(self, "active_push_transport", "rpc") == "headless"
        ):
            return self._run_headless_push_batch(targets, emit_done=emit_done)
        title = titles[phase]
        column = column_by_phase[phase]
        total = len(targets)
        self.ui_queue.put(("batch_progress", (0, total)))
        self._batch_done = 0
        self._batch_active = 0
        phase_abort = threading.Event()
        attempted = set()
        failed_items = set(preflight_skipped)

        def run_one(item):
            if self.stop_flag.is_set():
                return
            spm = item["spm"]
            iid = str(spm)
            with self.state_lock:
                attempted.add(iid)
                self._batch_active += 1
                active = self._batch_active
                done = self._batch_done
            self.ui_queue.put(
                ("progress", f"{title} {done}/{total} · 실행 중 {active}개")
            )
            try:
                if phase == "check":
                    self._job_check(iid, spm)
                elif phase == "spm":
                    self._job_spm(iid, spm)
                elif phase == "blender":
                    self._job_blender(iid, spm, item)
                else:
                    self._job_push(iid, spm)
            except Exception as exc:
                reason = compact_error_message(exc)
                kind = getattr(exc, "kind", "data_error")
                if phase == "push" and kind == "data_error" and "시간 초과" in reason:
                    kind = "push_timeout"
                    reason = "Push 작업 시간 초과 — Unreal/RPC 상태 확인 필요"
                status_text = self._failure_status_text(reason, kind)
                tag = {
                    "manual_required": "수동",
                    "unreal_crash": "Unreal 중단",
                    "unreal_unavailable": "Unreal 중단",
                    "rpc_timeout": "RPC 중단",
                    "push_timeout": "Push 중단",
                }.get(kind, "실패")
                self.log(f"[{tag}] {spm.name}: {reason}")
                details = {}
                if getattr(exc, "log_file", None):
                    details["log"] = str(exc.log_file)
                if getattr(exc, "report_file", None):
                    details["report"] = str(exc.report_file)
                self._record_phase_status(
                    iid,
                    column,
                    status_text,
                    kind,
                    reason,
                    details=details,
                    persist=phase != "check",
                )
                with self.state_lock:
                    failed_items.add(iid)
                if phase == "push" and kind in PUSH_ABORT_KINDS:
                    self._phase_abort_reason = reason
                    phase_abort.set()
                    self.log(
                        "[Push 단계 중단] Unreal/RPC 상태가 안전하지 않아 남은 항목을 실행하지 않습니다."
                    )
            finally:
                with self.state_lock:
                    self._batch_active -= 1
                    self._batch_done += 1
                    done = self._batch_done
                    active = self._batch_active
                self.ui_queue.put(("batch_progress", (done, total)))
                self.ui_queue.put(
                    ("progress", f"{title} {done}/{total} · 실행 중 {active}개")
                )

        # Independent SPM and Blender jobs can overlap. RPC Push stays serial;
        # headless Push parallelizes only its Blender export stage below.
        if phase == "spm":
            workers = self.cfg.get("spm_parallel_jobs", 1)
        elif phase == "check":
            workers = self.cfg.get("check_parallel_jobs", 8)
        elif phase == "blender":
            workers = self.cfg.get("blender_parallel_jobs", 2)
        else:
            workers = 1
        workers = max(1, min(int(workers), total))
        if workers > 1:
            self.log(f"{title}: {workers}개 동시 실행")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pool.map(run_one, targets)
        else:
            for item in targets:
                if self.stop_flag.is_set() or phase_abort.is_set():
                    break
                run_one(item)

        if phase_abort.is_set():
            deferred_reason = f"Unreal/RPC 중단으로 미실행 — {self._phase_abort_reason}"
            for item in targets:
                iid = str(item["spm"])
                if iid in attempted:
                    continue
                self._record_phase_status(
                    iid,
                    column,
                    deferred_reason,
                    "not_run_unreal",
                    deferred_reason,
                    persist=False,
                )
                self.log(f"[미실행] {item['spm'].name}: {deferred_reason}")

        with self.state_lock:
            self._phase_failed_items = set(failed_items)
            if phase != "check":
                save_state(self.state)
        if emit_done:
            progress = (
                f"{title} 중단 — {self._phase_abort_reason}"
                if phase_abort.is_set()
                else "대기"
            )
            self.ui_queue.put(("progress", progress))
            self.ui_queue.put(("done", None))
        self.log(f"{title} 배치 종료.")
        return not phase_abort.is_set()

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def _latest_log_line(log_file, max_bytes=8192):
        try:
            with Path(log_file).open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - max_bytes))
                text = handle.read().decode("utf-8", errors="replace")
            return next(
                (line.strip() for line in reversed(text.splitlines()) if line.strip()),
                "",
            )
        except OSError:
            return ""

    def _run_limited(
        self, cmd, log_name, timeout, affinity=True, progress_callback=None, env=None
    ):
        log_file = LOG_DIR / log_name
        proc = launch_limited(
            cmd,
            self.cfg,
            log_file=str(log_file),
            affinity=affinity,
            env=env,
        )
        with self.procs_lock:
            self.active_procs.add(proc)
        try:
            started = time.monotonic()
            deadline = time.time() + timeout
            next_progress = 0.0
            while proc.poll() is None:
                if self.stop_flag.is_set():
                    tree_stopped = terminate_process_tree(proc)
                    detail = "" if tree_stopped else " (자식 프로세스 종료 확인 실패)"
                    raise RuntimeError("사용자 중지" + detail)
                if time.time() > deadline:
                    tree_stopped = terminate_process_tree(proc)
                    detail = "" if tree_stopped else " — 자식 프로세스 종료 확인 실패"
                    timeout_error = RuntimeError(
                        f"시간 초과({timeout}s){detail} — 로그: {log_file}"
                    )
                    timeout_error.log_file = log_file
                    raise timeout_error
                now = time.monotonic()
                if progress_callback is not None and now >= next_progress:
                    progress_callback(
                        now - started,
                        self._latest_log_line(log_file),
                    )
                    next_progress = now + 1.0
                interval = float(self.cfg.get("process_poll_interval", 0.2))
                time.sleep(max(0.05, min(interval, 1.0)))
        finally:
            # A progress callback, log reader, or UI shutdown can raise while
            # the child is still alive.  Leaving that Blender/SpeedTree tree
            # behind makes every later batch slower and can retain gigabytes
            # of working memory, so every exit path owns its child cleanup.
            if proc.poll() is None:
                terminate_process_tree(proc)
            # Always close the Job, even when the direct parent has already
            # exited.  That is the path where a crashed Blender/Python wrapper
            # can otherwise leave a multi-GB SpeedTree descendant behind.
            close_process_kill_job(proc)
            with self.procs_lock:
                self.active_procs.discard(proc)
            handle = getattr(proc, "sk_log_handle", None)
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass
        return proc.returncode, log_file

    @staticmethod
    def _repair_contract_current(spm):
        """Prove the saved blend/report came from the current SPM content."""
        report_path = (
            Path(spm).parent / "reports" /
            f"{Path(spm).stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (report.get("handoff_preflight") or {}).get("status") != "ok":
                return False
            envelope = report.get("speedtree_pipeline_contract")
            if not isinstance(envelope, dict):
                return False
            validate_preflight_envelope(envelope, spm, require_ok=True)
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    def _handoff_ready(self, spm):
        """핸드오프 산출물(push에 필요한 것들)이 준비됐는지 → (ok, 설명)."""
        leaf_ok, leaf_reason = self._leaf_reference_ready(spm)
        if not leaf_ok:
            return False, leaf_reason
        blend = blend_path_for(spm)
        if not blend.exists():
            return False, "생성 필요 — blend 없음 → ② Blender Repair"
        receipt_current = self._repair_contract_current(spm)
        if blend.stat().st_mtime < spm.stat().st_mtime and not receipt_current:
            return False, "Blender 갱신 필요 — SPM이 더 최근에 수정됨 → ② Blender Repair"
        material_ok, material_reason = self._material_export_ready(
            spm, content_receipt_current=receipt_current
        )
        if not material_ok:
            return False, material_reason
        wind_json = blend.parent / "JSON" / f"{spm.stem}_dynamic_wind_import_from_megaplant_groups.json"
        if not wind_json.exists():
            return False, "wind JSON 없음 → ② 필요"
        texture_ok, texture_reason = self._texture_normalization_ready(
            spm, content_receipt_current=receipt_current
        )
        if not texture_ok:
            return False, texture_reason
        return True, "준비됨 ✓"

    @staticmethod
    def _leaf_reference_ready(spm):
        contract = inspect_spm_leaf_contract(spm)
        return leaf_contract_user_message(contract)

    @staticmethod
    def _material_export_ready(spm, content_receipt_current=False):
        contract = inspect_spm_leaf_contract(spm)
        exported = inspect_speedtree_material_export(spm, contract)
        all_exported = inspect_all_speedtree_material_export(spm)
        if all_exported.get("status") not in {"ok", "not_applicable"}:
            exported = all_exported
        status = exported.get("status")
        if status in {"ok", "not_applicable"} or (
            status == "stale" and content_receipt_current
        ):
            return True, "SpeedTree 재질 export 정상"
        missing = list(exported.get("missing_materials") or [])
        if status == "missing_stmat":
            return False, "SpeedTree .stmat 없음 → ② Blender Repair"
        if status == "invalid_stmat":
            return False, (
                "SpeedTree .stmat 파싱 실패 — "
                + str(exported.get("error") or "파일 확인")
            )
        if status == "stale":
            return False, "SPM 변경 후 SpeedTree .stmat이 오래됨 → ② 다시 실행"
        if missing:
            return False, (
                "현재 내보내는 노드의 재질이 SpeedTree FBX에서 빠짐 — "
                + ", ".join(missing)
                + " → 표시된 SpeedTree 노드의 재질/텍스처 확인"
            )
        return False, "SpeedTree 재질 export 사전검사 실패 → ② 확인"

    @staticmethod
    def _texture_normalization_ready(spm, content_receipt_current=None):
        report_path = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        if not report_path.is_file():
            return False, "텍스처 정규화 정보 없음 → ② 필요"
        if content_receipt_current is None:
            content_receipt_current = App._repair_contract_current(spm)
        try:
            if (
                report_path.stat().st_mtime_ns < Path(spm).stat().st_mtime_ns
                and not content_receipt_current
            ):
                return False, "SPM 변경 후 Repair 보고서가 오래됨 → ② 다시 실행"
        except OSError as exc:
            return False, f"텍스처 보고서 시간 확인 실패: {exc}"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return False, f"텍스처 정규화 보고서 오류: {exc}"
        envelope = report.get("speedtree_pipeline_contract")
        if envelope is None:
            return False, (
                "공통 SpeedTree 계약 정보 없음 → ② Blender Repair 다시 실행"
            )
        if envelope is not None:
            if not isinstance(envelope, dict):
                return False, "공통 SpeedTree 계약 형식 오류 → ② 다시 실행"
            try:
                validate_preflight_envelope(envelope, spm, require_ok=True)
            except (OSError, ValueError, RuntimeError) as exc:
                return False, (
                    "SpeedTree 계약이 현재 SPM/STMAT과 맞지 않음 → "
                    f"② Blender Repair 다시 실행 ({exc})"
                )
        normalization = report.get("texture_normalization") or {}
        missing = list(normalization.get("missing", []))
        def recorded_file_missing(value):
            try:
                candidate = Path(value) if value else None
                return not candidate or not candidate.is_file() or candidate.stat().st_size <= 0
            except OSError:
                return True

        # A report can outlive one of its local T_ files. Re-check recorded
        # paths cheaply so deletion/OneDrive placeholders cannot pass on a
        # stale "ok" label. Preserved Cluster rows carry their own source-file
        # map and are validated the same way.
        for material in normalization.get("materials", []):
            if material.get("status") not in {"ok", "preserved_cluster"}:
                continue
            files = (
                material.get("files")
                or material.get("preserved_files")
                or material.get("source_maps")
                or {}
            )
            missing_roles = [
                role for role, value in files.items()
                if recorded_file_missing(value)
            ]
            if missing_roles:
                missing.append(
                    {
                        "material": material.get("material", "?"),
                        "expected_texture_base": (
                            material.get("texture_base")
                            or material.get("expected_texture_base")
                            or "보존 Cluster"
                        ),
                        "missing_roles": missing_roles,
                    }
                )
        if missing:
            details = []
            for item in missing:
                roles = ",".join(item.get("missing_roles", [])) or "대응 세트"
                details.append(
                    f"{item.get('material', '?')}→"
                    f"{item.get('expected_texture_base', 'T_?')}[{roles}]"
                )
            return False, (
                "텍스처 준비 안 됨: " + " | ".join(details)
                + " → PCG ③ 또는 ② Repair 확인"
            )
        if normalization.get("status") not in {"ok", "preserved_cluster"}:
            return False, "텍스처 정규화 미완료 → ② 필요"
        handoff = report.get("handoff_preflight")
        if not isinstance(handoff, dict):
            return False, "② 사전검사 정보 없음 → ② Blender Repair 다시 실행"
        if handoff.get("status") != "ok":
            slots = handoff.get("empty_material_slots") or []
            outputs = handoff.get("missing_outputs") or []
            materials = (
                handoff.get("missing_materials")
                or (handoff.get("material_export") or {}).get("missing_materials")
                or []
            )
            vertex_contract = handoff.get("vertex_color_contract") or {}
            payload_contract = handoff.get("vertex_payload_contract") or {}
            reasons = []
            if slots:
                reasons.append(
                    "머티리얼 빈 슬롯 "
                    + ", ".join(
                        f"{item.get('object', '?')}[{item.get('slot', '?')}]"
                        for item in slots
                    )
                )
            if outputs:
                reasons.append("핸드오프 파일 누락 " + ", ".join(map(str, outputs)))
            if materials:
                reasons.append("SpeedTree export 재질 누락 " + ", ".join(materials))
            if vertex_contract.get("status") == "blocked":
                reasons.append("버텍스 컬러 검사 실패")
            if payload_contract.get("status") == "blocked":
                reasons.append("AO/Nanite UV payload 검사 실패")
            return False, "② 사전검사 차단: " + (" | ".join(reasons) or "보고서 확인")
        preserved_count = sum(
            1 for item in normalization.get("materials", [])
            if item.get("status") == "preserved_cluster"
        )
        if preserved_count:
            return True, f"텍스처 준비 완료 · 보존 Cluster {preserved_count}세트"
        return True, "텍스처 정규화 완료"

    def _blend_status_text(self, spm):
        """Return the live SK handoff status; never trust a saved UI label."""
        leaf_ok, leaf_reason = self._leaf_reference_ready(spm)
        if not leaf_ok:
            return leaf_reason
        blend = blend_path_for(spm)
        receipt_current = self._repair_contract_current(spm)
        try:
            if not blend.is_file():
                return "생성 필요 — blend 없음 · ② Blender Repair 실행"
            if (
                blend.stat().st_mtime_ns < Path(spm).stat().st_mtime_ns
                and not receipt_current
            ):
                return "Blender 갱신 필요 — SPM이 더 최근에 수정됨 · ② 다시 실행"
        except OSError as exc:
            return f"확인 실패 — {exc}"
        material_ok, material_reason = self._material_export_ready(
            spm, content_receipt_current=receipt_current
        )
        if not material_ok:
            return f"Repair 필요 — {material_reason}"
        texture_ok, texture_reason = self._texture_normalization_ready(
            spm, content_receipt_current=receipt_current
        )
        if not texture_ok:
            return f"Repair 필요 — {texture_reason}"
        if "보존 Cluster" in texture_reason:
            return "최신 ✓ · 보존 Cluster ✓"
        return "최신 ✓"

    def _record_live_blend_status(self, iid, spm, persist=True):
        text = self._blend_status_text(spm)
        self.ui_queue.put(("cell", (iid, "blend_status", text)))
        if persist:
            with self.state_lock:
                self.state.setdefault(iid, {})["blend_status"] = text
                save_state(self.state)
        return text

    def _job_check(self, iid, spm):
        from spm_audit import audit_spm, sk_readiness

        entry = self.state.get(iid, {})
        manual_bones_locked = self.items[iid].get("manual_bones_locked", False)
        summary = entry.get("spm_summary", "")
        if manual_bones_locked:
            text = manual_bone_status_text(spm)
        else:
            audit = audit_spm(spm, analyze_bone_graph=True)
            readiness = sk_readiness(audit)
            parts = spm_check_status_parts(audit)
            if not readiness["ready"]:
                disabled = ", ".join(
                    f"{item['generator']}({item['style']:g}/{item['bones']:g})"
                    for item in readiness["disabled_generators"]
                )
                text = f"오류: SK 미제작 · {disabled}"
            else:
                text = " · ".join(parts) if parts else "본 설정 없음"
        if not manual_bones_locked and summary and summary not in text:
            text += f" | {summary}"
        self.ui_queue.put(("cell", (iid, "spm_status", text)))

        self._record_live_blend_status(iid, spm, persist=False)

        ok, why = self._handoff_ready(spm)
        pushed = self._current_push_status_text(iid, spm)
        push_text = why if not pushed or not ok else f"{why} | {pushed}"
        self.ui_queue.put(("cell", (iid, "push_status", push_text)))

    @staticmethod
    def _spm_report_summary(rep):
        total = rep.get("total_bones")
        meta = rep.get("calibration") or {}
        branches = meta.get("total_branches")
        if rep.get("status") == "not-sk-ready":
            return "SK 미제작"
        if rep.get("status") == "manual-required":
            return "수동 처리 필요"
        if total is not None and branches:
            if meta.get("base_priority_applied"):
                disabled = meta.get("disabled_base_generator_count", 0)
                tag = f" [상한·Base {disabled}개 OFF]"
            else:
                tag = " [상한]" if meta.get("capped") else ""
            graph_tag = ""
            if meta.get("root_target_generator_count") is not None:
                graph_tag = (
                    f" · 대상 {meta['root_target_generator_count']}"
                    f"/Base제외 {meta.get('base_excluded_generator_count', 0)}"
                )
            return f"본 {total} / 가지 {branches}{tag}{graph_tag}"
        if meta.get("mode") == "no_branch_generators":
            return "SpeedTree 본 없음 → rigid 1본 폴백"
        return rep.get("status", "?")

    def _job_spm(self, iid, spm):
        entry = self.state.setdefault(iid, {})
        item = self.items[iid]
        if item.get("manual_bones_locked", False):
            summary = f"{manual_bone_status_text(spm)} · ① 전체 건너뜀"
            self.ui_queue.put(("cell", (iid, "spm_status", summary)))
            with self.state_lock:
                entry["spm_status"] = summary
                save_state(self.state)
            self.log(f"본 세팅 건너뜀 (수동 본 유지): {spm.name}")
            return

        snapshot = self._current_spm_snapshot(item)
        cache = entry.get("calibration_cache")
        if (
            not self.force_rerun
            and calibration_cache_matches(
                cache,
                snapshot["fingerprint"],
                self.spm_calibration_signature,
                getattr(self, "legacy_spm_calibration_signature", None),
            )
        ):
            summary = cache.get("summary", cache.get("status", "캐시"))
            cached_text = f"{summary} ✓ (변경 없음)"
            if cache.get("status") == "not-sk-ready":
                raise RuntimeError(f"SK 미제작: {cache.get('error', '본 설정 필요')} (캐시)")
            self.ui_queue.put(("cell", (iid, "spm_status", cached_text)))
            with self.state_lock:
                cache["settings_signature"] = self.spm_calibration_signature
                entry["spm_status"] = cached_text
                entry["spm_summary"] = summary
                save_state(self.state)
            self.log(f"본 세팅 건너뜀 (SPM/옵션 변경 없음): {spm.name}")
            return

        started = time.perf_counter()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log(f"SPM 본 세팅 시작: {spm.name}")
        self.ui_queue.put(("cell", (iid, "spm_status", "캘리브레이션 중...")))
        report_path = LOG_DIR / f"{spm.stem}_spm_{stamp}.json"
        cmd = [sys.executable.replace("pythonw.exe", "python.exe"), "-X", "utf8", "-u",
               str(TOOL_DIR / "spm_audit.py"), str(spm), "--report", str(report_path)]
        # In parallel mode, don't pin every worker to the same core subset.
        parallel = self.cfg.get("spm_parallel_jobs", 1) > 1
        progress_state = {"stage": "준비 중", "stage_started": 0.0}

        def report_progress(total_elapsed, latest_line):
            if "[SpeedTree]" in latest_line:
                stage = latest_line.split("[SpeedTree]", 1)[1].strip()
                if stage != progress_state["stage"]:
                    progress_state["stage"] = stage
                    progress_state["stage_started"] = total_elapsed
            stage_elapsed = max(0.0, total_elapsed - progress_state["stage_started"])
            limit = int(self.cfg.get("spm_verify_timeout", 120))
            remaining = max(0, limit - int(stage_elapsed))
            elapsed_text = time.strftime("%M:%S", time.gmtime(stage_elapsed))
            text = (
                f"{progress_state['stage']} · {elapsed_text} "
                f"(수동 전환까지 {remaining}s)"
            )
            self.ui_queue.put(("cell", (iid, "spm_status", text)))

        code, log_file = self._run_limited(cmd, f"{spm.stem}_spm_{stamp}.log",
                                           self.cfg.get("spm_verify_timeout", 120) * 5,
                                           affinity=not parallel,
                                           progress_callback=report_progress)
        if not report_path.exists():
            raise RuntimeError(f"본 세팅 실패 — 로그: {log_file}")
        rep = json.loads(report_path.read_text(encoding="utf-8"))[0]
        status = rep.get("status")
        if status == "failed" or (code != 0 and status != "not-sk-ready"):
            raise RuntimeError(f"본 세팅 실패: {rep.get('error', '?')} — 로그: {log_file}")
        summary = self._spm_report_summary(rep)
        warn = " ⚠" if rep.get("warnings") else ""
        duration = time.perf_counter() - started
        try:
            final_snapshot = file_content_snapshot(spm)
            item["spm_snapshot"] = final_snapshot
        except OSError as exc:
            final_snapshot = None
            self.log(f"  [캐시 경고] 최종 SPM 지문 계산 실패: {spm.name}: {exc}")
        cacheable = status in {"calibrated", "already-ok", "manual-required", "not-sk-ready"}
        with self.state_lock:
            if cacheable and final_snapshot:
                entry["calibration_cache"] = {
                    "version": CALIBRATION_CACHE_VERSION,
                    "spm_fingerprint": final_snapshot["fingerprint"],
                    "settings_signature": self.spm_calibration_signature,
                    "status": status,
                    "summary": summary,
                    "error": rep.get("error", ""),
                    "probe_cache_hit": bool((rep.get("calibration") or {}).get("probe_cache_hit")),
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                }
            entry["spm_last_duration_seconds"] = round(duration, 3)
            entry["spm_status"] = f"{summary}{warn}"
            entry["spm_summary"] = summary
            entry["blend_status"] = self._blend_status_text(spm)
            save_state(self.state)
        self.ui_queue.put(("cell", (iid, "blend_status", entry["blend_status"])))
        if status == "not-sk-ready":
            raise RuntimeError(f"SK 미제작: {rep.get('error', '보이는 Branch의 본 설정이 모두 꺼져 있음')}")
        self.ui_queue.put(("cell", (iid, "spm_status", f"{summary}{warn}")))
        for warning in rep.get("warnings", []):
            self.log(f"  [경고] {spm.name}: {warning}")
        if status == "manual-required":
            self.log(f"자동 본 세팅 건너뜀: {spm.name} — 수동 처리 필요 (원본 복원됨)")
        else:
            self.log(f"본 세팅 완료: {spm.name} — {summary}")

    def _job_blender(self, iid, spm, item):
        from spm_audit import audit_spm, sk_readiness

        blend = blend_path_for(spm)
        leaf_ok, leaf_reason = self._leaf_reference_ready(spm)
        if not leaf_ok:
            status = self._record_live_blend_status(iid, spm)
            contract = inspect_spm_leaf_contract(spm)
            self.log(f"  [② 조기 차단] {spm.name}: {leaf_reason}")
            raise BatchItemError(
                leaf_reason,
                kind="data_error",
                report={
                    "status": "blocked",
                    "error": leaf_reason,
                    "blend_status": status,
                    "leaf_reference_contract": contract,
                },
            )
        handoff_ok, _handoff_reason = self._handoff_ready(spm)
        if not self.force_rerun and handoff_ok:
            self._record_live_blend_status(iid, spm)
            self.log(f"건너뜀 (blend 최신): {spm.name}")
            return
        readiness = sk_readiness(
            audit_spm(
                spm,
                analyze_bone_graph=not item.get("manual_bones_locked", False),
            )
        )
        if not readiness["ready"]:
            raise RuntimeError(f"SK 미제작: {readiness['error']}")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        entry = self.state.setdefault(iid, {})
        self.log(f"재질 사전검사 시작: {spm.name} (Blender 실행 전)")
        self.ui_queue.put(("cell", (iid, "blend_status", "재질 사전검사 중...")))
        fbx_ini = Path(self.cfg["fbx_ini"]).resolve()
        try:
            speedtree_cli = fbx_ini.parents[2] / "speedtree_cli.py"
        except IndexError as exc:
            raise BatchItemError(
                f"SpeedTree export helper 경로를 찾을 수 없음: {fbx_ini}",
                kind="data_error",
            ) from exc
        if not speedtree_cli.is_file():
            raise BatchItemError(
                f"SpeedTree export helper 없음: {speedtree_cli}",
                kind="data_error",
            )
        material_report = LOG_DIR / (
            f"{spm.stem}_material_preflight_{stamp}.json"
        )
        material_cmd = [
            sys.executable,
            str(TOOL_DIR / "jobs" / "speedtree_material_preflight.py"),
            "--spm", str(spm),
            "--speedtree-exe", str(self.cfg["speedtree_exe"]),
            "--fbx-ini", str(fbx_ini),
            "--speedtree-cli", str(speedtree_cli),
            "--report", str(material_report),
            "--timeout", str(
                self.cfg.get("speedtree_material_preflight_timeout", 900)
            ),
        ]
        material_code, material_log = self._run_limited(
            material_cmd,
            f"{spm.stem}_material_preflight_{stamp}.log",
            self.cfg.get("speedtree_material_preflight_timeout", 900) + 30,
            affinity=False,
        )
        material_result = load_job_report(material_report)
        if material_code != 0 or material_result.get("status") != "ok":
            reason = summarize_job_failure(material_result, material_log)
            self.log(
                f"  [Blender 실행 전 차단] {spm.name}: {reason} — "
                f"상세 보고서: {material_report}"
            )
            raise BatchItemError(
                reason,
                kind="data_error",
                report=material_result,
                log_file=material_log,
                report_file=material_report,
            )
        self.log(f"재질 사전검사 통과: {spm.name}")
        self.log(f"Blender repair 시작: {spm.name} (수분 소요될 수 있음)")
        self.ui_queue.put(("cell", (iid, "blend_status", "Blender repair 중...")))
        wind = item["wind_override"]
        if wind == "auto":
            wind = wind_preset_for(spm.stem)
        job_report = LOG_DIR / f"{spm.stem}_bwr_{stamp}.json"
        pipeline_report = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            previous_pipeline_report = pipeline_report.read_bytes()
        except OSError:
            previous_pipeline_report = None
        cmd = [
            self.cfg["blender_exe"], "--factory-startup", "-b",
            "--python", str(TOOL_DIR / "jobs" / "bwr_headless_job.py"), "--",
            "--spm", str(spm), "--blend", str(blend),
            "--wind", wind,
            "--material-contract", str(material_report),
            "--report", str(job_report),
        ]
        parallel = self.cfg.get("blender_parallel_jobs", 2) > 1
        code, log_file = self._run_limited(
            cmd,
            f"{spm.stem}_bwr_{stamp}.log",
            self.cfg.get("blender_job_timeout", 3600),
            affinity=not parallel,
        )
        result = load_job_report(job_report)
        if code != 0 or result.get("status") != "ok":
            if (
                previous_pipeline_report is not None
                and result.get("status") not in {"ok", "blocked"}
            ):
                try:
                    atomic_write_bytes(pipeline_report, previous_pipeline_report)
                    self.log(
                        f"  [② 복구] 실패 전 최신성 보고서 보존: {spm.name}"
                    )
                except OSError as exc:
                    self.log(
                        f"  [② 복구 경고] 이전 보고서 복원 실패: "
                        f"{spm.name}: {exc}"
                    )
            reason = summarize_job_failure(result, log_file)
            self.log(f"  [Blender 실패 원인] {reason} — 상세 로그: {log_file}")
            raise BatchItemError(
                reason,
                kind="data_error",
                report=result,
                log_file=log_file,
                report_file=job_report,
            )
        handoff_ok, handoff_reason = self._handoff_ready(spm)
        blend_status = self._blend_status_text(spm)
        self.ui_queue.put(("cell", (iid, "blend_status", blend_status)))
        with self.state_lock:
            entry["blend_status"] = blend_status
            save_state(self.state)
        if not handoff_ok:
            raise BatchItemError(
                f"② 완료 후 사전검사 실패: {handoff_reason}",
                kind="data_error",
                report=result,
                log_file=log_file,
                report_file=job_report,
            )
        for warning in result.get("warnings", []):
            self.log(f"  [② 경고] {spm.name}: {warning}")
        self.log(f"repair 완료: {blend.name}")

    def _push_preflight(self, targets):
        """Return (ready items, fatal Unreal reason) after recording all skips."""
        self.log("push 준비 검사 중...")
        transport = getattr(
            self,
            "active_push_transport",
            self.cfg.get("push_transport", "rpc"),
        )
        if transport == "rpc" and not self._unreal_running():
            reason = "Unreal Editor 종료 — MyProject2가 실행 중이 아님"
            self.log(f"[중단] {reason}")
            for item in targets:
                iid = str(item["spm"])
                self._record_phase_status(
                    iid,
                    "push_status",
                    f"중단: {reason}",
                    "unreal_unavailable",
                    reason,
                    persist=False,
                )
            with self.state_lock:
                save_state(self.state)
            return [], reason
        if transport == "headless" and self._unreal_running():
            reason = "headless Push는 자산 잠금 충돌 방지를 위해 Unreal Editor를 닫아야 함"
            self.log(f"[중단] {reason}")
            for item in targets:
                self._record_phase_status(
                    str(item["spm"]),
                    "push_status",
                    f"중단: {reason}",
                    "unreal_unavailable",
                    reason,
                    persist=False,
                )
            with self.state_lock:
                save_state(self.state)
            return [], reason
        ready = []
        for item in targets:
            spm = item["spm"]
            ok, why = self._handoff_ready(spm)
            if ok:
                ready.append(item)
            else:
                status_text = f"건너뜀: {why}"
                self._record_phase_status(
                    str(spm),
                    "push_status",
                    status_text,
                    "preflight_skip",
                    why,
                    persist=False,
                )
                self.log(f"[준비 안 됨] {spm.name}: {why}")
        with self.state_lock:
            save_state(self.state)
        self.log(f"준비 검사: {len(ready)}/{len(targets)}개 push 가능.")
        return ready, None

    @staticmethod
    def _unreal_running():
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
            capture_output=True, text=True, creationflags=0x08000000,
        )
        return "UnrealEditor.exe" in (result.stdout or "")

    def _set_push_state(self, iid, kind, status_text, details=None, message=None):
        self.ui_queue.put(("cell", (iid, "push_status", status_text)))
        with self.state_lock:
            entry = self.state.setdefault(iid, {})
            error_message = message or status_text
            existing_error = entry.get("push_status_error") or {}
            unchanged = (
                entry.get("push_status") == status_text
                and entry.get("push_status_kind") == kind
                and (not details or entry.get("push_paths") == details)
                and (
                    kind in {"exported_pending_unreal", "importing", "imported_ok"}
                    or (
                        existing_error.get("kind") == kind
                        and existing_error.get("message") == error_message
                    )
                )
            )
            if unchanged:
                return
            entry["push_status"] = status_text
            entry["push_status_kind"] = kind
            if kind in {"exported_pending_unreal", "importing", "imported_ok"}:
                entry.pop("push_status_error", None)
            else:
                error = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "kind": kind,
                    "message": error_message,
                }
                if details:
                    error.update(details)
                entry["push_status_error"] = error
            if details:
                entry["push_paths"] = details
            save_state(self.state)

    def _push_dependency_paths(self):
        send2ue_dir = Path(self.cfg["send2ue_dir"])
        return [
            TOOL_DIR / "jobs" / "send2ue_push_job.py",
            TOOL_DIR / "jobs" / "vertex_color_contract.py",
            TOOL_DIR / "unreal_ingest.py",
            send2ue_dir / "core" / "export.py",
            send2ue_dir / "core" / "ingest.py",
            send2ue_dir / "dependencies" / "unreal.py",
            send2ue_dir / "resources" / "extensions" / "send2ue_material_pipeline.py",
            send2ue_dir / "resources" / "pipeline" / "ue_material_setup.py",
            Path(
                r"C:\Users\PARK\Documents\GitHub\ue-unique-export-names-addon"
                r"\ue_unique_export_names_addon\unreal_material_json.py"
            ),
        ]

    @staticmethod
    def _push_material_contract(spm):
        """Write a strict, live-validated contract wrapper for Blender Push."""
        spm = Path(spm)
        repair_report = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        try:
            payload = json.loads(repair_report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"SpeedTree Repair contract report could not be read: {exc}"
            ) from exc
        envelope = payload.get("speedtree_pipeline_contract")
        if not isinstance(envelope, dict):
            raise RuntimeError(
                "SpeedTree Repair report has no current pipeline contract; "
                "run Blender Repair again"
            )
        if (payload.get("handoff_preflight") or {}).get("status") != "ok":
            raise RuntimeError(
                "SpeedTree Repair handoff preflight is not ready; "
                "run Blender Repair again"
            )
        validate_preflight_envelope(envelope, spm, require_ok=True)
        source_fingerprint = str(envelope.get("source_fingerprint") or "")
        suffix = source_fingerprint[:16] or hashlib.sha256(
            _normalized_path(spm).encode("utf-8")
        ).hexdigest()[:16]
        contract_path = LOG_DIR / (
            f"{spm.stem}_push_material_contract_{suffix}.json"
        )
        atomic_write_json(
            contract_path,
            {
                "status": "ok",
                "speedtree_pipeline_contract": envelope,
                "source_repair_report": str(repair_report.resolve()),
            },
        )
        return contract_path

    def _current_push_status_text(self, iid, spm):
        """Show current receipt validity without hashing multi-GB blend files."""
        entry = self.state.get(iid, {})
        saved = str(entry.get("push_status") or "")
        kind = str(entry.get("push_status_kind") or "")
        success_like = kind == "imported_ok" or saved.startswith("완료")
        export_like = kind == "exported_pending_unreal" or saved.startswith(
            "export 완료"
        )
        if not (success_like or export_like):
            return saved or "-"

        source_cache = entry.get("push_source_fingerprint_cache") or {}
        export_cache = entry.get("push_export_cache") or {}
        if (
            source_cache.get("version")
            != PUSH_SOURCE_FINGERPRINT_CACHE_VERSION
            or not source_cache.get("fingerprint")
        ):
            return "Push 재확인 필요 — 최신성 영수증 없음"
        try:
            current_snapshot = push_source_snapshot(
                blend_path_for(spm), self._push_dependency_paths()
            )
        except (OSError, ValueError, KeyError):
            return "Push 재확인 필요 — 입력 파일 확인"
        if source_cache.get("snapshot") != current_snapshot:
            return "Push 재확인 필요 — Blender/파이프라인 변경"
        if export_cache.get("source_fingerprint") != source_cache.get(
            "fingerprint"
        ):
            return "Push 재확인 필요 — export 영수증 불일치"
        manifest_path = Path(export_cache.get("manifest") or "")
        if not manifest_path.is_file():
            return "Push 재확인 필요 — export manifest 없음"
        export_fingerprint = export_cache.get("fingerprint")
        if success_like:
            if entry.get("push_import_fingerprint") != export_fingerprint:
                return "Push 재확인 필요 — Unreal 결과 불일치"
            return "완료 (현재 최신)"
        return "export 완료 · Unreal 대기"

    def _source_push_fingerprint(self, blend, iid=None):
        """Hash a large source blend once, then reuse its stable stat cache."""
        state_entry = self.state.setdefault(iid, {}) if iid else {}
        fingerprint, record, cache_hit = cached_push_source_fingerprint(
            blend,
            self._push_dependency_paths(),
            cache=state_entry.get("push_source_fingerprint_cache"),
        )
        if iid:
            with self.state_lock:
                self.state.setdefault(iid, {})[
                    "push_source_fingerprint_cache"
                ] = record
        if cache_hit:
            self.log(f"[source hash cache] {Path(blend).name}: 재사용")
        return fingerprint

    def _cached_manifest_item(self, iid, source_fingerprint):
        if getattr(self, "force_rerun", False):
            return None
        cache = self.state.get(iid, {}).get("push_export_cache") or {}
        if cache.get("source_fingerprint") != source_fingerprint:
            return None
        manifest_path = Path(cache.get("manifest", ""))
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = (manifest.get("items") or [])[0]
        except (OSError, ValueError, IndexError, TypeError):
            return None
        if str(item.get("queue_id")) != iid or not manifest_item_files_match(item):
            return None
        return item

    def _export_manifest_item(self, iid, spm, batch_stamp):
        blend = blend_path_for(spm)
        source_fingerprint = self._source_push_fingerprint(blend, iid)
        cached = self._cached_manifest_item(iid, source_fingerprint)
        import_report = LOG_DIR / f"{spm.stem}_unreal_{batch_stamp}.json"
        if cached is not None:
            cached = dict(cached)
            cached["report_path"] = str(import_report.resolve())
            details = {
                "manifest": self.state[iid]["push_export_cache"]["manifest"],
                "export_report": cached.get("export_report_path", ""),
                "import_report": str(import_report),
                "cache": "hit",
            }
            self._set_push_state(
                iid,
                "exported_pending_unreal",
                "export 완료 · Unreal 대기 (cache)",
                details=details,
            )
            self.log(f"[export cache] {spm.name}: {cached['fingerprint']}")
            return cached

        cache_root = LOG_DIR / "send2ue_export_cache" / source_fingerprint / spm.stem
        manifest_path = LOG_DIR / f"{spm.stem}_manifest_{source_fingerprint}.json"
        export_report = LOG_DIR / f"{spm.stem}_export_{source_fingerprint}.json"
        export_log_name = f"{spm.stem}_export_{batch_stamp}.log"
        checkpoint_path = LOG_DIR / f"{spm.stem}_rpc_checkpoint_{batch_stamp}.json"
        batch_report = LOG_DIR / f"{spm.stem}_rpc_batch_{batch_stamp}.json"
        send2ue_unreal_py = (
            Path(self.cfg["send2ue_dir"]) / "dependencies" / "unreal.py"
        )
        material_contract = self._push_material_contract(spm)
        cmd = [
            self.cfg["blender_exe"],
            "--factory-startup",
            "-b",
            str(blend),
            "--python",
            str(TOOL_DIR / "jobs" / "send2ue_push_job.py"),
            "--",
            "--transport",
            "headless_export",
            "--report",
            str(export_report),
            "--spm",
            str(spm),
            "--material-contract",
            str(material_contract),
            "--manifest",
            str(manifest_path),
            "--checkpoint",
            str(checkpoint_path),
            "--batch-report",
            str(batch_report),
            "--item-import-report",
            str(import_report),
            "--export-root",
            str(cache_root),
            "--queue-id",
            iid,
            "--source-fingerprint",
            source_fingerprint,
            "--unreal-ingest",
            str(TOOL_DIR / "unreal_ingest.py"),
            "--send2ue-unreal-py",
            str(send2ue_unreal_py),
            "--max-push-polygons",
            str(self.cfg.get("push_max_polygons", 2_000_000)),
            "--max-push-bones",
            str(self.cfg.get("push_max_bones", 1_500)),
        ]
        wind_override = self.items.get(iid, {}).get("wind_override", "auto")
        resolved_wind = (
            wind_preset_for(spm.stem)
            if wind_override == "auto"
            else wind_override
        )
        if resolved_wind == "TREE":
            cmd.append("--require-green-signal")
        code, log_file = self._run_limited(
            cmd,
            export_log_name,
            self.cfg.get("push_job_timeout", 1800),
            affinity=self.cfg.get("blender_parallel_jobs", 2) <= 1,
        )
        result = load_job_report(export_report)
        if code != 0 or result.get("status") != "exported_pending_unreal":
            reason = summarize_job_failure(result, log_file)
            raise BatchItemError(
                reason,
                kind=classify_push_failure(result, log_file),
                report=result,
                log_file=log_file,
                report_file=export_report,
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            item = (manifest.get("items") or [])[0]
        except (OSError, ValueError, IndexError, TypeError) as exc:
            raise BatchItemError(
                f"manifest read failed: {exc}",
                kind="data_error",
                log_file=log_file,
                report_file=export_report,
            ) from exc
        if not manifest_item_files_match(item):
            raise BatchItemError(
                "manifest exported-file fingerprint verification failed",
                kind="data_error",
                log_file=log_file,
                report_file=export_report,
            )

        post_export_source_fingerprint = self._source_push_fingerprint(blend, iid)
        with self.state_lock:
            self.state.setdefault(iid, {})["push_export_cache"] = {
                "source_fingerprint": post_export_source_fingerprint,
                "manifest": str(manifest_path),
                "fingerprint": item["fingerprint"],
            }
        details = {
            "manifest": str(manifest_path),
            "export_report": str(export_report),
            "export_log": str(log_file),
            "import_report": str(import_report),
            "cache": "miss",
        }
        self._set_push_state(
            iid,
            "exported_pending_unreal",
            "export 완료 · Unreal 대기",
            details=details,
        )
        return item

    def _sync_headless_checkpoint(self, checkpoint_path, item_by_id, log_file=None):
        try:
            checkpoint = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        labels = {
            "importing": "Unreal import 중...",
            "imported_ok": "완료 (headless)",
            "data_error": "실패: data error",
            "manual_required": "수동 처리 필요",
            "unreal_crash": "Unreal commandlet crash",
            "not_run": "미실행",
        }
        for queue_id, result in (checkpoint.get("items") or {}).items():
            if queue_id not in item_by_id:
                continue
            status = result.get("status", "not_run")
            message = result.get("message") or labels.get(status, status)
            text = labels.get(status, status)
            if status in {"data_error", "manual_required", "unreal_crash", "not_run"}:
                text = f"{text}: {compact_error_message(message, 80)}"
            item = item_by_id[queue_id]
            details = {
                "manifest": str(item.get("batch_manifest", "")),
                "checkpoint": str(checkpoint_path),
                "report": str(item.get("report_path", "")),
                "batch_report": str(item.get("batch_report", "")),
            }
            if log_file:
                details["log"] = str(log_file)
            if status == "imported_ok":
                self.state.setdefault(queue_id, {})["push_import_fingerprint"] = item[
                    "fingerprint"
                ]
            self._set_push_state(
                queue_id,
                status,
                text,
                details=details,
                message=message,
            )
        return checkpoint

    @staticmethod
    def _record_headless_process_exit(checkpoint_path, max_item_crash_retries):
        checkpoint_path = Path(checkpoint_path)
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        current_id = checkpoint.get("current_item")
        state = (checkpoint.get("items") or {}).get(current_id)
        if not current_id or not state or state.get("status") != "importing":
            return checkpoint
        crash_count = int(state.get("crash_count", 0)) + 1
        status = (
            "manual_required"
            if crash_count > int(max_item_crash_retries)
            else "unreal_crash"
        )
        message = (
            f"Unreal commandlet crash retry limit exceeded ({max_item_crash_retries})"
            if status == "manual_required"
            else "UnrealEditor-Cmd exited while this item was importing"
        )
        state.update(
            {
                "status": status,
                "crash_count": crash_count,
                "message": message,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        checkpoint["current_item"] = None
        checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(checkpoint_path, checkpoint)
        return checkpoint

    def _run_headless_push_batch(self, targets, emit_done=True):
        batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        total = len(targets)
        self.ui_queue.put(("batch_progress", (0, total)))
        exported_by_index = {}
        failed_items = set(getattr(self, "_phase_failed_items", set()))
        workers = max(
            1,
            min(int(self.cfg.get("blender_parallel_jobs", 2)), total),
        )
        if workers > 1:
            self.log(f"Send2UE export: {workers}개 동시 실행")

        def export_one(index, item):
            if self.stop_flag.is_set():
                return index, None
            spm = item["spm"]
            iid = str(spm)
            self.ui_queue.put(("cell", (iid, "push_status", "Send2UE export 중...")))
            try:
                return index, self._export_manifest_item(iid, spm, batch_stamp)
            except Exception as exc:
                reason = compact_error_message(exc)
                kind = getattr(exc, "kind", "data_error")
                details = {}
                if getattr(exc, "log_file", None):
                    details["log"] = str(exc.log_file)
                if getattr(exc, "report_file", None):
                    details["report"] = str(exc.report_file)
                self._set_push_state(
                    iid,
                    kind,
                    self._failure_status_text(reason, kind),
                    details=details,
                    message=reason,
                )
                self.log(f"[headless export {kind}] {spm.name}: {reason}")
                with self.state_lock:
                    failed_items.add(iid)
                return index, None

        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(export_one, index, item): item
                for index, item in enumerate(targets)
            }
            for future in as_completed(futures):
                index, exported_item = future.result()
                if exported_item is not None:
                    exported_by_index[index] = exported_item
                completed += 1
                self.ui_queue.put(("batch_progress", (completed, total)))

        exported = [exported_by_index[index] for index in sorted(exported_by_index)]

        if self.stop_flag.is_set():
            for item in targets:
                iid = str(item["spm"])
                if self.state.get(iid, {}).get("push_status_kind") not in {
                    "exported_pending_unreal", "imported_ok", "data_error", "manual_required"
                }:
                    self._set_push_state(iid, "not_run", "미실행: 사용자 중지")
            if emit_done:
                self.ui_queue.put(("progress", "중지됨"))
                self.ui_queue.put(("done", None))
            self._phase_failed_items = failed_items
            return False

        pending = []
        for item in exported:
            iid = str(item["queue_id"])
            entry = self.state.setdefault(iid, {})
            if (
                not self.force_rerun
                and entry.get("push_import_fingerprint") == item["fingerprint"]
            ):
                self._set_push_state(iid, "imported_ok", "완료 (import cache)")
                continue
            pending.append(item)

        if not pending:
            self._phase_failed_items = failed_items
            success_count = sum(
                1 for item in targets
                if str(item["spm"]) not in failed_items
            )
            failure_count = len(failed_items)
            if emit_done:
                text = (
                    "Unreal Push 완료 (cache)"
                    if not failure_count
                    else (
                        f"Unreal Push 종료 — 성공 {success_count}개 · "
                        f"실패/준비 제외 {failure_count}개"
                    )
                )
                self.ui_queue.put(("progress", text))
                self.ui_queue.put(("done", None))
            return not failure_count

        manifest_path = LOG_DIR / f"headless_queue_{batch_stamp}.json"
        checkpoint_path = LOG_DIR / f"headless_queue_{batch_stamp}_checkpoint.json"
        report_path = LOG_DIR / f"headless_queue_{batch_stamp}_report.json"
        for item in pending:
            item["batch_manifest"] = str(manifest_path)
            item["batch_report"] = str(report_path)
        manifest = {
            "schema_version": PUSH_MANIFEST_SCHEMA_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "checkpoint_path": str(checkpoint_path),
            "report_path": str(report_path),
            "max_item_crash_retries": int(
                self.cfg.get("headless_item_crash_retries", 2)
            ),
            "items": pending,
        }
        atomic_write_json(manifest_path, manifest)
        item_by_id = {str(item["queue_id"]): item for item in pending}

        commandlet = self.cfg["unreal_editor_cmd"]
        project = self.cfg["unreal_project"]
        runner = TOOL_DIR / "unreal_ingest.py"
        cmd = [
            commandlet,
            project,
            "-run=pythonscript",
            f"-script={runner}",
            "-unattended",
            "-NoSplash",
            "-NoSound",
            "-UTF8Output",
        ]
        env = os.environ.copy()
        env.update(
            {
                "SK_BATCH_MANIFEST_PATH": str(manifest_path.resolve()),
                "SK_BATCH_CHECKPOINT_PATH": str(checkpoint_path.resolve()),
                "SK_BATCH_REPORT_PATH": str(report_path.resolve()),
            }
        )
        max_restarts = max(0, int(self.cfg.get("headless_batch_max_restarts", 10)))
        complete = False
        last_log = None
        checkpoint = {}
        for launch_index in range(max_restarts + 1):
            if self.stop_flag.is_set():
                break
            self.log(
                f"UnrealEditor-Cmd headless 시작 ({launch_index + 1}/{max_restarts + 1})"
            )
            attempt_log = LOG_DIR / (
                f"headless_unreal_{batch_stamp}_{launch_index + 1}.log"
            )
            last_log = attempt_log
            try:
                code, last_log = self._run_limited(
                    cmd,
                    attempt_log.name,
                    self.cfg.get("headless_job_timeout", 14_400),
                    affinity=False,
                    progress_callback=lambda _elapsed, _line: self._sync_headless_checkpoint(
                        checkpoint_path,
                        item_by_id,
                        attempt_log,
                    ),
                    env=env,
                )
            except Exception as exc:
                code = -1
                self.log(f"[headless watchdog] {exc}")
            checkpoint = self._sync_headless_checkpoint(
                checkpoint_path,
                item_by_id,
                last_log,
            )
            complete = bool(checkpoint.get("complete")) and report_path.is_file()
            if complete:
                break
            checkpoint = self._record_headless_process_exit(
                checkpoint_path,
                manifest["max_item_crash_retries"],
            )
            self._sync_headless_checkpoint(
                checkpoint_path,
                item_by_id,
                last_log,
            )
            self.log(
                f"[headless watchdog] commandlet 종료 code={code}; checkpoint 재개"
            )

        if not complete:
            checkpoint = self._sync_headless_checkpoint(
                checkpoint_path,
                item_by_id,
                last_log,
            )
            terminal = {
                "imported_ok",
                "data_error",
                "manual_required",
                "unreal_crash",
            }
            for iid in item_by_id:
                status = (checkpoint.get("items") or {}).get(iid, {}).get("status")
                if status not in terminal:
                    self._set_push_state(
                        iid,
                        "not_run",
                        "미실행: headless watchdog 재시작 상한 초과",
                        details={
                            "manifest": str(manifest_path),
                            "checkpoint": str(checkpoint_path),
                            "log": str(last_log or ""),
                        },
                    )
            self._phase_abort_reason = "headless watchdog 재시작 상한 초과"

        for iid, result in (checkpoint.get("items") or {}).items():
            if result.get("status") != "imported_ok":
                failed_items.add(str(iid))

        with self.state_lock:
            self._phase_failed_items = set(failed_items)
            save_state(self.state)
        if emit_done:
            success_count = sum(
                1 for item in targets
                if str(item["spm"]) not in failed_items
            )
            failure_count = len(failed_items)
            if not complete:
                progress_text = self._phase_abort_reason
            elif failure_count:
                progress_text = (
                    f"Unreal Push 종료 — 성공 {success_count}개 · "
                    f"실패/준비 제외 {failure_count}개"
                )
            else:
                progress_text = "Unreal Push 완료"
            self.ui_queue.put(
                ("progress", progress_text)
            )
            self.ui_queue.put(("done", None))
        return complete and not failed_items

    def _job_push(self, iid, spm):
        blend = blend_path_for(spm)
        source_fingerprint = self._source_push_fingerprint(blend, iid)
        if not getattr(self, "force_rerun", False):
            cached = self._cached_manifest_item(iid, source_fingerprint)
            if (
                cached is not None
                and self.state.get(iid, {}).get("push_import_fingerprint")
                == cached.get("fingerprint")
            ):
                self._set_push_state(iid, "imported_ok", "완료 (import cache)")
                self.log(f"Unreal push 건너뜀 (입력/가져오기 변경 없음): {spm.name}")
                return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log(f"Unreal push 시작: {blend.name}")
        self.ui_queue.put(("cell", (iid, "push_status", "push 중...")))
        job_report = LOG_DIR / f"{spm.stem}_push_{stamp}.json"
        manifest_path = LOG_DIR / f"{spm.stem}_rpc_manifest_{stamp}.json"
        checkpoint_path = LOG_DIR / f"{spm.stem}_rpc_checkpoint_{stamp}.json"
        batch_report = LOG_DIR / f"{spm.stem}_rpc_batch_{stamp}.json"
        import_report = LOG_DIR / f"{spm.stem}_rpc_unreal_{stamp}.json"
        export_root = LOG_DIR / "send2ue_rpc_export" / stamp / spm.stem
        send2ue_unreal_py = (
            Path(self.cfg["send2ue_dir"]) / "dependencies" / "unreal.py"
        )
        material_contract = self._push_material_contract(spm)
        cmd = [
            self.cfg["blender_exe"], "--factory-startup", "-b", str(blend),
            "--python", str(TOOL_DIR / "jobs" / "send2ue_push_job.py"), "--",
            "--report", str(job_report),
            "--spm", str(spm),
            "--material-contract", str(material_contract),
            "--transport", "rpc",
            "--manifest", str(manifest_path),
            "--checkpoint", str(checkpoint_path),
            "--batch-report", str(batch_report),
            "--item-import-report", str(import_report),
            "--export-root", str(export_root),
            "--queue-id", iid,
            "--source-fingerprint", source_fingerprint,
            "--unreal-ingest", str(TOOL_DIR / "unreal_ingest.py"),
            "--send2ue-unreal-py", str(send2ue_unreal_py),
            "--max-push-polygons", str(self.cfg.get("push_max_polygons", 2_000_000)),
            "--max-push-bones", str(self.cfg.get("push_max_bones", 1_500)),
            "--rpc-timeout-min", str(self.cfg.get("push_rpc_timeout_min", 180)),
            "--rpc-timeout-max", str(self.cfg.get("push_rpc_timeout_max", 900)),
        ]
        wind_override = self.items.get(iid, {}).get("wind_override", "auto")
        resolved_wind = (
            wind_preset_for(spm.stem)
            if wind_override == "auto"
            else wind_override
        )
        if resolved_wind == "TREE":
            cmd.append("--require-green-signal")
        code, log_file = self._run_limited(cmd, f"{spm.stem}_push_{stamp}.log",
                                           self.cfg.get("push_job_timeout", 1800))
        result = load_job_report(job_report)
        if code != 0 or result.get("status") != "ok":
            reason = summarize_job_failure(result, log_file)
            kind = classify_push_failure(result, log_file)
            self.log(f"  [Unreal 실패 원인] {reason} — 상세 로그: {log_file}")
            raise BatchItemError(
                reason,
                kind=kind,
                report=result,
                log_file=log_file,
                report_file=job_report,
            )
        wind_info = result.get("wind")
        wind_ok = "wind ✓" if isinstance(wind_info, dict) and wind_info.get("ok") else "wind -"
        self.ui_queue.put(("cell", (iid, "push_status", f"완료 ({wind_ok})")))
        entry = self.state.setdefault(iid, {})
        entry["push_status"] = f"완료 {datetime.now():%m-%d %H:%M}"
        entry["push_status_kind"] = "imported_ok"
        manifest_fingerprint = result.get("manifest_fingerprint")
        entry["push_import_fingerprint"] = manifest_fingerprint
        if manifest_path.is_file() and manifest_fingerprint:
            entry["push_export_cache"] = {
                "source_fingerprint": source_fingerprint,
                "manifest": str(manifest_path),
                "fingerprint": manifest_fingerprint,
            }
        entry["push_paths"] = {
            "manifest": str(manifest_path),
            "checkpoint": str(checkpoint_path),
            "report": str(job_report),
            "import_report": str(import_report),
            "log": str(log_file),
        }
        entry.pop("push_status_error", None)
        save_state(self.state)
        self.log(f"push 완료: {result.get('unreal_folder', '?')}{result.get('unit_name', '')}")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
