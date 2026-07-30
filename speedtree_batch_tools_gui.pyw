"""Tabbed entry point for the three SpeedTree data-management GUIs.

The original tools stay independently launchable.  This module embeds their
existing App classes in Notebook pages and only absorbs window-level calls such
as title() and geometry(), so the tool implementations remain the source of
truth.
"""

from __future__ import annotations

import ctypes
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


REPO_DIR = Path(__file__).resolve().parent
ERROR_LOG = REPO_DIR / "speedtree_batch_tools_error.log"
ICON_PNG = REPO_DIR / "assets" / "speedtree_batch_tools_icon_512.png"
ICON_ICO = REPO_DIR / "assets" / "speedtree_batch_tools.ico"
APP_USER_MODEL_ID = "PARK.SpeedTree.BatchTools"
BASE_WINDOW_TITLE = "SpeedTree Batch Tools — 통합 데이터 관리"
ACTIVITY_POLL_MS = 250
COMPLETION_BANNER_MS = 4000


def record_error(label, exc) -> bool:
    """Append one traceback to the shared error log; never raise from here."""

    try:
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n[{datetime.now().isoformat(timespec='seconds')}] {label}\n"
            )
            handle.write("".join(traceback.format_exception(exc)))
        return True
    except OSError:
        return False


def report_fatal_startup_error(exc) -> None:
    """Make a launcher crash visible.

    ``pythonw`` has no console, so an exception raised before ``mainloop``
    would otherwise make a double-clicked .bat look like it did nothing at
    all.  Log it, then show a native message box that needs no working Tk.
    """

    logged = record_error("launcher startup", exc)
    detail = "".join(traceback.format_exception(exc))[-1200:]
    message = (
        "SpeedTree Batch Tools를 시작하지 못했습니다.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        + (f"오류 로그: {ERROR_LOG}\n\n" if logged else "")
        + detail
    )
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(
                None, message, "SpeedTree Batch Tools 실행 실패", 0x10
            )
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


def apply_app_user_model_id() -> None:
    """Give Windows a stable identity for taskbar grouping and pinning."""

    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except (AttributeError, OSError):
        pass


def apply_app_icon(root: tk.Tk) -> None:
    """Apply the project icon while keeping launch resilient to missing assets."""

    if ICON_PNG.is_file():
        try:
            root._speedtree_app_icon = tk.PhotoImage(file=str(ICON_PNG))
            root.iconphoto(True, root._speedtree_app_icon)
        except tk.TclError:
            pass
    if os.name == "nt" and ICON_ICO.is_file():
        try:
            root.iconbitmap(default=str(ICON_ICO))
        except tk.TclError:
            pass


@dataclass(frozen=True)
class ToolSpec:
    label: str
    module_name: str
    script: Path
    launcher: Path


TOOLS = (
    ToolSpec(
        "SK Batch",
        "_speedtree_integrated_sk_batch_gui",
        REPO_DIR / "sk_batch" / "sk_batch_gui.pyw",
        REPO_DIR / "sk_batch" / "SK_Batch.bat",
    ),
    ToolSpec(
        "SPM Generator Sync",
        "_speedtree_integrated_spm_generator_sync_gui",
        REPO_DIR / "spm_generator_sync" / "spm_generator_sync_gui.pyw",
        REPO_DIR / "spm_generator_sync" / "SPM_Generator_Sync.bat",
    ),
    ToolSpec(
        "PCG ST9 Texture",
        "_speedtree_integrated_pcg_texture_gui",
        REPO_DIR / "pcg_st9_texture_batch" / "pcg_texture_gui.pyw",
        REPO_DIR / "pcg_st9_texture_batch" / "PCG_ST9_Texture_Batch.bat",
    ),
)


def _string_var_value(app, *names):
    """Read the first useful Tk status variable exposed by an embedded app."""

    for name in names:
        variable = getattr(app, name, None)
        getter = getattr(variable, "get", None)
        if not callable(getter):
            continue
        try:
            value = str(getter()).strip()
        except (AttributeError, RuntimeError, tk.TclError):
            continue
        if value:
            return value
    return ""


def app_activity_snapshot(app):
    """Return ``(is_busy, detail)`` for any of the three embedded tools.

    Each tool keeps its own worker model.  The integrated launcher deliberately
    observes their public-ish state instead of changing those job pipelines.
    """

    if app is None:
        return False, ""

    busy = bool(getattr(app, "_busy", False))
    for name in ("worker", "scan_worker"):
        worker = getattr(app, name, None)
        is_alive = getattr(worker, "is_alive", None)
        if callable(is_alive):
            try:
                busy = busy or bool(is_alive())
            except RuntimeError:
                pass

    detail = _string_var_value(
        app,
        "progress_text_var",
        "progress_var",
        "status_var",
    )
    return busy, detail


def activity_completion_detail(app, previous=""):
    """Prefer a finished app status and never leave a stale “running” label."""

    current = _string_var_value(
        app,
        "progress_text_var",
        "progress_var",
        "status_var",
    )
    in_progress_markers = (
        "실행 중",
        "스캔 중",
        "검사 중",
        "처리 중",
        "저장 중",
        "동기화 중",
        "불러오는 중",
        "재검사 중",
    )
    if current not in {"", "대기", "준비됨"} and not any(
        marker in current for marker in in_progress_markers
    ):
        return current

    previous = str(previous or "").replace("실행 중 0개", "완료").strip()
    if previous and not any(marker in previous for marker in in_progress_markers):
        return previous
    return "작업이 정상적으로 끝났습니다."


class ToolTab(ttk.Frame):
    """A normal frame that ignores window-only settings from embedded apps."""

    def title(self, value=None):
        if value is None:
            return self.winfo_toplevel().title()
        return None

    wm_title = title

    def geometry(self, value=None):
        if value is None:
            return self.winfo_toplevel().geometry()
        return ""

    wm_geometry = geometry

    def minsize(self, width=None, height=None):
        if width is None and height is None:
            return self.winfo_toplevel().minsize()
        return None

    wm_minsize = minsize


def load_tool_module(spec: ToolSpec):
    """Load a .pyw source file under a stable, collision-free module name."""

    if not spec.script.is_file():
        raise FileNotFoundError(f"GUI 파일을 찾을 수 없습니다: {spec.script}")

    loaded = sys.modules.get(spec.module_name)
    loaded_path = getattr(loaded, "__file__", None)
    if loaded_path and Path(loaded_path).resolve() == spec.script.resolve():
        return loaded

    loader = importlib.machinery.SourceFileLoader(spec.module_name, str(spec.script))
    module_spec = importlib.util.spec_from_loader(spec.module_name, loader)
    if module_spec is None:
        raise ImportError(f"GUI 모듈 정보를 만들 수 없습니다: {spec.script}")

    module = importlib.util.module_from_spec(module_spec)
    sys.modules[spec.module_name] = module
    try:
        loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.module_name, None)
        raise
    return module


def tree_rows(tree, parent=""):
    """Return Treeview row ids in their visible hierarchy order."""

    rows = []
    for iid in tree.get_children(parent):
        rows.append(iid)
        rows.extend(tree_rows(tree, iid))
    return rows


def matching_tree_rows(tree, query):
    """Find rows whose displayed tree text or column values contain query."""

    needle = str(query).strip().casefold()
    if not needle:
        return []
    matches = []
    for iid in tree_rows(tree):
        item = tree.item(iid)
        displayed = [item.get("text", ""), *(item.get("values") or ())]
        if needle in " ".join(str(value) for value in displayed).casefold():
            matches.append(iid)
    return matches


def file_paths_for_app_row(app, iid):
    """Return concrete path candidates represented by one file row."""
    item_meta = getattr(app, "item_meta", {})
    items = getattr(app, "items", {})
    row_copy_paths = getattr(app, "row_copy_paths", {})
    if iid in item_meta:
        meta = item_meta[iid]
        if meta.get("kind") != "spm" or not meta.get("file"):
            return []
        try:
            return [Path(meta.get("folder", "")) / meta["file"]]
        except TypeError:
            return []

    entry = items.get(iid)
    if entry is not None:
        # SK Batch file rows expose an exact `spm`; PCG top rows expose an
        # aggregate `item` and must not delete all files behind it.
        if entry.get("spm"):
            try:
                return [Path(entry["spm"])]
            except TypeError:
                return []
        return []

    # PCG's explicit Cluster/blend/SPM child rows each represent one file.
    # Folder/helper rows either resolve to a directory or multiple paths.
    paths = list(row_copy_paths.get(iid) or ())
    if len(paths) != 1:
        return []
    try:
        return [Path(paths[0])]
    except TypeError:
        return []


def selected_file_paths_for_app(app):
    """Return unique existing files represented by selected file rows only."""

    tree = getattr(app, "tree", None)
    if tree is None:
        return []

    candidates = []
    for iid in tree.selection():
        candidates.extend(file_paths_for_app_row(app, iid))

    result = []
    seen = set()
    for path in candidates:
        try:
            path = path.expanduser().absolute()
        except (OSError, RuntimeError):
            continue
        if not path.is_file():
            continue
        key = os.path.normcase(str(path)).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def remove_deleted_tree_rows(app, deleted_paths):
    """Immediately remove selected rows whose concrete files were deleted."""
    tree = getattr(app, "tree", None)
    if tree is None:
        return 0
    deleted_keys = {
        os.path.normcase(str(Path(path).expanduser().absolute())).casefold()
        for path in deleted_paths
    }
    removed = 0
    for iid in tuple(tree.selection()):
        row_keys = {
            os.path.normcase(str(path.expanduser().absolute())).casefold()
            for path in file_paths_for_app_row(app, iid)
        }
        if row_keys & deleted_keys:
            try:
                tree.delete(iid)
                removed += 1
            except tk.TclError:
                pass
    return removed


class FindDialog(tk.Toplevel):
    """Small non-modal find window for the active tool's Treeview."""

    def __init__(self, owner):
        super().__init__(owner.root)
        self.owner = owner
        self.query_var = tk.StringVar()
        self.result_var = tk.StringVar(value="찾을 내용을 입력하세요")

        # Keep Windows from briefly showing the Toplevel at its default
        # desktop position before its requested size is known.
        self.withdraw()
        self.title("찾기")
        self.resizable(False, False)
        self.transient(owner.root)
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        self.entry = ttk.Entry(body, textvariable=self.query_var, width=44)
        self.entry.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Button(body, text="이전", command=lambda: self.find(-1)).grid(
            row=1, column=0, padx=(0, 6), pady=(9, 0)
        )
        ttk.Button(body, text="다음", command=lambda: self.find(1)).grid(
            row=1, column=1, padx=(0, 6), pady=(9, 0)
        )
        ttk.Button(body, text="닫기", command=self.close).grid(
            row=1, column=2, pady=(9, 0)
        )
        ttk.Label(body, textvariable=self.result_var, foreground="#555555").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(9, 0)
        )
        self.entry.bind("<Return>", lambda _event: self.find(1))
        self.entry.bind("<Shift-Return>", lambda _event: self.find(-1))
        self.bind("<Escape>", lambda _event: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after_idle(self._show_centered)

    def _show_centered(self):
        self.update_idletasks()
        parent = self.owner.root
        parent.update_idletasks()
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        self.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.deiconify()
        self.lift()
        self._focus_entry()

    def _focus_entry(self):
        self.entry.focus_set()
        self.entry.selection_range(0, "end")

    def find(self, direction=1):
        app = self.owner.current_app()
        tree = getattr(app, "tree", None) if app is not None else None
        if tree is None:
            self.result_var.set("현재 탭의 목록을 불러오는 중입니다")
            return "break"

        matches = matching_tree_rows(tree, self.query_var.get())
        if not matches:
            self.result_var.set("일치하는 항목이 없습니다")
            return "break"

        focused = tree.focus()
        if focused in matches:
            index = (matches.index(focused) + direction) % len(matches)
        else:
            index = 0 if direction > 0 else len(matches) - 1
        iid = matches[index]
        parent = tree.parent(iid)
        while parent:
            tree.item(parent, open=True)
            parent = tree.parent(parent)
        tree.selection_set(iid)
        tree.focus(iid)
        tree.see(iid)
        self.result_var.set(f"{len(matches)}개 중 {index + 1}번째")
        return "break"

    def close(self):
        self.owner.find_dialog = None
        self.destroy()


class IntegratedApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.apps = {}
        self.load_states = ["pending"] * len(TOOLS)
        self.status_var = tk.StringVar(value="통합 도구 준비 중...")
        self.find_dialog = None
        self._busy_indices = set()
        self._last_activity_detail = ""
        self._completion_after_id = None

        root.title(BASE_WINDOW_TITLE)
        root.geometry("1500x920")
        root.minsize(1120, 700)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        style = ttk.Style(root)
        style.configure(
            "TNotebook.Tab",
            padding=(18, 8),
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "Activity.Horizontal.TProgressbar",
            troughcolor="#0d47a1",
            background="#90caf9",
            bordercolor="#0d47a1",
            lightcolor="#90caf9",
            darkcolor="#90caf9",
            thickness=11,
        )
        style.configure(
            "Success.Horizontal.TProgressbar",
            troughcolor="#1b5e20",
            background="#a5d6a7",
            bordercolor="#1b5e20",
            lightcolor="#a5d6a7",
            darkcolor="#a5d6a7",
            thickness=11,
        )

        self.activity_frame = tk.Frame(
            root,
            background="#0b57d0",
            padx=14,
            pady=8,
        )
        self.activity_frame.grid(row=0, column=0, sticky="ew")
        self.activity_frame.columnconfigure(1, weight=1)
        self.activity_heading_var = tk.StringVar(value="작업 실행 중")
        self.activity_detail_var = tk.StringVar()
        self.activity_heading = tk.Label(
            self.activity_frame,
            textvariable=self.activity_heading_var,
            background="#0b57d0",
            foreground="white",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        )
        self.activity_heading.grid(row=0, column=0, sticky="w")
        self.activity_detail = tk.Label(
            self.activity_frame,
            textvariable=self.activity_detail_var,
            background="#0b57d0",
            foreground="#e8f0fe",
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.activity_detail.grid(row=0, column=1, sticky="ew", padx=(18, 12))
        self.activity_progress = ttk.Progressbar(
            self.activity_frame,
            mode="indeterminate",
            maximum=100,
            length=250,
            style="Activity.Horizontal.TProgressbar",
        )
        self.activity_progress.grid(row=0, column=2, sticky="e")
        self.activity_frame.grid_remove()

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=1, column=0, sticky="nsew")
        self.tabs = []
        for index, spec in enumerate(TOOLS, start=1):
            tab = ToolTab(self.notebook)
            self.notebook.add(tab, text=f"{index}  {spec.label}")
            self.tabs.append(tab)
            self._show_placeholder(index - 1)

        footer = ttk.Frame(root, padding=(8, 5))
        footer.grid(row=2, column=0, sticky="ew")
        ttk.Label(
            footer,
            textvariable=self.status_var,
            foreground="#555555",
        ).pack(side="left")
        ttk.Label(
            footer,
            text="Ctrl+1/2/3 탭 이동 · Ctrl+F 찾기 · Delete Atlas 대상 해제/일반 파일 삭제",
            foreground="#777777",
        ).pack(side="left", padx=(18, 0))
        ttk.Button(
            footer,
            text="현재 탭을 별도 창으로 열기",
            command=self.open_current_standalone,
        ).pack(side="right")

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        for index in range(len(TOOLS)):
            root.bind(
                f"<Control-Key-{index + 1}>",
                lambda _event, target=index: self.select_tab(target),
            )
        root.bind("<Control-f>", self.open_find)
        root.bind("<Delete>", self.delete_selected_files)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._schedule_load(0)
        self.root.after(ACTIVITY_POLL_MS, self._poll_activity)

    def current_app(self):
        selected = self.notebook.select()
        if not selected:
            return None
        return self.apps.get(self.notebook.index(selected))

    def open_find(self, _event=None):
        if self.find_dialog is not None and self.find_dialog.winfo_exists():
            self.find_dialog.lift()
            self.find_dialog._focus_entry()
        else:
            self.find_dialog = FindDialog(self)
        return "break"

    def delete_selected_files(self, event=None):
        app = self.current_app()
        tree = getattr(app, "tree", None) if app is not None else None
        if tree is None or (event is not None and self.root.focus_get() is not tree):
            return None

        handle_delete_key = getattr(app, "handle_delete_key", None)
        if callable(handle_delete_key) and handle_delete_key():
            return "break"

        paths = selected_file_paths_for_app(app)
        if not paths:
            self.status_var.set("삭제 안 함 · 실제 파일 행만 삭제할 수 있습니다")
            status = getattr(app, "status_var", None)
            if status is not None:
                status.set("삭제할 실제 파일 행을 선택하세요 · 폴더/집계 행은 삭제되지 않습니다")
            return "break"

        preview = "\n".join(f"• {path}" for path in paths[:8])
        if len(paths) > 8:
            preview += f"\n• 외 {len(paths) - 8}개"
        confirmed = messagebox.askyesno(
            "선택 파일 삭제 경고",
            f"선택한 파일 {len(paths)}개를 영구 삭제합니다.\n"
            "이 작업은 되돌릴 수 없습니다.\n\n"
            f"{preview}\n\n계속할까요?",
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            return "break"

        deleted = []
        errors = []
        for path in paths:
            try:
                path.unlink()
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            if path.exists():
                errors.append(f"{path}: 삭제 호출 후에도 파일이 남아 있습니다")
                continue
            deleted.append(path)

        if deleted:
            remove_deleted_tree_rows(app, deleted)
            refresh = getattr(app, "refresh", None)
            if not callable(refresh):
                refresh = getattr(app, "scan", None)
            if callable(refresh):
                refresh()
            self.status_var.set(f"파일 삭제 완료 · {len(deleted)}개")
        if errors:
            if not deleted:
                self.status_var.set("파일 삭제 실패 · 파일이 그대로 보존되었습니다")
            messagebox.showerror(
                "일부 파일 삭제 실패",
                "\n".join(errors[:8]),
                parent=self.root,
            )
        return "break"

    def _clear_tab(self, index):
        for child in self.tabs[index].winfo_children():
            child.destroy()

    def _show_placeholder(self, index):
        self._clear_tab(index)
        body = ttk.Frame(self.tabs[index], padding=28)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=f"{TOOLS[index].label}\n\n이 탭을 처음 열 때 데이터를 불러옵니다.",
            justify="center",
            foreground="#666666",
            font=("Segoe UI", 11),
        ).place(relx=0.5, rely=0.45, anchor="center")

    def _on_tab_changed(self, _event=None):
        selected = self.notebook.select()
        if selected:
            index = self.notebook.index(selected)
            self._schedule_load(index)
            state = self.load_states[index]
            if state == "loaded":
                self.status_var.set(f"{TOOLS[index].label} 준비됨")
            elif state == "failed":
                self.status_var.set(f"{TOOLS[index].label} 로드 실패 · 오류 로그를 확인하세요")
            else:
                self.status_var.set(f"{TOOLS[index].label} 불러오는 중...")

    def _schedule_load(self, index):
        if self.load_states[index] != "pending":
            return
        self.load_states[index] = "scheduled"
        self.root.after_idle(lambda target=index: self._load_tool(target))

    def _load_tool(self, index):
        if self.load_states[index] not in {"pending", "scheduled"}:
            return
        spec = TOOLS[index]
        self.load_states[index] = "loading"
        self._clear_tab(index)
        ttk.Label(
            self.tabs[index],
            text=f"{spec.label} 데이터를 불러오는 중...",
            foreground="#555555",
            font=("Segoe UI", 11),
        ).place(relx=0.5, rely=0.45, anchor="center")
        self.status_var.set(f"{spec.label} 불러오는 중...")
        self.root.update_idletasks()

        try:
            module = load_tool_module(spec)
            if not hasattr(module, "App"):
                raise AttributeError(f"{spec.script.name}에 App 클래스가 없습니다.")
            self._clear_tab(index)
            self.apps[index] = module.App(self.tabs[index])
        except Exception as exc:
            self.apps.pop(index, None)
            self.load_states[index] = "failed"
            self._record_error(spec, exc)
            self._show_load_error(index, exc)
            self.status_var.set(f"{spec.label} 로드 실패 · 오류 로그를 확인하세요")
            return

        self.load_states[index] = "loaded"
        self.status_var.set(f"{spec.label} 준비됨")

    def _set_activity_colors(self, background, foreground):
        self.activity_frame.configure(background=background)
        self.activity_heading.configure(
            background=background,
            foreground=foreground,
        )
        self.activity_detail.configure(
            background=background,
            foreground=foreground,
        )

    def _set_tab_activity_markers(self, busy_indices):
        for index, spec in enumerate(TOOLS, start=1):
            marker = "● " if index - 1 in busy_indices else ""
            self.notebook.tab(
                self.tabs[index - 1],
                text=f"{marker}{index}  {spec.label}",
            )

    def _show_running_activity(self, snapshots):
        activity_was_running = bool(self._busy_indices)
        if self._completion_after_id is not None:
            try:
                self.root.after_cancel(self._completion_after_id)
            except tk.TclError:
                pass
            self._completion_after_id = None

        busy_indices = {index for index, _detail in snapshots}
        labels = [TOOLS[index].label for index, _detail in snapshots]
        details = [
            f"{TOOLS[index].label}: {detail or '처리 중...'}"
            for index, detail in snapshots
        ]
        if len(labels) == 1:
            heading = f"● {labels[0]} 작업 실행 중"
            detail = snapshots[0][1] or "처리 중..."
            title = f"● 실행 중 · {labels[0]} — SpeedTree Batch Tools"
        else:
            heading = f"● {len(labels)}개 작업 실행 중"
            detail = "   |   ".join(details)
            title = f"● 실행 중 ({len(labels)}) — SpeedTree Batch Tools"

        if detail not in {"대기", "준비됨"}:
            self._last_activity_detail = detail
        self._set_activity_colors("#0b57d0", "white")
        self.activity_heading_var.set(heading)
        self.activity_detail_var.set(detail)
        if not activity_was_running:
            self.activity_progress.stop()
            self.activity_progress.configure(
                mode="indeterminate",
                value=0,
                style="Activity.Horizontal.TProgressbar",
            )
            self.activity_progress.start(12)
        self.activity_frame.grid()
        self._set_tab_activity_markers(busy_indices)
        self.root.title(title)
        self._busy_indices = busy_indices

    def _show_activity_complete(self, completed_indices):
        labels = [TOOLS[index].label for index in sorted(completed_indices)]
        label = labels[0] if len(labels) == 1 else f"{len(labels)}개 작업"
        details = [
            activity_completion_detail(
                self.apps.get(index),
                self._last_activity_detail if len(completed_indices) == 1 else "",
            )
            for index in sorted(completed_indices)
        ]
        detail = "   |   ".join(
            f"{TOOLS[index].label}: {text}"
            for index, text in zip(sorted(completed_indices), details)
        )
        if len(details) == 1:
            detail = details[0]
        self.activity_progress.stop()
        self.activity_progress.configure(
            mode="determinate",
            value=100,
            style="Success.Horizontal.TProgressbar",
        )
        self._set_activity_colors("#2e7d32", "white")
        self.activity_heading_var.set(f"✓ {label} 완료")
        self.activity_detail_var.set(detail)
        self.activity_frame.grid()
        self._set_tab_activity_markers(set())
        self.root.title(f"✓ 완료 · {label} — SpeedTree Batch Tools")
        self._busy_indices = set()
        self._completion_after_id = self.root.after(
            COMPLETION_BANNER_MS,
            self._hide_activity,
        )

    def _hide_activity(self):
        self._completion_after_id = None
        self.activity_progress.stop()
        self.activity_frame.grid_remove()
        self._set_tab_activity_markers(set())
        self.root.title(BASE_WINDOW_TITLE)
        self._last_activity_detail = ""

    def _poll_activity(self):
        snapshots = []
        for index, app in self.apps.items():
            busy, detail = app_activity_snapshot(app)
            if busy:
                snapshots.append((index, detail))

        if snapshots:
            self._show_running_activity(snapshots)
        elif self._busy_indices:
            self._show_activity_complete(self._busy_indices)
        try:
            self.root.after(ACTIVITY_POLL_MS, self._poll_activity)
        except tk.TclError:
            pass

    def _show_load_error(self, index, exc):
        self._clear_tab(index)
        body = ttk.Frame(self.tabs[index], padding=28)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=f"{TOOLS[index].label}을 통합 탭에서 불러오지 못했습니다.",
            font=("Segoe UI", 12, "bold"),
            foreground="#9c1c1c",
        ).pack(pady=(100, 10))
        ttk.Label(
            body,
            text=str(exc) or type(exc).__name__,
            justify="center",
            wraplength=850,
        ).pack(pady=(0, 18))
        buttons = ttk.Frame(body)
        buttons.pack()
        ttk.Button(
            buttons,
            text="다시 시도",
            command=lambda target=index: self.retry(target),
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons,
            text="개별 창으로 열기",
            command=lambda target=index: self.open_standalone(target),
        ).pack(side="left", padx=4)

    @staticmethod
    def _record_error(spec, exc):
        record_error(spec.label, exc)

    def retry(self, index):
        sys.modules.pop(TOOLS[index].module_name, None)
        self.load_states[index] = "pending"
        self._show_placeholder(index)
        self._schedule_load(index)

    def select_tab(self, index):
        self.notebook.select(index)
        self._schedule_load(index)
        return "break"

    def open_current_standalone(self):
        self.open_standalone(self.notebook.index(self.notebook.select()))

    def open_standalone(self, index):
        launcher = TOOLS[index].launcher
        if not launcher.is_file():
            messagebox.showerror(
                "개별 실행 실패",
                f"실행 파일을 찾을 수 없습니다:\n{launcher}",
                parent=self.root,
            )
            return
        try:
            if os.name == "nt":
                os.startfile(launcher)
            else:
                subprocess.Popen([str(launcher)], cwd=str(launcher.parent))
        except OSError as exc:
            messagebox.showerror("개별 실행 실패", str(exc), parent=self.root)

    def close(self):
        if self.find_dialog is not None and self.find_dialog.winfo_exists():
            self.find_dialog.destroy()
        for app in self.apps.values():
            shutdown_queue = getattr(app, "shutdown_shared_queue", None)
            if callable(shutdown_queue):
                try:
                    shutdown_queue()
                except Exception:
                    pass
            persist = getattr(app, "persist_config", None)
            if callable(persist):
                try:
                    persist()
                except Exception:
                    pass
        self.root.destroy()


def main():
    apply_app_user_model_id()
    root = tk.Tk()
    apply_app_icon(root)
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    IntegratedApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - last resort, see report_fatal
        report_fatal_startup_error(exc)
        sys.exit(1)
