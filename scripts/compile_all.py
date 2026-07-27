from pathlib import Path

count = 0
for path in sorted(Path(".").rglob("*.py")):
    if ".git" in path.parts or "__pycache__" in path.parts:
        continue
    compile(path.read_bytes(), str(path), "exec")
    count += 1
if count == 0:
    raise SystemExit("no Python files were compiled")
print(f"compiled {count} Python files")
