"""scout/test_gather_lite.py — light_evidence identity guards (2026-07-15
stale-ticker fix, T3). Plain-Python asserts; resolve_ticker and every fetch
helper are monkeypatched, so NO network and NO .env needed:

    scout/.venv/bin/python -m scout.test_gather_lite

Covers:
  - an inactive/delisted ticker (ARJT shape) → light_evidence returns the
    Inactive sentinel with an honest delisted reason (NOT None, NOT a pack), and
    the quick-take caller path surfaces "inactive/delisted", not "can't resolve";
  - a non-filer-but-active ticker (PHTN reassignment) → the snippet IS built, its
    FIRST line after the title is the identity warning verbatim, and prices are
    attributed to the Alpaca-listed instrument name, not the assumed company;
  - a clean filer → normal snippet, no warning line, no reassignment header;
  - both sources miss → None (unchanged).
"""

from __future__ import annotations

from scout import gather


def _patch(resolve_result, *, price=None):
    """Swap resolve_ticker + the fetch helpers light_evidence calls. Returns
    restore(). `price` (a dict) drives _alpaca_price_range; the rest return inert
    'no data' shapes so the snippet builds without network."""
    from scout import options_ref
    orig = {
        "resolve": gather.resolve_ticker,
        "price": gather._alpaca_price_range,
        "edgar": gather._edgar_latest,
        "cons": gather._consensus_snapshot,
        "opts": options_ref.options_snapshot_md,
    }
    gather.resolve_ticker = lambda s: resolve_result
    gather._alpaca_price_range = lambda s: (price or {"error": "no price this run"})
    gather._edgar_latest = lambda cik, **k: {"error": "no CIK"}
    gather._consensus_snapshot = lambda s: {"note": "consensus unavailable"}
    options_ref.options_snapshot_md = lambda s, spot: "## Options\n- NOT FOUND"

    def restore():
        gather.resolve_ticker = orig["resolve"]
        gather._alpaca_price_range = orig["price"]
        gather._edgar_latest = orig["edgar"]
        gather._consensus_snapshot = orig["cons"]
        options_ref.options_snapshot_md = orig["opts"]

    return restore


# ── inactive/delisted → Inactive sentinel, honest reason, NO pack ─────────────
def test_inactive_returns_sentinel_not_none():
    res = {"resolved": True, "active": False, "sec_filer": False,
           "name": "Ardent Rocketworks Holdings Inc.", "cik": None,
           "identity_warning": "inactive/delisted on exchange — no longer trades "
           "(status=inactive)"}
    restore = _patch(res)
    try:
        out = gather.light_evidence("ARJT")
    finally:
        restore()
    assert isinstance(out, gather.Inactive), out
    assert out is not None
    assert "inactive/delisted" in out, out
    assert "no live data exists" in out, out
    assert "queue entry is stale" in out, out
    assert "ARJT" in out, out


def test_inactive_caller_surfaces_delisted_not_unresolvable():
    # The quick-take caller (research.underwrite) must raise with the delisted
    # reason, never the misleading "could not be resolved" text.
    from scout import research
    res = {"resolved": True, "active": False, "sec_filer": False,
           "name": "Ardent", "cik": None,
           "identity_warning": "inactive/delisted on exchange"}
    restore = _patch(res)
    err = None
    try:
        try:
            research.underwrite("ARJT", depth="quick")
        except ValueError as e:
            err = str(e)
    finally:
        restore()
    assert err is not None, "expected a ValueError"
    assert "inactive/delisted" in err, err
    assert "could not be resolved" not in err, err


# ── non-filer but active (PHTN) → snippet built, warning first, price attributed ─
def test_non_filer_snippet_leads_with_warning_and_attributes_price():
    warning = ("not in SEC EDGAR's ticker registry — ticker may have been "
               "reassigned to a different security (Alpaca lists: Vega Photonics "
               "& Optical ETF); verify identity before use")
    res = {"resolved": True, "active": True, "sec_filer": False, "cik": None,
           "name": "Vega Photonics & Optical ETF",
           "alpaca_name": "Vega Photonics & Optical ETF", "exchange": "ARCA",
           "sources": ["Alpaca assets"], "identity_warning": warning}
    restore = _patch(res, price={"latest_close": 12.30, "asof": "2026-07-15",
                                 "low_52w": 8.0, "high_52w": 20.0,
                                 "recent": [("2026-07-14", 12.10)]})
    try:
        out = gather.light_evidence("PHTN")
    finally:
        restore()
    assert not isinstance(out, gather.Inactive), out
    md, n = out
    body_lines = md.splitlines()
    # title first, identity warning verbatim on the VERY NEXT line
    assert body_lines[0].startswith("# PHTN "), body_lines[0]
    assert body_lines[1] == f"> ⚠ IDENTITY WARNING: {warning}", body_lines[1]
    # the price section attributes prices to the Alpaca-listed instrument
    price_hdr = next(l for l in body_lines if l.startswith("## Price"))
    assert "ALPACA-LISTED instrument" in price_hdr, price_hdr
    assert "Vega Photonics & Optical ETF" in price_hdr, price_hdr
    assert "NOT confirmed to be PHTN's intended company" in price_hdr, price_hdr


def test_clean_filer_snippet_has_no_identity_warning():
    res = {"resolved": True, "active": True, "sec_filer": True, "cik": 320193,
           "name": "Apple Inc.", "alpaca_name": "Apple Inc.", "exchange": "NASDAQ",
           "sources": ["SEC EDGAR company_tickers", "Alpaca assets"],
           "identity_warning": None}
    restore = _patch(res, price={"error": "no price this run"})
    try:
        out = gather.light_evidence("AAPL")
    finally:
        restore()
    md, n = out
    assert "IDENTITY WARNING" not in md, md
    assert "ALPACA-LISTED instrument" not in md, md
    assert md.splitlines()[0].startswith("# AAPL "), md


def test_unresolvable_returns_none():
    res = {"resolved": False, "active": None, "sec_filer": False,
           "identity_warning": None}
    restore = _patch(res)
    try:
        out = gather.light_evidence("ZZZZQ")
    finally:
        restore()
    assert out is None, out
