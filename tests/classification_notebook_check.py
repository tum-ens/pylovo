"""Check that every pylovo name a classification notebook uses exists with a matching signature.

Reads the code cells of the notebooks with the json module, parses them with ast (no execution, no
database access) and verifies, against the installed pylovo package:

* every ``from pylovo... import name`` resolves,
* every call of an imported function/class binds to its signature (argument names and counts),
* every method called on a variable that was assigned from an imported class exists and binds.

Used by ``test_classification_packaging.py``; also runnable from the repository root:
``uv run python tests/classification_notebook_check.py notebook_tutorials/classification``
"""
import ast
import importlib
import inspect
import json
import sys
from pathlib import Path


def code_of(nb_path: Path) -> str:
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    cells = [("".join(c["source"])) for c in nb["cells"] if c["cell_type"] == "code"]
    lines = []
    for cell in cells:
        for line in cell.splitlines():
            if line.lstrip().startswith(("%", "!")):
                line = "pass  # magic: " + line
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


def check(nb_path: Path) -> list[str]:
    problems: list[str] = []
    tree = ast.parse(code_of(nb_path))
    imported: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("pylovo"):
            try:
                mod = importlib.import_module(node.module)
            except Exception as exc:
                problems.append(f"import {node.module}: {type(exc).__name__}: {exc}")
                continue
            for alias in node.names:
                if not hasattr(mod, alias.name):
                    problems.append(f"{node.module} has no attribute {alias.name!r}")
                else:
                    imported[alias.asname or alias.name] = getattr(mod, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("pylovo"):
                    try:
                        importlib.import_module(alias.name)
                    except Exception as exc:
                        problems.append(f"import {alias.name}: {type(exc).__name__}: {exc}")

    # variable -> class for "x = Cls(...)" and "with Cls(...) as x"
    var_class: dict[str, type] = {}
    for node in ast.walk(tree):
        value, targets = None, []
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            value, targets = node.value, [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.withitem) and isinstance(node.context_expr, ast.Call) and isinstance(node.optional_vars, ast.Name):
            value, targets = node.context_expr, [node.optional_vars.id]
        if value is not None and isinstance(value.func, ast.Name) and inspect.isclass(imported.get(value.func.id)):
            for t in targets:
                var_class[t] = imported[value.func.id]

    def bind(fn, call: ast.Call, label: str, skip_self=False):
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return
        params = list(sig.parameters.values())
        if skip_self and params and params[0].name in ("self", "cls"):
            params = params[1:]
            sig = sig.replace(parameters=params)
        args = [object()] * sum(1 for a in call.args if not isinstance(a, ast.Starred))
        kwargs = {k.arg: object() for k in call.keywords if k.arg}
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            return
        try:
            sig.bind(*args, **kwargs)
        except TypeError as exc:
            problems.append(f"line {call.lineno}: {label}{sig}: {exc}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in imported:
            obj = imported[node.func.id]
            if callable(obj):
                bind(obj, node, node.func.id)
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in var_class:
            cls = var_class[node.func.value.id]
            name = node.func.attr
            if not hasattr(cls, name):
                problems.append(f"line {node.lineno}: {cls.__name__} has no attribute {name!r}")
            else:
                attr = getattr(cls, name)
                if callable(attr):
                    bind(attr, node, f"{cls.__name__}.{name}", skip_self=True)
    return problems


def main() -> int:
    root = Path(sys.argv[1])
    bad = 0
    for nb in sorted(root.rglob("*.ipynb")):
        problems = check(nb)
        status = "OK " if not problems else "BAD"
        print(f"{status} {nb}")
        for p in problems:
            print("     ", p)
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
