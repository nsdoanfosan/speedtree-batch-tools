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
    LOG_DIR,
    blend_path_for,
    launch_limited,
    load_config,
    load_state,
    save_config,
    save_state,
    scan_sk_spms,
    wind_preset_for,
)

WIND_CYCLE = ["auto", "TREE", "BUSH", "GRASS", "NONE"]
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
        self.stop_flag = threading.Event()
        self.active_procs = set()          # all running child procs (serial or parallel)
        self.procs_lock = threading.Lock()
        self.state_lock = threading.Lock()  # guards self.state writes across worker threads

        root.title("SK Vegetation Batch — 검사 → 본 세팅 → Blender → Unreal")
        root.geometry("1240x760")
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
        ttk.Button(top, text="...", width=3, command=self._pick_root).pack(side="left")
        ttk.Button(top, text="스캔", command=self.scan).pack(side="left", padx=6)
        ttk.Button(top, text="전체 선택", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(top, text="전체 해제", command=lambda: self._set_all(False)).pack(side="left", padx=4)

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
               "· 큰 나무: '가지 수 × 이 값'이 아래 '최대 총 본 수'를 넘으면 상한이 걸려, "
               "긴 가지에만 본이 남고 자잘한 잔가지는 자동으로 0~1개가 됨\n"
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
                "이 상한이 걸리면 총 본을 여기에 맞추고, 짧은 잔가지는 0~1개, 굵고 긴 "
                "가지에만 본이 배분됩니다 (예: 1500 → elm_03 약 1776본).")
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
                "SpeedTree 익스포트는 파일 크기와 무관하게 프로세스 기동에 ~16초가 걸려서, "
                "여러 파일을 동시에 돌리면 거의 그 배수만큼 빨라집니다(4개 ≈ 3.8배 실측).\n"
                "메모리/디스크가 부족하면 값을 낮추세요. (②③에는 적용 안 됨)")
        Tooltip(lbl6, tip6); Tooltip(spin6, tip6)

        self.force_var = tk.BooleanVar(value=False)
        chk = ttk.Checkbutton(opts, text="완료된 항목도 다시 실행", variable=self.force_var)
        chk.pack(side="left", padx=12)
        Tooltip(chk, ("② Blender Repair에서, 이미 SPM보다 최신인 .blend가 있는 항목은 기본적으로 "
                      "건너뜁니다. 이 옵션을 켜면 전부 다시 만듭니다."))

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
        self.btn_stop = ttk.Button(actions, text="중지", command=self.stop_batch, state="disabled")
        self.btn_stop.pack(side="left", padx=10)
        self.progress_var = tk.StringVar(value="대기")
        ttk.Label(actions, textvariable=self.progress_var).pack(side="left", padx=14)

        cols = ("wind", "spm_status", "blend_status", "push_status", "folder")
        self.tree = ttk.Treeview(self.root, columns=cols, show="tree headings", height=16)
        self.tree.heading("#0", text="파일 (클릭=선택 토글)")
        self.tree.column("#0", width=310, anchor="w")
        headers = {
            "wind": ("Wind (더블클릭=변경)", 140),
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
        self.tree.bind("<Double-1>", self._on_double_click)

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
                elif kind == "done":
                    for btn in (self.btn_check, self.btn_spm, self.btn_blender, self.btn_push):
                        btn.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_ui_queue)

    # ------------------------------------------------------------------ scan
    def scan(self):
        root = self.root_var.get()
        self.cfg["root"] = root
        save_config(self._collect_cfg())
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        spms = scan_sk_spms(root)
        for spm in spms:
            iid = str(spm)
            entry = self.state.get(iid, {})
            wind_override = entry.get("wind_override", "auto")
            self.items[iid] = {"spm": spm, "checked": True, "wind_override": wind_override}
            self.tree.insert(
                "", "end", iid=iid,
                text=f"{CHECK_ON} {spm.name}",
                values=(
                    self._wind_label(iid),
                    entry.get("spm_status", "-"),
                    entry.get("blend_status", "-"),
                    entry.get("push_status", "-"),
                    str(spm.parent),
                ),
            )
        self.log(f"스캔 완료: SK SPM {len(spms)}개. 먼저 [🔍 검사]로 상태를 확인해보세요.")

    def _wind_label(self, iid):
        item = self.items[iid]
        auto = wind_preset_for(item["spm"].stem)
        if item["wind_override"] == "auto":
            return f"{auto} (자동)"
        return f"{item['wind_override']} (수동)"

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "tree":
            return
        iid = self.tree.identify_row(event.y)
        if iid in self.items:
            item = self.items[iid]
            item["checked"] = not item["checked"]
            mark = CHECK_ON if item["checked"] else CHECK_OFF
            self.tree.item(iid, text=f"{mark} {item['spm'].name}")

    def _on_double_click(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid not in self.items:
            return
        item = self.items[iid]
        idx = WIND_CYCLE.index(item["wind_override"]) if item["wind_override"] in WIND_CYCLE else 0
        item["wind_override"] = WIND_CYCLE[(idx + 1) % len(WIND_CYCLE)]
        self.state.setdefault(iid, {})["wind_override"] = item["wind_override"]
        save_state(self.state)
        self.tree.set(iid, "wind", self._wind_label(iid))

    def _set_all(self, checked):
        for iid, item in self.items.items():
            item["checked"] = checked
            mark = CHECK_ON if checked else CHECK_OFF
            self.tree.item(iid, text=f"{mark} {item['spm'].name}")

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

    def start_batch(self, phase):
        targets = [item for item in self.items.values() if item["checked"]]
        if not targets:
            messagebox.showinfo("SK Batch", "선택된 항목이 없습니다.")
            return
        self.cfg = self._collect_cfg()
        save_config(self.cfg)
        # snapshot tk vars on the main thread; the worker must not touch them
        self.force_rerun = bool(self.force_var.get())
        self.stop_flag.clear()
        for btn in (self.btn_check, self.btn_spm, self.btn_blender, self.btn_push):
            btn.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.worker = threading.Thread(target=self._run_batch, args=(phase, targets), daemon=True)
        self.worker.start()

    def stop_batch(self):
        self.stop_flag.set()
        with self.procs_lock:
            procs = list(self.active_procs)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        self.log("중지 요청됨 — 실행 중인 작업을 종료합니다.")

    def _run_batch(self, phase, targets):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if phase == "push":
            targets = self._push_preflight(targets)
            if not targets:
                self.log("push 가능한 항목이 없습니다. (준비 검사 결과를 표에서 확인)")
                self.ui_queue.put(("progress", "대기"))
                self.ui_queue.put(("done", None))
                return
        titles = {"check": "검사", "spm": "SPM 본 세팅", "blender": "Blender Repair", "push": "Unreal Push"}
        column_by_phase = {"check": "spm_status", "spm": "spm_status",
                           "blender": "blend_status", "push": "push_status"}
        title = titles[phase]
        column = column_by_phase[phase]
        total = len(targets)
        self._batch_done = 0

        def run_one(item):
            if self.stop_flag.is_set():
                return
            spm = item["spm"]
            iid = str(spm)
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
                self.log(f"[실패] {spm.name}: {exc}")
                self.ui_queue.put(("cell", (iid, column, f"실패: {exc}")))
                state_entry = self.state.setdefault(iid, {})
                state_entry[column] = f"실패 {datetime.now():%m-%d %H:%M}"
                state_entry[f"{column}_error"] = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "message": str(exc),
                }
                save_state(self.state)
            finally:
                with self.state_lock:
                    self._batch_done += 1
                    done = self._batch_done
                self.ui_queue.put(("progress", f"{title} {done}/{total}: {spm.name}"))

        # Only ① SPM 본 세팅 fans out — its cost is cold-start-bound SpeedTree
        # exports that scale across cores. Blender (memory-heavy) and Push
        # (single Unreal RPC) stay serial.
        workers = self.cfg.get("spm_parallel_jobs", 1) if phase == "spm" else 1
        workers = max(1, min(int(workers), total))
        if workers > 1:
            self.log(f"{title}: {workers}개 동시 실행")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pool.map(run_one, targets)
        else:
            for item in targets:
                if self.stop_flag.is_set():
                    break
                run_one(item)

        self.ui_queue.put(("progress", "대기"))
        self.ui_queue.put(("done", None))
        self.log(f"{title} 배치 종료.")

    # ------------------------------------------------------------------ jobs
    def _run_limited(self, cmd, log_name, timeout, affinity=True):
        log_file = LOG_DIR / log_name
        proc = launch_limited(cmd, self.cfg, log_file=str(log_file), affinity=affinity)
        with self.procs_lock:
            self.active_procs.add(proc)
        try:
            deadline = time.time() + timeout
            while proc.poll() is None:
                if self.stop_flag.is_set():
                    proc.kill()
                    raise RuntimeError("사용자 중지")
                if time.time() > deadline:
                    proc.kill()
                    raise RuntimeError(f"시간 초과({timeout}s) — 로그: {log_file}")
                time.sleep(1.0)
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
        return True, "준비됨 ✓"

    def _job_check(self, iid, spm):
        from spm_audit import audit_spm, sk_readiness

        audit = audit_spm(spm)
        readiness = sk_readiness(audit)
        gens = audit["generators"]
        n_rel = sum(1 for g in gens if g["style"] == 1.0)
        n_abs = sum(1 for g in gens if g["style"] == 0.0 and (g["bones"] or 0) > 0)
        n_off = sum(1 for g in gens if g["style"] == 0.0 and (g["bones"] or 0) == 0)
        n_mat = sum(1 for m in audit["materials"] if m["needs_prefix"])
        entry = self.state.setdefault(iid, {})
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
        if not readiness["ready"]:
            disabled = ", ".join(
                f"{item['generator']}({item['style']:g}/{item['bones']:g})"
                for item in readiness["disabled_generators"]
            )
            text = f"오류: SK 미제작 · {disabled}"
        else:
            text = " · ".join(parts) if parts else "본 설정 없음"
        if summary:
            text += f" | {summary}"
        self.ui_queue.put(("cell", (iid, "spm_status", text)))

        blend = blend_path_for(spm)
        if not blend.exists():
            blend_text = "없음"
        elif blend.stat().st_mtime < spm.stat().st_mtime:
            blend_text = "오래됨 (SPM이 더 최신)"
        else:
            blend_text = "최신 ✓"
        self.ui_queue.put(("cell", (iid, "blend_status", blend_text)))

        ok, why = self._handoff_ready(spm)
        pushed = entry.get("push_status", "")
        push_text = why if not pushed or not ok else f"{why} | {pushed}"
        self.ui_queue.put(("cell", (iid, "push_status", push_text)))

    def _job_spm(self, iid, spm):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        entry = self.state.setdefault(iid, {})
        self.log(f"SPM 본 세팅 시작: {spm.name}")
        self.ui_queue.put(("cell", (iid, "spm_status", "캘리브레이션 중...")))
        report_path = LOG_DIR / f"{spm.stem}_spm_{stamp}.json"
        cmd = [sys.executable.replace("pythonw.exe", "python.exe"),
               str(TOOL_DIR / "spm_audit.py"), str(spm), "--report", str(report_path)]
        # In parallel mode, don't pin to a core subset — let the concurrent
        # cold-start-bound exports spread across all cores.
        parallel = self.cfg.get("spm_parallel_jobs", 1) > 1
        code, log_file = self._run_limited(cmd, f"{spm.stem}_spm_{stamp}.log",
                                           self.cfg.get("spm_verify_timeout", 900) * 5,
                                           affinity=not parallel)
        if not report_path.exists():
            raise RuntimeError(f"본 세팅 실패 — 로그: {log_file}")
        rep = json.loads(report_path.read_text(encoding="utf-8"))[0]
        if rep.get("status") == "not-sk-ready":
            raise RuntimeError(f"SK 미제작: {rep.get('error', '보이는 Branch의 본 설정이 모두 꺼져 있음')}")
        if code != 0 or rep.get("status") == "failed":
            raise RuntimeError(f"본 세팅 실패: {rep.get('error', '?')} — 로그: {log_file}")
        total = rep.get("total_bones")
        meta = rep.get("calibration") or {}
        branches = meta.get("total_branches")
        warn = " ⚠" if rep.get("warnings") else ""
        if total is not None and branches:
            tag = " [상한]" if meta.get("capped") else ""
            summary = f"본 {total} / 가지 {branches}{tag}"
        elif meta.get("mode") == "no_branch_generators":
            summary = "SpeedTree 본 없음 → rigid 1본 폴백"
        else:
            summary = rep.get("status", "?")
        self.ui_queue.put(("cell", (iid, "spm_status", f"{summary}{warn}")))
        with self.state_lock:
            entry["spm_status"] = f"{summary}{warn}"
            entry["spm_summary"] = summary
            save_state(self.state)
        for warning in rep.get("warnings", []):
            self.log(f"  [경고] {spm.name}: {warning}")
        self.log(f"본 세팅 완료: {spm.name} — {summary}")

    def _job_blender(self, iid, spm, item):
        from spm_audit import audit_spm, sk_readiness

        blend = blend_path_for(spm)
        handoff_ok, _handoff_reason = self._handoff_ready(spm)
        if not self.force_rerun and handoff_ok:
            self.ui_queue.put(("cell", (iid, "blend_status", "최신 ✓ (건너뜀)")))
            self.log(f"건너뜀 (blend 최신): {spm.name}")
            return
        readiness = sk_readiness(audit_spm(spm))
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
        result = json.loads(job_report.read_text(encoding="utf-8")) if job_report.exists() else {}
        if code != 0 or result.get("status") != "ok":
            raise RuntimeError(f"repair 실패: {result.get('error', '?')} — 로그: {log_file}")
        warn = " ⚠" if result.get("warnings") else ""
        self.ui_queue.put(("cell", (iid, "blend_status", f"완료 (wind {wind}){warn}")))
        entry["blend_status"] = f"완료 {datetime.now():%m-%d %H:%M}"
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
            self.cfg["blender_exe"], "-b", str(blend),
            "--python", str(TOOL_DIR / "jobs" / "send2ue_push_job.py"), "--",
            "--report", str(job_report),
        ]
        code, log_file = self._run_limited(cmd, f"{spm.stem}_push_{stamp}.log",
                                           self.cfg.get("push_job_timeout", 1800))
        result = json.loads(job_report.read_text(encoding="utf-8")) if job_report.exists() else {}
        if code != 0 or result.get("status") != "ok":
            raise RuntimeError(f"push 실패: {result.get('error', '?')} — 로그: {log_file}")
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
