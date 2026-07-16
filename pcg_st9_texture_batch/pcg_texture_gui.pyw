"""PCG ST9 → SK 전환 준비 보드.

목적: 언리얼 PCG에서 쓰는 ST9 나무(WPO + 마스크 머티리얼)를
SK_ 데이터(나나이트 + 논마스크 지오메트리 + 버추얼 텍스처)로 바꾸기 위해,
나무 폴더마다 "뭐가 되어 있고 뭐가 남았는지"를 한 화면에서 보여준다.

단계 (표의 컬럼 순서 = 작업 순서):
  ① SK SPM + M_ 이름 : 원본 SPM을 SK_이름.spm 으로 복사하고 머티리얼 이름에
                       M_ 을 붙인다. 이 도구가 자동으로 해 준다.
  ② 잎 메시 (Blender) : 헤드리스 아틀라스 리프 제너레이터로 오파시티 없는 잎
                       지오메트리와 blend를 만들고, 선택 시 SK SPM에도 반영한다.
  ③ 텍스처 (Substance) : SBS에 원본을 연결해 6장
                       (color/normal/extra/height/opacity/subsurface)
                       익스포트하고, 필요하면 HBAO와 T_ 그래프도 만든다.

①~③은 실행 전 확인창을 띄우며, SPM/SBS/기존 출력은 수정 전에 백업한다.
"""
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(TOOL_DIR))

from batch_ui_common import CheckedRowController, copy_selected_row_paths

from pcg_texture_common import (
    TARGETS_PATH, load_config, load_pcg_targets, save_config,
)
from pcg_texture_audit import (
    make_report,
    prepare_sk,
    register_blend_source_images,
    save_spm_analysis_cache,
)
from export_review_queue import GENERIC_MATERIAL_RE
from export_texture_plan import bucket_refs, build_texture_plan_from_report
import sbs_auto
from migrate_current_sk_textures import (
    build_job as build_texture_job,
    complete_output_set,
    expected_job_size,
    job_needs_source_repair,
    run_job as run_texture_job,
)
from unreal_texture_sync import UnrealTextureSyncDeferred, sync_texture_files
from spm_texture_normalize import (
    cleanup_preserved_cluster_outputs,
    jobs_from_texture_plan,
    normalize_spms_transactionally,
    output_paths,
)

CHECK_ON = "☑"
CHECK_OFF = "☐"
TARGET_ROW_COLORS = {
    "target_pcg": "#E8F3FF",
    "target_level": "#FFF0D0",
    "target_both": "#E4F4E4",
}


def focus_data_asset_label(paths):
    names = []
    for path in paths or []:
        name = str(path).rsplit("/", 1)[-1]
        if name.startswith("DA_Base_"):
            name = "DB " + name[len("DA_Base_"):]
        names.append(name)
    return "/".join(names) or "PCG"


def step3_item_state(item):
    """Summarize unique texture outputs separately from SPM connection work."""
    all_entries = list(item.get("cluster_items") or [])
    local_entries = [row for row in all_entries if not row.get("shared_from")]
    missing_rows = [row for row in local_entries if row.get("missing_export_maps")]
    missing_maps = sum(len(row.get("missing_export_maps") or []) for row in missing_rows)
    connection_rows = [
        row for row in all_entries if row.get("connection_update_needed")
    ]
    total_maps = len(local_entries) * len(sbs_auto.RENDER_MAPS)
    return {
        "sets": len(local_entries),
        "shared_sets": len(all_entries) - len(local_entries),
        "missing_sets": len(missing_rows),
        "missing_maps": missing_maps,
        "complete_maps": total_maps - missing_maps,
        "total_maps": total_maps,
        "connection_sets": len(connection_rows),
    }


def step3_selection_state(entries):
    """Return the dynamic label/state for the selected step-3 action."""
    selected = [entry for entry in entries.values() if entry.get("checked")]
    if not selected:
        return {"text": "③ 실행 — 선택 항목 없음", "state": "disabled"}
    states = [step3_item_state(entry.get("item") or {}) for entry in selected]
    missing_sets = sum(row["missing_sets"] for row in states)
    missing_maps = sum(row["missing_maps"] for row in states)
    connection_sets = sum(row["connection_sets"] for row in states)
    texture_sets = sum(row["sets"] + row["shared_sets"] for row in states)
    if missing_sets:
        return {
            "text": (
                f"③ 실행 — 연결 텍스처 {missing_sets}세트 완성 "
                f"(누락 {missing_maps}장)"
            ),
            "state": "normal",
        }
    if connection_sets:
        return {
            "text": f"③ 실행 — 완료 텍스처 연결 정리 ({connection_sets}세트)",
            "state": "normal",
        }
    if texture_sets:
        return {
            "text": f"③ Unreal 동기화 — 완료 텍스처 확인 ({texture_sets}세트)",
            "state": "normal",
        }
    return {"text": "③ 실행 — 선택 항목에 텍스처 없음", "state": "disabled"}


def spm_paths_for_item(item):
    """Return the concrete SPM paths represented by a PCG folder row."""

    paths = []
    for status in item.get("target_spm_statuses") or []:
        path = status.get("sk_spm") or status.get("source_spm")
        if path:
            paths.append(path)
    if not paths:
        paths.extend(item.get("sk_spms") or [])
    if not paths and item.get("chosen_spm"):
        paths.append(item["chosen_spm"])
    return paths


def blender_user_startup_path(blender_exe):
    version = Path(blender_exe).parent.name.rsplit(" ", 1)[-1]
    appdata = os.environ.get("APPDATA")
    if not appdata or not version or not version[0].isdigit():
        return None
    startup = (Path(appdata) / "Blender Foundation" / "Blender" /
               version / "config" / "startup.blend")
    return startup if startup.is_file() else None


def atlas_blender_command(blender_exe):
    """Save a clean atlas file with PARK's startup UI/workspaces embedded."""
    startup = blender_user_startup_path(blender_exe)
    command = [blender_exe]
    if startup:
        # Factory preferences avoid loading every interactive add-on in a
        # background process. Opening the user's startup file explicitly still
        # carries its screens/workspaces into the blend that the job saves.
        command += ["--factory-startup", "--background", str(startup)]
    else:
        command += ["--background"]
    return command + [
        "--python", str(TOOL_DIR / "jobs" / "atlas_blend_job.py"), "--",
    ]

# 자동 적용을 막는 문제들의 한국어 설명
BLOCKER_TEXT = {
    "duplicate": "같은 이름이 다른 폴더에도 매칭됨 — 어느 폴더가 진짜인지 먼저 확인",
    "source_review": "이 폴더에서 원본 SPM을 못 찾음 — 파일 이름을 직접 확인 필요",
    "generic_only": "남은 작업이 'Material 2' 같은 기본 이름뿐 — SpeedTree에서 이름 지은 뒤 다시 ① 실행",
}


def split_generic(names):
    """머티리얼 이름을 (정상 이름, 'Material 2' 같은 기본 이름)으로 나눈다."""
    unprefixed = [n for n in names if not str(n).strip().startswith("M_")]
    generic = [n for n in unprefixed if GENERIC_MATERIAL_RE.match(str(n).strip())]
    normal = [n for n in unprefixed if n not in generic]
    return normal, generic


def generic_material_issues(item):
    """Return exact SPM/material locations for default SpeedTree names."""
    issues = []
    statuses = item.get("target_spm_statuses") or []
    if statuses:
        for status in statuses:
            _normal, generic = split_generic(
                status.get("materials_missing_m_prefix", []))
            spm = status.get("sk_spm") or status.get("source_spm") or ""
            for material in generic:
                issues.append({
                    "mesh_name": status.get("mesh_name", ""),
                    "spm": spm,
                    "material": material,
                })
        return issues
    _normal, generic = split_generic(item.get("materials_missing_m_prefix", []))
    spm = item.get("chosen_spm") or next(iter(item.get("sk_spms") or []), "")
    for material in generic:
        issues.append({"mesh_name": "", "spm": spm, "material": material})
    return issues


def generic_material_summary(item):
    issues = generic_material_issues(item)
    if not issues:
        return ""
    grouped = {}
    for issue in issues:
        label = Path(issue["spm"]).name if issue["spm"] else "SPM 미상"
        grouped.setdefault(label, []).append(issue["material"])
    if len(issues) == 1:
        issue = issues[0]
        return f"⚠ {Path(issue['spm']).name or 'SPM 미상'} → {issue['material']}"
    locations = ", ".join(
        f"{spm}({len(materials)})" for spm, materials in grouped.items())
    return f"⚠ 기본명 {len(issues)}개 · {locations}"


class Tooltip:
    """말풍선 도움말: 위젯에 마우스를 올리면 설명이 뜬다."""

    def __init__(self, widget, text, wrap=420):
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
        self.report = None
        self.items = {}  # iid(folder) -> {"item": dict, "checked": bool}
        self.checked_rows = CheckedRowController(self.items, self._redraw_checked_row)
        self.texplan_cache = {}  # folder -> texture plan rows (선택 시 지연 계산)
        self.worker = None
        self._busy = False
        self._initial_refreshing = True
        self._pending_refresh = False
        self.focus_label = focus_data_asset_label(self.cfg.get("pcg_focus_data_assets"))
        root.title("PCG ST9 → SK 전환 준비 보드")
        root.geometry("1320x820")
        self._build_ui()
        self._set_busy(True)
        self.status_var.set("초기 검사 중...")
        self.root.after(0, self._start_initial_refresh)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        intro = ttk.Label(
            self.root, padding=(8, 6, 8, 0), foreground="#444",
            text="PCG에서 쓰는 ST9 나무를 SK_(나나이트+논마스크+VT)로 바꾸는 준비 보드입니다. "
                 "행을 클릭하면 아래에 '다음에 뭘 하면 되는지'가 나옵니다. "
                 "①~③은 실행 전 대상·변경·백업 범위를 확인합니다.",
        )
        intro.pack(fill="x")

        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="나무 루트:").pack(side="left")
        self.root_var = tk.StringVar(value=self.cfg["tree_root"])
        ttk.Entry(top, textvariable=self.root_var, width=62).pack(side="left", padx=4)
        ttk.Button(top, text="...", width=3, command=self.pick_root).pack(side="left")
        btn_refresh = ttk.Button(top, text="🔍 다시 검사 (자산 수정 없음)", command=self.refresh)
        btn_refresh.pack(side="left", padx=6)
        Tooltip(btn_refresh, "아무것도 수정하지 않습니다.\n"
                             "폴더마다 SK SPM / M_ 이름 / 잎 메시 blend / 텍스처 6장 "
                             "상태를 다시 읽어서 표를 갱신합니다.")
        ttk.Button(top, text="전체 선택", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(top, text="전체 해제", command=lambda: self._set_all(False)).pack(side="left", padx=4)

        src = ttk.Frame(self.root, padding=(6, 0, 6, 3))
        src.pack(fill="x")
        ttk.Label(src, text=f"{self.focus_label} PCG + 배치 레벨 대상:").pack(side="left")
        btn_live = ttk.Button(src, text="Unreal에서 읽기", command=self.refresh_pcg_targets)
        btn_live.pack(side="left", padx=(6, 0))
        Tooltip(btn_live, "언리얼 에디터가 켜져 있을 때 사용.\n"
                          f"{self.focus_label} 활성 항목과 설정된 작업 레벨에 직접 배치된 ST9 메시를 읽습니다.\n"
                          "현재 설정 레벨: /Game/Level/Cliff_final_01")
        btn_saved = ttk.Button(src, text="저장된 리포트에서 읽기", command=self.import_saved_pcg_report)
        btn_saved.pack(side="left", padx=(6, 0))
        Tooltip(btn_saved, "언리얼 에디터가 꺼져 있을 때 사용.\n"
                           "이전에 저장해 둔 PCG 덤프 파일에서 메시 목록을 읽어 옵니다.")
        self.use_pcg_targets_var = tk.BooleanVar(value=TARGETS_PATH.exists())
        chk_pcg = ttk.Checkbutton(src, text=f"{self.focus_label}/Cliff 대상만 보기",
                                  variable=self.use_pcg_targets_var, command=self.refresh)
        chk_pcg.pack(side="left", padx=10)
        Tooltip(chk_pcg, f"켜면: {self.focus_label}의 Weight>0 항목 또는 Cliff_final_01 직접 배치와\n"
                         "매칭되는 나무 폴더만 표에 나옵니다.\n"
                         "끄면: 루트 아래 모든 나무 폴더가 나옵니다.")
        self.targets_info_var = tk.StringVar(value="")
        ttk.Label(src, textvariable=self.targets_info_var, foreground="#666").pack(side="left", padx=8)

        legend = ttk.Frame(self.root, padding=(6, 0, 6, 3))
        legend.pack(fill="x")
        ttk.Label(legend, text="행 색:").pack(side="left")
        for text, tag in (
            (f"{self.focus_label} PCG", "target_pcg"),
            ("Cliff 직접 배치", "target_level"),
            ("PCG + 직접 배치", "target_both"),
        ):
            tk.Label(
                legend, text=f"  {text}  ",
                background=TARGET_ROW_COLORS[tag], relief="solid", borderwidth=1,
            ).pack(side="left", padx=(5, 0))

        actions = ttk.Frame(self.root, padding=6)
        actions.pack(fill="x")
        self.btn_prepare = ttk.Button(actions, text="① 실행 — SK 만들기 + M_ 이름 붙이기 (선택 항목)",
                                      command=self.start_prepare)
        self.btn_prepare.pack(side="left")
        Tooltip(self.btn_prepare, "체크된 행 중 ①이 필요한 항목에만:\n"
                                  "· SK SPM이 없으면 → 원본 SPM을 복사해서 SK_이름.spm 생성\n"
                                  "· 머티리얼 이름 앞에 M_ 을 붙임 (send2ue 임포트 규칙 때문)\n"
                                  "수정 전 원본은 각 폴더의 _spm_backups\\ 에 백업됩니다.\n"
                                  "문제가 있는 항목(중복 매칭·원본 불명·기본 이름 머티리얼)은\n"
                                  "자동으로 건너뛰고 표와 로그에 이유를 표시합니다.")
        self.force_var = tk.BooleanVar(value=False)
        chk_force = ttk.Checkbutton(actions, text="⚠ 문제 표시된 항목도 적용", variable=self.force_var)
        chk_force.pack(side="left", padx=10)
        Tooltip(chk_force, "기본은 끔(안전). 켜면:\n"
                           "· '중복 매칭' 경고가 있어도 그대로 적용\n"
                           "· 'Material 2' 같은 기본 이름도 M_Material 2 로 강제 변경\n"
                           "백업은 똑같이 남습니다.\n"
                           "원본 SPM을 아예 못 찾은 항목은 켜도 처리할 수 없습니다.")
        btn_open = ttk.Button(actions, text="선택 폴더 열기", command=self.open_selected_folder)
        btn_open.pack(side="left", padx=10)
        Tooltip(btn_open, "표에서 클릭한 행의 나무 폴더를 탐색기로 엽니다.")
        btn_copy = ttk.Button(
            actions, text="선택 SPM 경로 복사", command=self.copy_selected_paths
        )
        btn_copy.pack(side="left")
        Tooltip(btn_copy, "선택한 행의 SPM 전체 경로를 복사합니다. Everything에 바로 붙여넣을 수 있습니다.")
        self.status_var = tk.StringVar(value="대기")
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        actions2 = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        actions2.pack(fill="x")
        self.btn_step2 = ttk.Button(actions2, text="② 실행 — 잎 메시 blend 만들기 (선택 항목)",
                                    command=self.start_step2)
        self.btn_step2.pack(side="left")
        Tooltip(self.btn_step2, "체크된 행의 최종/Cluster SPM을 따라 원본 잎 아틀라스를 찾고\n"
                                "아틀라스 리프 제너레이터를 돌립니다 (묶음당 수십초~수분):\n"
                                "· Cluster 결과 TGA가 아니라 그 Cluster SPM 내부 원본을 사용\n"
                                "· 같은 원본 아틀라스를 여러 SPM이 쓰면 한 번만 생성\n"
                                "· 알베도+알파의 모든 알파 아일랜드를 잎 메시로 생성\n"
                                "· Quality=Low, Plate=One Plate 고정\n"
                                "· atlas 폴더에 M_이름.blend 저장 (기존 파일은 안 건드림)\n"
                                "알베도/알파 원본을 못 찾은 묶음은 이유를 표시하고 건너뜁니다.\n"
                                "메시를 눈으로 확인/정리하려면 저장된 blend를 열면 됩니다.")
        self.spm_push_var = tk.BooleanVar(value=False)
        chk_spm = ttk.Checkbutton(
            actions2, text="최종 SK에 잎 메시 머티리얼 생성",
            variable=self.spm_push_var)
        chk_spm.pack(side="left", padx=8)
        Tooltip(chk_spm, "켜면 ② 직후 Material_v8과 FBX/XML Mesh 자산을 최종 SK SPM에 등록합니다.\n"
                         "Cluster SPM은 원본 아틀라스 추적에만 쓰며 수정/개수 계산에서 제외합니다.\n"
                         "Leaf Mesh Generator의 Material/Mesh 슬롯에는 자동 연결하지 않습니다.\n"
                         "SPM 파일이 수정되므로, 메시를 먼저 눈으로 확인하고 싶으면 끄고\n"
                         "blend를 열어 본 뒤 애드온에서 직접 반영하세요.")
        self.btn_step3 = ttk.Button(actions2, text="③ 실행 — 연결 텍스처 만들기 (선택 항목)",
                                    command=self.start_step3)
        self.btn_step3.pack(side="left", padx=10)
        Tooltip(self.btn_step3, "체크된 행의 표시된 SpeedTree Generator에 연결된 모든 텍스처 세트를 처리합니다.\n"
                                "각 세트는 color/normal/extra/height/opacity/subsurface 6장을 4K 이내로 완성합니다 (세트당 ~1분):\n"
                                "· 숨김 Generator와 숨김 부모 아래 항목은 생성 대상에서 제외\n"
                                "· *_yellow / *_green처럼 같은 원본을 공유하는 파생 슬롯은 한 세트로 통합\n"
                                "· 아틀라스뿐 아니라 bark/stem/surface 등 표시 Generator의 연결 텍스처도 모두 대상\n"
                                "· 생성 후 /Game/Textures의 같은 T_ 이름으로 Unreal에 자동 동기화\n"
                                "· 내용 MD5가 같으면 checkout/import/save하지 않고, 신규만 Perforce add\n"
                                "· 머티리얼 M_ 이름은 유지하고 출력 텍스처만 T_ 이름으로 저장\n"
                                "· SBS의 T_ 그래프를 렌더하고, 기존 M_ 그래프는 T_로 안전하게 이름 변경\n"
                                "· 없으면: 원본 텍스처를 자동 매핑해 렌더하고, 나중에 Designer에서\n"
                                "  관리할 수 있게 T_ 그래프를 SBS에 새로 넣습니다 (수정 전 백업 저장)\n"
                                "AO 없으면 height에서 Designer HBAO 생성, SDF=0, 노멀 방식은 원본 출처로 자동 판정.\n"
                                "기존 T_ 6장은 렌더 전에 _pcgtex_backups\\ 에 백업하고,\n"
                                "새 T_ 렌더가 성공하면 대응하는 기존 M_ 출력은 삭제합니다.")
        cols = ("pcg", "step1", "step2", "step3", "next")
        self.tree = ttk.Treeview(self.root, columns=cols, show="tree headings", height=16)
        self.tree.heading("#0", text="나무 폴더 (첫 클릭=이 행만 활성 · Ctrl+C=SPM 경로)")
        self.tree.column("#0", width=250, anchor="w")
        headers = {
            "pcg": ("PCG/레벨 사용", 145),
            "step1": ("① SK + M_ 이름", 300),
            "step2": ("② 잎 메시 (Blender)", 150),
            "step3": ("③ 텍스처 (Substance)", 180),
            "next": ("다음 할 일", 300),
        }
        for key, (label, width) in headers.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        for tag, color in TARGET_ROW_COLORS.items():
            self.tree.tag_configure(tag, background=color)
        self.tree.pack(fill="both", expand=True, padx=6, pady=(0, 2))
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Control-c>", self.copy_selected_paths, add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_details())
        header_tip = ("컬럼 = 작업 순서입니다.\n"
                      "PCG/레벨 사용: 각 위치에서 사용하는 메시 이름 개수\n"
                      "①: SK SPM 존재 + 머티리얼 M_ 이름 (이 도구가 자동 처리)\n"
                      "②: 잎 지오메트리 blend — 헤드리스 Blender 자동 생성\n"
                      "③: 사용 머티리얼별 T_ 텍스처 6장 — sbsrender 자동 생성")
        Tooltip(self.tree, header_tip, wrap=460)

        details = ttk.LabelFrame(self.root, text="선택한 폴더의 할 일 (위 표에서 행을 클릭)", padding=4)
        details.pack(fill="both", padx=6, pady=(0, 4))
        det_wrap = ttk.Frame(details)
        det_wrap.pack(fill="both", expand=True)
        self.text = tk.Text(det_wrap, height=14, wrap="none", state="disabled",
                            font=("Malgun Gothic", 9))
        det_scroll = ttk.Scrollbar(det_wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=det_scroll.set)
        det_scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        logf = ttk.LabelFrame(self.root, text="로그", padding=4)
        logf.pack(fill="x", padx=6, pady=(0, 6))
        self.log_text = tk.Text(logf, height=6, wrap="none", state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def log(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def pick_root(self):
        path = filedialog.askdirectory(initialdir=self.root_var.get())
        if path:
            self.root_var.set(path)

    def _set_all(self, checked):
        self.checked_rows.set_all(checked)
        self._update_step3_button()

    def _redraw_checked_row(self, iid, entry):
        mark = CHECK_ON if entry["checked"] else CHECK_OFF
        self.tree.item(iid, text=f"{mark} {entry['item']['name']}")

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "tree":
            return
        iid = self.tree.identify_row(event.y)
        self.checked_rows.click(iid)
        self._update_step3_button()

    def copy_selected_paths(self, _event=None):
        count = copy_selected_row_paths(
            self.root,
            self.tree,
            self.items,
            lambda entry: spm_paths_for_item(entry["item"]),
        )
        if count:
            self.status_var.set(f"SPM 경로 복사 완료 · {count}개")
        else:
            self.status_var.set("복사할 SPM이 있는 행을 먼저 클릭하세요")
        return "break"

    # ------------------------------------------------------------------ scan
    def _start_initial_refresh(self):
        """Run only the first read-only audit off the Tk main thread."""
        self._initial_refreshing = True
        self._set_busy(True)
        self.status_var.set("초기 검사 중...")
        self.cfg["tree_root"] = self.root_var.get()
        save_config(self.cfg)
        cfg = dict(self.cfg)
        use_pcg_targets = bool(self.use_pcg_targets_var.get())

        def worker():
            report = None
            error = None
            try:
                pcg_targets = load_pcg_targets() if use_pcg_targets else None
                report = make_report(cfg, pcg_targets=pcg_targets)
                save_spm_analysis_cache()
            except Exception as exc:
                error = exc
            self.root.after(
                0,
                lambda result=report, failure=error:
                    self._initial_refresh_done(result, failure),
            )

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _initial_refresh_done(self, report, error=None):
        """Apply the initial worker result on the Tk main thread."""
        self.worker = None
        self._initial_refreshing = False
        if error is not None:
            messagebox.showerror("검사 실패", str(error))
            self.status_var.set("검사 실패")
            self._set_busy(False)
        else:
            self.report = report
            self.texplan_cache.clear()
            self.populate()
            self._update_summary()
            self._set_busy(False)
        # A refresh requested mid-initial-scan (e.g. the target refresh
        # buttons finishing first) must not be dropped: the initial worker
        # captured the old targets, so run the queued refresh now.
        if getattr(self, "_pending_refresh", False):
            self._pending_refresh = False
            self.refresh()

    def refresh(self):
        if getattr(self, "_initial_refreshing", False):
            self._pending_refresh = True
            self.status_var.set("초기 검사 중... (끝나면 자동으로 다시 검사)")
            return
        self.cfg["tree_root"] = self.root_var.get()
        save_config(self.cfg)
        self.status_var.set("검사 중...")
        self.root.update_idletasks()
        try:
            pcg_targets = load_pcg_targets() if self.use_pcg_targets_var.get() else None
            self.report = make_report(self.cfg, pcg_targets=pcg_targets)
            save_spm_analysis_cache()
        except Exception as exc:
            messagebox.showerror("검사 실패", str(exc))
            self.status_var.set("검사 실패")
            return
        self.texplan_cache.clear()
        self.populate()
        self._update_summary()

    def _update_summary(self):
        items = self.report["items"]
        n = len(items)
        done = sum(1 for i in items if i["status"] == "ready")
        need1 = sum(1 for i in items if i["status"] in ("needs_sk", "needs_m_prefix"))
        need23 = sum(1 for i in items if i["status"] == "needs_texture_work")
        review = sum(1 for i in items if i["status"] in ("needs_source_review", "needs_duplicate_review"))
        pcg = self.report.get("pcg_targets", {})
        if pcg.get("mesh_count"):
            head = f"{self.focus_label}/Cliff에서 쓰는 나무 폴더 {n}개"
            source_time = f" · {pcg['generated_at']}" if pcg.get("generated_at") else ""
            level_reports = pcg.get("levels") or []
            level_names = [Path(row.get("level", "")).name for row in level_reports if row.get("level")]
            level_label = ", ".join(level_names) or "작업 레벨"
            overlap = pcg.get("pcg_level_overlap_mesh_count", 0)
            overlap_text = f" · 양쪽 중복 {overlap}개" if overlap else ""
            self.targets_info_var.set(
                f"고유 대상 {pcg['mesh_count']}개 ({self.focus_label} PCG {pcg.get('pcg_mesh_count', 0)} · "
                f"{level_label} 직접 배치 {pcg.get('level_mesh_count', 0)}{overlap_text}) 중 "
                f"{pcg['matched_mesh_count']}개가 나무 폴더에 매칭됨{source_time}"
            )
            unmatched = pcg.get("unmatched_mesh_names", [])
            if unmatched:
                self.log(f"⚠ 사용 메시 {len(unmatched)}개는 매칭되는 나무 폴더를 못 찾았습니다: "
                         + ", ".join(unmatched[:10])
                         + (" ..." if len(unmatched) > 10 else ""))
            dup = pcg.get("duplicate_mesh_matches", {})
            if dup:
                self.log(f"⚠ 사용 메시 {len(dup)}개는 폴더가 2개 이상 매칭됩니다 (표에 '중복' 표시): "
                         + ", ".join(dup))
        else:
            head = f"나무 폴더 {n}개 (대상 필터 없음)"
            self.targets_info_var.set("")
        self.status_var.set(
            f"{head} — ✅ 다 됨 {done} · ① 필요 {need1} · ②③ 남음 {need23} · ⚠ 확인 필요 {review}"
        )
        self.log(f"검사 완료: {head}. ✅ {done} / ① {need1} / ②③ {need23} / ⚠ {review}")

    @staticmethod
    def _target_source_text(item):
        pcg_count = len(item.get("pcg_mesh_names") or [])
        level_count = len(item.get("level_mesh_names") or [])
        parts = []
        if pcg_count:
            parts.append(f"PCG {pcg_count}")
        if level_count:
            parts.append(f"레벨 {level_count}")
        return " / ".join(parts) or "-"

    @staticmethod
    def _target_row_tag(item):
        in_pcg = bool(item.get("pcg_mesh_names"))
        in_level = bool(item.get("level_mesh_names"))
        if in_pcg and in_level:
            return "target_both"
        if in_pcg:
            return "target_pcg"
        if in_level:
            return "target_level"
        return ""

    def populate(self):
        old_checked = {iid: e["checked"] for iid, e in self.items.items()}
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        for item in self.report["items"]:
            iid = item["folder"]
            checked = old_checked.get(iid, True)
            self.items[iid] = {"item": item, "checked": checked}
            mark = CHECK_ON if checked else CHECK_OFF
            row_tag = self._target_row_tag(item)
            self.tree.insert(
                "", "end", iid=iid,
                text=f"{mark} {item['name']}",
                values=(
                    self._target_source_text(item),
                    self.step1_text(item),
                    self.step2_text(item),
                    self.step3_text(item),
                    item["actions"][0] if item["actions"] else "없음 — 준비 끝 ✓",
                ),
                tags=(row_tag,) if row_tag else (),
            )
        self.checked_rows.sync_after_reload()
        self._update_step3_button()

    # ---------------------------------------------------------- column texts
    def step1_text(self, item):
        if item.get("duplicate_target_mesh_names") or item.get("duplicate_pcg_target_mesh_names"):
            return "⚠ 중복 매칭 확인"
        statuses = item.get("target_spm_statuses") or []
        if statuses:
            n_src = sum(1 for s in statuses if s["status"] == "needs_source_review")
            n_sk = sum(1 for s in statuses if s["status"] == "needs_sk")
            missing = [m for s in statuses for m in s.get("materials_missing_m_prefix", [])]
            renames = [pair for s in statuses for pair in s.get("material_renames_needed", [])]
            if n_src:
                return f"⚠ 원본 못 찾음 {n_src}개"
            if n_sk:
                return f"SK 없음 {n_sk}개 → [① 실행]"
        else:
            if not item.get("sk_spms"):
                return "SK 없음 → [① 실행]" if item.get("source_spms") else "⚠ SPM 없음"
            missing = item.get("materials_missing_m_prefix", [])
            renames = item.get("material_renames_needed", [])
        normal, generic = split_generic(missing)
        common = [pair for pair in renames if str(pair[0]).lower().startswith("m_")]
        if common and not normal:
            return f"공용 머티리얼 {len(common)}개 이름 통일 → [① 실행]"
        if normal:
            return f"머티리얼 이름 {len(renames)}개 정리 → [① 실행]"
        if generic:
            return generic_material_summary(item)
        return "완료 ✓"

    def step2_text(self, item):
        sources = item.get("leaf_mesh_sources") or []
        if not sources:
            return "원본 잎 아틀라스 없음"
        have = sum(1 for source in sources if source.get("atlas_blends"))
        targets = {
            target.get("spm", "").lower()
            for source in sources for target in source.get("targets", [])
            if target.get("spm")
        }
        if have == len(sources):
            return f"잎 매쉬 {len(sources)}개 완료 ✓"
        remaining = len(sources) - have
        if have:
            return f"잎 매쉬 {have}/{len(sources)}개 완료 · {remaining}개 만들기"
        return f"잎 매쉬 {len(sources)}개 만들기 · 최종 SK {len(targets)}개"

    def step3_text(self, item):
        state = step3_item_state(item)
        if not state["sets"]:
            if state["connection_sets"]:
                return f"공유 텍스처 · 연결 {state['connection_sets']}세트 정리"
            if state["shared_sets"]:
                return f"공유 텍스처 {state['shared_sets']}세트 사용 ✓"
            return "-"
        if state["missing_sets"]:
            return (
                f"연결 텍스처 {state['sets']}세트 · "
                f"{state['complete_maps']}/{state['total_maps']}장 · "
                f"{state['missing_maps']}장 생성"
            )
        if state["connection_sets"]:
            return (
                f"연결 텍스처 {state['sets']}세트 · "
                f"{state['total_maps']}장 완료 · 연결 정리"
            )
        return (
            f"연결 텍스처 {state['sets']}세트 · "
            f"{state['total_maps']}장 완료 ✓"
        )

    # ------------------------------------------------------------- details
    def selected_item(self):
        sel = self.tree.selection()
        if not sel:
            return None
        entry = self.items.get(sel[0])
        return entry["item"] if entry else None

    def _texplan_rows(self, item):
        folder = item["folder"]
        if folder not in self.texplan_cache:
            try:
                mini = {"items": [item], "pcg_targets": self.report.get("pcg_targets", {})}
                plan = build_texture_plan_from_report(mini, "<board>")
                self.texplan_cache[folder] = plan.get("items", [])
            except Exception:
                self.texplan_cache[folder] = []
        return self.texplan_cache[folder]

    @staticmethod
    def _detail_texture_rows(item):
        """Build lightweight display rows without reopening SPM/SBS files.

        The full texture plan resolves provenance and is intentionally kept for
        the execution buttons.  A row selection must only display information
        already collected by the audit, otherwise every first click performs a
        fresh material-reference scan and makes the Treeview feel unresponsive.
        """
        rows = []
        for cluster in item.get("cluster_items") or []:
            row = dict(cluster)
            buckets = bucket_refs(cluster.get("source_refs") or [])
            row.update({
                "folder": item.get("folder", ""),
                "sbs_files": item.get("sbs_files") or [],
                "normal_convention": item.get("normal_convention", "unknown"),
            })
            for kind, refs in buckets.items():
                row.setdefault(f"source_{kind}", refs)
            # Color and opacity come from the exact SpeedTree material slots;
            # keep those authoritative instead of guessing from file names.
            if cluster.get("source_albedo"):
                row["source_albedo"] = cluster["source_albedo"]
            if cluster.get("source_alpha"):
                row["source_alpha"] = cluster["source_alpha"]
            rows.append(row)
        return rows

    def show_details(self):
        item = self.selected_item()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if item:
            self.text.insert("end", self._detail_text(item))
        self.text.configure(state="disabled")

    def _detail_text(self, item):
        L = []
        L.append(f"📁 {item['name']}   {item['folder']}")
        target_names = item.get("target_mesh_names") or item.get("pcg_target_mesh_names") or []
        pcg_names = item.get("pcg_mesh_names") or []
        level_names = item.get("level_mesh_names") or []
        placements = item.get("level_placements") or []
        if pcg_names:
            data_assets = [Path(path).name for path in item.get("pcg_data_assets") or []]
            data_asset_text = f" ({', '.join(data_assets)})" if data_assets else ""
            L.append(
                f"{self.focus_label} PCG에서 사용하는 메시 {len(pcg_names)}개{data_asset_text}: "
                f"{', '.join(pcg_names)}"
            )
        if level_names:
            level_paths = sorted({row.get("level", "") for row in placements if row.get("level")})
            level_label = ", ".join(Path(path).name for path in level_paths) or "작업 레벨"
            component_count = len(placements)
            instance_count = sum(int(row.get("instance_count", 1)) for row in placements)
            actor_count = len({row.get("actor_name") or row.get("actor") for row in placements})
            L.append(
                f"{level_label} 직접 배치 메시 {len(level_names)}개: {', '.join(level_names)} "
                f"(액터 {actor_count} · 컴포넌트 {component_count} · 인스턴스 {instance_count})"
            )
        if not target_names:
            L.append("대상 매칭 정보 없음 (전체 보기 모드이거나 PCG/작업 레벨에서 안 쓰는 폴더)")
        for dup in item.get("duplicate_target_mesh_names") or item.get("duplicate_pcg_target_mesh_names") or []:
            L.append(f"⚠ '{dup}' 은(는) 다른 폴더에도 매칭됩니다 — 어느 폴더가 진짜인지 먼저 정한 뒤 ①을 실행하세요.")
        L.append("")

        # ① SK + M_
        L.append("── ① SK SPM + M_ 머티리얼 이름 ──   (이 도구의 [① 실행] 버튼이 자동 처리)")
        statuses = item.get("target_spm_statuses") or []
        if statuses:
            for s in statuses:
                mesh = s["mesh_name"]
                if s["status"] == "needs_source_review":
                    L.append(f"  · {mesh}: ⚠ 이 폴더에서 원본 SPM을 못 찾음 — 파일 이름을 확인해 주세요.")
                elif not s.get("sk_spm"):
                    src = Path(s["source_spm"]).name if s.get("source_spm") else "?"
                    L.append(f"  · {mesh}: SK 없음 → [① 실행]이 {src} 을(를) 복사해 SK_{src} 생성")
                else:
                    miss = s.get("materials_missing_m_prefix", [])
                    common = [pair for pair in s.get("material_renames_needed", [])
                              if str(pair[0]).lower().startswith("m_")]
                    if miss or common:
                        normal, generic = split_generic(miss)
                        parts = []
                        if normal:
                            parts.append(f"M_ 필요 {len(normal)}개: " + ", ".join(normal))
                        if generic:
                            parts.append(f"⚠ 기본 이름 {len(generic)}개(SpeedTree에서 이름 지은 뒤 ①): "
                                         + ", ".join(generic))
                        if common:
                            parts.append("공용 이름 통일: "
                                         + ", ".join(f"{old}→{new}" for old, new in common))
                        L.append(f"  · {mesh}: {Path(s['sk_spm']).name} ✓ / " + " / ".join(parts))
                    else:
                        L.append(f"  · {mesh}: {Path(s['sk_spm']).name} ✓  머티리얼 이름 완료 ✓")
        else:
            if item.get("sk_spms"):
                L.append("  · SK SPM: " + ", ".join(Path(p).name for p in item["sk_spms"]) + " ✓")
            elif item.get("source_spms"):
                src = Path(item["source_spms"][0]).name
                L.append(f"  · SK 없음 → [① 실행]이 {src} 을(를) 복사해 SK_{src} 생성")
            else:
                L.append("  · ⚠ 이 폴더에 SPM이 없습니다.")
            miss = item.get("materials_missing_m_prefix", [])
            if miss:
                normal, generic = split_generic(miss)
                if normal:
                    L.append(f"  · M_ 필요한 머티리얼 {len(normal)}개: " + ", ".join(normal))
                if generic:
                    L.append(f"  · ⚠ 기본 이름 머티리얼 {len(generic)}개 (SpeedTree에서 이름 지은 뒤 ①): "
                             + ", ".join(generic))
            common = [pair for pair in item.get("material_renames_needed", [])
                      if str(pair[0]).lower().startswith("m_")]
            if common:
                L.append("  · 공용 머티리얼 이름 통일: "
                         + ", ".join(f"{old} → {new}" for old, new in common))
        generic_issues = generic_material_issues(item)
        if generic_issues:
            L.append("  · ⚠ 기본 이름 문제 위치 (이 SPM을 SpeedTree에서 열어 이름 수정):")
            grouped = {}
            for issue in generic_issues:
                key = (issue["spm"], issue["mesh_name"])
                grouped.setdefault(key, []).append(issue["material"])
            for (spm, mesh), names in grouped.items():
                mesh_note = f" / 사용 메시: {mesh}" if mesh else ""
                L.append(f"      SPM: {spm or '미상'}{mesh_note}")
                L.append(f"      머티리얼: {', '.join(names)}")
        L.append("  왜? SK SPM은 나나이트 전환용 복사본, M_ 이름은 send2ue 임포트 규칙입니다.")
        L.append("")

        # ② 원본 잎 아틀라스 / ③ 텍스처 계열
        clusters = item.get("cluster_items", [])
        rows = self._detail_texture_rows(item) if clusters else []
        L.append("── ② 잎 메시 만들기 ──   ([② 실행] 버튼이 자동 처리 · Blender 아틀라스 리프 제너레이터)")
        leaf_sources = item.get("leaf_mesh_sources") or []
        if not leaf_sources:
            L.append("  · 최종/Cluster SPM에서 메시화할 원본 잎 아틀라스를 찾지 못했습니다.")
        for source in leaf_sources:
            kinds = source.get("source_kinds") or [source.get("source_kind", "direct")]
            route = "Cluster SPM 내부 원본" if "cluster" in kinds else "최종 SPM 직접 원본"
            blends = source.get("atlas_blends") or []
            state = f"{Path(blends[0]).name} ✓ 있음" if blends else "blend 없음 → [② 실행]"
            L.append(f"  · {source['source_family']}: {state}  ({route})")
            L.append(f"      Albedo: {source['albedo']}")
            L.append(f"      Alpha:  {source['alpha']}")
            L.append(f"      생성 이름: {source['atlas_base']} / Quality: Low / Plate Mode: One Plate")
            for trace in source.get("trace_sources", []):
                materials = ", ".join(trace.get("material_names") or []) or "머티리얼 미상"
                L.append(f"      추적 근거(수정 안 함): {trace.get('spm', '')}  /  원본 머티리얼: {materials}")
            for target in source.get("targets", []):
                materials = ", ".join(target.get("material_names") or [])
                material_note = f"  /  직접 원본 머티리얼: {materials}" if materials else ""
                L.append(f"      최종 SK 적용 대상: {target.get('spm', '')}{material_note}")
            if len(source.get("targets", [])) > 1:
                L.append("      ↳ 같은 원본 아틀라스이므로 메시 생성은 한 번, 모든 최종 SK에 함께 반영")
        if leaf_sources:
            L.append("  왜? Cluster 결과 TGA가 아니라, 그것을 만드는 원본 잎 아틀라스의 알파를 실제 지오메트리로 바꾸기 위해서입니다.")
        L.append("")

        L.append("── ③ 사용 머티리얼별 텍스처 6장 ──   ([③ 실행] 버튼이 자동 처리 · Substance Designer)")
        if not clusters:
            L.append("  · 해당 없음.")
        seen_bases = set()
        for row in rows:
            base = row["atlas_base"]
            if base in seen_bases:
                continue
            seen_bases.add(base)
            if row.get("shared_from"):
                L.append(f"  · {base}: 다른 폴더({row['shared_from']})에서 관리")
                continue
            texture_base = row.get("texture_base") or base
            missing = row.get("missing_export_maps") or []
            if not missing:
                if row.get("connection_update_needed"):
                    aliases = ", ".join(row.get("connection_materials") or [])
                    L.append(
                        f"  · {base} → {texture_base}: 6장 모두 있음 · "
                        f"SpeedTree 연결 정리 필요 ({aliases})")
                else:
                    L.append(f"  · {base} → {texture_base}: 6장 모두 있음 ✓")
                continue
            L.append(f"  · {base} → {texture_base}: 누락 → {', '.join(missing)} → [③ 실행]이 자동 렌더")
            if not row.get("m_graph"):
                try:
                    selected = sbs_auto.select_source_set(row, require_alpha=False)
                    L.append(f"      SpeedTree 원본 참조: {selected['label']}")
                    if row.get("material_spms"):
                        L.append("      연결 근거 SPM: "
                                 + ", ".join(Path(path).name for path in row["material_spms"]))
                except Exception as exc:
                    legacy = row.get("legacy_export_maps") or {}
                    if all(legacy.get(name) for name in sbs_auto.RENDER_MAPS):
                        L.append("      기존 M_ 출력은 SBS의 Unreal용 출력이므로 원본 입력에서 제외")
                    L.append(f"      ⚠ {exc}")
            sbs = ([row.get("m_graph_sbs")] if row.get("m_graph_sbs")
                   else row.get("sbs_files") or [])
            L.append(f"      SBS: {Path(sbs[0]).name if sbs else '⚠ 없음 — [③ 실행]이 텍스처만 생성'}"
                     f"   저장 위치: {row.get('texture_dir', '')}")
            conv = row.get("normal_convention", "unknown")
            if conv == "OpenGL":
                L.append("      노멀: OpenGL=true (TCom/Megascan 원본)")
            elif conv == "DirectX":
                L.append("      노멀: OpenGL=false (Substance 머티리얼 원본)")
            else:
                L.append("      노멀: ⚠ 원본 출처 불명 — TCom/Megascan이면 OpenGL, sbsar이면 DirectX")
            L.append("      AO: 원본 AO 없으면 height로 HBAO 만들어 연결 / SDF: 연결만 하고 값 0")
        return "\n".join(L)

    # ------------------------------------------------------------- ① 실행
    def _build_prepare_rows(self):
        """체크된 행에서 ①이 필요한 작업 목록을 만들고, 문제(blocker)를 분류한다."""
        rows = []
        for iid, entry in self.items.items():
            if not entry["checked"]:
                continue
            item = entry["item"]
            duplicates = set(item.get("duplicate_target_mesh_names") or item.get("duplicate_pcg_target_mesh_names", []))
            statuses = item.get("target_spm_statuses") or []
            jobs = []  # (mesh_name or None)
            if statuses:
                for s in statuses:
                    if s["status"] in ("needs_sk", "needs_m_prefix"):
                        jobs.append(s["mesh_name"])
                    elif s["status"] == "needs_source_review":
                        rows.append({
                            "item": item, "mesh": s["mesh_name"], "preview": None,
                            "blockers": ["source_review"], "generic": [],
                        })
            else:
                if (not item.get("sk_spms") or item.get("materials_missing_m_prefix")
                        or item.get("material_renames_needed")):
                    if item.get("source_spms") or item.get("sk_spms"):
                        jobs.append(None)
                    else:
                        continue
            for mesh in jobs:
                blockers = []
                if mesh and mesh in duplicates:
                    blockers.append("duplicate")
                try:
                    preview = prepare_sk(item["folder"], [mesh] if mesh else None, dry_run=True)
                except Exception as exc:
                    rows.append({"item": item, "mesh": mesh, "preview": None,
                                 "blockers": [f"오류: {exc}"], "generic": []})
                    continue
                targets = preview.get("targets") or [preview]
                target = targets[0] if targets else {}
                if target.get("status") == "skipped":
                    blockers.append(f"자동 처리 불가: {target.get('reason', '?')}")
                patch = target.get("patch") or {}
                renames = patch.get("renames", [])
                normal, generic = split_generic([old for old, _new in renames])
                # 기본 이름 머티리얼은 이름을 바꾸지 않고 남겨둔다(부분 적용).
                # SK 생성도, 정상 이름 변경도 없다면 이 항목은 ①이 할 일이 없다.
                if not target.get("would_create") and not normal and generic:
                    blockers.append("generic_only")
                rows.append({"item": item, "mesh": mesh, "preview": target,
                             "blockers": blockers, "generic": generic,
                             "normal_renames": normal})
        return rows

    @staticmethod
    def _blocker_text(code):
        return BLOCKER_TEXT.get(code, code)

    def start_prepare(self):
        if not self.report:
            self.refresh()
        checked = [e for e in self.items.values() if e["checked"]]
        if not checked:
            messagebox.showinfo("① 실행", "체크된 행이 없습니다. 폴더 이름 왼쪽 ☑를 클릭해 선택하세요.")
            return
        self.status_var.set("① 준비 상태 확인 중...")
        self.root.update_idletasks()
        rows = self._build_prepare_rows()
        if not rows:
            messagebox.showinfo("① 실행", "체크된 항목 중 ①이 필요한 것이 없습니다.\n(모두 SK와 M_ 이름이 이미 완료된 상태)")
            self.status_var.set("대기")
            return
        force = bool(self.force_var.get())
        doable = []
        skipped = []
        for row in rows:
            hard_block = row["preview"] is None or any(
                b.startswith("자동 처리 불가") or b.startswith("오류")
                or b in ("source_review", "generic_only")
                for b in row["blockers"]
            )
            if hard_block or (row["blockers"] and not force):
                skipped.append(row)
            else:
                # force가 아니면 기본 이름 머티리얼은 이름을 바꾸지 않고 남겨둔다.
                row["exclude"] = [] if force else list(row["generic"])
                doable.append(row)
        n_create = sum(1 for r in doable if r["preview"].get("would_create"))
        n_rename = sum(
            len((r["preview"].get("patch") or {}).get("renames", [])) - len(r["exclude"])
            for r in doable
        )
        n_generic_kept = sum(len(r["exclude"]) for r in doable)
        msg = ["① SK 만들기 + 머티리얼 이름 정리\n", "지금 실행하면:"]
        msg.append(f" · SK SPM 새로 만들기: {n_create}개 (원본 SPM을 SK_이름.spm 으로 복사)")
        msg.append(f" · 머티리얼 M_ / 공용 이름 정리: {n_rename}개")
        if n_generic_kept:
            msg.append(f" · 'Material 2' 같은 기본 이름 {n_generic_kept}개는 그대로 둡니다 "
                       "(SpeedTree에서 이름 지은 뒤 다시 ① 실행)")
        msg.append(" · 수정 전 원본은 각 폴더의 _spm_backups\\ 에 백업됩니다.")
        if skipped:
            msg.append(f"\n건너뛰는 항목 {len(skipped)}개 (로그에 이유 표시):")
            for row in skipped[:8]:
                label = row["mesh"] or row["item"]["name"]
                msg.append(f" · {label}: {self._blocker_text(row['blockers'][0])}")
            if len(skipped) > 8:
                msg.append(f" · ... 외 {len(skipped) - 8}개")
        if not doable:
            msg.append("\n적용할 수 있는 항목이 없습니다.")
            messagebox.showinfo("① 실행", "\n".join(msg))
            self.status_var.set("대기")
            for row in skipped:
                label = row["mesh"] or row["item"]["name"]
                self.log(f"[① 건너뜀] {label}: " + "; ".join(self._blocker_text(b) for b in row["blockers"]))
            return
        msg.append("\n계속할까요?")
        if not messagebox.askyesno("① 실행", "\n".join(msg)):
            self.status_var.set("대기")
            return
        for row in skipped:
            label = row["mesh"] or row["item"]["name"]
            self.log(f"[① 건너뜀] {label}: " + "; ".join(self._blocker_text(b) for b in row["blockers"]))
            self.tree.set(row["item"]["folder"], "step1", "⚠ 건너뜀 (로그 참조)")
        self._set_busy(True)
        self.status_var.set(f"① 적용 중... ({len(doable)}개)")
        self.worker = threading.Thread(target=self._run_prepare, args=(doable,), daemon=True)
        self.worker.start()

    def _run_prepare(self, rows):
        done = 0
        failed = 0
        for row in rows:
            item = row["item"]
            mesh = row["mesh"]
            label = mesh or item["name"]
            try:
                result = prepare_sk(item["folder"], [mesh] if mesh else None,
                                    dry_run=False, exclude_materials=row.get("exclude"))
                targets = result.get("targets") or [result]
                for target in targets:
                    if target.get("created"):
                        self._ui(lambda t=target: self.log(f"[① SK 생성] {Path(t['created']).name}"))
                    patch = target.get("patch") or {}
                    for old, new in patch.get("renames", []):
                        self._ui(lambda o=old, n=new, lb=label: self.log(
                            f"[① 머티리얼 이름] {lb}: {o} → {n}"))
                    if patch.get("backup"):
                        self._ui(lambda p=patch: self.log(f"    백업: {p['backup']}"))
                done += 1
                self._ui(lambda i=item: self.tree.set(i["folder"], "step1", "완료 ✓ (방금 적용)"))
            except Exception as exc:
                failed += 1
                self._ui(lambda lb=label, e=exc: self.log(f"[① 실패] {lb}: {e}"))
        self._ui(lambda: self._prepare_finished(done, failed))

    def _ui(self, fn):
        self.root.after(0, fn)

    def _set_busy(self, busy):
        self._busy = bool(busy)
        state = "disabled" if busy else "normal"
        for btn in (self.btn_prepare, self.btn_step2):
            btn.configure(state=state)
        self._update_step3_button()

    def _update_step3_button(self):
        """Keep the action truthful after scans and checkbox changes."""
        if not hasattr(self, "btn_step3"):
            return
        state = step3_selection_state(getattr(self, "items", {}))
        self.btn_step3.configure(
            text=state["text"],
            state="disabled" if getattr(self, "_busy", False) else state["state"],
        )

    def _prepare_finished(self, done, failed):
        self._set_busy(False)
        self.log(f"① 완료: 처리 {done}개, 실패 {failed}개. 표를 다시 검사합니다.")
        self.refresh()

    # ------------------------------------------------------------- ②③ 공용
    def _checked_texplan_rows(self):
        """체크된 행의 (item, texplan row) 목록. 같은 atlas_base는 폴더당 1번."""
        result = []
        for entry in self.items.values():
            if not entry["checked"]:
                continue
            item = entry["item"]
            if not item.get("cluster_items"):
                continue
            seen = set()
            for row in self._texplan_rows(item):
                base = row.get("atlas_base", "")
                if not base or base.lower() in seen:
                    continue
                seen.add(base.lower())
                result.append((item, row))
        return result

    def _graph_albedo_alpha(self, row):
        """SBS의 T_ 그래프(또는 레거시 M_) 원본 연결을 그대로 쓴다."""
        if not row.get("m_graph") or not row.get("m_graph_sbs"):
            return None, None
        try:
            info = sbs_auto.parse_m_graph(row["m_graph_sbs"], row["m_graph"])
        except Exception:
            return None, None

        def usable(slot):
            path = info["inputs"].get(slot)
            if not path:
                return None
            path = Path(path)
            if not path.exists() or "neutral" in path.name.lower():
                return None
            return path

        return usable("Base_Color"), usable("Opacity")

    # ------------------------------------------------------------- ② 실행
    def _step2_jobs(self):
        grouped, skipped = {}, []
        atlas_root = Path(self.cfg["atlas_root"])
        for entry in self.items.values():
            if not entry["checked"]:
                continue
            item = entry["item"]
            for source in item.get("leaf_mesh_sources") or []:
                base = source.get("atlas_base", "")
                if source.get("atlas_blends"):
                    continue
                albedo = Path(source.get("albedo", ""))
                alpha = Path(source.get("alpha", ""))
                if not albedo.exists() or not alpha.exists():
                    missing = []
                    if not albedo.exists():
                        missing.append("알베도")
                    if not alpha.exists():
                        missing.append("알파")
                    skipped.append((item, base, f"{'/'.join(missing)} 원본 파일 없음"))
                    continue
                key = (str(albedo.resolve()).lower(), str(alpha.resolve()).lower())
                job = grouped.get(key)
                if job is None:
                    job = {
                        "item": item, "items": [item], "base": base,
                        "albedo": albedo, "alpha": alpha,
                        "blend_out": atlas_root / f"{base}.blend",
                        "target_spms": [], "target_details": [],
                    }
                    grouped[key] = job
                elif item not in job["items"]:
                    job["items"].append(item)
                known_spms = {path.lower() for path in job["target_spms"]}
                for target in source.get("targets", []):
                    spm = target.get("spm")
                    if spm and spm.lower() not in known_spms:
                        job["target_spms"].append(spm)
                        known_spms.add(spm.lower())
                    detail = (spm, tuple(target.get("material_names") or []))
                    if detail not in job["target_details"]:
                        job["target_details"].append(detail)
        return list(grouped.values()), skipped

    def start_step2(self):
        if not self.report:
            self.refresh()
        self.status_var.set("② 대상 확인 중...")
        self.root.update_idletasks()
        jobs, skipped = self._step2_jobs()
        push_spm = bool(self.spm_push_var.get())
        for item, base, reason in skipped:
            self.log(f"[② 건너뜀] {item['name']} / {base}: {reason}")
        if not jobs:
            messagebox.showinfo("② 실행", "체크된 항목 중 잎 메시 blend를 만들 것이 없습니다."
                                + (f"\n(건너뜀 {len(skipped)}개 — 로그 참조)" if skipped else ""))
            self.status_var.set("대기")
            return
        no_spm = [j for j in jobs if push_spm and not j["target_spms"]]
        msg = ["② 잎 메시 blend 만들기 (헤드리스 Blender)\n"]
        msg.append(f"만들 blend {len(jobs)}개 (Quality=Low, One Plate, 모든 알파 아일랜드):")
        for j in jobs[:10]:
            msg.append(
                f" · {j['base']}.blend  (알베도: {Path(j['albedo']).name} / "
                f"최종 SK: {len(j['target_spms'])}개)")
        if len(jobs) > 10:
            msg.append(f" · ... 외 {len(jobs) - 10}개")
        msg.append(f"저장 위치: {self.cfg['atlas_root']}")
        if push_spm:
            msg.append("\nMaterial_v8 + Mesh 자산을 최종 SK에 등록합니다.")
            msg.append("Cluster SPM은 수정하지 않으며 Leaf Mesh Generator에는 자동 연결하지 않습니다.")
            if no_spm:
                msg.append(f"⚠ 최종 SK가 없는 {len(no_spm)}개는 blend만 만듭니다.")
        if skipped:
            msg.append(f"\n건너뜀 {len(skipped)}개 (원본 못 찾음 — 로그 참조)")
        msg.append("\n묶음당 수십초~수분 걸립니다. 계속할까요?")
        if not messagebox.askyesno("② 실행", "\n".join(msg)):
            self.status_var.set("대기")
            return
        self._set_busy(True)
        self.worker = threading.Thread(target=self._run_step2, args=(jobs, push_spm), daemon=True)
        self.worker.start()

    def _run_step2(self, jobs, push_spm):
        from pcg_texture_common import REPORT_DIR
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        done = failed = 0
        total = len(jobs)
        for index, job in enumerate(jobs, 1):
            base = job["base"]
            self._ui(lambda i=index, b=base: self.status_var.set(f"② {i}/{total}: {b} 생성 중..."))
            self._ui(lambda it=job["item"]: self.tree.set(it["folder"], "step2", "만드는 중..."))
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = REPORT_DIR / f"atlas_job_{base}_{stamp}.json"
            cmd = atlas_blender_command(self.cfg.get("blender_exe", "")) + [
                "--albedo", str(job["albedo"]),
                "--alpha", str(job["alpha"]),
                "--material-name", base,
                "--blend-out", str(job["blend_out"]),
                "--report", str(report_path),
                "--quality", "SPEEDTREE_LOW",
                "--plate-mode", "SINGLE",
            ]
            if push_spm and job["target_spms"]:
                for spm in job["target_spms"]:
                    cmd += ["--spm", str(spm)]
                cmd.append("--build-spm")
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace",
                                        timeout=self.cfg.get("atlas_job_timeout", 1800),
                                        creationflags=0x08000000)
                data = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
                if result.returncode != 0 or data.get("status") != "ok":
                    raise RuntimeError(data.get("error") or (result.stderr or result.stdout)[-400:])
                register_blend_source_images(
                    job["blend_out"], (job["albedo"], job["alpha"]),
                    authoritative=True)
                save_spm_analysis_cache()
                done += 1
                spm_note = " + 최종 SK에 머티리얼·메시 자산 등록됨" if data.get("spm_built") else ""
                self._ui(lambda b=base, d=data, s=spm_note: self.log(
                    f"[② 완료] {b}.blend — 잎 메시 {d.get('meshes', '?')}개{s}"))
                if data.get("spm_backups"):
                    self._ui(lambda b=base, paths=data["spm_backups"]: self.log(
                        f"[② SPM 백업] {b}: {len(paths)}개 → {Path(paths[0]).parent}"))
            except Exception as exc:
                failed += 1
                self._ui(lambda b=base, e=exc: self.log(f"[② 실패] {b}: {e}"))
        self._ui(lambda: self._batch_finished("②", done, failed))

    # ------------------------------------------------------------- ③ 실행
    def _step3_jobs(self):
        jobs, skipped = [], []
        for item, row in self._checked_texplan_rows():
            base = row["atlas_base"]
            if row.get("shared_from"):
                continue  # 다른 폴더에서 관리 — 그쪽 행에서 처리
            try:
                job = build_texture_job(row)
                job["item"] = item
                job["size_log2"] = expected_job_size(job)
                expected_pixels = sbs_auto.size_log2_pixels(job["size_log2"])
                graph_needs_update = False
                if job.get("mode") == "render":
                    graph_needs_update = sbs_auto.managed_graph_resolution_state(
                        job["sbs"], job["graph"])["needs_update"]
                source_needs_repair = job_needs_source_repair(job)
                complete = complete_output_set(row, expected_pixels=expected_pixels)
                if complete and not graph_needs_update and not source_needs_repair:
                    continue
                jobs.append(job)
            except Exception as exc:
                skipped.append((item, base, str(exc)))
        return jobs, skipped

    def _step3_sync_files(self):
        """Collect complete selected sets, including rows that need no render."""
        files = []
        seen = set()
        for _item, row in self._checked_texplan_rows():
            texture_dir = row.get("texture_dir")
            texture_base = row.get("texture_base") or row.get("atlas_base")
            if not texture_dir or not texture_base:
                continue
            paths = output_paths(texture_dir, texture_base)
            selected = [paths[role] for role in sbs_auto.RENDER_MAPS]
            if not all(path.is_file() and path.stat().st_size > 0 for path in selected):
                continue
            for path in selected:
                key = os.path.normcase(os.path.abspath(str(path)))
                if key not in seen:
                    seen.add(key)
                    files.append(str(path))
        return files

    def start_step3(self):
        if not self.report:
            self.refresh()
        self.status_var.set("③ 대상 확인 중...")
        self.root.update_idletasks()
        jobs, skipped = self._step3_jobs()
        sync_files = self._step3_sync_files()
        for item, base, reason in skipped:
            self.log(f"[③ 건너뜀] {item['name']} / {base}: {reason}")
        checked_folders = sorted({
            entry["item"]["folder"] for entry in self.items.values()
            if entry["checked"] and entry["item"].get("sk_spms")
        })
        if not jobs:
            if not checked_folders and not sync_files:
                messagebox.showinfo("③ 실행", "체크된 항목 중 텍스처를 만들 것이 없습니다."
                                    + (f"\n(건너뜀 {len(skipped)}개 — 로그 참조)" if skipped else ""))
                self.status_var.set("대기")
                return
            if not messagebox.askyesno(
                    "③ 실행", "6장 출력은 이미 있습니다.\n"
                    "체크된 SK SPM의 머티리얼 슬롯을 정리하고 Unreal의 T_ 에셋도\n"
                    "내용 해시 기준으로 확인·동기화할까요?"):
                self.status_var.set("대기")
                return
            self._set_busy(True)
            self.worker = threading.Thread(
                target=self._run_step3,
                args=([], checked_folders, sync_files), daemon=True)
            self.worker.start()
            return
        n_render = sum(1 for j in jobs if j["mode"] == "render")
        n_insert = sum(1 for j in jobs if j["mode"] == "insert")
        n_nosbs = sum(1 for j in jobs if j["mode"] == "render_only")
        n_direct = sum(1 for j in jobs if j["mode"] == "direct")
        n_cluster_normalize = sum(
            1 for j in jobs if j.get("normalize_cluster"))
        msg = ["③ 연결 텍스처 세트 만들기 (sbsrender, 원본 비율·최대 축 4K)\n"]
        msg.append(f"렌더할 텍스처 세트 {len(jobs)}개 · 출력 {len(jobs) * len(sbs_auto.RENDER_MAPS)}장:")
        msg.append(f" · SBS의 T_ 그래프 설정 사용 (기존 M_은 T_로 이름 변경): {n_render}개")
        if n_insert:
            msg.append(f" · SBS에 T_ 그래프 새로 넣고 렌더: {n_insert}개 (수정 전 SBS 백업 저장)")
        if n_nosbs:
            msg.append(f" · SBS 파일이 없어 텍스처만 생성: {n_nosbs}개")
        if n_direct:
            msg.append(
                f" · 절차형 SBS 그래프 자체를 렌더: {n_direct}개"
                + (f" (Cluster_System 연결·정규화 {n_cluster_normalize}개)"
                   if n_cluster_normalize else ""))
        for j in jobs[:10]:
            mode_txt = {"render": "기존 그래프", "insert": "그래프 삽입",
                        "render_only": "SBS 없음", "direct": "절차형 그래프"}[j["mode"]]
            if j.get("normalize_cluster"):
                mode_txt += " → Cluster 정규화"
            msg.append(
                f" · {j['base']} → {j['texture_base']} × {len(sbs_auto.RENDER_MAPS)} ({mode_txt})")
        if len(jobs) > 10:
            msg.append(f" · ... 외 {len(jobs) - 10}개")
        if skipped:
            msg.append(f"\n건너뜀 {len(skipped)}개 (원본 못 찾음 — 로그 참조)")
        msg.append("\n묶음당 ~1분 걸립니다. 계속할까요?")
        if not messagebox.askyesno("③ 실행", "\n".join(msg)):
            self.status_var.set("대기")
            return
        self._set_busy(True)
        # Shared texture owners can render on behalf of another checked row,
        # but every checked SK still needs its own material slots normalized.
        affected_folders = checked_folders
        self.worker = threading.Thread(
            target=self._run_step3,
            args=(jobs, affected_folders, sync_files), daemon=True)
        self.worker.start()

    def _run_step3(self, jobs, affected_folders, sync_files=None):
        done = failed = 0
        sync_candidates = list(sync_files or [])
        total = len(jobs)
        timeout = self.cfg.get("sbsrender_timeout", 1800)
        for index, job in enumerate(jobs, 1):
            base = job["base"]
            texture_base = job["texture_base"]
            self._ui(lambda i=index, b=base, t=texture_base: self.status_var.set(
                f"③ {i}/{total}: {b} → {t} 렌더 중..."))
            self._ui(lambda it=job["item"]: self.tree.set(it["folder"], "step3", "렌더 중..."))
            try:
                result = run_texture_job(job, self.cfg, timeout)
                sync_candidates.extend(result.get("files") or [])
                done += 1
                self._ui(lambda b=base, r=result: self.log(
                    f"[③ 완료] {b} → {r['texture_base']} — "
                    f"{len(r['files'])}장 / {Path(r['files'][0]).parent}"))
                continue
                if job.get("rename_graph_to"):
                    rename = sbs_auto.rename_managed_graph(
                        job["sbs"], job["graph"], job["rename_graph_to"])
                    job["graph"] = job["rename_graph_to"]
                    self._ui(lambda b=base, r=rename: self.log(
                        f"[③ 그래프 이름] {r['old']} → {r['new']} "
                        f"(백업: {Path(r['backup']).name})"))
                if job["mode"] == "render":
                    info = sbs_auto.parse_m_graph(job["sbs"], job["graph"])
                    inputs, params = info["inputs"], info["params"]
                    if not inputs.get("Base_Color"):
                        raise RuntimeError(f"{job['graph']}: 그래프의 원본 연결이 깨져 있음 (Designer에서 확인)")
                    # A real Translucency/Subsurface source wins over a stale
                    # neutral-black graph resource.  Amount is always identity 1.
                    try:
                        planned_inputs, _notes = sbs_auto.plan_inputs_from_row(
                            job["row"], require_alpha=False)
                    except Exception:
                        planned_inputs = {}
                    desired_subsurface = planned_inputs.get("Subsurface")
                    current_subsurface = inputs.get("Subsurface")
                    if desired_subsurface and (
                            not current_subsurface
                            or "neutral_black" in Path(current_subsurface).name.lower()):
                        patch = sbs_auto.patch_m_graph_input_resource(
                            job["sbs"], job["graph"], "Subsurface", desired_subsurface)
                        inputs["Subsurface"] = desired_subsurface
                        self._ui(lambda b=base, r=patch: self.log(
                            f"[③ Subsurface 연결] {b}: Translucency 원본 연결 "
                            f"(백업: {Path(r['backup']).name})"))
                    white = sbs_auto.neutral_image("white")
                    current_amount = inputs.get("Subsurface_Amount")
                    if current_amount and Path(current_amount).resolve() != white.resolve():
                        patch = sbs_auto.patch_m_graph_input_resource(
                            job["sbs"], job["graph"], "Subsurface_Amount", white)
                        self._ui(lambda b=base, r=patch: self.log(
                            f"[③ Subsurface Amount] {b}: 1로 고정 "
                            f"(백업: {Path(r['backup']).name})"))
                    inputs["Subsurface_Amount"] = white
                else:
                    inputs = job["inputs"]
                    params = sbs_auto.default_params(job["normal_opengl"])
                    for note in job.get("notes", []):
                        self._ui(lambda b=base, n=note: self.log(f"[③ 참고] {b}: {n}"))
                had_graph_ao = bool(inputs.get("Ambient_Occlusion"))
                inputs, hbao_path = sbs_auto.ensure_hbao_input(
                    texture_base, inputs, job["out_dir"], cfg=self.cfg, timeout=timeout)
                if hbao_path:
                    self._ui(lambda b=base, p=hbao_path: self.log(
                        f"[③ HBAO] {b}: height에서 생성 → {p}"))
                    if job["mode"] == "render" and had_graph_ao:
                        patch = sbs_auto.patch_m_graph_input_resource(
                            job["sbs"], job["graph"], "Ambient_Occlusion", hbao_path)
                        self._ui(lambda b=base, r=patch: self.log(
                            f"[③ HBAO 연결] {b}: SBS AO 입력 갱신 "
                            f"(백업: {Path(r['backup']).name})"))
                if job["mode"] != "render":
                    if job["mode"] == "insert":
                        insert_result = sbs_auto.insert_m_graph(
                            job["sbs"], texture_base, inputs,
                            normal_opengl=job["normal_opengl"], cfg=self.cfg)
                        self._ui(lambda b=texture_base, r=insert_result, s=job["sbs"]: self.log(
                            f"[③ 그래프 삽입] {b} → {Path(s).name} "
                            f"(백업: {Path(r['backup']).name})"))
                    else:
                        self._ui(lambda b=base: self.log(
                            f"[③ 참고] {b}: SBS 파일이 없어 그래프 삽입은 생략, 텍스처만 생성"))
                render_info = sbs_auto.render_maps(
                    texture_base, inputs, params, job["out_dir"],
                    cfg=self.cfg, timeout=timeout, return_info=True)
                files = render_info["files"]
                if render_info.get("backup_dir"):
                    self._ui(lambda b=base, p=render_info["backup_dir"]: self.log(
                        f"[③ 기존 출력 백업] {b}: {p}"))
                if render_info.get("normal_green_corrected"):
                    self._ui(lambda b=base: self.log(
                        f"[③ 노멀] {b}: DirectX 원본 보존을 위해 출력 G 채널 보정"))
                deleted_legacy = sbs_auto.delete_legacy_m_outputs(
                    base, job["out_dir"],
                    legacy_maps=job["row"].get("legacy_export_maps"))
                if deleted_legacy:
                    self._ui(lambda b=base, paths=deleted_legacy: self.log(
                        f"[③ 기존 M_ 출력 삭제] {b}: {len(paths)}개"))
                done += 1
                self._ui(lambda b=base, t=texture_base, f=files: self.log(
                    f"[③ 완료] {b} → {t} — {len(f)}장 저장: {Path(f[0]).parent}"))
            except Exception as exc:
                failed += 1
                self._ui(lambda b=base, e=exc: self.log(f"[③ 실패] {b}: {e}"))
        if failed == 0 and affected_folders:
            try:
                self._ui(lambda: self.status_var.set("③ SK SPM 머티리얼 슬롯 정리 중..."))
                report = make_report(self.cfg, targets=affected_folders)
                save_spm_analysis_cache()
                plan = build_texture_plan_from_report(report, "<step3-normalize>")
                spm_jobs = jobs_from_texture_plan(plan)
                normalized = normalize_spms_transactionally(
                    spm_jobs,
                    backup_root=Path(self.cfg["tree_root"]) / "_spm_backups",
                    skip_unbuildable=True,
                )
                cleanup = cleanup_preserved_cluster_outputs(plan)
                self._ui(lambda r=normalized: self.log(
                    f"[③ SK SPM 정리] {len(r['spms'])}개 SPM / "
                    f"{r['materials']}개 머티리얼 — 백업: {r['backup_dir']}"))
                for entry in normalized.get("skipped", []):
                    self._ui(lambda e=entry: self.log(
                        f"[③ SK SPM 정리 건너뜀] {Path(e['spm']).name}: {e['reason']}"))
                if cleanup["cleaned"]:
                    self._ui(lambda r=cleanup: self.log(
                        f"[③ Cluster 원본 보존] {len(r['cleaned'])}개 머티리얼 복원"))
            except Exception as exc:
                failed += 1
                self._ui(lambda e=exc: self.log(
                    f"[③ SK SPM 정리 실패] 출력 파일은 보존됨, SPM은 변경하지 않음: {e}"))
        if sync_candidates and self.cfg.get("unreal_texture_sync_enabled", True):
            try:
                unique_candidates = []
                seen_candidates = set()
                for path in sync_candidates:
                    absolute = os.path.abspath(str(path))
                    key = os.path.normcase(absolute)
                    if key not in seen_candidates:
                        seen_candidates.add(key)
                        unique_candidates.append(absolute)
                self._ui(lambda: self.status_var.set(
                    "③ Unreal 텍스처 내용 해시 확인·동기화 중..."))
                sync_report = sync_texture_files(unique_candidates, cfg=self.cfg)
                counts = sync_report.get("counts") or {}
                mode = sync_report.get("mode", "unknown")
                self._ui(lambda c=counts, m=mode: self.log(
                    "[③ Unreal 동기화] "
                    f"방식={m} · 동일 {c.get('unchanged', 0)} · "
                    f"재임포트 {c.get('reimported', 0)} · 설정 {c.get('configured', 0)} · "
                    f"신규 add {c.get('created', 0)}"))
                for error in sync_report.get("errors") or []:
                    failed += 1
                    self._ui(lambda e=error: self.log(f"[③ Unreal 동기화 실패] {e}"))
            except UnrealTextureSyncDeferred as exc:
                self._ui(lambda e=exc: self.log(
                    f"[③ Unreal 동기화 보류] 로컬 TGA/SPM은 완료됨: {e}"))
            except Exception as exc:
                failed += 1
                self._ui(lambda e=exc: self.log(
                    f"[③ Unreal 동기화 실패] 로컬 TGA/SPM은 보존됨: {e}"))
        self._ui(lambda: self._batch_finished("③", done, failed))

    def _batch_finished(self, label, done, failed):
        self._set_busy(False)
        self.log(f"{label} 완료: 성공 {done}개, 실패 {failed}개. 표를 다시 검사합니다.")
        self.refresh()

    # ---------------------------------------------------------- PCG + placed-level targets
    def refresh_pcg_targets(self):
        self.status_var.set("Unreal에서 PCG + 작업 레벨 대상 읽는 중...")

        def worker():
            cmd = [
                sys.executable.replace("pythonw.exe", "python.exe"),
                str(TOOL_DIR / "refresh_pcg_targets.py"),
                "--out", str(TARGETS_PATH),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000)
            self.root.after(0, lambda: self._pcg_targets_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def import_saved_pcg_report(self):
        self.status_var.set("저장된 PCG 리포트 읽는 중...")

        def worker():
            cmd = [
                sys.executable.replace("pythonw.exe", "python.exe"),
                str(TOOL_DIR / "import_pcg_display_report.py"),
                "--out", str(TARGETS_PATH),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000)
            self.root.after(0, lambda: self._pcg_targets_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _pcg_targets_done(self, result):
        if result.returncode != 0:
            messagebox.showerror("대상 읽기 실패", (result.stderr or result.stdout)[-2000:])
            self.status_var.set("PCG + 작업 레벨 대상 읽기 실패")
            return
        self.use_pcg_targets_var.set(True)
        self.log((result.stdout or "PCG + 작업 레벨 대상 목록 갱신됨").strip())
        self.refresh()

    def open_selected_folder(self):
        item = self.selected_item()
        if not item:
            messagebox.showinfo("폴더 열기", "표에서 행을 먼저 클릭하세요.")
            return
        subprocess.Popen(["explorer", item["folder"]])


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
