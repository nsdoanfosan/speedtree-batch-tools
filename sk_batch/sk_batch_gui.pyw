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
import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))

from sk_common import (
    CALIBRATION_CACHE_VERSION,
    LOG_DIR,
    blend_path_for,
    calibration_cache_matches,
    calibration_settings_signature,
    compact_error_message,
    file_content_snapshot,
    is_manual_bones_locked,
    launch_limited,
    load_config,
    load_job_report,
    load_state,
    manual_bones_marker_path,
    save_config,
    save_state,
    scan_sk_spms,
    set_manual_bones_marker,
    summarize_job_failure,
    terminate_process_tree,
    wind_preset_for,
)

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
        self.ui_queue = queue.Queue()
        self.worker = None
        self.cell_editor = None
        self.stop_flag = threading.Event()
        self.active_procs = set()          # all running child procs (serial or parallel)
        self.procs_lock = threading.Lock()
        self.state_lock = threading.RLock()  # guards self.state writes across worker threads
        self.spm_calibration_signature = calibration_settings_signature(self.cfg)

        root.title("SK Vegetation Batch — 검사 → 본 세팅 → Blender → Unreal")
        root.geometry("1460x760")
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

        opts = ttk.LabelFrame(self.root, text="옵션 (각 항목에 마우스를 올리면 설명이 뜹니다)", padding=6)
        opts.pack(fill="x", padx=6)

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
                                   "파일당 수분~수십분. 이미 최신인 blend는 건너뜁니다("
                                   "'완료된 항목도 다시 실행'으로 강제 가능)."))
        self.btn_push = ttk.Button(actions, text="③ Unreal Push",
                                   command=lambda: self.start_batch("push"))
        self.btn_push.pack(side="left", padx=6)
        Tooltip(self.btn_push, ("push 전에 준비 검사를 먼저 전부 통과시킵니다:\n"
                                "· .blend 존재 + SPM보다 최신인지\n"
                                "· wind JSON(핸드오프 산출물) 존재\n"
                                "· 언리얼 에디터 실행 여부\n"
                                "준비 안 된 항목은 이유를 표시하고 건너뛴 뒤, 준비된 것만 push합니다."))
        self.btn_all = ttk.Button(
            actions,
            text="🌙 전체 자동 ①→②→③",
            command=self.start_full_pipeline,
        )
        self.btn_all.pack(side="left", padx=(10, 4))
        Tooltip(
            self.btn_all,
            "선택된 항목을 밤새 순서대로 처리합니다.\n"
            "① SPM 본 세팅 전체 완료 → ② Blender Repair 전체 완료 → "
            "③ Unreal Push 순서입니다.\n"
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
        self.tree = ttk.Treeview(self.root, columns=cols, show="tree headings", height=16)
        self.tree.heading("#0", text="파일 (클릭=선택 토글)")
        self.tree.column("#0", width=310, anchor="w")
        headers = {
            "bone_mode": ("본 모드 (▼ 클릭)", 135),
            "wind": ("Wind (▼ 클릭)", 145),
            "spm_status": ("① SPM 본 세팅", 210),
            "blend_status": ("② Blender", 160),
            "push_status": ("③ Unreal", 190),
            "folder": ("폴더", 300),
        }
        for key, (label, width) in headers.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Button-1>", self._on_click)

        logf = ttk.LabelFrame(self.root, text="로그", padding=4)
        logf.pack(fill="both", padx=6, pady=4)
        self.log_text = tk.Text(logf, height=9, wrap="none", state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _pick_root(self):
        path = filedialog.askdirectory(initialdir=self.root_var.get())
        if path:
            self.root_var.set(path)

    def log(self, msg):
        self.ui_queue.put(("log", msg))

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
                        self.tree.set(iid, column, value)
                elif kind == "progress":
                    self.progress_var.set(payload)
                elif kind == "batch_progress":
                    done, total = payload
                    percent = (done / total * 100.0) if total else 0.0
                    self.batch_progress.configure(value=percent)
                    self.batch_progress_var.set(f"{done}/{total} ({percent:.0f}%)")
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
    def _snapshot_spm(spm):
        try:
            return str(spm), file_content_snapshot(spm), None
        except OSError as exc:
            return str(spm), None, str(exc)

    def scan(self):
        root = self.root_var.get()
        self.cfg = self._collect_cfg()
        self.cfg["root"] = root
        self.spm_calibration_signature = calibration_settings_signature(self.cfg)
        save_config(self.cfg)
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        spms = scan_sk_spms(root)
        snapshots = {}
        if spms:
            workers = min(8, len(spms))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for iid, snapshot, error in pool.map(self._snapshot_spm, spms):
                    if snapshot:
                        snapshots[iid] = snapshot
                    elif error:
                        self.log(f"[경고] SPM 지문 계산 실패: {Path(iid).name}: {error}")
        for spm in spms:
            iid = str(spm)
            entry = self.state.setdefault(iid, {})
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
            }
            self.tree.insert(
                "", "end", iid=iid,
                text=self._item_label(iid),
                values=(
                    self._bone_mode_label(iid),
                    self._wind_label(iid),
                    entry.get("spm_status", "-"),
                    entry.get("blend_status", "-"),
                    entry.get("push_status", "-"),
                    str(spm.parent),
                ),
            )
        save_state(self.state)
        cache_count = sum(
            1
            for iid, item in self.items.items()
            if item.get("spm_snapshot")
            and calibration_cache_matches(
                self.state.get(iid, {}).get("calibration_cache"),
                item["spm_snapshot"]["fingerprint"],
                self.spm_calibration_signature,
            )
        )
        self.log(
            f"스캔 완료: SK SPM {len(spms)}개 · 본 세팅 캐시 {cache_count}개. "
            "먼저 [🔍 검사]로 상태를 확인해보세요."
        )

    def _item_label(self, iid):
        item = self.items[iid]
        mark = CHECK_ON if item["checked"] else CHECK_OFF
        lock = "🔒 " if item.get("manual_bones_locked", False) else ""
        return f"{mark} {lock}{item['spm'].name}"

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
            item = self.items[iid]
            item["checked"] = not item["checked"]
            self.tree.item(iid, text=self._item_label(iid))
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
        for iid, item in self.items.items():
            item["checked"] = checked
            self.tree.item(iid, text=self._item_label(iid))

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
            entry["spm_status"] = "수동 본 유지 🔒"
            entry["spm_summary"] = "수동 본 유지 🔒"
        else:
            entry.pop("manual_bones_locked", None)
            entry["spm_status"] = "자동 계산 대상"
            entry.pop("spm_summary", None)
        self.tree.item(iid, text=self._item_label(iid))
        self.tree.set(iid, "bone_mode", self._bone_mode_label(iid))
        self.tree.set(iid, "spm_status", entry["spm_status"])
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
        return snapshot

    def _spm_cache_matches(self, item):
        snapshot = self._current_spm_snapshot(item)
        entry = self.state.get(str(item["spm"]), {})
        return calibration_cache_matches(
            entry.get("calibration_cache"),
            snapshot["fingerprint"],
            self.spm_calibration_signature,
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
        save_config(self.cfg)
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
        targets = [item for item in self.items.values() if item["checked"]]
        if not targets:
            messagebox.showinfo("SK Batch", "선택된 항목이 없습니다.")
            return
        self.cfg = self._collect_cfg()
        self.spm_calibration_signature = calibration_settings_signature(self.cfg)
        save_config(self.cfg)
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
        self.log("🌙 전체 자동 시작: ① SPM → ② Blender → ③ Unreal")
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
        for phase, label in phases:
            if self.stop_flag.is_set():
                break
            self.log(f"🌙 {label} 시작")
            phase_ok = self._run_batch(phase, targets, emit_done=False)
            self.log(f"🌙 {label} 종료")
            if not phase_ok:
                pipeline_abort = getattr(self, "_phase_abort_reason", None)
                break
        if self.stop_flag.is_set():
            final_text = "중지됨"
        elif pipeline_abort:
            final_text = f"전체 자동 중단 — {pipeline_abort}"
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

    def _run_batch(self, phase, targets, emit_done=True):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._phase_abort_reason = None
        if phase == "spm":
            targets = sorted(targets, key=self._spm_schedule_key)
        if phase == "push":
            targets = self._push_preflight(targets)
            if not targets:
                self.log("push 가능한 항목이 없습니다. (준비 검사 결과를 표에서 확인)")
                if emit_done:
                    self.ui_queue.put(("progress", "대기"))
                    self.ui_queue.put(("done", None))
                return True
        titles = {"check": "검사", "spm": "SPM 본 세팅", "blender": "Blender Repair", "push": "Unreal Push"}
        column_by_phase = {"check": "spm_status", "spm": "spm_status",
                           "blender": "blend_status", "push": "push_status"}
        title = titles[phase]
        column = column_by_phase[phase]
        total = len(targets)
        self.ui_queue.put(("batch_progress", (0, total)))
        self._batch_done = 0
        self._batch_active = 0
        phase_abort = threading.Event()

        def run_one(item):
            if self.stop_flag.is_set():
                return
            spm = item["spm"]
            iid = str(spm)
            with self.state_lock:
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
                self.log(f"[실패] {spm.name}: {reason}")
                self.ui_queue.put(("cell", (iid, column, f"실패: {reason}")))
                with self.state_lock:
                    state_entry = self.state.setdefault(iid, {})
                    state_entry[column] = f"실패: {reason}"
                    state_entry[f"{column}_error"] = {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "message": reason,
                    }
                    save_state(self.state)
                if phase == "push" and reason.startswith("Unreal 연결 실패"):
                    self._phase_abort_reason = reason
                    phase_abort.set()
                    self.log(
                        "[Push 단계 중단] Unreal RPC 연결이 끊겨 남은 항목을 실행하지 않습니다."
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

        # Pure inspection and independent SPM calibration are parallelized.
        # Blender and Push stay serial due to memory and editor RPC constraints.
        if phase == "spm":
            workers = self.cfg.get("spm_parallel_jobs", 1)
        elif phase == "check":
            workers = self.cfg.get("check_parallel_jobs", 8)
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

        with self.state_lock:
            save_state(self.state)
        if emit_done:
            self.ui_queue.put(("progress", "대기"))
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
        self, cmd, log_name, timeout, affinity=True, progress_callback=None
    ):
        log_file = LOG_DIR / log_name
        proc = launch_limited(cmd, self.cfg, log_file=str(log_file), affinity=affinity)
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
                    raise RuntimeError(
                        f"시간 초과({timeout}s){detail} — 로그: {log_file}"
                    )
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
            with self.procs_lock:
                self.active_procs.discard(proc)
            handle = getattr(proc, "sk_log_handle", None)
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass
        return proc.returncode, log_file

    def _handoff_ready(self, spm):
        """핸드오프 산출물(push에 필요한 것들)이 준비됐는지 → (ok, 설명)."""
        blend = blend_path_for(spm)
        if not blend.exists():
            return False, "blend 없음 → ② 필요"
        if blend.stat().st_mtime < spm.stat().st_mtime:
            return False, "blend가 SPM보다 오래됨 → ② 필요"
        wind_json = blend.parent / "JSON" / f"{spm.stem}_dynamic_wind_import_from_megaplant_groups.json"
        if not wind_json.exists():
            return False, "wind JSON 없음 → ② 필요"
        texture_ok, texture_reason = self._texture_normalization_ready(spm)
        if not texture_ok:
            return False, texture_reason
        return True, "준비됨 ✓"

    @staticmethod
    def _texture_normalization_ready(spm):
        report_path = (
            spm.parent / "reports" /
            f"{spm.stem}_speedtree_repair_pipeline_report_codex.json"
        )
        if not report_path.is_file():
            return False, "텍스처 정규화 정보 없음 → ② 필요"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return False, f"텍스처 정규화 보고서 오류: {exc}"
        normalization = report.get("texture_normalization") or {}
        missing = normalization.get("missing", [])
        if missing:
            details = []
            for item in missing:
                roles = ",".join(item.get("missing_roles", [])) or "대응 세트"
                details.append(
                    f"{item.get('material', '?')}→"
                    f"{item.get('expected_texture_base', 'T_?')}[{roles}]"
                )
            return False, "텍스처 누락: " + " | ".join(details)
        if normalization.get("status") != "ok":
            return False, "텍스처 정규화 미완료 → ② 필요"
        return True, "텍스처 정규화 완료"

    def _job_check(self, iid, spm):
        from spm_audit import audit_spm, sk_readiness

        audit = audit_spm(spm, analyze_bone_graph=True)
        readiness = sk_readiness(audit)
        gens = audit["generators"]
        n_rel = sum(1 for g in gens if g["style"] == 1.0)
        n_abs = sum(1 for g in gens if g["style"] == 0.0 and (g["bones"] or 0) > 0)
        n_off = sum(1 for g in gens if g["style"] == 0.0 and (g["bones"] or 0) == 0)
        n_mat = sum(1 for m in audit["materials"] if m["needs_prefix"])
        entry = self.state.setdefault(iid, {})
        manual_bones_locked = self.items[iid].get("manual_bones_locked", False)
        summary = entry.get("spm_summary", "")
        parts = []
        if n_abs:
            parts.append(f"미보정 {n_abs}")
        if n_rel:
            parts.append(f"Relative {n_rel}")
        if n_off:
            parts.append(f"무본 {n_off}")
        if n_mat:
            parts.append(f"M_필요 {n_mat}")
        graph = audit.get("bone_graph") or {}
        if graph.get("root_target_generator_count"):
            parts.append(
                f"자동 대상 {graph['root_target_generator_count']} / "
                f"Base 제외 {graph['base_excluded_generator_count']}"
            )
        if graph.get("unknown_base_generators"):
            parts.append(f"Base 미분류 {len(graph['unknown_base_generators'])}")
        if manual_bones_locked:
            detail = " · ".join(parts) if parts else "본 설정 없음"
            if not readiness["ready"]:
                detail = "현재 본 0 — ② 실행 전 SpeedTree에서 직접 설정 필요"
            text = f"수동 본 유지 🔒 · {detail}"
        elif not readiness["ready"]:
            disabled = ", ".join(
                f"{item['generator']}({item['style']:g}/{item['bones']:g})"
                for item in readiness["disabled_generators"]
            )
            text = f"오류: SK 미제작 · {disabled}"
        else:
            text = " · ".join(parts) if parts else "본 설정 없음"
        if summary and summary not in text:
            text += f" | {summary}"
        self.ui_queue.put(("cell", (iid, "spm_status", text)))

        blend = blend_path_for(spm)
        if not blend.exists():
            blend_text = "없음"
        elif blend.stat().st_mtime < spm.stat().st_mtime:
            blend_text = "오래됨 (SPM이 더 최신)"
        else:
            texture_ok, texture_reason = self._texture_normalization_ready(spm)
            blend_text = "최신 ✓" if texture_ok else texture_reason
        self.ui_queue.put(("cell", (iid, "blend_status", blend_text)))

        ok, why = self._handoff_ready(spm)
        pushed = entry.get("push_status", "")
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
            summary = "수동 본 유지 🔒 (① 전체 건너뜀)"
            self.ui_queue.put(("cell", (iid, "spm_status", summary)))
            with self.state_lock:
                entry["spm_status"] = summary
                entry["spm_summary"] = summary
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
            )
        ):
            summary = cache.get("summary", cache.get("status", "캐시"))
            cached_text = f"{summary} ✓ (변경 없음)"
            if cache.get("status") == "not-sk-ready":
                raise RuntimeError(f"SK 미제작: {cache.get('error', '본 설정 필요')} (캐시)")
            self.ui_queue.put(("cell", (iid, "spm_status", cached_text)))
            with self.state_lock:
                entry["spm_status"] = cached_text
                entry["spm_summary"] = summary
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
            save_state(self.state)
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
        handoff_ok, _handoff_reason = self._handoff_ready(spm)
        if not self.force_rerun and handoff_ok:
            self.ui_queue.put(("cell", (iid, "blend_status", "최신 ✓ (건너뜀)")))
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
        self.log(f"Blender repair 시작: {spm.name} (수분 소요될 수 있음)")
        self.ui_queue.put(("cell", (iid, "blend_status", "repair 중...")))
        wind = item["wind_override"]
        if wind == "auto":
            wind = wind_preset_for(spm.stem)
        job_report = LOG_DIR / f"{spm.stem}_bwr_{stamp}.json"
        cmd = [
            self.cfg["blender_exe"], "-b",
            "--python", str(TOOL_DIR / "jobs" / "bwr_headless_job.py"), "--",
            "--spm", str(spm), "--blend", str(blend),
            "--wind", wind, "--report", str(job_report),
        ]
        code, log_file = self._run_limited(cmd, f"{spm.stem}_bwr_{stamp}.log",
                                           self.cfg.get("blender_job_timeout", 3600))
        result = load_job_report(job_report)
        if code != 0 or result.get("status") != "ok":
            reason = summarize_job_failure(result, log_file)
            self.log(f"  [Blender 실패 원인] {reason} — 상세 로그: {log_file}")
            raise RuntimeError(reason)
        normalization = result.get("texture_normalization") or {}
        missing = normalization.get("missing", [])
        if missing:
            details = []
            for missing_item in missing:
                roles = ",".join(missing_item.get("missing_roles", [])) or "대응 세트"
                details.append(
                    f"{missing_item.get('material', '?')}→"
                    f"{missing_item.get('expected_texture_base', 'T_?')}[{roles}]"
                )
            blend_status = "텍스처 누락: " + " | ".join(details)
            for warning in result.get("warnings", []):
                self.log(f"  [텍스처 누락] {spm.name}: {warning}")
        else:
            warn = " ⚠" if result.get("warnings") else ""
            blend_status = f"완료 (wind {wind}){warn}"
        self.ui_queue.put(("cell", (iid, "blend_status", blend_status)))
        entry["blend_status"] = blend_status
        save_state(self.state)
        self.log(f"repair 완료: {blend.name}")

    def _push_preflight(self, targets):
        """push 시작 전에 전 항목 준비 검사. 준비된 항목만 반환."""
        self.log("push 준비 검사 중...")
        if not self._unreal_running():
            self.log("[중단] 언리얼 에디터가 실행 중이 아닙니다. MyProject2를 먼저 열어주세요.")
            for item in targets:
                self.ui_queue.put(("cell", (str(item["spm"]), "push_status", "언리얼 에디터 꺼짐")))
            return []
        ready = []
        for item in targets:
            spm = item["spm"]
            ok, why = self._handoff_ready(spm)
            if ok:
                ready.append(item)
            else:
                self.ui_queue.put(("cell", (str(spm), "push_status", why)))
        self.log(f"준비 검사: {len(ready)}/{len(targets)}개 push 가능.")
        return ready

    @staticmethod
    def _unreal_running():
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
            capture_output=True, text=True, creationflags=0x08000000,
        )
        return "UnrealEditor.exe" in (result.stdout or "")

    def _job_push(self, iid, spm):
        blend = blend_path_for(spm)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log(f"Unreal push 시작: {blend.name}")
        self.ui_queue.put(("cell", (iid, "push_status", "push 중...")))
        job_report = LOG_DIR / f"{spm.stem}_push_{stamp}.json"
        cmd = [
            self.cfg["blender_exe"], "--factory-startup", "-b", str(blend),
            "--python", str(TOOL_DIR / "jobs" / "send2ue_push_job.py"), "--",
            "--report", str(job_report),
        ]
        code, log_file = self._run_limited(cmd, f"{spm.stem}_push_{stamp}.log",
                                           self.cfg.get("push_job_timeout", 1800))
        result = load_job_report(job_report)
        if code != 0 or result.get("status") != "ok":
            reason = summarize_job_failure(result, log_file)
            self.log(f"  [Unreal 실패 원인] {reason} — 상세 로그: {log_file}")
            raise RuntimeError(reason)
        wind_info = result.get("wind")
        wind_ok = "wind ✓" if isinstance(wind_info, dict) and wind_info.get("ok") else "wind -"
        self.ui_queue.put(("cell", (iid, "push_status", f"완료 ({wind_ok})")))
        entry = self.state.setdefault(iid, {})
        entry["push_status"] = f"완료 {datetime.now():%m-%d %H:%M}"
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
