"""tests/test_comps.py — unit cases for the deterministic peer comps machinery.

    python -m pytest tests/test_comps.py

All tickers are invented and every multiple below is a made-up test input; the
premium/discount figures are recomputed from those inputs.
"""

from __future__ import annotations

from scout import comps
from scout.db import Database


def test_verdict_median_premium():
    rows = [
        {"symbol": "NRDX", "ps": 9.0},
        {"symbol": "VBRG", "ps": 1.0},
        {"symbol": "MGRD", "ps": 5.0},
    ]
    line = comps.premium_discount_line("NRDX", rows)
    # P/S peer median = median(1,5)=5? no — median(1.0,5.0)=3.0; 9/3-1 = +200%
    assert "P/S 9.0× vs peer median 3.0× — a 200% premium" in line, line


def test_verdict_discount_direction():
    rows = [
        {"symbol": "A", "ps": 1.0},
        {"symbol": "B", "ps": 2.0},
        {"symbol": "C", "ps": 4.0},
    ]
    line = comps.premium_discount_line("A", rows)
    # peer median = median(2,4)=3.0; 1/3-1 = -66.7% -> discount
    assert "discount" in line and "67% discount" in line, line


def test_verdict_not_found_without_peers():
    rows = [{"symbol": "A", "ps": 1.0}]
    line = comps.premium_discount_line("A", rows)
    assert "NOT FOUND" in line, line


def test_not_found_cells_render():
    rows = [{"symbol": "A", "ps": 5.0}, {"symbol": "B"}]
    table = comps.render_comps_table("A", rows)
    assert "NOT FOUND" in table, "missing peer cells must render NOT FOUND"
    assert "**A** (subject)" in table
    assert "5.0×" in table


def test_cache_merge_preserves_prior_cells():
    db = Database(db_url="")  # JSON fallback
    db.apply_schema()
    db.delete("peer_metrics", {"symbol": "TSTA"})
    comps.upsert_peer_metrics(db, "TSTA", {"ps": 1.2, "gm": 0.15, "fwd_pe": 24.0},
                              source_url="10-K-tsta", doc_date="2026-02-01")
    # partial re-extract with only ps must NOT wipe gm / fwd_pe
    comps.upsert_peer_metrics(db, "TSTA", {"ps": 1.3})
    row = db.select_one("peer_metrics", {"symbol": "TSTA"})
    assert float(row["ps"]) == 1.3, row
    assert row["gm"] is not None and float(row["gm"]) == 0.15, "gm wiped by partial upsert"
    assert row["fwd_pe"] is not None and float(row["fwd_pe"]) == 24.0, "fwd_pe wiped"
    assert db.count("peer_metrics", {"symbol": "TSTA"}) == 1, "duplicate row created"
    db.delete("peer_metrics", {"symbol": "TSTA"})
    db.close()


def test_full_recompute_nulls_now_underivable_cells():
    """A FULL recompute (fundamentals freshly refetched) must null out any cell
    the fresh derivation didn't come back with — otherwise a multiple that was
    derivable last week (e.g. D/E, before a 10-K dropped the debt tag) survives
    under today's `asof` and reads as a fresh, current number when it is really
    a stale carry-over. Partial upserts (the default) must still preserve prior
    cells — covered by test_cache_merge_preserves_prior_cells above."""
    db = Database(db_url="")  # JSON fallback
    db.apply_schema()
    db.delete("peer_metrics", {"symbol": "TSTB"})
    # Week 1: full snapshot, D/E derivable (debt tag present).
    comps.upsert_peer_metrics(db, "TSTB", {"ps": 1.2, "gm": 0.15, "de": 0.4},
                              source_url="10-K-tstb-w1", doc_date="2026-02-01",
                              asof="2026-02-01", full_recompute=True)
    row1 = db.select_one("peer_metrics", {"symbol": "TSTB"})
    assert float(row1["de"]) == 0.4, row1
    # Week 2: full recompute from a fresh fundamentals fetch, debt tag now
    # absent (de undeliverable) — ps/gm still derivable, but the caller-supplied
    # metrics dict simply omits "de" the same way it always did.
    comps.upsert_peer_metrics(db, "TSTB", {"ps": 1.3, "gm": 0.16},
                              source_url="10-K-tstb-w2", doc_date="2026-05-01",
                              asof="2026-05-01", full_recompute=True)
    row2 = db.select_one("peer_metrics", {"symbol": "TSTB"})
    assert float(row2["ps"]) == 1.3 and float(row2["gm"]) == 0.16, row2
    assert row2["de"] is None, ("de must be NULLed on a full recompute that "
                                 "can no longer derive it, not carry over "
                                 "week 1's 0.4 under week 2's asof — that's "
                                 "date-laundering a stale multiple")
    assert str(row2["asof"])[:10] == "2026-05-01", row2["asof"]
    assert db.count("peer_metrics", {"symbol": "TSTB"}) == 1, "duplicate row created"
    db.delete("peer_metrics", {"symbol": "TSTB"})
    db.close()


def test_comps_table_carries_basis_note():
    # The comps table can show an XBRL-computed EBITDA margin while the body cites a
    # company-adjusted figure; a one-line basis note must label the difference so
    # the two numbers aren't read as a contradiction.
    db = Database(db_url="")  # JSON fallback
    db.apply_schema()
    md = comps.comps_table_md("NRDX", db, peer_symbols=["VBRG"])
    assert "Basis: metrics computed from XBRL filings (FY-latest)" in md, md
    assert "company-adjusted figures cited in the text" in md, md
    db.close()
