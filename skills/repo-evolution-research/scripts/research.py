#!/usr/bin/env python3
"""Thin entry point for the repo-evolution-research local CLI."""

# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "jsonschema>=4.23,<5",
# ]
# ///

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

MINIMUM_PYTHON = (3, 11)
SUPPORTED_SYSTEMS = ("Darwin", "Linux")

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _bootstrap_error(message: str, recovery: str) -> int:
    """Report an environment failure before the CLI package is imported."""
    envelope = {
        "ok": False,
        "command": None,
        "error": {
            "code": "environment_error",
            "message": message,
            "source": "bootstrap",
            "retryable": False,
            "recovery": recovery,
            "details": {},
        },
    }
    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    print(f"environment_error: {message}", file=sys.stderr)
    print(f"recovery: {recovery}", file=sys.stderr)
    return 3


def check_runtime() -> int | None:
    """Return an exit code when this interpreter or platform cannot run the CLI."""
    if sys.version_info < MINIMUM_PYTHON:
        return _bootstrap_error(
            f"需要 Python >= 3.11，当前解释器为 {platform.python_version()}",
            "使用技能目录下的 rer 启动器（uv 会选择合适解释器），或安装 Python 3.11+",
        )
    if platform.system() not in SUPPORTED_SYSTEMS:
        return _bootstrap_error(
            f"当前平台 {platform.system()} 不受支持：存储锁依赖 fcntl",
            "在 macOS、Linux 或 WSL 中运行",
        )
    return None


def main() -> int:
    from rer.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    failure = check_runtime()
    if failure is not None:
        raise SystemExit(failure)
    raise SystemExit(main())
