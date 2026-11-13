"""Compare pack budget fitting against a saved earlier packs.py implementation.

The baseline is imported as Python code: use a trusted file from this repository.
Inputs are synthetic; GitHub and existing research snapshots are not accessed.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "skills" / "repo-evolution-research" / "scripts"),
)

from rer import packs  # noqa: E402


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-file", type=Path, required=True)
    parser.add_argument("--candidates", type=positive_int, default=1000)
    parser.add_argument("--max-chars", type=positive_int, default=8000)
    parser.add_argument("--repeats", type=positive_int, default=5)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("rer._packs_baseline", args.baseline_file)
    if spec is None or spec.loader is None:
        parser.error("baseline-file must be a Python source file")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    candidates = {"candidates": [
        {
            "candidate_id": f"c-{index:05}",
            "entity_id": f"github.com/acme/demo/pull/{index + 1}",
            "kind": "pull_request", "module": "core", "title": f"Candidate {index}",
            "score": index % 100, "tier": "discovery", "preview_text": "preview " * 100,
            "recommended": index % 7 == 0, "agent_selected": index % 101 == 0,
        }
        for index in range(args.candidates)
    ]}
    methods = [("baseline", baseline), ("current", packs)]
    results: dict[str, list[float]] = {name: [] for name, _ in methods}
    reference = None
    for repeat in range(args.repeats + 1):
        for name, module in (methods if repeat % 2 == 0 else methods[::-1]):
            groups = copy.deepcopy(packs._groups(candidates))
            started = time.perf_counter()
            result = module._fit_budget(
                groups, max_chars=args.max_chars, manifest=None,
                candidates=None, research_handoff=True,
            )
            elapsed = time.perf_counter() - started
            if reference is None:
                reference = result
            if result != reference:
                raise RuntimeError("Budget fitting outputs differ")
            if repeat:
                results[name].append(elapsed)
    print(json.dumps({
        "python": platform.python_version(),
        "platform": platform.platform(),
        "candidates": args.candidates,
        "groups": 1,
        "max_chars": args.max_chars,
        "repeats": args.repeats,
        "outputs_match": True,
        "omitted": len(reference[1]),
        "median_seconds": {name: statistics.median(samples) for name, samples in results.items()},
        "scope": "budget fitting only; synthetic input construction excluded",
    }, indent=2))


if __name__ == "__main__":
    main()
