"""Add `= None` defaults to pydantic model fields that are Optional (v1 semantics)."""
import ast
import pathlib
import sys

MODELS = pathlib.Path("cortex_xdr_client/api/models")


def is_optional(node: ast.expr) -> bool:
    """True when the annotation allows None anywhere in a Union (pydantic v1 -> optional field)."""
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Subscript) and getattr(node.value, "id", None) == "Optional":
        return True
    if isinstance(node, ast.Subscript) and getattr(node.value, "id", None) == "Union":
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        return any(is_optional(arg) for arg in args)
    return False


changed = {}
for path in sorted(MODELS.glob("*.py")):
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    edits = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for stmt in cls.body:
            if not isinstance(stmt, ast.AnnAssign) or stmt.value is not None:
                continue
            if not is_optional(stmt.annotation):
                continue
            assert stmt.end_lineno == stmt.lineno, (path, stmt.lineno)
            idx = stmt.end_lineno - 1
            line = lines[idx]
            stripped = line.rstrip("\r\n")
            assert stripped.rstrip() == stripped, (path, stmt.lineno, repr(line))
            newline = line[len(stripped):]
            lines[idx] = stripped + " = None" + newline
            edits.append((stmt.lineno, stmt.target.id))
    if edits:
        path.write_text("".join(lines))
        changed[str(path)] = edits

for path, edits in changed.items():
    print(f"{path}: {len(edits)} fields")
print("total:", sum(len(e) for e in changed.values()), file=sys.stderr)
