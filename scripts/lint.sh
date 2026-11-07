#!/usr/bin/env bash
set -euo pipefail

PYTHON="${PYTHON:-python3}"
RUFF_MISSING=0

RUFF_CMD=()
if "$PYTHON" -m ruff --version >/dev/null 2>&1; then
  RUFF_CMD=("$PYTHON" -m ruff)
elif command -v ruff >/dev/null 2>&1; then
  RUFF_CMD=(ruff)
else
  echo "error: ruff is required; install it with: $PYTHON -m pip install 'ruff>=0.6'" >&2
  RUFF_MISSING=1
fi

"$PYTHON" -m compileall -q skills/repo-evolution-research/scripts tests

if [ "$RUFF_MISSING" -eq 0 ]; then
  "${RUFF_CMD[@]}" check skills/repo-evolution-research/scripts scripts tests
fi

"$PYTHON" - <<'PY'
import ast
import sys
from pathlib import Path

files = list(Path("skills/repo-evolution-research/scripts").rglob("*.py"))
files += list(Path("scripts").glob("*.py"))
files += list(Path("tests").rglob("*.py"))
problems = []

for path in files:
    text = path.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 100:
            problems.append(f"{path}:{number}: line length {len(line)} > 100")
    tree = ast.parse(text)
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported[alias.asname or alias.name] = node.lineno
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for name, line in imported.items():
        if name not in used and name != "annotations":
            problems.append(f"{path}:{line}: unused import {name}")

if problems:
    print("\n".join(problems))
    sys.exit(1)
PY

if [ "$RUFF_MISSING" -eq 1 ]; then
  echo "fallback lint ok; ruff missing" >&2
  exit 1
fi
echo "lint ok"
