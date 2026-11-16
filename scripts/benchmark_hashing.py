"""Compare the previous whole-file SHA-256 path with bounded-memory hashing.

This is a local synthetic benchmark, not a GitHub collection benchmark.
The generated file lives in a temporary directory and is removed on exit.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "skills" / "repo-evolution-research" / "scripts"),
)

from rer.store import file_sha256  # noqa: E402


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def whole_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=positive_int, default=32)
    parser.add_argument("--repeats", type=positive_int, default=5)
    args = parser.parse_args()
    results: dict[str, list[dict[str, float | int]]] = {"whole_file": [], "streaming": []}
    methods = [("whole_file", whole_file_sha256), ("streaming", file_sha256)]
    with tempfile.TemporaryDirectory(prefix="rer-hashing-") as directory:
        path = Path(directory) / "payload.bin"
        chunk = bytes(range(256)) * 4096
        expected = hashlib.sha256()
        with path.open("wb") as handle:
            for _ in range(args.size_mib):
                handle.write(chunk)
                expected.update(chunk)
        for _, method in methods:
            assert method(path) == expected.hexdigest()  # Warm the filesystem cache.
        for repeat in range(args.repeats):
            # Alternate order to avoid always measuring one method first.
            for name, method in (methods if repeat % 2 == 0 else methods[::-1]):
                gc.collect()
                tracemalloc.start()
                started = time.perf_counter()
                digest = method(path)
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                if digest != expected.hexdigest():
                    raise RuntimeError(f"{name}: SHA-256 mismatch")
                results[name].append({"seconds": elapsed, "peak_bytes": peak})
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "size_bytes": args.size_mib * 1024 * 1024,
        "repeats": args.repeats,
        "filesystem_cache": "warm",
        "hashes_match": True,
        "methods": {
            name: {
                "median_seconds": statistics.median(sample["seconds"] for sample in samples),
                "median_peak_bytes": statistics.median(sample["peak_bytes"] for sample in samples),
            }
            for name, samples in results.items()
        },
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
