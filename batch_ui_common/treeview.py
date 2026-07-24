"""Reusable row-selection and clipboard behavior for batch-tool Treeviews.

The helpers deliberately avoid importing tkinter.  GUI callers only need to
provide objects with the small methods used here (for example ``selection``
or ``clipboard_append``), which also keeps the behavior straightforward to
test without opening a window.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any


PathValue = str | os.PathLike[str]


class CheckedRowController:
    """Coordinate checked rows and the initial all-to-one click gesture.

    ``entries`` is retained by reference so callers may clear and repopulate
    the same mutable mapping during a scan.  ``redraw`` is called as
    ``redraw(iid, entry)`` whenever this controller changes an entry.
    """

    def __init__(
        self,
        entries: MutableMapping[Any, MutableMapping[str, Any]],
        redraw: Callable[[Any, MutableMapping[str, Any]], None],
        checked_key: str = "checked",
    ) -> None:
        self.entries = entries
        self.redraw = redraw
        self.checked_key = checked_key
        self.armed = False

    def _all_checked(self) -> bool:
        return bool(self.entries) and all(
            bool(entry.get(self.checked_key)) for entry in self.entries.values()
        )

    def sync_after_reload(self) -> bool:
        """Synchronize the first-click gesture with freshly loaded entries."""

        self.armed = self._all_checked()
        return self.armed

    def set_all(self, checked: bool) -> None:
        """Set and redraw every row, arming an all-checked list for one click."""

        value = bool(checked)
        for iid, entry in self.entries.items():
            entry[self.checked_key] = value
            self.redraw(iid, entry)
        self.armed = value and bool(self.entries)

    def click(self, iid: Any) -> bool:
        """Apply a row click and return whether ``iid`` referred to an entry."""

        if iid not in self.entries:
            return False

        if self.armed and self._all_checked():
            for other_iid, entry in self.entries.items():
                entry[self.checked_key] = other_iid == iid
                self.redraw(other_iid, entry)
        else:
            entry = self.entries[iid]
            entry[self.checked_key] = not bool(entry.get(self.checked_key))
            self.redraw(iid, entry)

        self.armed = False
        return True


def _path_values(paths: Iterable[PathValue] | PathValue | None) -> Iterable[Any]:
    if paths is None:
        return ()
    if isinstance(paths, (str, os.PathLike)):
        return (paths,)
    return paths


def _normalized_paths(
    paths: Iterable[PathValue] | PathValue | None,
) -> list[str]:
    """Return unique absolute paths using Windows-style comparisons."""

    values: list[str] = []
    seen: set[str] = set()
    for raw_path in _path_values(paths):
        if raw_path is None:
            continue
        try:
            raw_value = os.fspath(raw_path)
        except TypeError:
            continue
        if not isinstance(raw_value, (str, bytes)):
            continue
        if isinstance(raw_value, bytes):
            raw_value = os.fsdecode(raw_value)
        if not raw_value.strip():
            continue
        value = str(Path(raw_value).expanduser().resolve(strict=False))
        key = os.path.normcase(value).casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def _everything_query(values: list[str]) -> str:
    """Format normalized paths as one query accepted by Everything."""

    if len(values) <= 1:
        return values[0] if values else ""
    return "|".join(f'"{value}"' for value in values)


def clipboard_text(paths: Iterable[PathValue] | PathValue | None) -> str:
    """Return absolute paths as a query ready to paste into Everything.

    Blank values are ignored and duplicates are removed case-insensitively,
    matching Windows path semantics while preserving the first spelling. A
    single path stays raw for compatibility; multiple paths are quoted and
    joined with Everything's OR operator so every file appears in the result.
    """

    return _everything_query(_normalized_paths(paths))


def copy_paths_to_clipboard(root: Any, paths: Iterable[PathValue] | PathValue | None) -> int:
    """Copy paths through a tkinter-compatible root and return the path count."""

    values = _normalized_paths(paths)
    text = _everything_query(values)
    if not text:
        return 0
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update_idletasks()
    return len(values)


def selected_row_paths(
    tree: Any,
    rows_by_iid: Mapping[Any, Any],
    paths_for_row: Callable[[Any], Iterable[PathValue] | PathValue | None],
) -> list[PathValue]:
    """Collect path values for valid rows in the Treeview selection order."""

    paths: list[PathValue] = []
    for iid in tree.selection():
        if iid not in rows_by_iid:
            continue
        row_paths = paths_for_row(rows_by_iid[iid])
        for path in _path_values(row_paths):
            if path is not None:
                paths.append(path)
    return paths


def copy_selected_row_paths(
    root: Any,
    tree: Any,
    rows_by_iid: Mapping[Any, Any],
    paths_for_row: Callable[[Any], Iterable[PathValue] | PathValue | None],
) -> int:
    """Copy paths derived from selected rows and return the copied path count."""

    return copy_paths_to_clipboard(
        root,
        selected_row_paths(tree, rows_by_iid, paths_for_row),
    )
