"""Render one standalone squash candidate script for a migration range, offline.

``prepare_candidate`` reads the registered modules' scripts, never a database. The range comes
from ``squash.build_spec``; the body is either synthesized from the old scripts' syntax tree (a small
allow-list of writer-style declarations and ``op`` calls, nothing is executed) or taken from a
hand-written ``script`` whose declarations and entry points are checked against the same spec. The
candidate carries the ``bedrock_squash`` literal and is written exclusively outside every active
``migrations`` directory, after all checks and rendering, so a failure leaves no file behind.

This file must not use ``from __future__ import annotations``.
"""

import ast
import io
import pprint
import tokenize
from datetime import UTC, datetime
from inspect import CO_ASYNC_GENERATOR, CO_COROUTINE, CO_GENERATOR, CO_ITERABLE_COROUTINE
from pathlib import Path
from types import CodeType
from typing import TYPE_CHECKING, Any

from .graph import MigrationError, MigrationGraph, _strings
from .squash import SquashSpec, build_spec
from .writer import _SCRIPT_TEMPLATE, _docstring

if TYPE_CHECKING:
    from ...module.registry import ModuleRegistry

_BY_HAND = "write the squash by hand and pass --script"
_SAFE_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_DECLARED = ("revision", "down_revision", "depends_on")
_NOT_PLAIN = CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR | CO_ITERABLE_COROUTINE
_ANNOTATIONS = {"revision": {"str"}, "down_revision": {"str | None"}, "depends_on": {"Sequence[str]"}}

_TYPE_KW = {"length", "precision", "scale", "timezone", "asdecimal", "as_uuid", "native_uuid", "collation"}
_CONSTRAINT_KW = {"name"}
_FK_KW = {"name", "ondelete", "onupdate"}
_COLUMN_KW = {"nullable", "primary_key", "unique", "index", "server_default", "default", "autoincrement", "comment"}
_SA_CALLS: dict[str, set[str]] = {
    **dict.fromkeys(
        "Integer BigInteger SmallInteger String Text Boolean Float Numeric DateTime Date Time LargeBinary "
        "JSON Uuid".split(),
        _TYPE_KW,
    ),
    "Column": _COLUMN_KW,
    "PrimaryKeyConstraint": _CONSTRAINT_KW,
    "UniqueConstraint": _CONSTRAINT_KW,
    "ForeignKeyConstraint": _FK_KW,
    "ForeignKey": _FK_KW,
}
_OP_CALLS: dict[str, set[str]] = {
    "create_table": {"schema", "comment", "if_not_exists"},
    "drop_table": {"schema", "if_exists"},
    "create_index": {"unique", "schema", "if_not_exists"},
    "drop_index": {"table_name", "schema", "if_exists"},
    "add_column": {"schema"},
    "drop_column": {"schema"},
}


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _docstring_node(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _read(path: Path, app: str) -> tuple[str, str]:
    """Source text and encoding exactly as the graph loader's ``compile`` of the bytes sees them (BOM, cookie)."""
    try:
        data = path.read_bytes()
        encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
        return data.decode(encoding), encoding
    except (OSError, SyntaxError, LookupError, UnicodeDecodeError) as exc:
        raise MigrationError(f"Cannot read squash script {path}: {exc}", app) from exc


class _Source:
    """One script's source with errors that name the file and line."""

    def __init__(self, path: Path, app: str) -> None:
        self.path = path
        self.app = app

    def parse(self, text: str) -> ast.Module:
        try:
            return ast.parse(text, filename=str(self.path))
        except SyntaxError as exc:
            raise MigrationError(f"{self.path}:{exc.lineno}: invalid syntax; {_BY_HAND}.", self.app) from exc

    def fail(self, node: ast.AST, what: str) -> MigrationError:
        return MigrationError(f"{self.path}:{getattr(node, 'lineno', 1)}: {what}; {_BY_HAND}.", self.app)

    def expr(self, node: ast.expr) -> None:
        """Accept constants, lists/tuples of them, ``op.f('name')`` and allow-listed ``sa.X(...)`` calls."""
        if isinstance(node, ast.Constant):
            if type(node.value) not in (str, int, float, bool, type(None)):
                raise self.fail(node, f"unsupported constant {node.value!r}")
        elif (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and type(node.operand.value) in (int, float)
        ):
            pass
        elif isinstance(node, (ast.List, ast.Tuple)):
            for elt in node.elts:
                self.expr(elt)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner, attr = node.func.value, node.func.attr
            if _is_name(owner, "op") and attr == "f":
                if node.keywords or len(node.args) != 1 or not isinstance(node.args[0], ast.Constant):
                    raise self.fail(node, "op.f() takes one string constant")
            elif _is_name(owner, "sa") and attr in _SA_CALLS:
                self.call(node, _SA_CALLS[attr], f"sa.{attr}")
            else:
                raise self.fail(node, f"unsupported call '{ast.unparse(node.func)}'")
        else:
            raise self.fail(node, f"unsupported expression '{ast.unparse(node)}'")

    def call(self, node: ast.Call, keywords: set[str], label: str) -> None:
        for arg in node.args:
            self.expr(arg)
        for kw in node.keywords:
            if kw.arg is None or kw.arg not in keywords:
                raise self.fail(kw.value, f"unsupported argument '{kw.arg or '**'}' of {label}")
            self.expr(kw.value)

    def body(self, func: ast.FunctionDef) -> list[str]:
        """Validate a writer-style entry point and return its operations as source lines."""
        args = func.args
        if (
            args.args
            or args.posonlyargs
            or args.kwonlyargs
            or args.vararg
            or args.kwarg
            or func.decorator_list
            or getattr(func, "type_params", None)
        ):
            raise self.fail(func, f"{func.name}() must take no arguments, type parameters or decorators")
        if func.returns is not None and not (isinstance(func.returns, ast.Constant) and func.returns.value is None):
            raise self.fail(func, f"{func.name}() may only be annotated '-> None'")
        steps: list[str] = []
        for index, stmt in enumerate(func.body):
            if isinstance(stmt, ast.Pass) or (index == 0 and _docstring_node(stmt)):
                continue
            call = stmt.value if isinstance(stmt, ast.Expr) else None
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and _is_name(call.func.value, "op")
                and call.func.attr in _OP_CALLS
            ):
                raise self.fail(stmt, f"unsupported statement in {func.name}()")
            self.call(call, _OP_CALLS[call.func.attr], f"op.{call.func.attr}")
            steps.append(ast.unparse(call))
        return steps

    def steps(self, text: str) -> tuple[list[str], list[str]]:
        """Return ``(upgrade, downgrade)`` operation lines of a script that only uses the allowed syntax."""
        tree = self.parse(text)
        found: dict[str, ast.FunctionDef] = {}
        seen: set[str] = set()
        for index, stmt in enumerate(tree.body):
            if index == 0 and _docstring_node(stmt):
                continue
            if isinstance(stmt, ast.ImportFrom) and stmt.level == 0:
                if (stmt.module, [(a.name, a.asname) for a in stmt.names]) in (
                    ("collections.abc", [("Sequence", None)]),
                    ("alembic", [("op", None)]),
                ):
                    continue
            elif isinstance(stmt, ast.Import) and [(a.name, a.asname) for a in stmt.names] == [("sqlalchemy", "sa")]:
                continue
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                target = stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else None
                if isinstance(stmt, ast.AnnAssign):
                    target = stmt.target
                    if stmt.value is not None and ast.unparse(stmt.annotation) not in _ANNOTATIONS.get(
                        getattr(target, "id", ""), ()
                    ):
                        raise self.fail(stmt, "unsupported annotation")
                if isinstance(target, ast.Name) and target.id in _DECLARED and stmt.value is not None:
                    if target.id in seen:
                        raise self.fail(stmt, f"duplicate '{target.id}'")
                    try:
                        ast.literal_eval(stmt.value)
                    except (ValueError, TypeError) as exc:
                        raise self.fail(stmt, f"'{target.id}' must be a literal") from exc
                    seen.add(target.id)
                    continue
            elif isinstance(stmt, ast.FunctionDef) and stmt.name in ("upgrade", "downgrade"):
                if stmt.name in found:
                    raise self.fail(stmt, f"duplicate {stmt.name}()")
                found[stmt.name] = stmt
                continue
            raise self.fail(stmt, "unsupported top-level statement")
        for name in ("upgrade", "downgrade"):
            if name not in found:
                raise self.fail(tree, f"missing {name}()")
        return self.body(found["upgrade"]), self.body(found["downgrade"])


def _auto_body(graph: MigrationGraph, spec: SquashSpec) -> tuple[str, str]:
    """Indented upgrade (original order) and downgrade (reversed script order) bodies of the range."""
    ups: list[list[str]] = []
    downs: list[list[str]] = []
    for key, _ in spec.old:
        path = graph[key].path
        text, _ = _read(path, spec.app)
        up, down = _Source(path, spec.app).steps(text)
        ups.append(up)
        downs.append(down)

    def join(chunks: list[list[str]]) -> str:
        lines = [line for chunk in chunks for step in chunk for line in step.split("\n")]
        return "\n    ".join(lines) or "pass"

    return join(ups), join(downs[::-1])


def _declared(tree: ast.Module) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for stmt in tree.body:
        target = stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else None
        if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target = stmt.target
        if isinstance(target, ast.Name) and target.id in (*_DECLARED, "bedrock_squash"):
            try:
                out[target.id] = ast.literal_eval(stmt.value)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                out[target.id] = _NOT_LITERAL
    return out


_NOT_LITERAL = object()


def _manual(text: str, script: Path, spec: SquashSpec) -> str:
    """Check a hand-written candidate against *spec* and return its source ready for the squash literal."""
    src = _Source(script, spec.app)
    tree = src.parse(text)
    try:
        code = compile(text, str(script), "exec")
    except (SyntaxError, ValueError) as exc:
        raise MigrationError(f"{script}:{getattr(exc, 'lineno', None) or 1}: {exc}; {_BY_HAND}.", spec.app) from exc
    flags = {c.co_name: c.co_flags for c in code.co_consts if isinstance(c, CodeType)}
    guarded = {*_DECLARED, "upgrade", "downgrade", "bedrock_squash"}
    plain = {id(s.targets[0]) for s in tree.body if isinstance(s, ast.Assign) and len(s.targets) == 1}
    plain |= {id(s.target) for s in tree.body if isinstance(s, ast.AnnAssign)}
    entries = {id(s) for s in tree.body if isinstance(s, ast.FunctionDef) and s.name in ("upgrade", "downgrade")}
    for node in ast.walk(tree):
        bound: str | None = None
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and not (id(node) in plain and node.id in _DECLARED)
        ):
            bound = node.id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and id(node) not in entries:
            bound = node.name
        elif isinstance(node, ast.alias):
            if node.name == "*":
                raise src.fail(tree, "star imports are not allowed")
            bound = (node.asname or node.name).partition(".")[0]
        elif isinstance(node, ast.ExceptHandler):
            bound = node.name
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound = next(iter(guarded.intersection(node.names)), None)
        if bound in guarded:
            raise src.fail(node, f"'{bound}' may not be rebound or shadowed")
    for name in ("upgrade", "downgrade"):
        funcs = [s for s in tree.body if isinstance(s, ast.FunctionDef) and s.name == name]
        if len(funcs) != 1:
            raise src.fail(tree, f"expected exactly one top-level {name}()")
        func = funcs[0]
        args = func.args
        if args.args or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or func.decorator_list:
            raise src.fail(func, f"{name}() must take no arguments and have no decorators")
        if getattr(func, "type_params", None) or name not in flags or flags[name] & _NOT_PLAIN:
            raise src.fail(func, f"{name}() must be a plain synchronous function")
    declared = _declared(tree)
    for name in _DECLARED:
        stmts = [
            s
            for s in tree.body
            if (isinstance(s, ast.Assign) and any(_is_name(t, name) for t in s.targets))
            or (isinstance(s, ast.AnnAssign) and _is_name(s.target, name))
        ]
        if len(stmts) > 1 or (name == "revision" and not stmts):
            raise src.fail(stmts[1] if stmts else tree, f"'{name}' must be declared exactly once")
        if declared.get(name) is _NOT_LITERAL:
            raise src.fail(stmts[0], f"'{name}' must be a literal")
    if "bedrock_squash" in declared or any(
        isinstance(n, ast.Name) and n.id == "bedrock_squash" for n in ast.walk(tree)
    ):
        raise src.fail(tree, "the candidate must not define 'bedrock_squash'; it is added by prepare")
    if declared.get("revision") != spec.end[1]:
        raise src.fail(tree, f"revision {declared.get('revision')!r} must equal the squash end '{spec.end[1]}'")
    refs: dict[str, tuple[str, ...]] = {}
    for name in ("down_revision", "depends_on"):
        try:
            refs[name] = _strings(declared.get(name), name, "", spec.app)
        except MigrationError as exc:
            raise src.fail(tree, f"'{name}' must be None, a string, or a list/tuple of strings") from exc
    local = {f"{spec.app}:{r}" for a, r in spec.parents if a == spec.app}
    other = {f"{a}:{r}" for a, r in spec.parents if a != spec.app}
    for name, got, want in (
        ("down_revision", {f"{spec.app}:{d}" for d in refs["down_revision"]}, local),
        ("depends_on", set(refs["depends_on"]), other),
    ):
        if got != want:
            raise src.fail(tree, f"'{name}' {sorted(got)} must equal the range's external parents {sorted(want)}")
    return text.rstrip("\n") + "\n\n"


def _candidate(
    registry: "ModuleRegistry", graph: MigrationGraph, spec: SquashSpec, script: Path | None
) -> tuple[str, str]:
    """Complete candidate source with the replacement literal and its file encoding, verified before writing."""
    first, last = spec.old[0][0][1], spec.old[-1][0][1]
    encoding = "utf-8"
    if script is not None:
        text, encoding = _read(script, spec.app)
        head = _manual(text, script, spec)
    else:
        ups, downs = _auto_body(graph, spec)
        local = [r for a, r in spec.parents if a == spec.app]
        if len(local) > 1:
            raise MigrationError(f"Range {first}..{last} has several parents inside '{spec.app}'.", spec.app)
        head = _SCRIPT_TEMPLATE.format(
            message=_docstring(f"Squash {spec.app} {first}..{last}."),
            revision=spec.end[1],
            revises=local[0] if local else "",
            create_date=datetime.now(UTC).isoformat(),
            imports="",
            down_revision=local[0] if local else None,
            depends_on=tuple(f"{a}:{r}" for a, r in sorted(spec.parents) if a != spec.app),
            upgrades=ups,
            downgrades=downs,
        )
        head = head.rstrip("\n") + "\n\n"
    source = head + "bedrock_squash = " + pprint.pformat(spec.metadata(), sort_dicts=False) + "\n"
    try:
        compile(source.encode(encoding), spec.end[1], "exec")  # the bytes the loader will compile
    except (UnicodeEncodeError, SyntaxError, ValueError) as exc:
        raise MigrationError(f"Candidate of '{spec.end[1]}' is not valid {encoding} source: {exc}", spec.app) from exc
    if _declared(ast.parse(source)).get("bedrock_squash") != spec.metadata():
        raise MigrationError(f"Squash literal of '{spec.end[1]}' does not round-trip.", spec.app)
    return source, encoding


def _manifest(graph: MigrationGraph, spec: SquashSpec, candidate: Path) -> str:
    old = [graph[key].path for key, _ in spec.old]
    kept_name = spec.end == spec.old[-1][0]
    lines = [
        f"Squash of {spec.app}: {spec.old[0][0][1]} .. {spec.old[-1][0][1]} -> {spec.end[1]}",
        f"Candidate: {candidate}",
        "",
        "Release steps (in version control, one change):",
    ]
    target = old[-1] if kept_name else old[-1].parent / f"{spec.end[1]}.py"
    if target in old:  # the candidate takes over an old file's name; never add and delete one path
        lines.append(f"  1. Replace {target} with the candidate (same file name).")
    elif target.exists() or target.is_symlink():
        raise MigrationError(f"{target} already exists in the active migrations; choose another --rename.", spec.app)
    else:
        lines.append(f"  1. Add the candidate as {target}.")
    lines.append("  2. Delete these files:")
    lines.extend(f"       {path}" for path in old if path != target)
    lines += [
        "  3. Replay on a copy, review, then release.",
        "  4. Deploy with the ordinary `bedrock db upgrade` or `ensure_schema`: a complete range in the target's",
        "     ancestry is reconciled before business migrations (fresh executes, current skips).",
        f"     A partly applied range is refused; first upgrade it to {spec.old[-1][0][1]} on the bridge release.",
        "  5. Optional preview or handover/recovery: `bedrock db squash apply` is read-only; `--apply` switches manually.",
    ]
    return "\n".join(lines) + "\n"


def _check_out(registry: "ModuleRegistry", out: Path, end: str) -> tuple[Path, Path, Path]:
    """Resolve the output directory and files; reject unsafe or occupied targets without touching disk."""
    if not end or not set(end) <= _SAFE_ID:
        raise MigrationError(f"Revision '{end}' is not a safe file name.")
    if out.is_symlink():
        raise MigrationError(f"Output directory {out} must not be a symbolic link.")
    resolved = out.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise MigrationError(f"Output path {out} is not a directory.")
    for app in registry.all():
        active = (app.package_dir / "migrations").resolve()
        if resolved.is_relative_to(active):
            raise MigrationError(
                f"Output {out} overlaps the active migrations of '{app.name}'; use a separate directory."
            )
    candidate = resolved / f"{end}.py"
    manifest = resolved / f"{end}.files.txt"
    for target in (candidate, manifest):
        if target.exists() or target.is_symlink():
            raise MigrationError(f"{target} already exists; choose another --out or remove it.")
    return resolved, candidate, manifest


def prepare_candidate(
    registry: "ModuleRegistry",
    app: str,
    first: str,
    through: str,
    *,
    out: Path,
    script: Path | None = None,
    rename: str | None = None,
) -> Path:
    """Write ``<out>/<end>.py`` (plus ``<end>.files.txt`` listing the originals) and return the script path.

    Args:
        registry: Loaded modules; their scripts are read, no database is touched.
        app: Module owning the range.
        first: First revision of the range.
        through: Last revision of the range.
        out: Directory for the candidate; must be outside every active ``migrations`` directory.
        script: Hand-written candidate to check and use instead of synthesizing one.
        rename: Full new end revision instead of keeping *through*.

    Raises:
        MigrationError: Invalid range, unsupported syntax (with file and line), unsafe or occupied output.
    """
    graph = MigrationGraph.load(registry)
    spec = build_spec(graph, app, first, through, rename)
    directory, path, manifest = _check_out(registry, out, spec.end[1])
    source, encoding = _candidate(registry, graph, spec, script)
    listing = _manifest(graph, spec, path)

    missing = [p for p in (directory, *directory.parents) if not p.exists()]
    written: list[Path] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for target, text, enc in ((path, source, encoding), (manifest, listing, "utf-8")):
            with target.open("x", encoding=enc) as handle:
                written.append(target)
                handle.write(text)
    except BaseException as exc:
        for target in written:
            target.unlink(missing_ok=True)
        for created in missing:  # child first; stop at the first directory that is not ours to remove
            try:
                created.rmdir()
            except OSError:
                break
        if isinstance(exc, OSError):
            raise MigrationError(f"Cannot write the candidate under {directory}: {exc}", app) from exc
        raise
    return path
