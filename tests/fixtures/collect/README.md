# Collector golden fixtures

These fixtures pin the deterministic collector output used by Phase 0/1 of
the collector refactor.

Generate them with:

```bash
.venv/bin/python scripts/gen_collect_golden.py
```

The generator uses the fake GitHub transport from `tests/test_pull_requests.py`
and `tests/test_activity.py`, with fixed clocks. To verify provenance against
the pre-refactor baseline:

```bash
git worktree add --detach /tmp/rer-base ea06c7e
mkdir -p /tmp/rer-base/scripts
cp scripts/gen_collect_golden.py /tmp/rer-base/scripts/
.venv/bin/python /tmp/rer-base/scripts/gen_collect_golden.py
diff -r tests/fixtures/collect /tmp/rer-base/tests/fixtures/collect
git worktree remove --force /tmp/rer-base
```

Covered scenarios:

- `complete`: happy path
- `partial_truncated`: files total mismatch
- `head_changed`: head SHA changes between the start and end of a read
- `manifest_coverage.complete` / `.partial`: CLI manifest coverage snapshot
- `activity_overview.complete`: activity discovery overview
