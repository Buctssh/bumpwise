import pathlib
import re

root = pathlib.Path("cortex_xdr_client")
total = 0
for path in sorted(root.rglob("*.py")):
    src = path.read_text()
    new = src.replace(".parse_obj(", ".model_validate(")
    n = src.count(".parse_obj(")
    if n:
        path.write_text(new)
        print(f"{path}: {n}")
        total += n
print("total", total)
