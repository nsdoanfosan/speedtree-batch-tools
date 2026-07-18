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
from collections import OrderedDict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


TOOL_DIR = Path(__file__).resolve().parent
REPO_DIR = TOOL_DIR.parent
sys.path.insert(0, str(REPO_DIR))
# Keep the tool folder first: the GUI engine is the sibling
# spm_generator_sync.py, not the repository package's limited public API.
sys.path.insert(0, str(TOOL_DIR))

from batch_ui_common import clipboard_text, copy_selected_row_paths


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
CACHE_VERSION = 2
DEFAULT_TREE_ROOT = Path(r"D:\OneDrive\Forestportfolio\02_nature\Tree")
DEFAULT_SPEEDTREE = Path(
    r"C:\Program Files\SpeedTree\SpeedTree Modeler v10.1.0\win64\SpeedTree_Modeler.exe"
)


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
            text="읽기 전용 미리보기입니다. 자식 전용 구조는 삭제되지 않습니다.",
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
        self.job_started_at = None
        self.job_stage = "대기"
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
        self.refresh()

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

        board_frame = ttk.Frame(self.root, padding=(8, 0, 8, 5))
        board_frame.pack(fill="both", expand=True)
        columns = ("role", "bases", "structure", "status", "last")
        self.tree = ttk.Treeview(
            board_frame, columns=columns, show="tree headings", selectmode="extended"
        )
        self.tree.heading("#0", text="나무 폴더 / SPM")
        self.tree.heading("role", text="관계")
        self.tree.heading("bases", text="따라가는 Base")
        self.tree.heading("structure", text="Base 구조")
        self.tree.heading("status", text="동기화 상태")
        self.tree.heading("last", text="마지막 적용")
        self.tree.column("#0", width=430, minwidth=280)
        self.tree.column("role", width=145, anchor="center")
        self.tree.column("bases", width=330)
        self.tree.column("structure", width=180)
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
            "follower_unique", background="#f7e5dd", foreground="#6f2818"
        )
        self.tree.tag_configure(
            "follower_missing", background="#fff2cc", foreground="#745000"
        )
        self.tree.tag_configure(
            "follower_delta", background="#f2dfcf", foreground="#612d12"
        )
        self.tree.tag_configure(
            "follower_risk", background="#ffd6d6", foreground="#8b0000",
            font=("Segoe UI", 9, "bold"),
        )
        self.tree.tag_configure("independent", foreground="#555")
        self.tree.tag_configure("candidate", font=("Segoe UI", 9, "bold"), background="#fff3c4")
        self.tree.tag_configure("unassigned", foreground="#8a4b00")
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
            text=("추가 예정 = 마스터에만 있는 구조입니다. 아직 동기화하지 않았거나 마스터를 나중에 "
                  "수정했을 때 생기며 다음 동기화에서 자식에 추가됩니다. · "
                  "자식 전용 = 자식에만 있는 변형 구조이며 자동 삭제하지 않고 보존합니다."),
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
        self.delta_box.tag_configure("unique", foreground="#7d2417", font=("Segoe UI", 9, "bold"))
        self.delta_box.tag_configure("muted", foreground="#777")
        self.delta_box.configure(state="disabled")

        Tooltip(
            self.preview_button,
            "SPM을 수정하지 않고 공통 노드·속성 변경·추가 예정·자식 전용 구조·색상 변경을 계산합니다.",
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

    def cached_master_signature(self, path: Path, categories: dict, document=None):
        key = json.dumps(
            [self._path_cache_key(path), categories],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if key in self.signature_cache:
            self.signature_cache.move_to_end(key)
            return self.signature_cache[key]
        document = document or self.cached_document(path)
        signature = engine.base_sync_signature(document, categories)
        return self._cache_store(self.signature_cache, key, signature, limit=64)

    def cached_follower_analysis(
        self, source_path: Path, target_path: Path, mapping: dict,
        source_document=None, target_document=None,
    ):
        key = json.dumps(
            [self._path_cache_key(source_path), self._path_cache_key(target_path), mapping],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if key in self.analysis_cache:
            self.analysis_cache.move_to_end(key)
            return self.analysis_cache[key]
        source_document = source_document or self.cached_document(source_path)
        target_document = target_document or self.cached_document(target_path)
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

    def pick_root(self):
        folder = filedialog.askdirectory(initialdir=self.root_var.get() or str(Path.home()))
        if folder:
            self.root_var.set(folder)
            self.persist_config()
            self.refresh()

    def refresh(self, reveal=None):
        self.persist_config()
        try:
            self.board = engine.scan_tree_folders(
                Path(self.root_var.get().strip()),
                sk_only=bool(self.sk_only_var.get()),
            )
        except Exception as exc:
            messagebox.showerror("검사 실패", str(exc), parent=self.root)
            return
        self.render_board()
        folder_count = len(self.board)
        spm_count = sum(len(item["spms"]) for item in self.board)
        self.status_var.set(f"{folder_count}개 폴더 · {spm_count}개 SPM")
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

    def master_status(self, folder: Path, group: dict, document=None) -> tuple[str, str]:
        categories = group.get("base_categories") or {}
        try:
            master_path = folder / group["master"]
            signature = self.cached_master_signature(master_path, categories, document=document)
        except Exception as exc:
            return "검사 실패", str(exc)
        followers = group.get("followers", [])
        if not followers:
            return "자식 없음", signature
        if any(not item.get("base_map_confirmed") for item in followers):
            return "매핑 필요", signature
        hashes = {item.get("last_master_hash") for item in followers}
        if hashes == {signature}:
            return "최신", signature
        return "마스터 변경", signature

    def render_board(self):
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
                status, signature = self.master_status(folder, group)
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
                        "missing": 0, "target_only": 0, "missing_bases": 0,
                        "missing_details": [], "target_only_details": [],
                    }
                    risk = {}
                    if not follower.get("base_map_confirmed"):
                        structure = "매핑 확인 필요"
                        current_target_hash = None
                    else:
                        try:
                            delta, current_target_hash = self.cached_follower_analysis(
                                folder / master, folder / name, mapping,
                            )
                            if delta.get("mapping_errors"):
                                structure = "매핑 오류"
                            elif delta["missing"] or delta["target_only"]:
                                parts = []
                                if delta.get("missing_bases"):
                                    parts.append(f"Base 추가 {delta['missing_bases']}")
                                if delta["missing"]:
                                    parts.append(f"추가 예정 {delta['missing']}")
                                if delta["target_only"]:
                                    parts.append(f"자식 전용 {delta['target_only']}")
                                structure = " · ".join(parts)
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
                    elif follower.get("last_sync") and last_hash != signature:
                        follower_status = "마스터 변경"
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
                            else "follower_delta" if delta.get("missing") and delta.get("target_only")
                            else "follower_missing" if delta.get("missing")
                            else "follower_unique" if delta.get("target_only")
                            else "follower",
                        )
                    )
                    self.item_meta[follower_iid] = {
                        "kind": "spm", "folder": folder, "file": name,
                        "role": "follower", "group": group, "follower": follower,
                        "master": master,
                        "missing_details": delta.get("missing_details") or [],
                        "target_only_details": delta.get("target_only_details") or [],
                        "missing_count": delta.get("missing", 0),
                        "target_only_count": delta.get("target_only", 0),
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
        try:
            save_analysis_cache(self.signature_cache, self.analysis_cache)
        except OSError:
            pass

    def selected_items(self):
        return [self.item_meta[iid] for iid in self.tree.selection()
                if iid in self.item_meta and self.item_meta[iid].get("kind") == "spm"]

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
            self.delta_box.insert("end", "자식 행을 선택하면 추가 예정·자식 전용 Generator 상세가 표시됩니다.", "muted")
            self.delta_box.configure(state="disabled")
            return
        missing = item.get("missing_details") or []
        unique = item.get("target_only_details") or []
        if not missing and not unique:
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
            insert_group("추가 예정 — 동기화 시 자식에 추가될 구조", missing, "missing")
        if unique:
            insert_group("자식 전용 — 자식에만 있어 보존되는 구조", unique, "unique")
        self.delta_box.configure(state="disabled")

    def on_double_click(self, _event=None):
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
        try:
            result = engine.promote_master(folder, item["file"])
        except Exception as exc:
            messagebox.showerror("마스터 지정 실패", str(exc), parent=self.root)
            return
        self.refresh(reveal=(folder, item["file"]))
        self.status_var.set(
            f"마스터 지정 완료 · {item['file']} · "
            f"Base 색상 {result['color_updates']}개 적용"
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
        if promote_master:
            engine.promote_master(folder, master)
        source_doc = engine.SPMDocument.from_path(folder / master, full=False)
        manifest = engine.load_manifest(folder)
        group = engine.find_group(manifest, master)
        categories = group.get("base_categories") or engine.source_base_categories(source_doc)
        group["base_categories"] = categories
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
                engine.assign_follower(
                    manifest, master, item["file"], suggestion, confirmed=False
                )
            else:
                engine.assign_follower(
                    manifest, master, item["file"], mapping, confirmed=True
                )
        engine.save_manifest(folder, manifest)
        self.refresh()

    def set_selected_independent(self):
        items = self.selected_items()
        if not items:
            return
        by_folder = {}
        for item in items:
            by_folder.setdefault(item["folder"], []).append(item["file"])
        for folder, names in by_folder.items():
            manifest = engine.load_manifest(folder)
            engine.set_independent(manifest, names)
            engine.save_manifest(folder, manifest)
        self.refresh()

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
        manifest = engine.load_manifest(item["folder"])
        group = engine.find_group(manifest, item["master"])
        follower = next(row for row in group["followers"] if row.get("file") == item["file"])
        follower["base_map"] = dialog.result
        follower["base_map_confirmed"] = True
        follower["last_master_hash"] = None
        follower["last_target_hash"] = None
        engine.save_manifest(item["folder"], manifest)
        self.refresh()

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
        manifest = engine.load_manifest(item["folder"])
        group = engine.find_group(manifest, item["file"])
        group["base_categories"] = dialog.result
        for follower in group.get("followers", []):
            follower["last_master_hash"] = None
            follower["last_target_hash"] = None
        engine.save_manifest(item["folder"], manifest)
        self.refresh()

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

    def _start_job(self, label, func, on_success):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("작업 중", "현재 작업이 끝날 때까지 기다려 주세요.", parent=self.root)
            return
        self.status_var.set(label)
        self.job_started_at = time.monotonic()
        self.job_stage = label
        self.progress_bar.configure(value=1)
        self.progress_text_var.set(f"{label} · 00:00")
        for button in (self.preview_button, self.apply_button, self.apply_all_button):
            button.configure(state="disabled")

        def report(stage, percent):
            self.job_queue.put(("progress", str(stage), max(0, min(100, int(percent)))))

        def run():
            try:
                self.job_queue.put(("done", True, func(report), on_success))
            except Exception as exc:
                self.job_queue.put(("done", False, exc, on_success))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_job)

    def _poll_job(self):
        completed = None
        while True:
            try:
                event = self.job_queue.get_nowait()
            except queue.Empty:
                break
            if event[0] == "progress":
                _kind, stage, percent = event
                self.job_stage = stage
                self.progress_bar.configure(value=percent)
                self.status_var.set(stage)
            elif event[0] == "done":
                completed = event

        elapsed = max(0, int(time.monotonic() - (self.job_started_at or time.monotonic())))
        elapsed_text = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        self.progress_text_var.set(f"{self.job_stage} · {elapsed_text}")
        if completed is None:
            if self.worker and self.worker.is_alive():
                self.root.after(100, self._poll_job)
            return

        _kind, ok, payload, on_success = completed
        for button in (self.preview_button, self.apply_button, self.apply_all_button):
            button.configure(state="normal")
        if ok:
            self.progress_bar.configure(value=100)
            self.progress_text_var.set(f"완료 · {elapsed_text}")
            on_success(payload)
        else:
            self.status_var.set("실패")
            self.progress_text_var.set(f"실패 · {self.job_stage} · {elapsed_text}")
            messagebox.showerror("작업 실패", str(payload), parent=self.root)

    def preview_selected(self):
        followers = self.followers_from_selection()
        if not followers:
            messagebox.showinfo("미리보기", "자식 또는 마스터 행을 선택하세요.", parent=self.root)
            return

        def build(report):
            folder = followers[0]["folder"]
            master = followers[0]["master"]
            report("마스터 구조 분석 중", 8)
            manifest = engine.load_manifest(folder)
            group = engine.find_group(manifest, master)
            source, _text, color_updates, master_ref_renames, color_warnings = engine.standardize_master_document(
                folder / master, group.get("base_categories") or {}
            )
            plans = []
            for index, item in enumerate(followers, start=1):
                report(
                    f"미리보기 계산 중 · {item['file']} ({index}/{len(followers)})",
                    15 + round(75 * (index - 1) / max(1, len(followers))),
                )
                follower = next(row for row in group["followers"] if row.get("file") == item["file"])
                if not follower.get("base_map_confirmed"):
                    raise engine.SyncError(f"Base 매핑을 먼저 확인하세요: {item['file']}")
                plan = engine.build_sync_plan(
                    folder / master, folder / item["file"],
                    follower.get("base_map") or {}, group.get("base_categories") or {},
                    master_document=source,
                )
                plan.master_color_updates = color_updates
                plan.master_reference_renames = master_ref_renames
                plan.warnings.extend(color_warnings)
                plans.append(plan)
            report("미리보기 결과 정리 중", 95)
            return plans

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

        self._start_job("변경 계산 중...", build, show)

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
            "적용 전 마스터와 모든 자식이 하나의 백업 폴더에 저장됩니다.\n"
            "자식 전용 구조, GUID, Random Seed, 재질과 Node/Freehand Edit는 유지됩니다."
        )
        if verify:
            message += (
                "\n적용 전 임시 복사본을 SpeedTree 10.1에서 실제 계산·XML export합니다. "
                "모두 성공한 뒤에만 원본을 백업하고 저장합니다."
            )
        if not messagebox.askyesno("동기화 적용", message, parent=self.root):
            return

        def apply(report):
            return engine.apply_group_transaction(
                folder, master, names,
                verify_speedtree=verify,
                speedtree_exe=Path(self.config["speedtree_exe"]),
                xml_ini=Path(self.config["xml_ini"]),
                progress_callback=report,
            )

        def done(result):
            self.refresh()
            changed = "\n".join(Path(path).name for path in result["changed_files"]) or "변경 없음"
            messagebox.showinfo(
                "동기화 완료",
                f"상태: {result['status']}\n\n변경 파일:\n{changed}\n\n"
                f"백업: {result.get('backup_dir') or '새 백업 없음'}",
                parent=self.root,
            )

        self._start_job("SPM 동기화 및 검증 중...", apply, done)

    def apply_selected(self):
        self._apply_followers(self.followers_from_selection())

    def apply_all_children(self):
        items = self.selected_items()
        masters = [item for item in items if item.get("role") == "master"]
        if len(masters) != 1:
            messagebox.showinfo("전체 자식", "마스터 행 하나를 선택하세요.", parent=self.root)
            return
        self._apply_followers(self.followers_from_selection())


def main():
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.persist_config(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
