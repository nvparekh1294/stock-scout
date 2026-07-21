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


def _patch(resolve_result, *, price=None, facts="__none__", split="none"):
    """Swap resolve_ticker + the fetch helpers light_evidence calls. Returns
    restore(). `price` (a dict) drives _alpaca_price_range; `facts` drives the
    valuation snapshot's EDGAR fetch (fundamentals.company_facts_metrics) — a
    dict/None/Exception; defaults to None so the snippet builds without network.
    `split` drives the split-detection guard (fundamentals.split_since_fy):
    'none' | 'split' | 'unknown', defaulting to 'none' so no Alpaca history is
    fetched. The rest return inert 'no data' shapes so nothing touches network."""
    from scout import options_ref
    from scout import fundamentals
    facts_val = None if facts == "__none__" else facts
    orig = {
        "resolve": gather.resolve_ticker,
        "price": gather._alpaca_price_range,
        "edgar": gather._edgar_latest,
        "cons": gather._consensus_snapshot,
        "opts": options_ref.options_snapshot_md,
        "facts": fundamentals.company_facts_metrics,
        "split": fundamentals.split_since_fy,
    }
    gather.resolve_ticker = lambda s: resolve_result
    gather._alpaca_price_range = lambda s: (price or {"error": "no price this run"})
    gather._edgar_latest = lambda cik, **k: {"error": "no CIK"}
    gather._consensus_snapshot = lambda s: {"note": "consensus unavailable"}
    options_ref.options_snapshot_md = lambda s, spot: "## Options\n- NOT FOUND"

    def _facts_stub(cik):
        if isinstance(facts_val, Exception):
            raise facts_val
        return facts_val
    fundamentals.company_facts_metrics = _facts_stub
    fundamentals.split_since_fy = lambda symbol, fy_end: split

    def restore():
        gather.resolve_ticker = orig["resolve"]
        gather._alpaca_price_range = orig["price"]
        gather._edgar_latest = orig["edgar"]
        gather._consensus_snapshot = orig["cons"]
        options_ref.options_snapshot_md = orig["opts"]
        fundamentals.company_facts_metrics = orig["facts"]
        fundamentals.split_since_fy = orig["split"]

    return restore


def _valuation_block(md: str) -> str:
    """The '## Valuation snapshot' section only, up to the next '## ' header."""
    lines = md.splitlines()
    i = lines.index("## Valuation snapshot")
    block = ["## Valuation snapshot"]
    for l in lines[i + 1:]:
        if l.startswith("## "):
            break
        block.append(l)
    return "\n".join(block)


_CLEAN_FILER = {"resolved": True, "active": True, "sec_filer": True, "cik": 320193,
                "name": "Apple Inc.", "alpaca_name": "Apple Inc.", "exchange": "NASDAQ",
                "sources": ["SEC EDGAR company_tickers", "Alpaca assets"],
                "identity_warning": None}
_PRICE = {"latest_close": 100.0, "asof": "2026-07-21", "low_52w": 80.0,
          "high_52w": 120.0, "recent": [("2026-07-20", 99.0)]}


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


# ── Valuation snapshot (EDGAR company-facts + latest split-adjusted close) ────
def test_valuation_snapshot_arithmetic():
    facts = {"revenue": 500.0, "rev_growth": 0.25, "eps_diluted": 5.0,
             "shares": 10.0, "net_income": 50.0, "fy_end": "2025-12-31",
             "entity": "Apple Inc.", "source_url": "https://data.sec.gov/x"}
    restore = _patch(_CLEAN_FILER, price=_PRICE, facts=facts)
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "## Valuation snapshot" in md, md
    assert "FY period end 2025-12-31" in block, block
    assert "split-adjusted" in block, block
    assert "Market cap: $1,000" in block, block   # 100 × 10
    assert "P/S: 2.0×" in block, block            # 1,000 / 500
    assert "Trailing P/E: 20.0×" in block, block  # 100 / 5
    assert "revenue YoY +25.0%" in block, block
    assert "FY diluted EPS: $5.00" in block, block


def test_valuation_snapshot_not_found_on_missing_facts():
    facts = {"revenue": None, "rev_growth": None, "eps_diluted": None,
             "shares": None, "net_income": None, "fy_end": None,
             "entity": "X", "source_url": "u"}
    restore = _patch(_CLEAN_FILER, price=_PRICE, facts=facts)
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "Market cap: NOT FOUND" in block, block
    assert "FY revenue: NOT FOUND" in block, block
    assert "FY diluted EPS: NOT FOUND" in block, block
    assert "P/S: NOT FOUND" in block, block
    assert "×" not in block, block


def test_valuation_snapshot_negative_eps_is_nm():
    facts = {"revenue": 500.0, "rev_growth": None, "eps_diluted": -2.0,
             "shares": 10.0, "net_income": -20.0, "fy_end": "2025-12-31",
             "entity": "X", "source_url": "u"}
    restore = _patch(_CLEAN_FILER, price=_PRICE, facts=facts)
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "Trailing P/E: n/m (earnings negative)" in block, block
    assert "revenue YoY NOT FOUND" in block, block


def test_valuation_snapshot_skipped_for_non_filer():
    warning = ("not in SEC EDGAR's ticker registry — ticker may have been "
               "reassigned; verify identity before use")
    res = {"resolved": True, "active": True, "sec_filer": False, "cik": None,
           "name": "Vega Photonics & Optical ETF",
           "alpaca_name": "Vega Photonics & Optical ETF", "exchange": "ARCA",
           "sources": ["Alpaca assets"], "identity_warning": warning}
    restore = _patch(res, price={"latest_close": 12.30, "asof": "2026-07-21",
                                 "low_52w": 8.0, "high_52w": 20.0, "recent": []})
    try:
        md, n = gather.light_evidence("PHTN")
    finally:
        restore()
    block = _valuation_block(md)
    assert "NOT COMPUTED" in block, block
    assert "not an SEC filer" in block, block
    assert "×" not in block, block
    assert md.splitlines()[1] == f"> ⚠ IDENTITY WARNING: {warning}", md


def test_valuation_snapshot_edgar_fetch_fails_no_crash():
    restore = _patch(_CLEAN_FILER, price=_PRICE, facts=None)
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "NOT FOUND: EDGAR company-facts could not be fetched" in block, block


def test_valuation_snapshot_exception_degrades_not_crash():
    restore = _patch(_CLEAN_FILER, price=_PRICE, facts=RuntimeError("boom"))
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "could not be computed" in block, block


def test_valuation_snapshot_increments_n_sources():
    facts = {"revenue": 500.0, "rev_growth": 0.1, "eps_diluted": 5.0, "shares": 10.0,
             "net_income": 50.0, "fy_end": "2025-12-31", "entity": "X", "source_url": "u"}
    r1 = _patch(_CLEAN_FILER, price=_PRICE, facts=facts)
    try:
        _, n_with = gather.light_evidence("AAPL")
    finally:
        r1()
    r2 = _patch(_CLEAN_FILER, price=_PRICE, facts=None)
    try:
        _, n_without = gather.light_evidence("AAPL")
    finally:
        r2()
    assert n_with == n_without + 1, (n_with, n_without)


def test_valuation_snapshot_split_since_fy_is_nm():
    # A split between the FY filing and today → filed EPS is pre-split, so a P/E
    # would be wrong by the split factor (the NFLX 10x error). Refuse a number.
    facts = {"revenue": 500.0, "rev_growth": 0.1, "eps_diluted": 5.0, "shares": 10.0,
             "net_income": 50.0, "fy_end": "2025-12-31", "entity": "X", "source_url": "u"}
    restore = _patch(_CLEAN_FILER, price=_PRICE, facts=facts, split="split")
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "Trailing P/E: n/m (stock split since the FY filing" in block, block
    assert "filed EPS not yet restated" in block, block
    assert "20.0×" not in block, block


def test_valuation_snapshot_split_unknown_prints_pe_with_caveat():
    facts = {"revenue": 500.0, "rev_growth": 0.1, "eps_diluted": 5.0, "shares": 10.0,
             "net_income": 50.0, "fy_end": "2025-12-31", "entity": "X", "source_url": "u"}
    restore = _patch(_CLEAN_FILER, price=_PRICE, facts=facts, split="unknown")
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "Trailing P/E: 20.0×" in block, block
    assert "caveat: if the stock split since the FY filing, filed EPS may be pre-split" \
        in block, block


def test_valuation_snapshot_negative_eps_missing_price_is_nm():
    # L5: negative EPS + no price must read 'n/m (earnings negative)', never a
    # NOT FOUND that wrongly claims EPS is missing (it was found).
    facts = {"revenue": 500.0, "rev_growth": None, "eps_diluted": -2.0, "shares": 10.0,
             "net_income": -20.0, "fy_end": "2025-12-31", "entity": "X", "source_url": "u"}
    restore = _patch(_CLEAN_FILER, price={"error": "no price this run"}, facts=facts)
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "Trailing P/E: n/m (earnings negative)" in block, block
    assert "FY diluted EPS: $-2.00" in block, block
    assert "no live price this run" in block, block  # L1: price clause dropped
    assert "split-adjusted" not in block, block


def test_valuation_source_line_dates_price_with_asof_not_today():
    # L2: the price is dated by its actual trading date (asof), not the fetch day.
    facts = {"revenue": 500.0, "rev_growth": 0.1, "eps_diluted": 5.0, "shares": 10.0,
             "net_income": 50.0, "fy_end": "2025-12-31", "entity": "X", "source_url": "u"}
    price = {"latest_close": 100.0, "asof": "2026-07-17", "low_52w": 1.0,
             "high_52w": 2.0, "recent": []}
    restore = _patch(_CLEAN_FILER, price=price, facts=facts)
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "price Alpaca IEX 2026-07-17, split-adjusted" in block, block
    assert "shares outstanding per latest EDGAR cover page" in block, block  # L3


def test_split_since_fy_ratio_logic():
    # Unit-test the detector itself against stubbed adjusted/raw closes.
    from scout import fundamentals as f
    orig = f._bar_close_near
    try:
        f._bar_close_near = lambda sym, tgt, adj: (10.0 if adj == "split" else 100.0)
        assert f.split_since_fy("X", "2020-12-31") == "split"
        f._bar_close_near = lambda sym, tgt, adj: 100.0
        assert f.split_since_fy("X", "2020-12-31") == "none"
        f._bar_close_near = lambda sym, tgt, adj: None
        assert f.split_since_fy("X", "2020-12-31") == "unknown"
    finally:
        f._bar_close_near = orig
    assert f.split_since_fy("X", None) == "unknown"


# ── XBRL tag-migration merge + staleness guard (NVDA P/S-185x bug) ────────────
def test_company_facts_merges_aliases_picks_latest_fy():
    # NVDA-shaped migration: the OLD revenue tag holds only older FYs, the NEW
    # tag holds the recent FYs. Best-across-aliases selection must use the RECENT
    # revenue, not the ~4-year-old figure the first-alias logic returned.
    from datetime import date, timedelta
    from scout import fundamentals as fund

    def fy(days_ago, val):
        e = date.today() - timedelta(days=days_ago)
        s = e - timedelta(days=364)
        return {"start": s.isoformat(), "end": e.isoformat(), "val": val,
                "fp": "FY", "form": "10-K"}

    latest_end = (date.today() - timedelta(days=100)).isoformat()
    old_tag = "RevenueFromContractWithCustomerExcludingAssessedTax"
    facts = {"entityName": "NVIDIA", "facts": {
        "us-gaap": {
            old_tag: {"units": {"USD": [fy(1200, 10e9), fy(835, 16e9), fy(470, 26.9e9)]}},
            "Revenues": {"units": {"USD": [fy(465, 60e9), fy(100, 130.9e9)]}},
            "EarningsPerShareDiluted": {"units": {"USD": [fy(100, 2.94)]}},
        },
        "dei": {"EntityCommonStockSharesOutstanding": {
            "units": {"shares": [{"end": latest_end, "val": 24.4e9}]}}},
    }}
    orig = fund._facts
    fund._facts = lambda cik: facts
    try:
        vs = fund.valuation_snapshot(1045810, price=170.0)  # symbol=None → no split fetch
    finally:
        fund._facts = orig
    assert vs["fy_end"] == latest_end, vs["fy_end"]
    assert abs(vs["revenue"] - 130.9e9) < 1, vs["revenue"]  # recent, not 26.9e9
    assert not vs["stale"], vs
    assert vs["ps"] is not None and vs["ps"] < 50, vs["ps"]  # ≈31.7×, not ~154×


def test_valuation_snapshot_stale_fy_renders_not_found():
    # Defense in depth: a latest FY older than ~450 days must NOT be divided into
    # today's price — every FY-derived line renders the staleness NOT FOUND reason.
    from datetime import date, timedelta
    stale_end = (date.today() - timedelta(days=500)).isoformat()
    facts = {"revenue": 26.9e9, "rev_growth": 0.5, "eps_diluted": 2.0, "shares": 24e9,
             "net_income": 5e9, "fy_end": stale_end, "entity": "X", "source_url": "u"}
    restore = _patch(_CLEAN_FILER, price=_PRICE, facts=facts)
    try:
        md, n = gather.light_evidence("AAPL")
    finally:
        restore()
    block = _valuation_block(md)
    assert "too stale to compute against today's price" in block, block
    assert "FY revenue: NOT FOUND (latest filed FY ends" in block, block
    assert "FY diluted EPS: NOT FOUND (latest filed FY ends" in block, block
    assert "Trailing P/E: NOT FOUND (latest filed FY ends" in block, block
    assert "P/S: NOT FOUND (latest filed FY ends" in block, block
    assert "- Market cap: $2.40T" in block, block  # current shares × price still prints
