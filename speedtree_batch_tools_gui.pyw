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


class IntegratedApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.apps = {}
        self.load_states = ["pending"] * len(TOOLS)
        self.status_var = tk.StringVar(value="통합 도구 준비 중...")

        root.title("SpeedTree Batch Tools — 통합 데이터 관리")
        root.geometry("1500x920")
        root.minsize(1120, 700)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        style = ttk.Style(root)
        style.configure(
            "TNotebook.Tab",
            padding=(18, 8),
            font=("Segoe UI", 10, "bold"),
        )

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.tabs = []
        for index, spec in enumerate(TOOLS, start=1):
            tab = ToolTab(self.notebook)
            self.notebook.add(tab, text=f"{index}  {spec.label}")
            self.tabs.append(tab)
            self._show_placeholder(index - 1)

        footer = ttk.Frame(root, padding=(8, 5))
        footer.grid(row=1, column=0, sticky="ew")
        ttk.Label(
            footer,
            textvariable=self.status_var,
            foreground="#555555",
        ).pack(side="left")
        ttk.Label(
            footer,
            text="Ctrl+1 / Ctrl+2 / Ctrl+3으로 탭 이동",
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
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._schedule_load(0)

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
        try:
            with ERROR_LOG.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n[{datetime.now().isoformat(timespec='seconds')}] {spec.label}\n"
                )
                handle.write("".join(traceback.format_exception(exc)))
        except OSError:
            pass

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
        for app in self.apps.values():
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
    main()
