"""scout/test_market_ref.py — the identity flags on resolve_ticker (2026-07-15
stale-queue-ticker fix). Plain-Python asserts; EDGAR + Alpaca lookups are
monkeypatched so there is NO network and NO .env needed:

    scout/.venv/bin/python -m scout.test_market_ref

Covers the four identity shapes:
  - a normal name (EDGAR match + Alpaca active) → no warning, sec_filer True,
    active True;
  - the PHTN shape (dropped from EDGAR, ticker reassigned to a DIFFERENT active
    Alpaca security) → sec_filer False, active True, reassignment warning;
  - the ARJT shape (gone from EDGAR, Alpaca inactive/untradable) → active False,
    warning leads with inactive/delisted;
  - both sources miss → resolved False (semantics UNCHANGED) with the new keys
    present and inert.
"""

from __future__ import annotations

from scout import market_ref


def _patch(edgar, alpaca):
    """Swap the two source lookups for fixed returns; returns restore()."""
    orig_e = market_ref._edgar_lookup
    orig_a = market_ref._alpaca_asset
    market_ref._edgar_lookup = lambda q: edgar
    market_ref._alpaca_asset = lambda t: alpaca

    def restore():
        market_ref._edgar_lookup = orig_e
        market_ref._alpaca_asset = orig_a

    return restore


def _edgar(ticker="AAPL", cik=320193, title="Apple Inc."):
    return {"ticker": ticker, "cik": cik, "title": title, "match": "ticker"}


def _alp(symbol, name, status="active", tradable=True, exchange="NASDAQ"):
    return {"symbol": symbol, "name": name, "exchange": exchange,
            "tradable": tradable, "status": status, "class": "us_equity"}


# ── normal: EDGAR filer + Alpaca active → clean, no warning ───────────────────
def test_edgar_and_active_alpaca_no_warning():
    restore = _patch(_edgar(), _alp("AAPL", "Apple Inc."))
    try:
        r = market_ref.resolve_ticker("AAPL")
    finally:
        restore()
    assert r["resolved"] is True, r
    assert r["sec_filer"] is True, r
    assert r["active"] is True, r
    assert r["identity_warning"] is None, r


# ── PHTN shape: not in EDGAR, Alpaca serves a DIFFERENT active security ────────
def test_reassigned_ticker_alpaca_only_active():
    # EDGAR misses (PHTN gone); Alpaca returns an ACTIVE but different instrument.
    restore = _patch(None, _alp("PHTN", "Vega Photonics & Optical ETF",
                                exchange="ARCA"))
    try:
        r = market_ref.resolve_ticker("PHTN")
    finally:
        restore()
    assert r["resolved"] is True, r                     # still a listed security
    assert r["sec_filer"] is False, r
    assert r["active"] is True, r
    w = r["identity_warning"]
    assert w and "reassigned" in w, w
    assert "Vega Photonics & Optical ETF" in w, w
    assert "SEC EDGAR" in w, w
    assert "verify identity" in w, w
    # not an inactive story — the delisted phrase must NOT appear here
    assert "inactive/delisted" not in w, w
    assert r["alpaca_name"] == "Vega Photonics & Optical ETF", r


# ── ARJT shape: gone from EDGAR, Alpaca inactive/untradable ────────────────────
def test_inactive_alpaca_flags_delisted():
    restore = _patch(None, _alp("ARJT", "Ardent Rocketworks Holdings Inc.",
                                status="inactive", tradable=False))
    try:
        r = market_ref.resolve_ticker("ARJT")
    finally:
        restore()
    assert r["resolved"] is True, r
    assert r["sec_filer"] is False, r
    assert r["active"] is False, r
    w = r["identity_warning"]
    assert w and w.startswith("inactive/delisted"), w   # inactive leads
    assert "status=inactive" in w, w


def test_inactive_even_when_edgar_present():
    # tradable False alone (status still 'active') must read as inactive too.
    restore = _patch(_edgar("ARJT", 12345, "Ardent"),
                     _alp("ARJT", "Ardent", status="active", tradable=False))
    try:
        r = market_ref.resolve_ticker("ARJT")
    finally:
        restore()
    assert r["sec_filer"] is True, r
    assert r["active"] is False, r
    # sec_filer True → no reassignment clause, only the inactive one
    assert r["identity_warning"].startswith("inactive/delisted"), r
    assert "reassigned" not in r["identity_warning"], r


# ── both sources miss: resolved semantics UNCHANGED, new keys inert ───────────
def test_both_miss_resolved_false_unchanged():
    restore = _patch(None, None)
    try:
        r = market_ref.resolve_ticker("ZZZZQ")
    finally:
        restore()
    assert r["resolved"] is False, r                    # unchanged semantics
    assert r["sec_filer"] is False, r
    assert r["active"] is None, r
    assert r["identity_warning"] is None, r
    assert "note" in r and "unresolved" in r["note"].lower(), r


# ── no Alpaca data at all (EDGAR-only): active is None, no warning ─────────────
def test_edgar_only_active_is_none():
    restore = _patch(_edgar(), None)
    try:
        r = market_ref.resolve_ticker("AAPL")
    finally:
        restore()
    assert r["resolved"] is True, r
    assert r["sec_filer"] is True, r
    assert r["active"] is None, r                        # absence of data, not "no"
    assert r["identity_warning"] is None, r
