"""Compare buffered and streaming JSONL writes on synthetic local snapshots."""

from __future__ import annotations

import argparse
import gc
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

from rer.store import SnapshotStore, file_sha256  # noqa: E402


class BufferedStore(SnapshotStore):
    """Retain the previous encoding path for a like-for-like comparison."""

    def _atomic_write_jsonl(self, path, records):
        self._atomic_write_bytes(path, b"".join(self._line(record) for record in records))


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=positive_int, default=10000)
    parser.add_argument("--text-chars", type=positive_int, default=1024)
    parser.add_argument("--repeats", type=positive_int, default=5)
    args = parser.parse_args()
    records = [{"id": str(index), "text": "x" * args.text_chars} for index in range(args.records)]
    methods = [("buffered", BufferedStore), ("streaming", SnapshotStore)]
    results: dict[str, list[dict[str, float | int]]] = {name: [] for name, _ in methods}
    digests: set[str] = set()
    size = 0
    with tempfile.TemporaryDirectory(prefix="rer-jsonl-") as directory:
        for repeat in range(args.repeats + 1):
            for name, store_type in (methods if repeat % 2 == 0 else methods[::-1]):
                root = Path(directory) / f"{name}-{repeat}"
                root.mkdir()
                store = store_type(root)
                gc.collect()
                tracemalloc.start()
                started = time.perf_counter()
                path = store.write_jsonl("records.jsonl", records)
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                digests.add(file_sha256(path))
                size = path.stat().st_size
                if repeat:  # First pair warms the code paths and is not measured.
                    results[name].append({"seconds": elapsed, "peak_bytes": peak})
                del store
    if len(digests) != 1:
        raise RuntimeError("JSONL output hashes differ")
    print(json.dumps({
        "python": platform.python_version(),
        "platform": platform.platform(),
        "records": args.records,
        "size_bytes": size,
        "repeats": args.repeats,
        "hashes_match": True,
        "scope": "write_jsonl including deduplication, fsync and index rebuild; input excluded",
        "methods": {
            name: {
                "median_seconds": statistics.median(sample["seconds"] for sample in samples),
                "median_peak_bytes": statistics.median(sample["peak_bytes"] for sample in samples),
            }
            for name, samples in results.items()
        },
    }, indent=2))


if __name__ == "__main__":
    main()
