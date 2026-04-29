"""Tests for local negotiation trace logging."""
from __future__ import annotations

import json

import trace_log


def test_write_trace_appends_ndjson(tmp_path):
    trace_log.write_trace(
        tmp_path,
        "prepare.start",
        phase="prepare",
        negotiation_id="neg_1",
        ignored_none=None,
    )
    trace_log.write_trace(tmp_path, "prepare.completed", phase="prepare")

    lines = (tmp_path / "trace.ndjson").read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event_type"] == "prepare.start"
    assert first["phase"] == "prepare"
    assert first["negotiation_id"] == "neg_1"
    assert "ignored_none" not in first
