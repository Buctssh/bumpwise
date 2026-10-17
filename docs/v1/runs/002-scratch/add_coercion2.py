"""Add coerce_numbers_to_str to the remaining model classes."""
import ast
import pathlib
import re

MODELS = pathlib.Path("cortex_xdr_client/api/models")


def has_str_field(cls: ast.ClassDef) -> bool:
    for stmt in cls.body:
        if not isinstance(stmt, ast.AnnAssign):
            continue
        for node in ast.walk(stmt.annotation):
            if isinstance(node, ast.Name) and node.id == "str":
                return True
    return False


def has_config(cls: ast.ClassDef) -> bool:
    return any(isinstance(s, ast.Assign) and getattr(s.targets[0], "id", None) == "model_config"
               for s in cls.body)


def is_model(cls: ast.ClassDef) -> bool:
    return any(getattr(base, "id", None) in ("BaseModel", "RootModel") or
               getattr(getattr(base, "value", None), "id", None) == "RootModel"
               for base in cls.bases)


for path in sorted(MODELS.glob("*.py")):
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    inserts = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        if not is_model(cls) or not has_str_field(cls) or has_config(cls):
            continue
        inserts[cls.body[-1].end_lineno] = True
    if not inserts:
        continue
    if "ConfigDict" not in src:
        src, n = re.subn(r"^from pydantic import (.*)$",
                         lambda m: "from pydantic import " + ", ".join(
                             sorted(set([p.strip() for p in m.group(1).split(",")] + ["ConfigDict"]))),
                         src, count=1, flags=re.M)
        assert n == 1, path
        lines = src.splitlines(keepends=True)
    out = []
    for idx, line in enumerate(lines, start=1):
        out.append(line)
        if idx in inserts:
            if line.strip():
                out.append("\n")
            out.append("    model_config = ConfigDict(coerce_numbers_to_str=True)\n")
    path.write_text("".join(out))
    print(f"{path}: {len(inserts)} classes")
