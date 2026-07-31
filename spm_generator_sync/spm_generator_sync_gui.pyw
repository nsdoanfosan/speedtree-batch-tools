"""Folder-based GUI for SpeedTree SPM master/follower synchronization."""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import OrderedDict, deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
sys.path.insert(0, str(REPO_DIR))
# Keep the tool folder first: the GUI engine is the sibling
# spm_generator_sync.py, not the repository package's limited public API.
sys.path.insert(0, str(TOOL_DIR))

from batch_ui_common import clipboard_text, copy_selected_row_paths
from cluster_blend_sync import (
    run_cluster_folder_relation_transaction,
    run_cluster_relation_transaction,
)
from cluster_source_prepare import prepare_cluster_source_if_required
from shared_queue_runtime import SharedQueueRuntime


def _load_sibling_engine():
    """Load the full GUI engine without colliding with the public package."""

    module_name = "_speedtree_spm_generator_sync_gui_engine"
    engine_path = TOOL_DIR / "spm_generator_sync.py"
    loaded = sys.modules.get(module_name)
    loaded_path = getattr(loaded, "__file__", None)
    if loaded_path and Path(loaded_path).resolve() == engine_path.resolve():
        return loaded
    spec = importlib.util.spec_from_file_location(module_name, engine_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"SPM Generator Sync engine을 불러올 수 없습니다: {engine_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


engine = _load_sibling_engine()


CONFIG_PATH = TOOL_DIR / "spm_generator_sync_config.json"
CACHE_PATH = TOOL_DIR / "spm_generator_sync_cache.json"
REPORT_DIR = TOOL_DIR / "reports"
CACHE_VERSION = 4
DEFAULT_TREE_ROOT = Path(r"D:\OneDrive\Forestportfolio\02_nature\Tree")
DEFAULT_SPEEDTREE = Path(
    r"C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\win64\SpeedTree_Modeler.exe"
)
DEFAULT_BLENDER = Path(
    r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
)
DEFAULT_CLUSTER_UNIT_PROBE = Path(
    r"C:\UnrealProjects\MyProject2\work\branch_cluster_uv_audit"
    r"\speedtree_unit_probe_10cm_user_scale_0_1_verified.json"
)


class SharedQueueEnqueueError(RuntimeError):
    """A runnable local mutation could not enter the global FIFO."""


def find_default_xml_ini() -> str:
    sk_config = REPO_DIR / "sk_batch" / "sk_batch_config.json"
    if sk_config.is_file():
        try:
            value = json.loads(sk_config.read_text(encoding="utf-8")).get("xml_ini")
            if value and Path(value).is_file():
                return str(Path(value))
        except (OSError, json.JSONDecodeError):
            pass
    sibling = (
        REPO_DIR.parent
        / "speedtree-bone-weight-repair-addon"
        / "speedtree_bone_weight_repair"
        / "presets"
        / "speedtree_10_1"
        / "Options_HI_Xml.ini"
    )
    return str(sibling)


def load_config() -> dict:
    default = {
        "tree_root": str(DEFAULT_TREE_ROOT if DEFAULT_TREE_ROOT.is_dir() else Path.home()),
        "speedtree_exe": str(DEFAULT_SPEEDTREE),
        "xml_ini": find_default_xml_ini(),
        "blender_exe": str(DEFAULT_BLENDER),
        "cluster_unit_probe": str(DEFAULT_CLUSTER_UNIT_PROBE),
        "cluster_capture_resolution": 1024,
        "verify_speedtree": True,
        "sk_only": True,
    }
    if CONFIG_PATH.is_file():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            default.update({key: value for key, value in loaded.items() if value is not None})
        except (OSError, json.JSONDecodeError):
            pass
    return default


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_connected_run_report(payload: dict) -> Path:
    """Persist one run-specific connected sync/Cluster refresh report."""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = REPORT_DIR / (
        f"connected_sync_cluster_refresh_{stamp}_{os.getpid()}_"
        f"{time.time_ns()}.json"
    )
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def load_analysis_cache() -> dict:
    empty = {"version": CACHE_VERSION, "signatures": {}, "analyses": {}}
    if not CACHE_PATH.is_file():
        return empty
    try:
        loaded = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if loaded.get("version") != CACHE_VERSION:
        return empty
    for key in ("signatures", "analyses"):
        if not isinstance(loaded.get(key), dict):
            return empty
    return loaded


def save_analysis_cache(signatures, analyses) -> None:
    payload = {
        "version": CACHE_VERSION,
        "signatures": dict(list(signatures.items())[-64:]),
        "analyses": dict(list(analyses.items())[-64:]),
    }
    temporary = CACHE_PATH.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, CACHE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def rgba_to_hex(category: str, base_name: str = "") -> str:
    rgba = engine.base_role_color(category, base_name or category) or engine.CATEGORY_COLORS[category]
    return "#{:02x}{:02x}{:02x}".format(
        round(rgba[0] * 255), round(rgba[1] * 255), round(rgba[2] * 255)
    )


def paths_for_row(row):
    """Adapt a Generator Sync SPM/folder row to the shared path API."""

    folder = Path(row.get("folder", ""))
    if row.get("kind") == "spm" and row.get("file"):
        return (folder / row["file"],)
    if row.get("kind") == "folder" and row.get("folder"):
        return (folder,)
    if row.get("kind") == "cluster_blend" and row.get("blend"):
        return (Path(row["blend"]),)
    if row.get("kind") == "cluster_relation":
        return tuple(
            Path(path) for path in (
                row.get("blend"), row.get("source_spm"),
                *(row.get("target_spms") or ()),
            ) if path
        )
    return ()


def clipboard_text_for_rows(rows) -> str:
    """Compatibility wrapper for existing callers and tests."""

    return clipboard_text(path for row in rows for path in paths_for_row(row))


class Tooltip:
    def __init__(self, widget, text, wrap=430):
        self.widget = widget
        self.text = text
        self.wrap = wrap
        self.tip = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event=None):
        if self.tip:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip.geometry(f"+{x}+{y}")
        tk.Label(
            self.tip,
            text=self.text,
            justify="left",
            wraplength=self.wrap,
            background="#fffbe6",
            relief="solid",
            borderwidth=1,
            padx=7,
            pady=5,
        ).pack()

    def hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ChoiceDialog(tk.Toplevel):
    def __init__(self, parent, title, prompt, values):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=prompt, wraplength=430).pack(anchor="w")
        self.var = tk.StringVar(value=values[0] if values else "")
        combo = ttk.Combobox(frame, textvariable=self.var, values=values,
                             state="readonly", width=54)
        combo.pack(fill="x", pady=10)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="확인", command=self.accept).pack(side="right", padx=6)
        self.bind("<Return>", lambda _event: self.accept())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.wait_window()

    def accept(self):
        self.result = self.var.get()
        self.destroy()


class CategoryDialog(tk.Toplevel):
    OPTIONS = ("Leaf", "Branch", "End", "색 없음")
    TO_VALUE = {"Leaf": "leaf", "Branch": "branch", "End": "end", "색 없음": None}
    FROM_VALUE = {value: key for key, value in TO_VALUE.items()}

    def __init__(self, parent, master_path: Path, current: dict):
        super().__init__(parent)
        self.result = None
        self.title("마스터 Base 색 분류")
        self.geometry("620x430")
        self.transient(parent)
        self.grab_set()
        document = engine.SPMDocument.from_path(master_path, full=False)
        self.vars = {}

        intro = ttk.Label(
            self,
            padding=10,
            text=("Base 이름은 자유롭게 유지합니다. 각 Base의 역할만 Leaf/Branch/End로 분류하면 "
                  "Base 본체, 하위 제너레이터, BaseRef와 자식 SPM에 같은 색이 적용됩니다."),
            wraplength=580,
        )
        intro.pack(fill="x")
        body = ttk.Frame(self, padding=(10, 0, 10, 8))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Base", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="분류", font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(body, text="표준색", font=("Segoe UI", 9, "bold")).grid(row=0, column=2, sticky="w")
        for row, base in enumerate(document.base_nodes(), start=1):
            name = document.generator_name(base)
            value = current.get(name) or engine.classify_base_name(name)
            var = tk.StringVar(value=self.FROM_VALUE.get(value, "색 없음"))
            self.vars[name] = var
            ttk.Label(body, text=name).grid(row=row, column=0, sticky="w", pady=5)
            combo = ttk.Combobox(body, textvariable=var, values=self.OPTIONS,
                                 state="readonly", width=24)
            combo.grid(row=row, column=1, sticky="w", padx=10, pady=5)
            swatch = tk.Label(body, text="      ", relief="solid", borderwidth=1)
            swatch.grid(row=row, column=2, sticky="w", pady=5)

            def update_swatch(*_args, target=swatch, variable=var):
                category = self.TO_VALUE.get(variable.get())
                target.configure(background=rgba_to_hex(category) if category else "#eeeeee")

            var.trace_add("write", update_swatch)
            update_swatch()
        body.columnconfigure(0, weight=1)
        buttons = ttk.Frame(self, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="저장", command=self.accept).pack(side="right", padx=6)
        self.wait_window()

    def accept(self):
        self.result = {name: self.TO_VALUE.get(var.get()) for name, var in self.vars.items()}
        self.destroy()


class BaseMapDialog(tk.Toplevel):
    INDEPENDENT = "— 독립 Base (동기화하지 않음) —"

    def __init__(self, parent, master_path: Path, target_path: Path,
                 current: dict | None = None):
        super().__init__(parent)
        self.result = None
        self.title(f"Base 매핑 · {target_path.name}")
        self.geometry("760x540")
        self.transient(parent)
        self.grab_set()
        source = engine.SPMDocument.from_path(master_path, full=False)
        target = engine.SPMDocument.from_path(target_path, full=False)
        source_names = [source.generator_name(item) for item in source.base_nodes()]
        suggestions = engine.suggest_base_map(source, target)
        current = current or {}
        self.vars = {}

        ttk.Label(
            self,
            padding=10,
            text=("자식 Base가 어떤 마스터 Base의 제작 규칙을 따라갈지 지정합니다. "
                  "Base 이름과 Base filter는 절대 변경하지 않습니다. 여러 자식 Base가 같은 "
                  "마스터 Base의 제작 규칙을 따라가도록 매핑할 수도 있습니다."),
            wraplength=720,
        ).pack(fill="x")
        body = ttk.Frame(self, padding=(10, 0, 10, 8))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="자식 Base", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="따라갈 마스터 Base", font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(body, text="구조", font=("Segoe UI", 9, "bold")).grid(row=0, column=2, sticky="w")
        values = [self.INDEPENDENT, *source_names]
        for row, base in enumerate(target.base_nodes(), start=1):
            name = target.generator_name(base)
            selected = current.get(name, suggestions.get(name))
            var = tk.StringVar(value=selected if selected in source_names else self.INDEPENDENT)
            self.vars[name] = var
            signature = target.base_type_signature(base)
            summary = ", ".join(f"{kind} {count}" for kind, count in signature)
            ttk.Label(body, text=name).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Combobox(body, textvariable=var, values=values, state="readonly", width=32).grid(
                row=row, column=1, sticky="ew", padx=10, pady=5
            )
            ttk.Label(body, text=summary, foreground="#666").grid(row=row, column=2, sticky="w", pady=5)
        body.columnconfigure(1, weight=1)
        self.missing_var = tk.StringVar()
        ttk.Label(
            self,
            textvariable=self.missing_var,
            padding=(10, 4),
            foreground="#8a4b00",
        ).pack(fill="x")
        self.mapping_warning_var = tk.StringVar()
        ttk.Label(
            self,
            textvariable=self.mapping_warning_var,
            padding=(10, 0, 10, 4),
            foreground="#a00000",
        ).pack(fill="x")

        def update_missing(*_args):
            selected_sources = {
                var.get() for var in self.vars.values()
                if var.get() != self.INDEPENDENT
            }
            missing = [name for name in source_names if name not in selected_sources]
            self.missing_var.set(
                "동기화 시 자식 Tree 아래 새 Base로 추가: " + ", ".join(missing)
                if missing else "추가될 마스터 Base 없음"
            )
            self.mapping_warning_var.set("Base 이름·GUID·Base filter는 그대로 보존됩니다.")

        for var in self.vars.values():
            var.trace_add("write", update_missing)
        update_missing()
        buttons = ttk.Frame(self, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="매핑 저장", command=self.accept).pack(side="right", padx=6)
        self.wait_window()

    def accept(self):
        self.result = {
            name: (None if var.get() == self.INDEPENDENT else var.get())
            for name, var in self.vars.items()
        }
        self.destroy()


class PreviewWindow(tk.Toplevel):
    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.title(title)
        self.geometry("900x660")
        self.transient(parent)
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=(
                "읽기 전용 미리보기입니다. 마스터 변경은 자식에게만 반영됩니다. "
                "관리 Base의 자식 초과 Generator는 삭제되며 부모·형제에게 전파되지 않습니다."
            ),
            foreground="#555",
        ).pack(anchor="w", pady=(0, 6))
        box = tk.Text(frame, wrap="word", font=("Consolas", 10))
        scroll = ttk.Scrollbar(frame, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        box.insert("1.0", text)
        box.configure(state="disabled")


class App:
    DRAGGABLE_ROLES = {"follower", "candidate", "independent", "unassigned"}
    DROP_TARGET_ROLES = {"master", "candidate", "independent", "unassigned"}

    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.board = []
        self.item_meta = {}
        self.job_queue = queue.Queue()
        self.worker = None
        self.pending_jobs = deque()
        self.active_job = None
        self.job_sequence = 0
        self.job_failures = []
        self._job_has_followup = False
        self.deferred_job_infos = deque()
        self._deferred_job_info_flush_scheduled = False
        self.job_started_at = None
        self.job_last_progress_at = None
        self.job_last_output_at = None
        self.job_stage = "대기"
        self.closing = False
        self.refresh_generation = 0
        self.shared_queue_runtime = SharedQueueRuntime(
            "spm_generator_sync"
        )
        self.drag_source_iids = ()
        self.drag_start = None
        self.drag_active = False
        self.drag_target_iid = None
        self.drag_status_before = ""
        self.document_cache = OrderedDict()
        disk_cache = load_analysis_cache()
        self.signature_cache = OrderedDict(disk_cache["signatures"].items())
        self.analysis_cache = OrderedDict(disk_cache["analyses"].items())
        root.title("SPM Generator Sync · 수종 계보 관리")
        root.geometry("1460x880")
        root.minsize(1120, 680)
        self._build_ui()
        self.root.after_idle(lambda: self.refresh(fast=True))

    def _build_ui(self):
        style = ttk.Style(self.root)
        style.configure("Treeview", rowheight=27, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        ttk.Label(
            self.root,
            padding=(10, 8, 10, 2),
            text=("SPM 행을 마스터 행 위로 드래그해 놓으면 자식으로 연결됩니다. "
                  "아직 마스터가 아닌 행 위에 놓으면 그 행을 마스터로 확정합니다."),
            foreground="#444",
        ).pack(fill="x")

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="나무 루트:").pack(side="left")
        self.root_var = tk.StringVar(value=self.config["tree_root"])
        ttk.Entry(top, textvariable=self.root_var, width=72).pack(side="left", padx=5)
        ttk.Button(top, text="...", width=3, command=self.pick_root).pack(side="left")
        ttk.Button(top, text="폴더 다시 검사", command=self.refresh).pack(side="left", padx=7)
        ttk.Button(top, text="선택 폴더 열기", command=self.open_selected_folder).pack(side="left")
        ttk.Button(top, text="선택 경로 복사", command=self.copy_selected_paths).pack(
            side="left", padx=(5, 0)
        )
        self.sk_only_var = tk.BooleanVar(value=bool(self.config.get("sk_only", True)))
        ttk.Checkbutton(
            top,
            text="SK_ SPM만 보기",
            variable=self.sk_only_var,
            command=self.refresh,
        ).pack(side="left", padx=12)

        relation = ttk.LabelFrame(self.root, text="계보 설정", padding=7)
        relation.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(relation, text="◆ 선택을 마스터로", command=self.set_selected_master).pack(side="left")
        ttk.Button(relation, text="↳ 선택을 자식으로 연결", command=self.assign_selected_followers).pack(
            side="left", padx=5
        )
        ttk.Button(relation, text="○ 선택을 독립으로", command=self.set_selected_independent).pack(side="left")
        ttk.Separator(relation, orient="vertical").pack(side="left", fill="y", padx=10)
        self.cluster_on_button = ttk.Button(
            relation, text="Cluster 관계 ON", command=lambda: self.set_selected_cluster_relation(True)
        )
        self.cluster_on_button.pack(side="left")
        self.cluster_refresh_button = ttk.Button(
            relation,
            text="Cluster 갱신",
            command=self.refresh_selected_cluster_relation,
        )
        self.cluster_refresh_button.pack(side="left", padx=(5, 0))
        self.cluster_off_button = ttk.Button(
            relation, text="Cluster 관계 OFF", command=lambda: self.set_selected_cluster_relation(False)
        )
        self.cluster_off_button.pack(side="left", padx=5)
        ttk.Separator(relation, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(relation, text="Base 매핑", command=self.edit_selected_base_map).pack(side="left")
        ttk.Button(relation, text="Base 색 분류", command=self.edit_selected_categories).pack(side="left", padx=5)

        actions = ttk.LabelFrame(self.root, text="검사 및 동기화", padding=7)
        actions.pack(fill="x", padx=8, pady=(0, 6))
        self.preview_button = ttk.Button(actions, text="변경 미리보기", command=self.preview_selected)
        self.preview_button.pack(side="left")
        self.apply_button = ttk.Button(actions, text="선택 자식 동기화", command=self.apply_selected)
        self.apply_button.pack(side="left", padx=5)
        self.apply_all_button = ttk.Button(actions, text="마스터의 모든 자식 동기화", command=self.apply_all_children)
        self.apply_all_button.pack(side="left")
        self.apply_connected_button = ttk.Button(
            actions,
            text="연결 전체 동기화 + Cluster 갱신",
            command=self.apply_connected_board,
        )
        self.apply_connected_button.pack(side="left", padx=(5, 0))
        self.cancel_job_button = ttk.Button(
            actions,
            text="현재 작업 취소",
            command=self.request_active_job_cancel,
            state="disabled",
        )
        self.cancel_job_button.pack(side="left", padx=(8, 0))
        self.verify_var = tk.BooleanVar(value=bool(self.config.get("verify_speedtree", True)))
        ttk.Checkbutton(actions, text="SpeedTree 10.1 실제 검증", variable=self.verify_var,
                        command=self.persist_config).pack(side="left", padx=12)
        self.status_var = tk.StringVar(value="대기")
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")
        self.progress_text_var = tk.StringVar(value="작업 대기")
        progress_row = ttk.Frame(actions)
        progress_row.pack(side="bottom", fill="x", pady=(8, 0))
        self.progress_bar = ttk.Progressbar(
            progress_row, mode="determinate", maximum=100, value=0,
        )
        self.progress_bar.pack(side="left", fill="x", expand=True)
        ttk.Label(
            progress_row, textvariable=self.progress_text_var, width=58, anchor="e",
        ).pack(side="right", padx=(10, 0))

        output_frame = ttk.LabelFrame(
            self.root,
            text="SpeedTree 프로세스 출력 (stdout / stderr)",
            padding=(6, 4),
        )
        output_frame.pack(fill="x", padx=8, pady=(0, 6))
        self.process_output = tk.Text(
            output_frame,
            height=6,
            wrap="none",
            font=("Consolas", 9),
            background="#111827",
            foreground="#d1d5db",
            insertbackground="#d1d5db",
            state="disabled",
        )
        output_scroll = ttk.Scrollbar(
            output_frame,
            orient="vertical",
            command=self.process_output.yview,
        )
        self.process_output.configure(yscrollcommand=output_scroll.set)
        self.process_output.grid(row=0, column=0, sticky="nsew")
        output_scroll.grid(row=0, column=1, sticky="ns")
        output_frame.columnconfigure(0, weight=1)
        self.process_output.tag_configure("stdout", foreground="#d1d5db")
        self.process_output.tag_configure("stderr", foreground="#fca5a5")
        self.process_output.tag_configure("system", foreground="#93c5fd")
        self.process_output_line_count = 0

        board_frame = ttk.Frame(self.root, padding=(8, 0, 8, 5))
        board_frame.pack(fill="both", expand=True)
        columns = ("role", "bases", "structure", "status", "last")
        self.tree = ttk.Treeview(
            board_frame, columns=columns, show="tree headings", selectmode="extended"
        )
        self.tree.heading("#0", text="나무 폴더 / SPM")
        self.tree.heading("role", text="관계")
        self.tree.heading("bases", text="따라가는 Base")
        self.tree.heading("structure", text="정규화 구조")
        self.tree.heading("status", text="동기화 상태")
        self.tree.heading("last", text="마지막 적용")
        self.tree.column("#0", width=430, minwidth=280)
        self.tree.column("role", width=145, anchor="center")
        self.tree.column("bases", width=330)
        self.tree.column("structure", width=240)
        self.tree.column("status", width=160, anchor="center")
        self.tree.column("last", width=155, anchor="center")
        yscroll = ttk.Scrollbar(board_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(board_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        board_frame.rowconfigure(0, weight=1)
        board_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("folder", font=("Segoe UI", 10, "bold"), background="#e7edf3")
        self.tree.tag_configure(
            "master", font=("Segoe UI", 10, "bold"), foreground="#073b72", background="#d8eaff"
        )
        self.tree.tag_configure("follower", background="#f4f9ff")
        self.tree.tag_configure(
            "follower_master_sync", background="#e8ddf5", foreground="#4b2c63"
        )
        self.tree.tag_configure(
            "follower_risk", background="#ffd6d6", foreground="#8b0000",
            font=("Segoe UI", 9, "bold"),
        )
        self.tree.tag_configure("independent", foreground="#555")
        self.tree.tag_configure("candidate", font=("Segoe UI", 9, "bold"), background="#fff3c4")
        self.tree.tag_configure("unassigned", foreground="#8a4b00")
        self.tree.tag_configure(
            "cluster_blend", font=("Segoe UI", 9, "bold"),
            foreground="#4b2c63", background="#eee5f5",
        )
        self.tree.tag_configure(
            "cluster_on", foreground="#0b5d2a", background="#e5f5e9",
        )
        self.tree.tag_configure(
            "cluster_pending", foreground="#8a4b00", background="#fff3c4",
        )
        self.tree.tag_configure("cluster_off", foreground="#666")
        self.tree.tag_configure(
            "drop_target", font=("Segoe UI", 10, "bold"),
            foreground="#073b24", background="#a9edc5",
        )
        self.tree.bind("<<TreeviewSelect>>", self.update_details)
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<ButtonPress-1>", self.on_drag_press, add="+")
        self.tree.bind("<B1-Motion>", self.on_drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self.on_drag_release, add="+")
        self.tree.bind("<Control-c>", self.copy_selected_paths, add="+")

        details = ttk.LabelFrame(self.root, text="선택 항목", padding=7)
        details.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(
            details,
            text=(
                "마스터 → 자식 단방향 동기화입니다. 마스터에만 있는 구조는 자식에 "
                "반영하고, 관리 Base의 자식 초과 Generator는 삭제합니다. 자식에서 "
                "부모·형제로 구조가 역전파되는 경로는 없습니다."
            ),
            foreground="#555",
        ).pack(anchor="w", pady=(0, 4))
        self.details_var = tk.StringVar(value="행을 선택하면 관계와 Base 매핑을 보여 줍니다.")
        ttk.Label(details, textvariable=self.details_var, justify="left", wraplength=1400).pack(anchor="w")
        self.delta_box = tk.Text(
            details, height=5, wrap="none", font=("Segoe UI", 9),
            background="#fbfbfb", relief="solid", borderwidth=1,
        )
        self.delta_box.pack(fill="x", pady=(5, 0))
        self.delta_box.tag_configure("missing", foreground="#9a5b00", font=("Segoe UI", 9, "bold"))
        self.delta_box.tag_configure("master_sync", foreground="#4b2c63", font=("Segoe UI", 9, "bold"))
        self.delta_box.tag_configure("remove", foreground="#9b1c1c", font=("Segoe UI", 9, "bold"))
        self.delta_box.tag_configure("muted", foreground="#777")
        self.delta_box.configure(state="disabled")

        Tooltip(
            self.preview_button,
            (
                "SPM을 수정하지 않고 마스터와 현재 SPM 사이의 Generator 구조·"
                "속성·색상 동기화를 계산합니다."
            ),
        )
        Tooltip(
            self.apply_button,
            "같은 마스터의 선택 자식들을 하나의 트랜잭션으로 처리합니다.\n"
            "마스터와 모든 대상은 _spm_backups에 먼저 백업되며 하나라도 실패하면 모두 복구됩니다.",
        )

    def persist_config(self):
        self.config.update({
            "tree_root": self.root_var.get().strip(),
            "verify_speedtree": bool(self.verify_var.get()),
            "sk_only": bool(self.sk_only_var.get()),
        })
        save_config(self.config)

    @staticmethod
    def _path_cache_key(path: Path):
        path = Path(path)
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)

    @staticmethod
    def _cache_store(cache, key, value, limit=32):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def cached_document(self, path: Path):
        key = self._path_cache_key(path)
        if key in self.document_cache:
            self.document_cache.move_to_end(key)
            return self.document_cache[key]
        document = engine.SPMDocument.from_path(path, full=False)
        return self._cache_store(self.document_cache, key, document, limit=24)

    def cached_master_signature(self, path: Path, categories: dict):
        key = json.dumps(
            [self._path_cache_key(path), categories],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if key in self.signature_cache:
            self.signature_cache.move_to_end(key)
            return self.signature_cache[key]
        document = self.cached_document(path)
        signature = engine.base_sync_signature(document, categories)
        return self._cache_store(self.signature_cache, key, signature, limit=64)

    def cached_follower_analysis(
        self, source_path: Path, target_path: Path, mapping: dict,
    ):
        key = json.dumps(
            [self._path_cache_key(source_path), self._path_cache_key(target_path), mapping],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if key in self.analysis_cache:
            self.analysis_cache.move_to_end(key)
            return self.analysis_cache[key]
        source_document = self.cached_document(source_path)
        target_document = self.cached_document(target_path)
        delta = engine.compare_base_structure(
            source_document, target_document, mapping, include_details=True
        )
        delta["scale_risk"] = engine.assess_scale_risk(
            source_document, target_document
        )
        target_hash = engine.target_sync_signature(
            source_document, target_document, mapping
        )
        return self._cache_store(
            self.analysis_cache, key, (delta, target_hash), limit=64
        )

    def _prepare_render_analysis(
        self,
        board,
        *,
        cache_snapshot,
        progress_callback=None,
    ):
        """Resolve SPM comparisons in the refresh worker, never in Tk."""

        document_cache = cache_snapshot["documents"]
        signature_cache = cache_snapshot["signatures"]
        analysis_cache = cache_snapshot["analyses"]
        masters = {}
        followers = {}

        def cached_document(path):
            key = self._path_cache_key(path)
            if key in document_cache:
                document_cache.move_to_end(key)
                return document_cache[key]
            document = engine.SPMDocument.from_path(path, full=False)
            return self._cache_store(
                document_cache, key, document, limit=24
            )

        def cached_master_signature(path, categories):
            key = json.dumps(
                [self._path_cache_key(path), categories],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if key in signature_cache:
                signature_cache.move_to_end(key)
                return signature_cache[key]
            signature = engine.base_sync_signature(
                cached_document(path), categories
            )
            return self._cache_store(
                signature_cache, key, signature, limit=64
            )

        def cached_follower_analysis(source_path, target_path, mapping):
            key = json.dumps(
                [
                    self._path_cache_key(source_path),
                    self._path_cache_key(target_path),
                    mapping,
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if key in analysis_cache:
                analysis_cache.move_to_end(key)
                return analysis_cache[key]
            source_document = cached_document(source_path)
            target_document = cached_document(target_path)
            delta = engine.compare_base_structure(
                source_document,
                target_document,
                mapping,
                include_details=True,
            )
            delta["scale_risk"] = engine.assess_scale_risk(
                source_document, target_document
            )
            target_hash = engine.target_sync_signature(
                source_document, target_document, mapping
            )
            return self._cache_store(
                analysis_cache,
                key,
                (delta, target_hash),
                limit=64,
            )

        units = []
        for folder_item in board:
            folder = Path(folder_item["folder"])
            for group in folder_item["manifest"].get("groups", []):
                master = group.get("master")
                if not master:
                    continue
                units.append(("master", folder, master, group))
                for follower in group.get("followers", []):
                    name = follower.get("file")
                    if name and follower.get("base_map_confirmed"):
                        units.append(
                            ("follower", folder, master, follower)
                        )

        total = len(units)
        for index, (kind, folder, master, row) in enumerate(
            units, start=1
        ):
            name = master if kind == "master" else row["file"]
            if progress_callback is not None:
                progress_callback(
                    f"보드 분석 {index}/{total} · {name}",
                    70 + int(29 * (index - 1) / max(1, total)),
                )
            if kind == "master":
                try:
                    signature = cached_master_signature(
                        folder / master,
                        row.get("base_categories") or {},
                    )
                    followers_in_group = row.get("followers", [])
                    if not followers_in_group:
                        status = "자식 없음"
                    elif any(
                        not item.get("base_map_confirmed")
                        for item in followers_in_group
                    ):
                        status = "매핑 필요"
                    else:
                        hashes = {
                            item.get("last_master_hash")
                            for item in followers_in_group
                        }
                        status = (
                            "최신"
                            if hashes == {signature}
                            else "마스터 변경"
                        )
                except Exception as exc:
                    status, signature = "검사 실패", str(exc)
                masters[(str(folder), master)] = (status, signature)
                continue

            try:
                delta, current_target_hash = cached_follower_analysis(
                    folder / master,
                    folder / row["file"],
                    row.get("base_map") or {},
                )
                followers[(str(folder), master, row["file"])] = {
                    "delta": delta,
                    "current_target_hash": current_target_hash,
                }
            except Exception as exc:
                followers[(str(folder), master, row["file"])] = {
                    "error": str(exc),
                }

        if progress_callback is not None:
            progress_callback(
                f"보드 분석 완료 · {total}/{total}",
                99,
            )
        return {
            "masters": masters,
            "followers": followers,
            "documents": document_cache,
            "signatures": signature_cache,
            "analyses": analysis_cache,
        }

    def pick_root(self):
        folder = filedialog.askdirectory(initialdir=self.root_var.get() or str(Path.home()))
        if folder:
            self.root_var.set(folder)
            self.persist_config()
            self.refresh()

    def refresh(self, reveal=None, *, fast=False):
        self.persist_config()
        board_root = Path(self.root_var.get().strip())
        sk_only = bool(self.sk_only_var.get())
        verify_physical = not fast
        cache_snapshot = {
            "documents": OrderedDict(
                getattr(self, "document_cache", OrderedDict())
            ),
            "signatures": OrderedDict(
                getattr(self, "signature_cache", OrderedDict())
            ),
            "analyses": OrderedDict(
                getattr(self, "analysis_cache", OrderedDict())
            ),
        }
        self.refresh_generation = getattr(
            self, "refresh_generation", 0
        ) + 1
        generation = self.refresh_generation

        def scan(report):
            scan_report = report
            if not fast:
                scan_report = lambda stage, percent: report(
                    stage, int(70 * percent / 100)
                )
            board = engine.scan_tree_folders(
                board_root,
                sk_only=sk_only,
                verify_physical=verify_physical,
                progress_callback=scan_report,
            )
            render_analysis = None
            if not fast:
                render_analysis = self._prepare_render_analysis(
                    board,
                    cache_snapshot=cache_snapshot,
                    progress_callback=report,
                )
            return {
                "board": board,
                "render_analysis": render_analysis,
            }

        def done(result):
            if generation != self.refresh_generation:
                return
            board = result["board"]
            render_analysis = result["render_analysis"]
            self.board = board
            if render_analysis is not None:
                self.document_cache = render_analysis["documents"]
                self.signature_cache = render_analysis["signatures"]
                self.analysis_cache = render_analysis["analyses"]
            self.render_board(
                fast=fast,
                prepared_analysis=render_analysis,
            )
            folder_count = len(self.board)
            spm_count = sum(len(item["spms"]) for item in self.board)
            cluster_count = sum(
                len(item.get("cluster_blends") or ())
                for item in self.board
            )
            self.status_var.set(
                f"{folder_count}개 폴더 · {spm_count}개 SPM · "
                f"Cluster blend {cluster_count}개"
            )
            if reveal:
                reveal_folder, reveal_file = reveal
                for iid, meta in self.item_meta.items():
                    if (
                        meta.get("kind") == "spm"
                        and meta.get("folder") == Path(reveal_folder)
                        and meta.get("file") == reveal_file
                    ):
                        self.tree.selection_set(iid)
                        self.tree.focus(iid)
                        self.tree.see(iid)
                        self.update_details()
                        break

        return self._start_job(
            "폴더 검사 중...",
            scan,
            done,
            queue_label=(
                "빠른 폴더 검사"
                if fast
                else "폴더 검사 및 물리 Cluster 검증"
            ),
            shared_queue=False,
        )

    def master_status(
        self,
        folder: Path,
        group: dict,
        *,
        fast=False,
    ) -> tuple[str, str | None]:
        categories = group.get("base_categories") or {}
        followers = group.get("followers", [])
        if fast:
            recorded = {
                str(item.get("last_master_hash") or "").strip()
                for item in followers
                if str(item.get("last_master_hash") or "").strip()
            }
            signature = next(iter(recorded)) if len(recorded) == 1 else None
            if not followers:
                return "자식 없음", signature
            if any(not item.get("base_map_confirmed") for item in followers):
                return "매핑 필요", signature
            return "정밀 검사 대기", signature
        try:
            master_path = folder / group["master"]
            signature = self.cached_master_signature(master_path, categories)
        except Exception as exc:
            return "검사 실패", str(exc)
        if not followers:
            return "자식 없음", signature
        if any(not item.get("base_map_confirmed") for item in followers):
            return "매핑 필요", signature
        hashes = {item.get("last_master_hash") for item in followers}
        if hashes == {signature}:
            return "최신", signature
        return "마스터 변경", signature

    def render_board(self, *, fast=False, prepared_analysis=None):
        prepared_masters = (
            prepared_analysis.get("masters", {})
            if prepared_analysis is not None else None
        )
        prepared_followers = (
            prepared_analysis.get("followers", {})
            if prepared_analysis is not None else None
        )
        self.tree.delete(*self.tree.get_children())
        self.item_meta.clear()
        for folder_item in self.board:
            folder = Path(folder_item["folder"])
            try:
                relative = folder.relative_to(Path(self.root_var.get().strip()))
                label = str(relative) if str(relative) != "." else folder.name
            except ValueError:
                label = str(folder)
            folder_iid = self.tree.insert(
                "", "end", text=f"▾ {label}", values=("폴더", "", "", "", ""),
                open=True, tags=("folder",)
            )
            self.item_meta[folder_iid] = {"kind": "folder", "folder": folder}
            manifest = folder_item["manifest"]
            related_files = set()
            for group in manifest.get("groups", []):
                master = group.get("master")
                if not master:
                    continue
                related_files.add(master)
                categories = group.get("base_categories") or {}
                category_text = ", ".join(
                    f"{name}:{category or '미분류'}" for name, category in categories.items()
                ) or "자동 분류"
                if prepared_masters is not None:
                    status, signature = prepared_masters.get(
                        (str(folder), master),
                        ("검사 실패", "준비된 분석 결과 없음"),
                    )
                else:
                    status, signature = self.master_status(
                        folder,
                        group,
                        fast=fast,
                    )
                master_iid = self.tree.insert(
                    folder_iid, "end", text=f"◆ {master}",
                    values=("MASTER", category_text, "기준 Base", status, ""),
                    open=True, tags=("master",)
                )
                self.item_meta[master_iid] = {
                    "kind": "spm", "folder": folder, "file": master,
                    "role": "master", "group": group, "signature": signature,
                }
                for follower in group.get("followers", []):
                    name = follower.get("file")
                    if not name:
                        continue
                    related_files.add(name)
                    mapping = follower.get("base_map") or {}
                    mapped = [
                        f"{target}←{source}" if source else f"{target}(독립)"
                        for target, source in mapping.items()
                    ]
                    delta = {
                        "missing": 0, "master_sync": 0, "missing_bases": 0,
                        "target_local": 0,
                        "remove": 0,
                        "missing_details": [], "master_sync_details": [],
                        "target_local_details": [],
                        "remove_details": [],
                    }
                    risk = {}
                    if not follower.get("base_map_confirmed"):
                        structure = "매핑 확인 필요"
                        current_target_hash = None
                    elif fast:
                        structure = "정밀 검사 대기"
                        current_target_hash = None
                    elif prepared_followers is not None:
                        prepared = prepared_followers.get(
                            (str(folder), master, name),
                            {"error": "준비된 분석 결과 없음"},
                        )
                        if prepared.get("error"):
                            structure = "검사 실패"
                            current_target_hash = None
                        else:
                            delta = prepared["delta"]
                            current_target_hash = prepared[
                                "current_target_hash"
                            ]
                            if delta.get("mapping_errors"):
                                structure = "매핑 오류"
                            elif delta["missing"] or delta.get("remove"):
                                structure = (
                                    "마스터→자식 반영 "
                                    f"{delta['missing']} · 초과삭제 {delta.get('remove', 0)}"
                                )
                            else:
                                structure = "동일"
                            risk = delta.get("scale_risk") or {}
                            if risk.get("level") == "blocked":
                                structure = f"⚠ 크기 {risk['ratio']:.2f}배 · " + structure
                            elif risk.get("level") == "warning":
                                structure = f"△ 크기 {risk['ratio']:.2f}배 · " + structure
                    else:
                        try:
                            delta, current_target_hash = self.cached_follower_analysis(
                                folder / master, folder / name, mapping,
                            )
                            if delta.get("mapping_errors"):
                                structure = "매핑 오류"
                            elif delta["missing"] or delta.get("remove"):
                                structure = (
                                    "마스터→자식 반영 "
                                    f"{delta['missing']} · 초과삭제 {delta.get('remove', 0)}"
                                )
                            else:
                                structure = "동일"
                            risk = delta.get("scale_risk") or {}
                            if risk.get("level") == "blocked":
                                structure = f"⚠ 크기 {risk['ratio']:.2f}배 · " + structure
                            elif risk.get("level") == "warning":
                                structure = f"△ 크기 {risk['ratio']:.2f}배 · " + structure
                        except Exception:
                            structure = "검사 실패"
                            current_target_hash = None
                    last_hash = follower.get("last_master_hash")
                    if not follower.get("base_map_confirmed"):
                        follower_status = "매핑 필요"
                    elif fast:
                        follower_status = (
                            "정밀 검사 대기"
                            if follower.get("last_sync")
                            else "미실행"
                        )
                    elif follower.get("last_sync") and last_hash != signature:
                        follower_status = "마스터 변경"
                    elif follower.get("last_sync") and delta.get("remove"):
                        follower_status = "정규화 필요"
                    elif follower.get("last_sync") and follower.get("last_target_hash") != current_target_hash:
                        follower_status = "차이 있음"
                    elif follower.get("last_sync"):
                        follower_status = "최신"
                    else:
                        follower_status = "미실행"
                    follower_iid = self.tree.insert(
                        master_iid, "end", text=f"↳ {name}",
                        values=("FOLLOWER", ", ".join(mapped), structure,
                                follower_status, follower.get("last_sync") or "—"),
                        tags=(
                            "follower_risk" if risk.get("level") == "blocked"
                            else "follower_master_sync"
                            if delta.get("missing") or delta.get("remove")
                            else "follower",
                        )
                    )
                    self.item_meta[follower_iid] = {
                        "kind": "spm", "folder": folder, "file": name,
                        "role": "follower", "group": group, "follower": follower,
                        "master": master,
                        "missing_details": delta.get("missing_details") or [],
                        "master_sync_details": delta.get("master_sync_details") or [],
                        "target_local_details": delta.get("target_local_details") or [],
                        "remove_details": delta.get("remove_details") or [],
                        "scale_risk": delta.get("scale_risk") or {},
                    }
            independent = set(manifest.get("independent", []))
            for name in folder_item["spms"]:
                if name in related_files:
                    continue
                if name in independent:
                    role = "independent"
                    role_text = "INDEPENDENT"
                    text = f"○ {name}"
                    tag = "independent"
                    status = "독립"
                elif name in folder_item.get("master_candidates", []):
                    role = "candidate"
                    role_text = "MASTER 후보"
                    text = f"★ {name}"
                    tag = "candidate"
                    status = "관계 미확정"
                else:
                    role = "unassigned"
                    role_text = "미지정"
                    text = f"? {name}"
                    tag = "unassigned"
                    status = "관계 미확정"
                iid = self.tree.insert(
                    folder_iid, "end", text=text,
                    values=(role_text, "", "", status, ""), tags=(tag,)
                )
                self.item_meta[iid] = {
                    "kind": "spm", "folder": folder, "file": name, "role": role,
                }
            cluster_blends = folder_item.get("cluster_blends") or []
            if cluster_blends:
                cluster_iid = f"cluster::{len(self.item_meta)}"
                cluster_path = Path(cluster_blends[0]["cluster_folder"])
                self.tree.insert(
                    folder_iid, "end", iid=cluster_iid,
                    text=f"▾ {cluster_path.name}",
                    values=("CLUSTER", "—", "정규화 3D + plan", "", ""),
                    open=True, tags=("cluster_blend",),
                )
                self.item_meta[cluster_iid] = {
                    "kind": "folder",
                    "folder": cluster_path,
                    "role": "cluster_folder",
                }
                for blend_index, blend_row in enumerate(cluster_blends):
                    blend_iid = f"{cluster_iid}::blend::{blend_index}"
                    targets = [
                        target for target in (blend_row.get("targets") or [])
                        if target.get("owner_target")
                    ]
                    relation = blend_row.get("folder_relation") or "empty"
                    total_count = int(blend_row.get("owner_target_count") or len(targets))
                    on_count = int(blend_row.get("owner_on_count") or 0)
                    refresh_required_count = int(
                        blend_row.get("refresh_required_count") or 0
                    )
                    refresh_deferred_count = int(
                        blend_row.get("refresh_deferred_count") or 0
                    )
                    refresh_reasons = list(
                        blend_row.get("refresh_reasons") or []
                    )
                    all_synced = bool(
                        relation == "on"
                        and targets
                        and all(target.get("status") == "synced" for target in targets)
                    )
                    if blend_row.get("registry_error"):
                        status_text = "대상 JSON 오류"
                        tag = "cluster_pending"
                    elif relation == "empty":
                        status_text = "부모 폴더에 SK_*.spm 없음"
                        tag = "cluster_pending"
                    elif relation == "off":
                        status_text = f"폴더 SK {total_count}개 전체 미적용"
                        tag = "cluster_off"
                    elif relation == "partial":
                        if refresh_required_count:
                            status_text = (
                                f"부분 연결 {on_count}/{total_count} · 원본 변경 · "
                                f"ON 대상 {refresh_required_count}개 갱신 필요"
                            )
                        else:
                            status_text = (
                                f"부분 연결 {on_count}/{total_count} · "
                                "ON/OFF로 정규화 필요"
                            )
                        tag = "cluster_pending"
                    elif refresh_required_count:
                        status_text = (
                            f"Cluster 원본 변경 · 폴더 SK "
                            f"{refresh_required_count}개 갱신 필요"
                        )
                        tag = "cluster_pending"
                    elif refresh_deferred_count:
                        status_text = (
                            f"폴더 SK {total_count}개 전체 ON · 정밀 검사 대기"
                        )
                        tag = "cluster_pending"
                    elif all_synced:
                        status_text = f"폴더 SK {total_count}개 메시 교체 완료 ✓"
                        tag = "cluster_on"
                    else:
                        status_text = f"폴더 SK {total_count}개 전체 ON · 동기화 점검 필요"
                        tag = "cluster_pending"
                    relation_label = {
                        "on": "ON", "off": "OFF",
                        "partial": "PARTIAL", "empty": "—",
                    }.get(relation, relation.upper())
                    material_names = sorted({
                        str(target.get("material"))
                        for target in targets if target.get("material")
                    })
                    material_text = ", ".join(material_names) or "원본 M_ 재료"
                    source_spm = Path(blend_row["source_spm"])
                    canonical_spm = Path(
                        blend_row.get("canonical_spm") or (
                            source_spm
                            if source_spm.name.casefold().startswith("sk_")
                            else source_spm.with_name("SK_" + source_spm.name)
                        )
                    )
                    mirror_spm = Path(
                        blend_row.get("mirror_spm") or (
                            canonical_spm.with_name(canonical_spm.name[3:])
                            if canonical_spm.name.casefold().startswith("sk_")
                            else source_spm
                        )
                    )
                    self.tree.insert(
                        cluster_iid, "end", iid=blend_iid,
                        text=f"◆ {Path(blend_row['blend']).name}",
                        values=(
                            relation_label,
                            "—",
                            f"폴더 SK {total_count}개 일괄 · {material_text}",
                            status_text,
                            "—",
                        ),
                        open=False, tags=(tag,),
                    )
                    self.item_meta[blend_iid] = {
                        "kind": "cluster_relation",
                        "folder": folder,
                        "file": Path(blend_row["blend"]).name,
                        "blend": Path(blend_row["blend"]),
                        "source_spm": source_spm,
                        "canonical_spm": canonical_spm,
                        "mirror_spm": mirror_spm,
                        "role": "cluster_relation",
                        "folder_relation": relation,
                        "relation_on": (
                            True if relation == "on"
                            else False if relation == "off"
                            else None
                        ),
                        "target_spms": [
                            Path(target["target_spm"]) for target in targets
                        ],
                        "on_target_spms": [
                            Path(target["target_spm"]) for target in targets
                            if target.get("relation_on")
                        ],
                        "target_count": total_count,
                        "on_count": on_count,
                        "all_synced": all_synced,
                        "refresh_required_count": refresh_required_count,
                        "refresh_reasons": refresh_reasons,
                        "material": material_text,
                    }
        try:
            save_analysis_cache(self.signature_cache, self.analysis_cache)
        except OSError:
            pass

    def selected_items(self):
        return [self.item_meta[iid] for iid in self.tree.selection()
                if iid in self.item_meta and self.item_meta[iid].get("kind") == "spm"]

    def selected_cluster_relations(self, *, include_cluster_folders=False):
        relation_iids = []
        for iid in self.tree.selection():
            meta = self.item_meta.get(iid, {})
            if meta.get("kind") == "cluster_relation":
                relation_iids.append(iid)
            elif include_cluster_folders and meta.get("role") == "cluster_folder":
                relation_iids.extend(self.tree.get_children(iid))

        rows = []
        seen = set()
        for iid in relation_iids:
            row = self.item_meta.get(iid, {})
            if row.get("kind") != "cluster_relation":
                continue
            key = os.path.normcase(
                str(Path(row["blend"]).absolute())
            ).casefold()
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        return rows

    def copy_selected_paths(self, _event=None):
        count = copy_selected_row_paths(
            self.root,
            self.tree,
            self.item_meta,
            paths_for_row,
        )
        if not count:
            self.status_var.set("복사할 SPM 또는 폴더 행을 선택하세요")
            return "break"
        self.status_var.set(f"전체 경로 복사 완료 · {count}개")
        return "break"

    def update_details(self, _event=None):
        cluster_relations = self.selected_cluster_relations()
        if cluster_relations:
            if len(cluster_relations) > 1:
                self.details_var.set(f"Cluster 관계 {len(cluster_relations)}개 선택됨")
            else:
                relation = cluster_relations[0]
                reason_labels = {
                    "canonical_source_missing": "canonical SPM 없음",
                    "canonical_source_changed": "canonical SPM 변경",
                    "recorded_source_conflict": "적용 영수증의 원본 해시 충돌",
                    "physical_capture_manifest_missing": (
                        "Blender physical-capture 영수증 없음"
                    ),
                    "physical_capture_changed": "Blender 촬영/plan 변경",
                }
                refresh_text = ", ".join(
                    reason_labels.get(reason, reason)
                    for reason in relation.get("refresh_reasons") or []
                )
                self.details_var.set("\n".join([
                    f"{relation['blend'].name} · 폴더 관계 "
                    f"{str(relation.get('folder_relation') or 'unknown').upper()}",
                    f"정규화 blend: {relation['blend']}",
                    f"canonical Cluster SPM: {relation['canonical_spm']}",
                    f"legacy mirror (읽기 전용): {relation['mirror_spm']}",
                    f"대상: 폴더 직하 SK_*.spm {relation.get('target_count', 0)}개 전체",
                    f"현재 적용: {relation.get('on_count', 0)}/{relation.get('target_count', 0)}",
                    (
                        f"갱신 필요: {relation.get('refresh_required_count', 0)}개"
                        + (f" · {refresh_text}" if refresh_text else "")
                    ),
                    f"교체 대상 재료: {relation.get('material') or '적용 후 manifest에서 확정'}",
                ]))
            self.show_delta_details(None)
            return
        items = self.selected_items()
        if not items:
            self.details_var.set("행을 선택하면 관계와 Base 매핑을 보여 줍니다.")
            self.show_delta_details(None)
            return
        if len(items) > 1:
            self.details_var.set(f"SPM {len(items)}개 선택됨")
            self.show_delta_details(None)
            return
        item = items[0]
        lines = [f"{item['file']} · {item['role']}", f"폴더: {item['folder']}"]
        if item["role"] == "follower":
            lines.append(f"마스터: {item['master']}")
            mapping = item["follower"].get("base_map") or {}
            lines.append("Base: " + ", ".join(
                f"{target} ← {source}" if source else f"{target} = 독립"
                for target, source in mapping.items()
            ))
            risk = item.get("scale_risk") or {}
            if risk:
                label = {"blocked": "차단", "warning": "주의", "safe": "안전"}.get(
                    risk.get("level"), risk.get("level")
                )
                lines.append(
                    f"크기 위험: {label} · 반경 {risk['master_radius']:g} → "
                    f"{risk['target_radius']:g} ({risk['ratio']:.2f}배)"
                )
                if risk.get("level") == "blocked":
                    lines.append("노드·폴리곤 폭증을 막기 위해 실제 동기화는 원본 쓰기 전에 차단됩니다.")
        self.details_var.set("\n".join(lines))
        self.show_delta_details(item)

    def show_delta_details(self, item):
        self.delta_box.configure(state="normal")
        self.delta_box.delete("1.0", "end")
        if not item or item.get("role") != "follower":
            self.delta_box.insert(
                "end",
                "SPM 행을 선택하면 마스터와 동기화될 Generator 상세가 표시됩니다.",
                "muted",
            )
            self.delta_box.configure(state="disabled")
            return
        missing = item.get("missing_details") or []
        remove = item.get("remove_details") or []
        if not missing and not remove:
            self.delta_box.insert("end", "구조 차이 없음 — 마스터와 자식의 관리 대상 구조가 같습니다.", "muted")
            self.delta_box.configure(state="disabled")
            return

        def insert_group(title, details, tag):
            self.delta_box.insert("end", f"{title} {len(details)}개\n", tag)
            for detail in details:
                category = engine.classify_base_name(detail.get("base", "")) or "branch"
                base_tag = f"base_{category}_{engine.canonical_base_name(detail.get('base', ''))}"
                self.delta_box.tag_configure(
                    base_tag,
                    foreground=rgba_to_hex(category, detail.get("base", "")),
                    font=("Segoe UI", 9, "bold"),
                )
                self.delta_box.insert("end", f"  [{detail.get('base', '?')}] ", base_tag)
                self.delta_box.insert(
                    "end", f"{detail.get('path', detail.get('name', '?'))} "
                    f"({detail.get('type', '?')})\n",
                )

        if missing:
            insert_group(
                "마스터 → 현재 SPM — 마스터 기준으로 반영될 구조",
                missing,
                "missing",
            )
        if remove:
            insert_group(
                "현재 SPM에서 삭제 — 부모에 없는 초과 Generator",
                remove,
                "remove",
            )
        self.delta_box.configure(state="disabled")

    def on_double_click(self, _event=None):
        relations = self.selected_cluster_relations()
        if len(relations) == 1:
            self.set_selected_cluster_relation(not relations[0]["relation_on"])
            return
        item = self.selected_items()
        if len(item) == 1 and item[0].get("role") == "follower":
            self.edit_selected_base_map()

    def _set_drop_target(self, iid):
        if iid == self.drag_target_iid:
            return
        self._clear_drop_target()
        if not iid:
            return
        tags = list(self.tree.item(iid, "tags"))
        if "drop_target" not in tags:
            tags.append("drop_target")
            self.tree.item(iid, tags=tuple(tags))
        self.drag_target_iid = iid

    def _clear_drop_target(self):
        if self.drag_target_iid and self.tree.exists(self.drag_target_iid):
            tags = tuple(
                tag for tag in self.tree.item(self.drag_target_iid, "tags")
                if tag != "drop_target"
            )
            self.tree.item(self.drag_target_iid, tags=tags)
        self.drag_target_iid = None

    def _reset_drag(self, restore_status=True):
        self._clear_drop_target()
        self.tree.configure(cursor="")
        if restore_status and self.drag_status_before:
            self.status_var.set(self.drag_status_before)
        self.drag_source_iids = ()
        self.drag_start = None
        self.drag_active = False

    def on_drag_press(self, event):
        self._reset_drag(restore_status=False)
        iid = self.tree.identify_row(event.y)
        meta = self.item_meta.get(iid, {})
        if meta.get("kind") != "spm" or meta.get("role") not in self.DRAGGABLE_ROLES:
            return
        selected = tuple(self.tree.selection())
        self.drag_source_iids = selected if iid in selected else (iid,)
        self.drag_source_iids = tuple(
            row for row in self.drag_source_iids
            if self.item_meta.get(row, {}).get("role") in self.DRAGGABLE_ROLES
        )
        self.drag_start = (event.x, event.y)
        self.drag_status_before = self.status_var.get()

    def _valid_drop_target(self, iid):
        target = self.item_meta.get(iid, {})
        if target.get("kind") != "spm" or target.get("role") not in self.DROP_TARGET_ROLES:
            return False
        if iid in self.drag_source_iids:
            return False
        sources = [self.item_meta.get(row, {}) for row in self.drag_source_iids]
        return bool(sources) and all(item.get("folder") == target.get("folder") for item in sources)

    def on_drag_motion(self, event):
        if not self.drag_start or not self.drag_source_iids:
            return
        if not self.drag_active:
            dx = abs(event.x - self.drag_start[0])
            dy = abs(event.y - self.drag_start[1])
            if max(dx, dy) < 6:
                return
            self.drag_active = True
        if event.y < 24:
            self.tree.yview_scroll(-1, "units")
        elif event.y > self.tree.winfo_height() - 24:
            self.tree.yview_scroll(1, "units")
        iid = self.tree.identify_row(event.y)
        if self._valid_drop_target(iid):
            self._set_drop_target(iid)
            target = self.item_meta[iid]
            prefix = "자식으로 연결" if target.get("role") == "master" else "마스터로 확정 후 연결"
            self.status_var.set(f"놓기: {target['file']} · {prefix}")
            self.tree.configure(cursor="hand2")
        else:
            self._clear_drop_target()
            self.status_var.set("마스터 또는 마스터로 만들 SPM 행 위에 놓으세요")
            self.tree.configure(cursor="arrow")
        return "break"

    def on_drag_release(self, _event):
        if not self.drag_active:
            self._reset_drag()
            return
        target_iid = self.drag_target_iid
        source_iids = self.drag_source_iids
        target = self.item_meta.get(target_iid, {})
        sources = [self.item_meta[row] for row in source_iids if row in self.item_meta]
        valid = bool(target_iid and self._valid_drop_target(target_iid))
        self._reset_drag(restore_status=not valid)
        if valid:
            self.status_var.set("자식 관계 설정 중")
            try:
                self.connect_followers_to_master(
                    sources,
                    target["file"],
                    promote_master=target.get("role") != "master",
                )
            except Exception as exc:
                self.refresh()
                messagebox.showerror("자식 연결 실패", str(exc), parent=self.root)
        return "break"

    def open_selected_folder(self):
        items = self.selected_items()
        if not items:
            selection = self.tree.selection()
            if selection and self.item_meta.get(selection[0], {}).get("kind") == "folder":
                folder = self.item_meta[selection[0]]["folder"]
            else:
                return
        else:
            folder = items[0]["folder"]
        os.startfile(str(folder))

    def set_selected_master(self):
        items = self.selected_items()
        if len(items) != 1:
            messagebox.showinfo("마스터 지정", "마스터로 지정할 SPM 하나를 선택하세요.", parent=self.root)
            return
        item = items[0]
        folder = item["folder"]
        if item.get("role") == "master":
            return

        def apply(report):
            report("마스터 관계 적용 중...", 35)
            return engine.promote_master(folder, item["file"])

        def done(result):
            self.refresh(reveal=(folder, item["file"]))
            self.status_var.set(
                f"마스터 지정 완료 · {item['file']} · "
                f"Base 색상 {result['color_updates']}개 적용"
            )

        self._start_job(
            "마스터 관계 적용 중...",
            apply,
            done,
            queue_label=f"마스터 지정 · {item['file']}",
        )

    def choose_master(self, folder: Path, manifest: dict):
        masters = [item.get("master") for item in manifest.get("groups", []) if item.get("master")]
        if not masters:
            messagebox.showinfo("자식 연결", "먼저 같은 폴더에서 마스터 SPM을 지정하세요.", parent=self.root)
            return None
        if len(masters) == 1:
            return masters[0]
        dialog = ChoiceDialog(self.root, "마스터 선택", "선택한 SPM이 따라갈 마스터:", masters)
        return dialog.result

    def assign_selected_followers(self):
        items = self.selected_items()
        if not items:
            messagebox.showinfo("자식 연결", "자식으로 연결할 SPM을 선택하세요.", parent=self.root)
            return
        folders = {str(item["folder"]) for item in items}
        if len(folders) != 1:
            messagebox.showinfo("자식 연결", "한 번에 같은 나무 폴더의 SPM만 연결할 수 있습니다.", parent=self.root)
            return
        folder = items[0]["folder"]
        manifest = engine.load_manifest(folder)
        master = self.choose_master(folder, manifest)
        if not master:
            return
        self.connect_followers_to_master(items, master, promote_master=False)

    def connect_followers_to_master(self, items, master, promote_master=False):
        if not items:
            return
        folder = items[0]["folder"]
        if any(item.get("folder") != folder for item in items):
            messagebox.showinfo(
                "자식 연결", "한 번에 같은 나무 폴더의 SPM만 연결할 수 있습니다.",
                parent=self.root,
            )
            return
        source_doc = engine.SPMDocument.from_path(folder / master, full=False)
        manifest = engine.load_manifest(folder)
        try:
            group = engine.find_group(manifest, master)
        except Exception:
            group = {}
        categories = (
            group.get("base_categories")
            or engine.source_base_categories(source_doc)
        )
        assignments = []
        for item in items:
            if item["file"] == master:
                continue
            target_doc = engine.SPMDocument.from_path(folder / item["file"], full=False)
            suggestion = engine.suggest_base_map(source_doc, target_doc, categories)
            dialog = BaseMapDialog(
                self.root, folder / master, folder / item["file"], suggestion
            )
            mapping = dialog.result
            if mapping is None:
                assignments.append(
                    (item["file"], suggestion, False)
                )
            else:
                assignments.append((item["file"], mapping, True))
        if not assignments and not promote_master:
            return

        def apply(report):
            report("자식 관계 manifest 다시 확인 중...", 15)
            if promote_master:
                engine.promote_master(folder, master)
            live_manifest = engine.load_manifest(folder)
            live_group = engine.find_group(live_manifest, master)
            live_group["base_categories"] = categories
            for name, mapping, confirmed in assignments:
                engine.assign_follower(
                    live_manifest,
                    master,
                    name,
                    mapping,
                    confirmed=confirmed,
                )
            engine.save_manifest(folder, live_manifest)
            return {
                "status": "completed",
                "followers": len(assignments),
            }

        def done(_result):
            self.refresh()

        self._start_job(
            "자식 관계 적용 중...",
            apply,
            done,
            queue_label=(
                f"자식 연결 · {master} → {len(assignments)}개"
            ),
        )

    def set_selected_independent(self):
        items = self.selected_items()
        if not items:
            return
        by_folder = {}
        for item in items:
            by_folder.setdefault(item["folder"], []).append(item["file"])
        def apply(report):
            total = len(by_folder)
            for index, (folder, names) in enumerate(
                    by_folder.items(), start=1):
                report(
                    f"독립 관계 적용 {index}/{total}",
                    int(90 * index / max(1, total)),
                )
                manifest = engine.load_manifest(folder)
                engine.set_independent(manifest, names)
                engine.save_manifest(folder, manifest)
            return {"status": "completed", "folders": total}

        self._start_job(
            "독립 관계 적용 중...",
            apply,
            lambda _result: self.refresh(),
            queue_label=(
                f"독립 관계 · SPM {sum(len(v) for v in by_folder.values())}개"
            ),
        )

    def refresh_selected_cluster_relation(self):
        self.set_selected_cluster_relation(True, refresh_only=True)

    def _execute_cluster_refresh_rows(self, rows, job_config, report):
        """Refresh exact ON relation rows without widening their target scope."""

        rows = list(rows)
        cluster_count = len(rows)
        if not cluster_count:
            return {
                "status": "ok",
                "mode": "sync",
                "cluster_count": 0,
                "results": [],
            }
        results = []
        for index, row in enumerate(rows, start=1):
            blend_path = Path(row["blend"])
            on_targets = list(row.get("on_target_spms") or [])
            blender_path = Path(
                job_config.get("blender_exe") or DEFAULT_BLENDER
            )
            unit_probe_path = Path(
                job_config.get("cluster_unit_probe")
                or DEFAULT_CLUSTER_UNIT_PROBE
            )
            capture_resolution = int(
                job_config.get("cluster_capture_resolution") or 1024
            )
            report(
                f"Cluster 갱신 {index}/{cluster_count} · {blend_path.name}",
                10 + int(80 * (index - 1) / cluster_count),
            )
            preparation = prepare_cluster_source_if_required(
                blend_path,
                on_targets,
                blender_exe=blender_path,
                unit_probe_path=unit_probe_path,
                capture_resolution=capture_resolution,
                progress_callback=lambda _stage, message, i=index: report(
                    f"Cluster 갱신 {i}/{cluster_count} · {message}",
                    10 + int(80 * (i - 0.5) / cluster_count),
                ),
            )
            relation_result = run_cluster_relation_transaction(
                blend_path,
                on_targets,
                enabled=True,
                blender_exe=blender_path,
                unit_probe_path=unit_probe_path,
                capture_resolution=capture_resolution,
                repair_runtime_config=job_config,
                force_refresh=True,
                progress_callback=(
                    lambda _stage, message, i=index: report(
                        f"Cluster refresh {i}/{cluster_count} · {message}",
                        10 + int(80 * (i - 0.5) / cluster_count),
                    )
                ),
            )
            relation_result["source_preparation"] = preparation
            results.append(relation_result)
        report("Cluster SPM 메시/Generator 검증 완료", 95)
        if cluster_count == 1:
            return results[0]
        return {
            "status": "ok",
            "mode": "sync",
            "cluster_count": cluster_count,
            "results": results,
        }

    def set_selected_cluster_relation(self, enabled, *, refresh_only=False):
        # ON/OFF is a folder-wide contract just like refresh.  Expand a
        # selected Cluster folder to its child relation rows so the folder
        # action cannot silently preserve an old one-target partial registry.
        rows = self.selected_cluster_relations(
            include_cluster_folders=True
        )
        if not rows:
            messagebox.showinfo(
                "Cluster 관계",
                "Cluster 폴더 또는 아래의 정규화 SK_*.blend 행을 선택하세요.",
                parent=self.root,
            )
            return
        blend_keys = {
            os.path.normcase(str(Path(row["blend"]).absolute())).casefold()
            for row in rows
        }
        if not refresh_only and len(blend_keys) != 1:
            messagebox.showinfo(
                "Cluster 관계",
                "한 번에는 같은 Cluster blend의 관계만 처리하세요.",
                parent=self.root,
            )
            return
        if refresh_only:
            partial_rows = [
                row for row in rows
                if row.get("folder_relation") == "partial"
            ]
            if partial_rows:
                messagebox.showinfo(
                    "Cluster 관계 불일치",
                    "일부 트리에만 연결된 기존 Cluster 관계가 있습니다. "
                    "해당 Cluster 관계를 ON 또는 OFF로 먼저 정규화하세요.",
                    parent=self.root,
                )
                return
            rows = [
                row for row in rows
                if (
                    row.get("folder_relation") == "on"
                    and row.get("on_target_spms")
                )
            ]
            if not rows:
                messagebox.showinfo(
                    "Cluster 갱신",
                    "현재 ON으로 연결된 대상이 없습니다. 먼저 관계를 ON으로 설정하세요.",
                    parent=self.root,
                )
                return
        changes = rows if refresh_only else [
            row for row in rows
            if (
                row.get("folder_relation") != ("on" if enabled else "off")
                or (enabled and not row.get("all_synced"))
            )
        ]
        if not changes:
            self.status_var.set(
                (
                    "선택 Cluster는 이미 최신입니다."
                    if refresh_only
                    else f"선택 관계는 이미 {'ON' if enabled else 'OFF'}입니다."
                )
            )
            return
        changes = [
            {
                **row,
                "on_target_spms": list(row.get("on_target_spms") or ()),
                "target_spms": list(row.get("target_spms") or ()),
            }
            for row in changes
        ]
        job_config = dict(self.config)
        blend = Path(changes[0]["blend"])
        requested_blend_keys = set(blend_keys)
        board_root = str(job_config.get("tree_root") or "")
        cluster_count = len(changes)
        if refresh_only:
            target_binding_count = sum(
                len(row.get("on_target_spms") or []) for row in changes
            )
            target_keys = {
                os.path.normcase(str(Path(target).absolute())).casefold()
                for row in changes
                for target in (row.get("on_target_spms") or [])
            }
            target_count = len(target_keys)
        else:
            target_binding_count = int(
                changes[0].get("target_count") or 0
            )
            target_count = target_binding_count
        action = (
            "갱신 · 현재 Blender 결과 재적용"
            if refresh_only
            else "ON · 메시 동기화" if enabled
            else "OFF · 원본 메시 복원"
        )
        target_label = (
            f"현재 ON으로 연결된 SK_*.spm {target_count}개 · "
            f"Cluster 연결 {target_binding_count}건"
            if refresh_only
            else f"부모 식생 폴더의 SK_*.spm {target_count}개 전체"
        )
        blend_label = (
            blend.name
            if cluster_count == 1
            else f"{cluster_count}개 (선택 Cluster 폴더 전체)"
        )
        message = (
            f"Cluster blend: {blend_label}\n관계: {action}\n"
            f"대상: {target_label}\n\n"
            + (
                (
                    "canonical Cluster SPM 변경을 검사하고, 필요하면 SK Batch blend에서 "
                    "정규화 3D prototype/plan/texture와 Atlas 설정을 자동 재생성한 뒤 "
                    "현재 ON 대상 SPM의 Generator 연결을 갱신합니다."
                    if refresh_only else
                    "Blender 5.1 백그라운드에서 Cluster Normalizer와 Atlas를 연속 실행해 "
                    "정규화 prototype/plan을 준비하고 대상 SPM의 기존 M_ 재료 mesh를 "
                    "교체합니다."
                )
                if enabled else
                "Atlas manifest의 백업 스냅샷으로 이 blend가 관리한 mesh와 Generator "
                "슬롯만 원래 상태로 복원합니다."
            )
        )
        title = "Cluster 갱신" if refresh_only else "Cluster 관계 적용"
        if not messagebox.askyesno(title, message, parent=self.root):
            return

        def apply(report):
            stage = (
                "Cluster 갱신"
                if refresh_only
                else f"Cluster 관계 {'ON' if enabled else 'OFF'}"
            )
            report(f"{stage} 준비", 10)
            if refresh_only:
                # A shared queue turn may begin long after the click. Re-scan
                # the registry/manifest contract now and refresh only blends
                # that are still fully ON; never replay captured target paths.
                runtime_board = engine.scan_tree_folders(
                    Path(board_root),
                    sk_only=bool(job_config.get("sk_only", True)),
                    verify_physical=False,
                )
                runtime_scope = self._connected_scope_from_board(
                    runtime_board
                )
                live_changes = [
                    row
                    for row in runtime_scope["cluster_rows"]
                    if (
                        os.path.normcase(
                            str(Path(row["blend"]).absolute())
                        ).casefold()
                        in requested_blend_keys
                    )
                ]
                if not live_changes:
                    raise RuntimeError(
                        "실행 차례에 Cluster 관계를 다시 확인했지만 "
                        "선택한 blend가 더 이상 완전한 ON 상태가 아닙니다."
                    )
                result = self._execute_cluster_refresh_rows(
                    live_changes,
                    job_config,
                    report,
                )
            else:
                result = run_cluster_folder_relation_transaction(
                    blend,
                    enabled=enabled,
                    blender_exe=Path(
                        job_config.get("blender_exe") or DEFAULT_BLENDER
                    ),
                    unit_probe_path=Path(
                        job_config.get("cluster_unit_probe")
                        or DEFAULT_CLUSTER_UNIT_PROBE
                    ),
                    capture_resolution=int(
                        job_config.get("cluster_capture_resolution") or 1024
                    ),
                    repair_runtime_config=job_config,
                    progress_callback=lambda _stage, message: report(
                        f"Cluster relationship · {message}",
                        55,
                    ),
                )
                report("Cluster SPM 메시/Generator 검증 완료", 95)
            return result

        def done(result):
            self.refresh()
            self.status_var.set(
                (
                    f"Cluster 갱신 완료 · Cluster {cluster_count}개 · "
                    f"ON 대상 SK {target_count}개 · 연결 {target_binding_count}건"
                    if refresh_only
                    else
                    f"Cluster 관계 {'ON' if enabled else 'OFF'} 완료 · "
                    f"폴더 SK {target_count}개"
                )
            )
            self._show_job_info(
                "Cluster 갱신 완료" if refresh_only else "Cluster 관계 완료",
                f"{blend_label}\n"
                + (
                    "현재 Blender 결과 재적용"
                    if refresh_only else f"관계 {'ON' if enabled else 'OFF'}"
                )
                + " · "
                + (
                    f"ON 대상 SK {target_count}개\n"
                    if refresh_only
                    else f"폴더 SK {target_count}개 전체\n"
                )
                + f"모드: {result.get('mode')}",
            )

        self._start_job(
            (
                "Cluster 갱신 적용 중..."
                if refresh_only
                else f"Cluster 관계 {'ON' if enabled else 'OFF'} 적용 중..."
            ),
            apply,
            done,
            queue_label=(
                f"Cluster 갱신 · {blend_label}"
                if refresh_only
                else f"Cluster 관계 {'ON' if enabled else 'OFF'} · {blend_label}"
            ),
        )

    def edit_selected_base_map(self):
        items = self.selected_items()
        if len(items) != 1 or items[0].get("role") != "follower":
            messagebox.showinfo("Base 매핑", "자식 SPM 하나를 선택하세요.", parent=self.root)
            return
        item = items[0]
        dialog = BaseMapDialog(
            self.root,
            item["folder"] / item["master"],
            item["folder"] / item["file"],
            item["follower"].get("base_map") or {},
        )
        if dialog.result is None:
            return
        mapping = dict(dialog.result)

        def apply(report):
            report("Base 매핑 manifest 다시 확인 중...", 25)
            manifest = engine.load_manifest(item["folder"])
            group = engine.find_group(manifest, item["master"])
            follower = next(
                row
                for row in group["followers"]
                if row.get("file") == item["file"]
            )
            follower["base_map"] = mapping
            follower["base_map_confirmed"] = True
            follower["last_master_hash"] = None
            follower["last_target_hash"] = None
            engine.save_manifest(item["folder"], manifest)
            return {"status": "completed"}

        self._start_job(
            "Base 매핑 적용 중...",
            apply,
            lambda _result: self.refresh(),
            queue_label=f"Base 매핑 · {item['file']}",
        )

    def edit_selected_categories(self):
        items = self.selected_items()
        if len(items) != 1 or items[0].get("role") != "master":
            messagebox.showinfo("Base 색 분류", "마스터 SPM 하나를 선택하세요.", parent=self.root)
            return
        item = items[0]
        dialog = CategoryDialog(
            self.root, item["folder"] / item["file"], item["group"].get("base_categories") or {}
        )
        if dialog.result is None:
            return
        categories = dict(dialog.result)

        def apply(report):
            report("Base 분류 manifest 다시 확인 중...", 25)
            manifest = engine.load_manifest(item["folder"])
            group = engine.find_group(manifest, item["file"])
            group["base_categories"] = categories
            for follower in group.get("followers", []):
                follower["last_master_hash"] = None
                follower["last_target_hash"] = None
            engine.save_manifest(item["folder"], manifest)
            return {"status": "completed"}

        self._start_job(
            "Base 분류 적용 중...",
            apply,
            lambda _result: self.refresh(),
            queue_label=f"Base 분류 · {item['file']}",
        )

    def followers_from_selection(self, require_same_group=True):
        items = self.selected_items()
        followers = []
        for item in items:
            if item.get("role") == "follower":
                followers.append(item)
            elif item.get("role") == "master":
                for follower in item["group"].get("followers", []):
                    followers.append({
                        "kind": "spm", "folder": item["folder"], "file": follower["file"],
                        "role": "follower", "group": item["group"], "follower": follower,
                        "master": item["file"],
                    })
        unique = {(str(item["folder"]), item["master"], item["file"]): item for item in followers}
        followers = list(unique.values())
        if not followers:
            return []
        groups = {(str(item["folder"]), item["master"]) for item in followers}
        if require_same_group and len(groups) != 1:
            messagebox.showinfo(
                "동기화 범위", "한 번에 같은 마스터의 자식들만 처리하세요.\n"
                "그래야 선택 대상 전체가 하나의 트랜잭션으로 백업·복구됩니다.", parent=self.root
            )
            return []
        return followers

    @staticmethod
    def _connected_scope_from_board(board):
        """Build a connected scope from one disk-scan board snapshot."""

        grouped = {}
        skipped = []
        seen_followers = set()
        cluster_rows = []
        seen_blends = set()
        for folder_item in board:
            folder = Path(folder_item["folder"])
            manifest = folder_item.get("manifest") or {}
            for group in manifest.get("groups") or ():
                if not isinstance(group, dict):
                    continue
                master = str(group.get("master") or "").strip()
                if not master:
                    continue
                for follower in group.get("followers") or ():
                    if not isinstance(follower, dict):
                        skipped.append({
                            "stage": "generator_sync",
                            "folder": str(folder),
                            "master": master,
                            "target": "",
                            "reason": "자식 관계 메타데이터 형식 오류",
                        })
                        continue
                    name = str(follower.get("file") or "").strip()
                    if not name:
                        continue
                    follower_key = (
                        os.path.normcase(
                            str(folder.absolute())
                        ).casefold(),
                        master.casefold(),
                        name.casefold(),
                    )
                    if follower_key in seen_followers:
                        continue
                    seen_followers.add(follower_key)
                    if not follower.get("base_map_confirmed"):
                        skipped.append({
                            "stage": "generator_sync",
                            "folder": str(folder),
                            "master": master,
                            "target": name,
                            "reason": "Base 매핑 미확정",
                        })
                        continue
                    group_key = (follower_key[0], follower_key[1])
                    scope_group = grouped.setdefault(group_key, {
                        "folder": folder,
                        "master": master,
                        "names": [],
                    })
                    scope_group["names"].append(name)

            for row in folder_item.get("cluster_blends") or ():
                if (
                    not isinstance(row, dict)
                    or row.get("folder_relation") != "on"
                ):
                    continue
                targets = [
                    target
                    for target in row.get("targets") or ()
                    if (
                        isinstance(target, dict)
                        and target.get("owner_target")
                    )
                ]
                on_target_spms = [
                    Path(target["target_spm"])
                    for target in targets
                    if target.get("relation_on") and target.get("target_spm")
                ]
                if not on_target_spms or not row.get("blend"):
                    continue
                blend = Path(row["blend"])
                blend_key = os.path.normcase(
                    str(blend.absolute())
                ).casefold()
                if blend_key in seen_blends:
                    continue
                seen_blends.add(blend_key)
                cluster_rows.append({
                    **row,
                    "kind": "cluster_relation",
                    "blend": blend,
                    "on_target_spms": on_target_spms,
                    "target_spms": [
                        Path(target["target_spm"])
                        for target in targets
                        if target.get("target_spm")
                    ],
                })

        groups = list(grouped.values())
        for group in groups:
            group["names"].sort(key=str.casefold)
        groups.sort(key=lambda group: (
            str(group["folder"]).casefold(),
            group["master"].casefold(),
        ))
        cluster_rows.sort(
            key=lambda row: str(row["blend"]).casefold()
        )
        return {
            "groups": groups,
            "cluster_rows": cluster_rows,
            "skipped": skipped,
        }

    def connected_board_scope(self):
        """Return the currently displayed board's executable connections."""

        return self._connected_scope_from_board(self.board)

    @staticmethod
    def _connected_result_summary(result):
        """Keep reports useful without serializing full SPM patch documents."""

        return {
            key: result.get(key)
            for key in (
                "status",
                "mode",
                "changed_files",
                "backup_dir",
                "master_hash",
                "cluster_count",
                "scale_skipped",
            )
            if result.get(key) is not None
        }

    def apply_connected_board(self):
        """Sync all confirmed board relations, then refresh all exact ON Clusters."""

        self._ensure_job_queue_state()
        queue_busy = self.active_job is not None or bool(self.pending_jobs)
        scope = self.connected_board_scope()
        groups = scope["groups"]
        cluster_rows = scope["cluster_rows"]
        skipped = scope["skipped"]
        follower_count = sum(len(group["names"]) for group in groups)
        cluster_target_keys = {
            os.path.normcase(str(Path(path).absolute())).casefold()
            for row in cluster_rows
            for path in row.get("on_target_spms") or ()
        }
        if not groups and not cluster_rows and not queue_busy:
            messagebox.showinfo(
                "연결 전체 처리",
                "현재 보드에 실행 가능한 연결이 없습니다.\n"
                "확정된 마스터→자식 또는 ON Cluster 관계를 먼저 설정하세요.",
                parent=self.root,
            )
            return

        verify = bool(self.verify_var.get())
        if groups and verify:
            if not Path(self.config.get("speedtree_exe", "")).is_file():
                messagebox.showerror(
                    "SpeedTree 경로",
                    "SpeedTree 실행 파일이 없습니다:\n"
                    f"{self.config.get('speedtree_exe', '')}",
                    parent=self.root,
                )
                return
            if not Path(self.config.get("xml_ini", "")).is_file():
                messagebox.showerror(
                    "XML 검증 설정",
                    "XML export 설정 파일이 없습니다:\n"
                    f"{self.config.get('xml_ini', '')}",
                    parent=self.root,
                )
                return

        message = (
            "현재 보드의 확정된 연결만 일괄 처리합니다.\n\n"
            f"Generator Sync: 마스터 {len(groups)}개 · 자식 {follower_count}개\n"
            f"Cluster 갱신: ON 관계 {len(cluster_rows)}개 · "
            f"대상 SK {len(cluster_target_keys)}개\n"
            f"사전 제외: {len(skipped)}개\n\n"
            "순서: 마스터→자식 데이터 동기화 후 ON Cluster 결과를 재적용합니다.\n"
            "독립·미지정·OFF·PARTIAL 관계는 변경하지 않습니다. "
            "개별 실패는 보고서에 기록하고 나머지 연결은 계속 처리합니다."
        )
        if verify and groups:
            message += "\nGenerator Sync 대상은 SpeedTree 10.1 사전검사를 수행합니다."
        if queue_busy:
            message += (
                "\n현재 작업 뒤에 대기하며, 실제 시작 시 연결 범위를 다시 검사합니다."
            )
        if not messagebox.askyesno(
            "연결 전체 동기화 + Cluster 갱신",
            message,
            parent=self.root,
        ):
            return

        job_config = dict(self.config)
        speedtree_exe = Path(job_config.get("speedtree_exe") or "")
        xml_ini = Path(job_config.get("xml_ini") or "")
        board_root = self.root_var.get().strip()

        def apply(report):
            started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            raise_if_cancelled = getattr(
                report, "raise_if_cancelled", lambda: None
            )
            process_output = getattr(report, "output", None)
            cancel_requested = getattr(report, "cancel_requested", None)
            report("실행 시점 연결 관계 다시 검사 중", 1)
            runtime_board = engine.scan_tree_folders(
                Path(board_root),
                sk_only=bool(job_config.get("sk_only", True)),
                verify_physical=False,
            )
            runtime_scope = self._connected_scope_from_board(runtime_board)
            runtime_groups = runtime_scope["groups"]
            runtime_cluster_rows = runtime_scope["cluster_rows"]
            runtime_skipped = runtime_scope["skipped"]
            runtime_follower_count = sum(
                len(group["names"]) for group in runtime_groups
            )
            runtime_cluster_target_keys = {
                os.path.normcase(
                    str(Path(path).absolute())
                ).casefold()
                for row in runtime_cluster_rows
                for path in row.get("on_target_spms") or ()
            }
            total_units = (
                len(runtime_groups) + len(runtime_cluster_rows)
            )
            payload = {
                "schema_version": 1,
                "started_at": started_at,
                "root": board_root,
                "verify_speedtree": verify,
                "queued_scope": {
                    "generator_group_count": len(groups),
                    "generator_follower_count": follower_count,
                    "cluster_relation_count": len(cluster_rows),
                    "cluster_target_count": len(cluster_target_keys),
                },
                "scope": {
                    "generator_group_count": len(runtime_groups),
                    "generator_follower_count": runtime_follower_count,
                    "cluster_relation_count": len(runtime_cluster_rows),
                    "cluster_target_count": len(
                        runtime_cluster_target_keys
                    ),
                },
                "skipped": list(runtime_skipped),
                "generator_sync": [],
                "cluster_refresh": [],
                "failures": [],
            }

            def unit_report(unit_index, label, stage, percent):
                overall = 2 + int(
                    94 * (
                        unit_index + max(0, min(100, int(percent))) / 100
                    ) / max(1, total_units)
                )
                report(f"{label} · {stage}", overall)

            unit_index = 0
            cancelled_exc = None
            for group in runtime_groups:
                label = (
                    f"Generator {unit_index + 1}/{total_units} · "
                    f"{group['master']}"
                )
                try:
                    raise_if_cancelled()
                    result = engine.apply_group_transaction(
                        group["folder"],
                        group["master"],
                        group["names"],
                        verify_speedtree=verify,
                        speedtree_exe=speedtree_exe,
                        xml_ini=xml_ini,
                        skip_blocked_scale=True,
                        progress_callback=(
                            lambda stage, percent, i=unit_index, text=label:
                            unit_report(i, text, stage, percent)
                        ),
                        process_output_callback=process_output,
                        cancel_requested=cancel_requested,
                    )
                    for entry in result.get("scale_skipped") or ():
                        risk = entry.get("scale_risk") or {}
                        payload["skipped"].append({
                            "stage": "generator_sync",
                            "folder": str(group["folder"]),
                            "master": group["master"],
                            "target": Path(
                                entry.get("target") or ""
                            ).name,
                            "reason": (
                                entry.get("reason")
                                or "크기 폭증 위험"
                            ),
                            "scale_risk": risk,
                        })
                    payload["generator_sync"].append({
                        "folder": str(group["folder"]),
                        "master": group["master"],
                        "followers": list(group["names"]),
                        "result": self._connected_result_summary(result),
                    })
                except engine.SyncCancelled as exc:
                    cancelled_exc = exc
                    payload["cancellation"] = exc.as_dict()
                    break
                except Exception as exc:
                    payload["failures"].append({
                        "stage": "generator_sync",
                        "folder": str(group["folder"]),
                        "master": group["master"],
                        "targets": list(group["names"]),
                        "reason": str(exc),
                    })
                unit_index += 1

            for row in runtime_cluster_rows:
                if cancelled_exc is not None:
                    break
                blend = Path(row["blend"])
                label = (
                    f"Cluster {unit_index + 1}/{total_units} · {blend.name}"
                )
                try:
                    raise_if_cancelled()
                    result = self._execute_cluster_refresh_rows(
                        [row],
                        job_config,
                        lambda stage, percent, i=unit_index, text=label:
                        unit_report(i, text, stage, percent),
                    )
                    payload["cluster_refresh"].append({
                        "blend": str(blend),
                        "targets": [
                            str(path)
                            for path in row.get("on_target_spms") or ()
                        ],
                        "result": self._connected_result_summary(result),
                    })
                    raise_if_cancelled()
                except engine.SyncCancelled as exc:
                    cancelled_exc = exc
                    payload["cancellation"] = exc.as_dict()
                    break
                except Exception as exc:
                    payload["failures"].append({
                        "stage": "cluster_refresh",
                        "blend": str(blend),
                        "targets": [
                            str(path)
                            for path in row.get("on_target_spms") or ()
                        ],
                        "reason": str(exc),
                    })
                unit_index += 1

            success_count = (
                len(payload["generator_sync"])
                + len(payload["cluster_refresh"])
            )
            payload["status"] = (
                "cancelled"
                if cancelled_exc is not None
                else "ok"
                if not payload["failures"]
                else "partial"
                if success_count
                else "failed"
            )
            payload["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            report("실행 보고서 저장 중", 98)
            try:
                payload["report_path"] = str(
                    write_connected_run_report(payload)
                )
            except Exception as exc:
                payload["report_error"] = str(exc)
            if cancelled_exc is not None:
                cancelled_exc.report_payload = payload
                cancelled_exc.report_path = payload.get("report_path")
                raise cancelled_exc
            return payload

        def done(result):
            self.refresh()
            sync_ok = len(result["generator_sync"])
            cluster_ok = len(result["cluster_refresh"])
            failure_count = len(result["failures"])
            runtime_group_count = int(
                result["scope"]["generator_group_count"]
            )
            runtime_cluster_count = int(
                result["scope"]["cluster_relation_count"]
            )
            report_path = result.get("report_path") or (
                f"저장 실패: {result.get('report_error', '알 수 없음')}"
            )
            self.status_var.set(
                f"연결 전체 처리 완료 · Generator "
                f"{sync_ok}/{runtime_group_count} · "
                f"Cluster {cluster_ok}/{runtime_cluster_count} · "
                f"실패 {failure_count}"
            )
            failure_lines = [
                f"· {failure.get('master') or Path(failure.get('blend', '')).name}: "
                f"{failure['reason']}"
                for failure in result["failures"][:8]
            ]
            detail = (
                f"Generator Sync 성공: {sync_ok}/{runtime_group_count} 그룹\n"
                f"Cluster 갱신 성공: {cluster_ok}/{runtime_cluster_count} 관계\n"
                f"사전 제외: {len(result['skipped'])}개\n"
                f"실패: {failure_count}개\n\n"
                f"보고서: {report_path}"
            )
            if failure_lines:
                detail += "\n\n실패 요약:\n" + "\n".join(failure_lines)
            self._show_job_info(
                (
                    "연결 전체 처리 완료"
                    if not failure_count
                    else "연결 전체 처리 완료 · 실패 있음"
                ),
                detail,
            )

        def cancelled(exc):
            self.refresh()
            payload = getattr(exc, "report_payload", {}) or {}
            report_path = getattr(exc, "report_path", None) or payload.get(
                "report_path"
            )
            self._show_job_info(
                "연결 전체 처리 취소됨",
                f"상태: {self._cancel_state_label(exc.termination_state)}\n"
                f"완료된 Generator 그룹: "
                f"{len(payload.get('generator_sync') or ())}\n"
                f"완료된 Cluster 관계: "
                f"{len(payload.get('cluster_refresh') or ())}\n\n"
                f"보고서: {report_path or '저장되지 않음'}",
            )

        self._start_job(
            "연결 데이터 동기화 및 Cluster 갱신 중...",
            apply,
            done,
            queue_label=(
                f"연결 전체 처리 · 자식 {follower_count}개 · "
                f"Cluster {len(cluster_rows)}개"
            ),
            on_cancel=cancelled,
        )

    def _ensure_job_queue_state(self):
        """Initialize queue fields for normal startup and lightweight tests."""
        if not hasattr(self, "pending_jobs"):
            self.pending_jobs = deque()
        if not hasattr(self, "active_job"):
            self.active_job = None
        if not hasattr(self, "job_sequence"):
            self.job_sequence = 0
        if not hasattr(self, "job_failures"):
            self.job_failures = []
        if not hasattr(self, "job_cancellations"):
            self.job_cancellations = []
        if not hasattr(self, "queue_run_total"):
            self.queue_run_total = 0
        if not hasattr(self, "queue_run_completed"):
            self.queue_run_completed = 0
        if not hasattr(self, "_job_has_followup"):
            self._job_has_followup = False
        if not hasattr(self, "deferred_job_infos"):
            self.deferred_job_infos = deque()
        if not hasattr(self, "_deferred_job_info_flush_scheduled"):
            self._deferred_job_info_flush_scheduled = False
        if not hasattr(self, "job_last_progress_at"):
            self.job_last_progress_at = None
        if not hasattr(self, "job_last_output_at"):
            self.job_last_output_at = None
        if not hasattr(self, "closing"):
            self.closing = False

    @staticmethod
    def _cancel_state_label(state):
        return {
            "cancelled_at_safe_boundary": "안전 경계에서 취소",
            "cancelled_before_launch": "실행 전 취소",
            "cancelled_after_exit": "종료 경합 중 취소",
            "cancelled_terminated": "정상 종료 요청으로 취소",
            "cancelled_killed": "강제 종료 fallback으로 취소",
        }.get(str(state), str(state or "취소"))

    def _append_process_output(self, channel, line):
        self._append_process_output_batch([(channel, line)])

    def _append_process_output_batch(self, entries):
        widget = getattr(self, "process_output", None)
        if widget is None or not entries:
            return
        fragments = []
        for channel, line in entries:
            channel = (
                channel
                if channel in {"stdout", "stderr", "system"}
                else "system"
            )
            prefix = {
                "stdout": "[stdout] ",
                "stderr": "[stderr] ",
                "system": "[system] ",
            }[channel]
            fragments.extend((prefix + str(line) + "\n", (channel,)))
        try:
            widget.configure(state="normal")
            widget.insert("end", fragments[0], *fragments[1:])
            self.process_output_line_count = (
                getattr(self, "process_output_line_count", 0) + len(entries)
            )
            overflow = self.process_output_line_count - 3000
            if overflow >= 200:
                widget.delete("1.0", f"{overflow + 1}.0")
                self.process_output_line_count -= overflow
            widget.see("end")
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def request_active_job_cancel(self):
        """Mark the current FIFO job cancelled without touching other jobs."""

        self._ensure_job_queue_state()
        job = self.active_job
        if job is None:
            return False
        cancel_event = job.get("cancel_event")
        if cancel_event is None or cancel_event.is_set():
            return False
        job["cancel_state"] = "requested"
        job["cancel_requested_at"] = time.monotonic()
        cancel_event.set()
        self.status_var.set(f"취소 요청됨 · {job['queue_label']}")
        self.progress_text_var.set(
            f"취소 요청됨 · {self.job_stage} · 안전 종료 대기"
        )
        button = getattr(self, "cancel_job_button", None)
        if button is not None:
            button.configure(state="disabled")
        self._append_process_output(
            "system",
            "취소 요청을 접수했습니다. 현재 안전 경계 또는 실행 중인 "
            "SpeedTree 프로세스 종료를 기다립니다.",
        )
        return True

    def _show_job_info(self, title, message):
        """Defer modal completion detail until every follow-up has run."""
        self._ensure_job_queue_state()
        if self._job_has_followup or self.pending_jobs:
            self.deferred_job_infos.append((str(title), str(message)))
            self.status_var.set(
                f"{title} · 상세는 대기열 완료 후 표시"
            )
            return
        messagebox.showinfo(title, message, parent=self.root)

    def _flush_deferred_job_infos(self):
        self._ensure_job_queue_state()
        self._deferred_job_info_flush_scheduled = False
        if (
            self.active_job is not None
            or self.pending_jobs
            or not self.deferred_job_infos
        ):
            return
        notices = list(self.deferred_job_infos)
        self.deferred_job_infos.clear()
        if len(notices) == 1:
            title, message = notices[0]
        else:
            title = "대기열 작업 완료"
            message = "\n\n".join(
                f"[{notice_title}]\n{notice_message}"
                for notice_title, notice_message in notices
            )
        messagebox.showinfo(title, message, parent=self.root)

    def _schedule_deferred_job_infos(self):
        self._ensure_job_queue_state()
        if (
            self.deferred_job_infos
            and not self._deferred_job_info_flush_scheduled
        ):
            self._deferred_job_info_flush_scheduled = True
            self.root.after_idle(self._flush_deferred_job_infos)

    def _start_job(
        self,
        label,
        func,
        on_success,
        *,
        queue_label=None,
        shared_queue=True,
        on_cancel=None,
    ):
        """Capture one request in the window-local FIFO."""
        self._ensure_job_queue_state()
        if self.active_job is None and not self.pending_jobs:
            self.job_failures = []
            self.job_cancellations = []
            self.queue_run_total = 0
            self.queue_run_completed = 0
        self.job_sequence += 1
        display_label = str(queue_label or label)
        job = {
            "id": self.job_sequence,
            "label": str(label),
            "queue_label": display_label,
            "func": func,
            "on_success": on_success,
            "on_cancel": on_cancel,
            "shared_queue": bool(shared_queue),
            "cancel_event": threading.Event(),
            "cancel_state": "queued",
        }
        self.pending_jobs.append(job)
        self.queue_run_total += 1
        if self.active_job is None:
            self._start_next_job()
        else:
            waiting = len(self.pending_jobs)
            self.status_var.set(
                f"대기열 추가 · {job['queue_label']} · 대기 {waiting}개"
            )
            self.progress_text_var.set(
                f"{self.job_stage} · 대기열 {waiting}개"
            )
        return job["id"]

    def _start_next_job(self):
        self._ensure_job_queue_state()
        if self.closing or self.active_job is not None or not self.pending_jobs:
            return
        job = self.pending_jobs.popleft()
        job.setdefault("cancel_event", threading.Event())
        job.setdefault("cancel_state", "queued")
        job.setdefault("on_cancel", None)
        self.active_job = job
        self.status_var.set(
            f"{job['queue_label']} · 실행"
            + (
                f" · 대기 {len(self.pending_jobs)}개"
                if self.pending_jobs else ""
            )
        )
        self.job_started_at = time.monotonic()
        self.job_last_progress_at = self.job_started_at
        self.job_last_output_at = None
        self.job_stage = job["label"]
        job["cancel_state"] = "running"
        self.progress_bar.configure(value=1)
        self.progress_text_var.set(
            f"{job['queue_label']} · 00:00"
            + (
                f" · 대기 {len(self.pending_jobs)}개"
                if self.pending_jobs else ""
            )
        )
        cancel_button = getattr(self, "cancel_job_button", None)
        if cancel_button is not None:
            cancel_button.configure(state="normal")

        def report(stage, percent):
            self.job_queue.put((
                "progress",
                job["id"],
                str(stage),
                max(0, min(100, int(percent))),
            ))

        def output(channel, line):
            self.job_queue.put((
                "output",
                job["id"],
                str(channel),
                str(line),
            ))

        def raise_if_cancelled():
            if job["cancel_event"].is_set():
                raise engine.SyncCancelled("cancelled_at_safe_boundary")

        report.output = output
        report.cancel_requested = job["cancel_event"].is_set
        report.cancel_event = job["cancel_event"]
        report.raise_if_cancelled = raise_if_cancelled

        def run():
            lease = None
            try:
                shared_runtime = getattr(
                    self, "shared_queue_runtime", None
                )
                shared_job_id = job.get("shared_queue_job_id")
                if (
                    job["shared_queue"]
                    and shared_job_id is None
                    and shared_runtime is not None
                ):
                    report("공용 대기열 등록 중", 0)
                    try:
                        shared = shared_runtime.enqueue(
                            job["queue_label"],
                            {
                                "tool": "spm_generator_sync",
                                "local_job_id": job["id"],
                                "label": job["queue_label"],
                            },
                        )
                    except Exception as exc:
                        raise SharedQueueEnqueueError(
                            "다른 창과 동시에 실행되지 않도록 작업을 "
                            f"시작하지 않았습니다.\n\n{exc}"
                        ) from exc
                    shared_job_id = shared["id"]
                    job["shared_queue_job_id"] = shared_job_id
                    job["shared_queue_sequence"] = shared["sequence"]
                if shared_job_id and shared_runtime is not None:
                    def report_wait(wait_state):
                        position = wait_state.get("position")
                        queued = wait_state.get("queued_count", 0)
                        report(
                            (
                                "공용 대기열 대기"
                                + (
                                    f" · 전체 {position}번째"
                                    if position else ""
                                )
                                + f" · 대기 {queued}개"
                            ),
                            0,
                        )

                    lease = shared_runtime.wait_for_turn(
                        shared_job_id,
                        on_wait=report_wait,
                        cancel_event=job["cancel_event"],
                    )
                    report("공용 대기열 진입 · 단독 실행", 1)
                raise_if_cancelled()
                payload = job["func"](report)
                raise_if_cancelled()
                queue_success = not (
                    isinstance(payload, dict)
                    and (
                        payload.get("failures")
                        or str(payload.get("status") or "").casefold()
                        in {"failed", "error", "partial", "cancelled"}
                    )
                )
                if lease is not None:
                    lease.finish(
                        success=queue_success,
                        result={
                            "tool": "spm_generator_sync",
                            "local_job_id": job["id"],
                            "outcome": (
                                "completed"
                                if queue_success else "failed"
                            ),
                        },
                    )
                self.job_queue.put(("done", job["id"], True, payload))
            except Exception as exc:
                if job["cancel_event"].is_set() and not isinstance(
                    exc, engine.SyncCancelled
                ):
                    exc = engine.SyncCancelled(
                        "cancelled_at_safe_boundary"
                    )
                cancelled = isinstance(exc, engine.SyncCancelled)
                if cancelled:
                    job["cancel_state"] = exc.termination_state
                if lease is not None and not lease.finished:
                    try:
                        lease.finish(
                            success=False,
                            result={
                                "tool": "spm_generator_sync",
                                "local_job_id": job["id"],
                                "outcome": (
                                    "cancelled" if cancelled else "failed"
                                ),
                                "termination_state": getattr(
                                    exc, "termination_state", None
                                ),
                                "error": str(exc),
                            },
                        )
                    except Exception as queue_exc:
                        exc = RuntimeError(
                            f"{exc}; 공용 대기열 종료 기록 실패: {queue_exc}"
                        )
                elif shared_job_id and shared_runtime is not None:
                    try:
                        shared_runtime.cancel(
                            shared_job_id,
                            reason="worker_wait_failed",
                        )
                    except Exception:
                        # wait_for_turn may already have terminalized and
                        # removed the ticket.  Preserve the causal failure;
                        # queue recovery remains the runtime's responsibility.
                        pass
                self.job_queue.put(("done", job["id"], False, exc))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_job)

    def _poll_job(self):
        self._ensure_job_queue_state()
        completed = None
        processed = 0
        output_batch = []
        while processed < 500:
            try:
                event = self.job_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if event[0] == "progress":
                _kind, job_id, stage, percent = event
                if (
                    self.active_job is None
                    or job_id != self.active_job["id"]
                ):
                    continue
                self.job_stage = stage
                self.job_last_progress_at = time.monotonic()
                self.progress_bar.configure(value=percent)
                self.status_var.set(
                    stage
                    + (
                        f" · 대기 {len(self.pending_jobs)}개"
                        if self.pending_jobs else ""
                    )
                )
            elif event[0] == "output":
                _kind, job_id, channel, line = event
                if (
                    self.active_job is None
                    or job_id != self.active_job["id"]
                ):
                    continue
                now = time.monotonic()
                self.job_last_progress_at = now
                self.job_last_output_at = now
                output_batch.append((channel, line))
            elif event[0] == "done":
                _kind, job_id, _ok, _payload = event
                if (
                    self.active_job is not None
                    and job_id == self.active_job["id"]
                ):
                    completed = event

        self._append_process_output_batch(output_batch)

        elapsed = max(0, int(time.monotonic() - (self.job_started_at or time.monotonic())))
        elapsed_text = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        heartbeat_age = max(
            0,
            int(
                time.monotonic()
                - (self.job_last_progress_at or time.monotonic())
            ),
        )
        output_age = (
            None
            if self.job_last_output_at is None
            else max(0, int(time.monotonic() - self.job_last_output_at))
        )
        self.progress_text_var.set(
            f"{self.job_stage} · {elapsed_text} · "
            f"마지막 진행 {heartbeat_age}초 전 · "
            + (
                f"마지막 출력 {output_age}초 전"
                if output_age is not None
                else "프로세스 출력 대기"
            )
            + (
                f" · 대기 {len(self.pending_jobs)}개"
                if self.pending_jobs else ""
            )
        )
        if (
            completed is not None
            and self.worker is not None
            and self.worker.is_alive()
        ):
            # The worker queues ``done`` as its final action, but can still be
            # unwinding the closure that owns this App and its Tk variables.
            # Keep completion on the Tk thread until the worker has actually
            # exited; otherwise a fast teardown may finalize Tk objects from
            # the worker thread.
            self.job_queue.put(completed)
            self.root.after(10, self._poll_job)
            return
        if completed is None:
            if processed >= 500 or not self.job_queue.empty():
                self.root.after(10, self._poll_job)
            elif self.worker and self.worker.is_alive():
                self.root.after(100, self._poll_job)
            return

        _kind, _job_id, ok, payload = completed
        job = self.active_job
        self.queue_run_completed += 1
        has_followup = bool(self.pending_jobs)
        self._job_has_followup = has_followup
        deferred_count = len(self.deferred_job_infos)
        if ok:
            self.progress_bar.configure(value=100)
            self.progress_text_var.set(f"완료 · {elapsed_text}")
            try:
                job["on_success"](payload)
            except Exception as exc:
                ok = False
                payload = exc
                while len(self.deferred_job_infos) > deferred_count:
                    self.deferred_job_infos.pop()
        has_followup = bool(self.pending_jobs)
        self._job_has_followup = has_followup
        cancelled = not ok and isinstance(payload, engine.SyncCancelled)
        if cancelled:
            state = payload.termination_state
            label = self._cancel_state_label(state)
            job["cancel_state"] = state
            self.job_cancellations.append((job["queue_label"], state))
            self.status_var.set(
                f"취소됨 · {job['queue_label']} · {label}"
                + (" · 다음 대기 작업 시작" if has_followup else "")
            )
            self.progress_text_var.set(f"취소됨 · {label} · {elapsed_text}")
            self._append_process_output("system", f"작업 취소 완료: {label}")
            on_cancel = job.get("on_cancel")
            if callable(on_cancel) and not self.closing:
                try:
                    on_cancel(payload)
                except Exception as exc:
                    self.job_failures.append(
                        (job["queue_label"] + " 취소 후 처리", str(exc))
                    )
        elif not ok:
            self.job_failures.append((job["queue_label"], str(payload)))
            self.status_var.set(
                f"실패 · {job['queue_label']}"
                + (" · 다음 대기 작업 시작" if has_followup else "")
            )
            self.progress_text_var.set(
                f"실패 · {self.job_stage} · {elapsed_text}"
            )
            if not has_followup and not self.closing:
                messagebox.showerror(
                    (
                        "공용 대기열 등록 실패"
                        if isinstance(payload, SharedQueueEnqueueError)
                        else "작업 실패"
                    ),
                    str(payload),
                    parent=self.root,
                )
        self._job_has_followup = False
        self.active_job = None
        self.worker = None
        cancel_button = getattr(self, "cancel_job_button", None)
        if cancel_button is not None:
            cancel_button.configure(state="disabled")
        if self.pending_jobs and not self.closing:
            self._start_next_job()
            if self.active_job is not None:
                return
        if self.queue_run_total > 1:
            if self.job_failures or self.job_cancellations:
                self.status_var.set(
                    f"대기열 완료 · {self.queue_run_completed}개 처리 · "
                    f"실패 {len(self.job_failures)}개 · "
                    f"취소 {len(self.job_cancellations)}개"
                )
            else:
                self.status_var.set(
                    f"대기열 완료 · {self.queue_run_completed}개"
                )
            self.progress_text_var.set(
                f"대기열 완료 · {self.queue_run_completed}/"
                f"{self.queue_run_total}"
            )
        self._schedule_deferred_job_infos()

    def preview_selected(self):
        followers = self.followers_from_selection()
        if not followers:
            messagebox.showinfo("미리보기", "자식 또는 마스터 행을 선택하세요.", parent=self.root)
            return

        def build(report):
            folder = followers[0]["folder"]
            master = followers[0]["master"]
            prepared = engine.build_group_sync_plans(
                folder,
                master,
                [item["file"] for item in followers],
                progress_callback=report,
            )
            report("미리보기 결과 정리 중", 95)
            return prepared["plans"]

        def show(plans):
            lines = []
            for index, plan in enumerate(plans):
                if index:
                    lines.extend(["", "=" * 88, ""])
                lines.extend(plan.summary_lines())
                if plan.master_color_updates:
                    lines.append(f"마스터 색상 정리: {plan.master_color_updates}개 아이콘")
            PreviewWindow(self.root, "SPM Generator Sync · 변경 미리보기", "\n".join(lines))
            self.status_var.set(f"미리보기 완료 · 자식 {len(plans)}개")

        self._start_job(
            "변경 계산 중...",
            build,
            show,
            queue_label=(
                f"변경 미리보기 · {followers[0]['master']} → "
                f"자식 {len(followers)}개"
            ),
        )

    def _apply_followers(self, followers):
        if not followers:
            return
        folder = followers[0]["folder"]
        master = followers[0]["master"]
        names = [item["file"] for item in followers]
        blocked = [
            item for item in followers
            if (item.get("scale_risk") or {}).get("level") == "blocked"
        ]
        if blocked:
            details = "\n".join(
                f"{item['file']}: {(item['scale_risk'])['ratio']:.2f}배 "
                f"({item['scale_risk']['master_radius']:g} → "
                f"{item['scale_risk']['target_radius']:g})"
                for item in blocked
            )
            messagebox.showerror(
                "크기 폭증 위험 · 동기화 차단",
                "작은 마스터용 Base를 큰 나무에 적용하면 거리 기반 노드와 폴리곤이 "
                "폭증할 수 있어 원본을 수정하지 않습니다. 큰 나무용 별도 마스터를 "
                f"사용하세요.\n\n{details}",
                parent=self.root,
            )
            return
        verify = bool(self.verify_var.get())
        if verify:
            if not Path(self.config.get("speedtree_exe", "")).is_file():
                messagebox.showerror(
                    "SpeedTree 경로", f"SpeedTree 실행 파일이 없습니다:\n{self.config.get('speedtree_exe', '')}",
                    parent=self.root,
                )
                return
            if not Path(self.config.get("xml_ini", "")).is_file():
                messagebox.showerror(
                    "XML 검증 설정", f"XML export 설정 파일이 없습니다:\n{self.config.get('xml_ini', '')}",
                    parent=self.root,
                )
                return
        message = (
            f"마스터: {master}\n자식: {', '.join(names)}\n\n"
            "마스터는 읽기 전용 기준으로 사용하며 선택 자식만 백업·저장됩니다.\n"
            "마스터에만 있는 Generator 구조는 자식에 반영됩니다. 자식에서 발견한 추가 "
            "Generator 구조는 관리 Base에서 삭제되고 부모·형제에게 전파되지 않습니다. "
            "Generation Pass는 자식 계산 순서에 맞게 재정규화합니다. GUID, Random Seed, "
            "재질과 Node/Freehand Edit는 각 SPM의 로컬 값을 유지합니다."
        )
        if verify:
            message += (
                "\n적용 전 임시 복사본을 SpeedTree 10.1에서 실제 계산·XML export합니다. "
                "모두 성공한 뒤에만 원본을 백업하고 저장합니다."
            )
        if not messagebox.askyesno("동기화 적용", message, parent=self.root):
            return
        speedtree_exe = Path(self.config["speedtree_exe"])
        xml_ini = Path(self.config["xml_ini"])

        def apply(report):
            return engine.apply_group_transaction(
                folder, master, names,
                verify_speedtree=verify,
                speedtree_exe=speedtree_exe,
                xml_ini=xml_ini,
                progress_callback=report,
                process_output_callback=getattr(report, "output", None),
                cancel_requested=getattr(report, "cancel_requested", None),
            )

        def done(result):
            self.refresh()
            changed = "\n".join(Path(path).name for path in result["changed_files"]) or "변경 없음"
            self._show_job_info(
                "동기화 완료",
                f"상태: {result['status']}\n\n변경 파일:\n{changed}\n\n"
                f"백업: {result.get('backup_dir') or '새 백업 없음'}",
            )

        def cancelled(_exc):
            self.refresh()

        self._start_job(
            "SPM 동기화 및 검증 중...",
            apply,
            done,
            queue_label=f"SPM 동기화 · {master} → 자식 {len(names)}개",
            on_cancel=cancelled,
        )

    def apply_selected(self):
        self._apply_followers(self.followers_from_selection())

    def apply_all_children(self):
        items = self.selected_items()
        masters = [item for item in items if item.get("role") == "master"]
        if len(masters) != 1:
            messagebox.showinfo("전체 자식", "마스터 행 하나를 선택하세요.", parent=self.root)
            return
        self._apply_followers(self.followers_from_selection())

    def shutdown_shared_queue(self):
        """Cancel local work, stop this job's child, then close queue tickets."""

        self._ensure_job_queue_state()
        self.closing = True
        active = self.active_job
        if active is not None:
            cancel_event = active.get("cancel_event")
            if cancel_event is not None:
                active["cancel_state"] = "requested_on_close"
                cancel_event.set()
        for job in list(self.pending_jobs):
            cancel_event = job.get("cancel_event")
            if cancel_event is not None:
                cancel_event.set()
            job["cancel_state"] = "cancelled_on_close"
        self.pending_jobs.clear()
        runtime = getattr(self, "shared_queue_runtime", None)
        if runtime is not None:
            runtime.shutdown()
        worker = getattr(self, "worker", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=5.0)


def main():
    root = tk.Tk()
    app = App(root)

    def close():
        app.shutdown_shared_queue()
        app.persist_config()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    main()
