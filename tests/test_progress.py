from __future__ import annotations

from rer.collectors.progress import has_blocking_gaps


def test_release_and_document_channel_policies() -> None:
    assert not has_blocking_gaps(
        [{"channel": "releases", "coverage_status": "partial"}]
    )
    assert has_blocking_gaps(
        [{"channel": "releases", "coverage_status": "unavailable"}]
    )
    assert not has_blocking_gaps(
        [{"channel": "releases", "coverage_status": "complete"}]
    )
    assert has_blocking_gaps(
        [{"channel": "docs", "coverage_status": "unavailable"}]
    )
    assert not has_blocking_gaps(
        [{"channel": "docs", "coverage_status": "partial"}]
    )
    assert not has_blocking_gaps(
        [{"channel": "docs", "coverage_status": "not_requested"}]
    )
