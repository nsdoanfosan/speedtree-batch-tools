"""Enumerate the reason codes production code can actually emit.

`repair_orchestration.evidence_reason_codes()` reads tokens out of evidence
fields named in `REASON_KEYS`.  A reason code is therefore any string literal
that a production module writes into one of those fields, and that set -- not
a hand-maintained list -- is what the repair contract has to cover.

The scan is deliberately syntactic.  It parses each module and collects string
constants that reach a `REASON_KEYS` field through a dict literal, a keyword
argument, a subscript assignment, or an append onto a list bound to such a
name.  It cannot see a code assembled at run time; those still have to be
registered by hand, and the registry accepts them.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from repair_orchestration import REASON_KEYS

REPO_DIR = Path(__file__).resolve().parent

# Directories that are not this repository's production source: the vendored
# checkout, agent worktrees, and the suites themselves.  A test may legitimately
# name a code the production code no longer emits.
SKIP_PARTS = frozenset({
    "work", ".claude", ".git", "tests", "__pycache__", "node_modules",
})

# A reason code is an identifier-shaped token.  The same fields also carry
# prose, paths and rendered messages, and none of those are contract tokens.
CODE_TOKEN = re.compile(r"^[a-z][a-z0-9_]{2,60}$")

# Local names that hold a list of reason codes before it is written into an
# evidence field, so `issues.append("...")` is reached as well.
REASON_LIST_NAMES = frozenset({
    "issues", "issue", "reasons", "reason_codes", "codes",
})

# Failure/status kinds can be persisted as a target's reason token even when
# the defining module stores them in a named set and compares them later.
# Keep this list narrow: collecting every module-level string collection would
# turn ordinary schema keys and configuration values into fake reason codes.
REASON_COLLECTION_NAMES = frozenset({
    "UNREAL_RECOVERY_FAILURE_KINDS",
    "BLENDER_EXPORT_RETRY_FAILURE_KINDS",
})

_MUTATORS = frozenset({"append", "add", "extend", "update"})

# Gates rarely build the evidence dict inline.  They call a local helper --
# `add_asset_issue("managed_mesh_owner_ambiguous", "...")`,
# `_record_issue("cluster_stale", ...)` -- whose first positional argument is
# the code.  Without this rule the whole `atlas_consumer_integrity` family,
# including the code that blocked the blackgum Cluster, is invisible.
_ISSUE_FUNCTION = re.compile(r"(issue|reason|block|exclusion)", re.IGNORECASE)

_REASON_ARGUMENT_NAMES = frozenset({
    "reason",
    "reason_code",
    "reason_token",
    "failure_kind",
    "terminal_reason",
})


def _string_constants(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # Runtime extraction normalizes evidence tokens with ``casefold()``.
        # The source scan must do the same or uppercase issue constants such
        # as NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE disappear from the
        # ledger even though the planner receives their lowercase form.
        normalized = node.value.strip().casefold()
        if CODE_TOKEN.match(normalized):
            yield normalized
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for element in node.elts:
            yield from _string_constants(element)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "list", "set", "tuple"}
    ):
        for argument in node.args:
            yield from _string_constants(argument)
    elif isinstance(node, ast.IfExp):
        yield from _string_constants(node.body)
        yield from _string_constants(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _string_constants(value)


def _is_reason_key(name) -> bool:
    return isinstance(name, str) and name.casefold() in REASON_KEYS


def scan_module(path: Path) -> set[str]:
    """Return every reason-code literal one module can write into evidence."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set()
    found: set[str] = set()
    reason_function_args: dict[str, set[int]] = {}
    for definition in ast.walk(tree):
        if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = [
            *definition.args.posonlyargs,
            *definition.args.args,
        ]
        if positional and positional[0].arg in {"self", "cls"}:
            positional = positional[1:]
        indexes = {
            index
            for index, argument in enumerate(positional)
            if argument.arg.casefold() in _REASON_ARGUMENT_NAMES
        }
        if indexes:
            reason_function_args.setdefault(definition.name, set()).update(
                indexes
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and _is_reason_key(key.value):
                    found.update(_string_constants(value))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if _is_reason_key(keyword.arg):
                    found.update(_string_constants(keyword.value))
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr in _MUTATORS
                and isinstance(function.value, ast.Name)
                and function.value.id in REASON_LIST_NAMES
            ):
                for argument in node.args:
                    found.update(_string_constants(argument))
            name = (
                function.id if isinstance(function, ast.Name)
                else function.attr if isinstance(function, ast.Attribute)
                else ""
            )
            if name and _ISSUE_FUNCTION.search(name) and node.args:
                found.update(_string_constants(node.args[0]))
            for index in reason_function_args.get(name, ()):
                if index < len(node.args):
                    found.update(_string_constants(node.args[index]))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(
                    target.slice, ast.Constant
                ):
                    if _is_reason_key(target.slice.value):
                        found.update(_string_constants(node.value))
                elif (
                    isinstance(target, ast.Name)
                    and target.id in REASON_COLLECTION_NAMES
                ):
                    found.update(_string_constants(node.value))
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id in REASON_COLLECTION_NAMES
            ):
                found.update(_string_constants(node.value))
    return found


def production_sources(root: Path | None = None):
    root = Path(root or REPO_DIR)
    for path in sorted(root.rglob("*.py")) + sorted(root.rglob("*.pyw")):
        if SKIP_PARTS & set(path.parts):
            continue
        if path.name.startswith("test_"):
            continue
        yield path


def emitted_reason_codes(root: Path | None = None) -> dict[str, list[str]]:
    """Map every emitted reason code to the modules that can emit it."""
    root = Path(root or REPO_DIR)
    result: dict[str, set[str]] = {}
    for path in production_sources(root):
        for code in scan_module(path):
            result.setdefault(code, set()).add(
                path.relative_to(root).as_posix()
            )
    return {code: sorted(paths) for code, paths in sorted(result.items())}


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import sys
    from repair_reason_registry import REASON_REGISTRY, UNCLASSIFIED

    emitted = emitted_reason_codes()
    unregistered = sorted(set(emitted) - set(REASON_REGISTRY))
    unclassified = sorted(
        code for code, row in REASON_REGISTRY.items()
        if row.disposition == UNCLASSIFIED
    )
    print(f"emitted reason codes      : {len(emitted)}")
    print(f"registered                : {len(REASON_REGISTRY)}")
    print(f"unregistered (must be 0)  : {len(unregistered)}")
    print(f"still unclassified        : {len(unclassified)}")
    for code in unregistered:
        print(f"  UNREGISTERED {code:<52} {emitted[code][0]}")
    sys.exit(1 if unregistered else 0)
