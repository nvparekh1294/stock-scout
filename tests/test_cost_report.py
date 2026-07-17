"""scout/test_cost_report.py — unit cases for the `cost_report` relay tool
(Task 3). Plain-Python asserts (no pytest, no LLM spend), seeded temp store.

    scout/.venv/bin/python -m scout.test_cost_report

Proves the deterministic spend summary: windowing (old rows excluded), operation
grouping (per-symbol tasks collapse to one operation), by-model breakdown,
totals, the empty-window message, and the dispatch() wiring.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scout import agent_tools
from scout import db as dbmod
from scout.db import Database


def _seeded_db(tmp: str) -> Database:
    dbmod.LOCALDB_DIR = Path(tmp)          # isolate the JSON store to a tempdir
    d = Database(db_url="")                # force JSON fallback
    d.apply_schema()
    now = datetime.now(timezone.utc)

    def add(model, task, i, o, c, usd, ts):
        d.insert("api_costs", {
            "ts": ts.isoformat(), "model": model, "task": task,
            "input_tokens": i, "output_tokens": o, "cached_tokens": c,
            "usd_estimate": usd})

    add("claude-opus-4-8", "QMEM-underwrite",   1000, 500,   0, 0.10, now)
    add("claude-opus-4-8", "ZYXA-underwrite", 2000, 400, 100, 0.20, now - timedelta(days=2))
    add("claude-sonnet-5", "QMEM-standard",      800, 300,   0, 0.03, now - timedelta(days=5))
    add("claude-haiku-4-5", "extract",         400,  50,   0, 0.001, now - timedelta(days=1))
    # OLD row, well outside the 30d window — must be excluded from every number.
    add("claude-opus-4-8", "OLD-underwrite",  9999, 9999,  0, 99.0, now - timedelta(days=90))
    return d


def test_report_excludes_rows_outside_window():
    _orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _seeded_db(tmp)
            out = agent_tools.cost_report(db, days=30)
            assert "$99" not in out, "an out-of-window row leaked into the report"
            assert "OLD" not in out, "an out-of-window operation leaked in"
            # Token-first total line: tokens lead, dollars follow.
            # in 4200→"4k", out 1250→"1k"; 0.10+0.20+0.03+0.001=0.331 → $0.33.
            assert "4k in / 1k out ≈ $0.33 at your configured rates" in out, out
            assert "4 calls" in out, out
            db.close()
    finally:
        dbmod.LOCALDB_DIR = _orig


def test_operations_group_by_stripping_ticker_prefix():
    _orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _seeded_db(tmp)
            out = agent_tools.cost_report(db, days=30)
            # QMEM-underwrite + ZYXA-underwrite collapse into one 'underwrite' op;
            # token-first line: in 3000→"3k", out 900→"900".
            assert "underwrite: 3k in / 900 out ≈ $0.30 (2)" in out, out
            assert "standard: 800 in / 300 out ≈ $0.03 (1)" in out, out
            db.close()
    finally:
        dbmod.LOCALDB_DIR = _orig


def test_by_model_breakdown():
    _orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _seeded_db(tmp)
            out = agent_tools.cost_report(db, days=30)
            assert "claude-opus-4-8: 3k in / 900 out ≈ $0.30 (2)" in out, out
            assert "claude-sonnet-5: 800 in / 300 out ≈ $0.03 (1)" in out, out
            assert "claude-haiku-4-5: 400 in / 50 out ≈ $0.00 (1)" in out, out
            db.close()
    finally:
        dbmod.LOCALDB_DIR = _orig


def test_narrow_window_excludes_older_rows():
    _orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _seeded_db(tmp)
            # 1-day window keeps only the 'now' row ($0.10); the day-1 extract
            # sits on the cutoff boundary and the day-2 / day-5 rows drop out.
            out = agent_tools.cost_report(db, days=1)
            assert "1k in / 500 out ≈ $0.10 at your configured rates" in out, out
            assert "1 calls" in out, out
            db.close()
    finally:
        dbmod.LOCALDB_DIR = _orig


def test_empty_window_message():
    _orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dbmod.LOCALDB_DIR = Path(tmp)
            db = Database(db_url="")
            db.apply_schema()
            out = agent_tools.cost_report(db, days=30)
            assert out.startswith("No API spend recorded"), out
            db.close()
    finally:
        dbmod.LOCALDB_DIR = _orig


def test_dispatch_routes_to_cost_report():
    _orig = dbmod.LOCALDB_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = _seeded_db(tmp)
            ctx = agent_tools.ToolContext(db)
            out = agent_tools.dispatch("cost_report", {"days": 30}, ctx)
            assert "≈ $0.33 at your configured rates" in out, out
            assert ctx.send_documents == [], "cost_report attaches no documents"
            # default days when omitted
            out2 = agent_tools.dispatch("cost_report", {}, ctx)
            assert "API cost — last 30d" in out2, out2
            db.close()
    finally:
        dbmod.LOCALDB_DIR = _orig


def test_cost_report_registered_in_schemas():
    names = {t["name"] for t in agent_tools.TOOL_SCHEMAS}
    assert "cost_report" in names, "cost_report must be registered in TOOL_SCHEMAS"
