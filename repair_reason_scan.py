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
from repair_reason_registry import REASON_CODE_TOKEN

REPO_DIR = Path(__file__).resolve().parent

# Directories that are not this repository's production source: the vendored
# checkout, agent worktrees, and the suites themselves.  A test may legitimately
# name a code the production code no longer emits.
SKIP_PARTS = frozenset({
    "work", ".claude", ".git", "tests", "__pycache__", "node_modules",
})

# A reason code is an identifier-shaped token.  The same fields also carry
# prose, paths and rendered messages, and none of those are contract tokens.
CODE_TOKEN = REASON_CODE_TOKEN

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
    "default_token",
    "default_reason_token",
})

# Expanding a local name into a plain ``reason``/``result`` field pulls in
# human-facing diagnostics and ordinary state labels. These keys, by contrast,
# are code-bearing at runtime and are safe conservative local-name sinks.
_LOCAL_REASON_SINK_KEYS = frozenset({
    "blocked_reason_token",
    "code",
    "codes",
    "delivery_reason",
    "issue_code",
    "issue_codes",
    "reason_code",
    "reason_codes",
    "reason_token",
    "terminal_reason",
})

_LOCAL_REASON_KEYWORD_NAMES = frozenset({
    "default_reason_token",
    "default_token",
    "failure_kind",
    "reason_code",
    "reason_token",
    "terminal_reason",
})


def _string_constants(node, bindings=None):
    bindings = bindings or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # Runtime extraction normalizes evidence tokens with ``casefold()``.
        # The source scan must do the same or uppercase issue constants such
        # as NORMALIZED_GENERATOR_DELIVERY_INCOMPLETE disappear from the
        # ledger even though the planner receives their lowercase form.
        normalized = node.value.strip().casefold()
        if CODE_TOKEN.match(normalized):
            yield normalized
    elif isinstance(node, ast.Name):
        yield from bindings.get(node.id, ())
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for element in node.elts:
            yield from _string_constants(element, bindings)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "list", "set", "tuple"}
    ):
        for argument in node.args:
            yield from _string_constants(argument, bindings)
    elif isinstance(node, ast.IfExp):
        yield from _string_constants(node.body, bindings)
        yield from _string_constants(node.orelse, bindings)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _string_constants(value, bindings)


def _is_reason_key(name) -> bool:
    return isinstance(name, str) and name.casefold() in REASON_KEYS


def _flow_reason_name_constants(tree: ast.AST) -> set[str]:
    """Resolve local literal carriers at every supported reason-code sink."""

    found: set[str] = set()

    def scan_expression(node, state):
        if node is None:
            return
        for child in ast.walk(node):
            if isinstance(child, ast.Dict):
                has_explicit_wrapper_code = any(
                    isinstance(key, ast.Constant)
                    and str(key.value or "").casefold()
                    in {"code", "reason_code", "reason_token"}
                    and bool(set(_string_constants(value, state)))
                    for key, value in zip(child.keys, child.values)
                )
                for key, value in zip(child.keys, child.values):
                    if (
                        isinstance(key, ast.Constant)
                        and (
                            str(key.value or "").casefold()
                            in _LOCAL_REASON_SINK_KEYS
                            or (
                                str(key.value or "").casefold() == "reason"
                                and has_explicit_wrapper_code
                            )
                        )
                    ):
                        found.update(_string_constants(value, state))
            elif isinstance(child, ast.Call):
                for keyword in child.keywords:
                    if str(keyword.arg or "").casefold() in (
                        _LOCAL_REASON_SINK_KEYS
                        | _LOCAL_REASON_KEYWORD_NAMES
                    ):
                        found.update(
                            _string_constants(keyword.value, state)
                        )
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and str(target.slice.value or "").casefold()
                        in _LOCAL_REASON_SINK_KEYS
                    ):
                        found.update(
                            _string_constants(child.value, state)
                        )
            elif (
                isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Subscript)
                and isinstance(child.target.slice, ast.Constant)
                and str(child.target.slice.value or "").casefold()
                in _LOCAL_REASON_SINK_KEYS
            ):
                found.update(_string_constants(child.value, state))

    def dedupe(states, limit=256):
        unique = []
        seen = set()
        for state in states:
            key = tuple(sorted(
                (name, tuple(sorted(values)))
                for name, values in state.items()
                if values
            ))
            if key in seen:
                continue
            seen.add(key)
            unique.append(state)
        if len(unique) > limit:
            # Losing a path can hide a newly emitted blocker and make the
            # registry ratchet pass vacuously. Conservatively merge instead:
            # this may over-report a literal, but it cannot under-report one.
            merged = {}
            for state in unique:
                for name, values in state.items():
                    merged.setdefault(name, set()).update(values)
            return [merged]
        return unique

    def compared_name(test):
        if not (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and len(test.comparators) == 1
            and isinstance(test.left, ast.Name)
        ):
            return None
        comparator = test.comparators[0]
        if (
            isinstance(test.ops[0], (ast.Eq, ast.NotEq))
            and isinstance(comparator, ast.Constant)
            and isinstance(comparator.value, str)
        ):
            return (
                test.left.id,
                {comparator.value.strip().casefold()},
                isinstance(test.ops[0], ast.Eq),
            )
        if isinstance(test.ops[0], (ast.In, ast.NotIn)) and isinstance(
            comparator, (ast.List, ast.Tuple, ast.Set)
        ):
            constants = set(_string_constants(comparator))
            if constants:
                return (
                    test.left.id,
                    constants,
                    isinstance(test.ops[0], ast.In),
                )
        return None

    def refine(states, test, truth):
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return refine(states, test.operand, not truth)
        comparison = compared_name(test)
        if comparison is None:
            return [dict(state) for state in states]
        name, constants, positive = comparison
        wants_match = truth is positive
        refined = []
        for state in states:
            values = set(state.get(name, ()))
            if not values:
                refined.append(dict(state))
                continue
            remaining = (
                constants & values
                if wants_match
                else values - constants
            )
            if remaining:
                updated = dict(state)
                updated[name] = remaining
                refined.append(updated)
        return refined

    def assign_name(state, name, value):
        updated = dict(state)
        constants = set(_string_constants(value, state))
        if constants:
            updated[name] = constants
        else:
            updated.pop(name, None)
        return updated

    def run_block(statements, states):
        states = dedupe(states)
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                run_block(statement.body, [{}])
                continue
            if isinstance(statement, ast.ClassDef):
                run_block(statement.body, [{}])
                continue
            if isinstance(statement, ast.If):
                scan_expression(statement.test, {})
                true_states = run_block(
                    statement.body,
                    refine(states, statement.test, True),
                )
                false_states = run_block(
                    statement.orelse,
                    refine(states, statement.test, False),
                )
                states = dedupe([*true_states, *false_states])
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                scan_expression(
                    statement.test if isinstance(statement, ast.While)
                    else statement.iter,
                    {},
                )
                body_states = run_block(
                    statement.body,
                    [dict(state) for state in states],
                )
                states = run_block(
                    statement.orelse,
                    dedupe([*states, *body_states]),
                )
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    scan_expression(item.context_expr, {})
                states = run_block(statement.body, states)
                continue
            if isinstance(statement, ast.Try):
                branches = [run_block(statement.body, states)]
                branches.extend(
                    run_block(handler.body, states)
                    for handler in statement.handlers
                )
                states = run_block(
                    statement.finalbody,
                    run_block(
                        statement.orelse,
                        dedupe([
                            state
                            for branch in branches
                            for state in branch
                        ]),
                    ),
                )
                continue

            next_states = []
            for state in states:
                scan_expression(statement, state)
                updated = dict(state)
                if isinstance(statement, ast.Assign):
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            updated = assign_name(
                                updated, target.id, statement.value
                            )
                elif (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.value is not None
                ):
                    updated = assign_name(
                        updated, statement.target.id, statement.value
                    )
                next_states.append(updated)
            states = dedupe(next_states)
        return states

    run_block(getattr(tree, "body", ()), [{}])
    return found


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
        defaults = definition.args.defaults
        if defaults:
            for argument, default in zip(positional[-len(defaults):], defaults):
                if argument.arg.casefold() in _REASON_ARGUMENT_NAMES:
                    found.update(_string_constants(default))
        for argument, default in zip(
            definition.args.kwonlyargs,
            definition.args.kw_defaults,
        ):
            if (
                default is not None
                and argument.arg.casefold() in _REASON_ARGUMENT_NAMES
            ):
                found.update(_string_constants(default))
    found.update(_flow_reason_name_constants(tree))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and _is_reason_key(key.value):
                    found.update(_string_constants(value))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    _is_reason_key(keyword.arg)
                    or str(keyword.arg or "").casefold()
                    in _REASON_ARGUMENT_NAMES
                ):
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
        if SKIP_PARTS & set(path.relative_to(root).parts):
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


def main(root: Path | None = None) -> int:
    """Print registry coverage and return a process exit status."""
    root = Path(root or REPO_DIR)
    source_count = sum(1 for _path in production_sources(root))

    from repair_reason_registry import REASON_REGISTRY, UNCLASSIFIED

    emitted = emitted_reason_codes(root)
    unregistered = sorted(set(emitted) - set(REASON_REGISTRY))
    unclassified = sorted(
        code for code, row in REASON_REGISTRY.items()
        if row.disposition == UNCLASSIFIED
    )
    print(f"production sources scanned: {source_count}")
    print(f"emitted reason codes      : {len(emitted)}")
    print(f"registered                : {len(REASON_REGISTRY)}")
    print(f"unregistered (must be 0)  : {len(unregistered)}")
    print(f"still unclassified        : {len(unclassified)}")
    if not source_count:
        print("  ERROR no production sources were scanned")
    for code in unregistered:
        print(f"  UNREGISTERED {code:<52} {emitted[code][0]}")
    if source_count and not emitted:
        print("  ERROR production sources emitted no reason codes")
    return 1 if not source_count or not emitted or unregistered else 0


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import sys

    sys.exit(main())
